"""Per-resource health for pipeline heads: TTL-cached probes, claim-time fallback selection,
watchdog freeze lookup.

A resource (heads.toml `[resources.*]`) is a shared account/limit several head profiles draw
from — red is a property of the resource, not of any one profile, so a card whose preferred
profile sits on a red resource can still claim onto a fallback profile that draws from a
*different*, green resource (`resolve_head`), and a worker already running on a red-resource
profile has its watchdog clock frozen rather than mistaken for a dead head (`resource_of`, used
by dispatcher._advance).

`refresh` is the only thing that ever runs a probe. It is meant to be called once per tick (from
precheck, which runs unconditionally every timer fire, and again from tick() for the rare case a
resource's TTL lapsed between the two) — probes cost real tokens/quota, so the TTL cache
(`PROBE_TTL_S`, ~5 min) is what keeps that cost to "once per window", not "once per 3-min tick".
Cache lives in a JSON file (state/pipeline/resource_health.json), not in memory: precheck and
tick are two separate `python3 -m triggered_agents` processes per timer fire (deploy/provision.py's
`_precheck_gate`), so nothing survives in-process between them.

A resource's red<->green flip is logged to runs.jsonl (`head-health`) exactly once per flip, not
on every re-probe of an unchanged status — a resource pinned red for hours must not spam the log
every tick, same principle as runtime/health.py's error-vs-freshness split.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path

from ...runtime.state import AgentState
from . import heads as heads_mod

STATE = AgentState("pipeline")
HEALTH_FILE = STATE.dir / "resource_health.json"

# ~5 min between real probes of the same resource; a single slow/broken probe gets killed rather
# than hanging a tick. Both env-overridable so an e2e/live check can tighten them.
PROBE_TTL_S = int(os.environ.get("TA_PROBE_TTL_S", "300"))
PROBE_TIMEOUT_S = int(os.environ.get("TA_PROBE_TIMEOUT_S", "20"))
# Comma-separated resource ids to report red without running their real probe at all — a live e2e
# proving the fallback machinery behaves right under a red resource (steward chain lands on the
# hermes head, product heads claim-skip) needs to redden claude-sub on demand, without actually
# touching the shared subscription. Bypasses the TTL cache too, so flipping it takes effect on the
# very next refresh().
_FORCE_RED_ENV = "TA_HEALTH_FORCE_RED"

GREEN = "green"
RED = "red"


def _load() -> dict:
    if not HEALTH_FILE.is_file():
        return {}
    try:
        return json.loads(HEALTH_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _save(cache: dict) -> None:
    STATE.ensure_dir()
    tmp = HEALTH_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(HEALTH_FILE)


def _forced_red() -> set[str]:
    return {r for r in os.environ.get(_FORCE_RED_ENV, "").split(",") if r}


def _run_probe_cmd(cmd: str) -> bool:
    """True (green) iff `cmd` exits 0 within PROBE_TIMEOUT_S. A timeout, a missing binary, or any
    other OSError all count as red — a probe that can't even run proves nothing about the
    resource being up."""
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True, timeout=PROBE_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return p.returncode == 0


def refresh(registry: heads_mod.Registry | None = None) -> dict[str, str]:
    """Re-probe every resource whose cached entry is missing or older than PROBE_TTL_S; return
    {resource_id: GREEN/RED} for every resource in the registry (cached entries included). Logs
    one `head-health` runs.jsonl event per resource whose status actually flipped since its last
    cached value — never on a fresh-cache reuse, never on a re-probe that confirms the same
    status. A resource named in TA_HEALTH_FORCE_RED is always RED here, real probe skipped
    entirely, TTL cache bypassed — see _FORCE_RED_ENV."""
    reg = registry or heads_mod.load_registry()
    cache = _load()
    now = time.time()
    dirty = False
    statuses: dict[str, str] = {}
    forced = _forced_red()
    for rid, res in reg.resources.items():
        entry = cache.get(rid)
        if rid not in forced and entry and now - entry.get("checked_at", 0) < PROBE_TTL_S:
            statuses[rid] = entry["status"]
            continue
        new_status = RED if rid in forced else (GREEN if _run_probe_cmd(res.get("probe", "true")) else RED)
        old_status = entry["status"] if entry else None
        cache[rid] = {"status": new_status, "checked_at": now}
        dirty = True
        statuses[rid] = new_status
        if old_status is not None and old_status != new_status:
            STATE.log_run("head-health", resource=rid, **{"from": old_status, "to": new_status})
    if dirty:
        _save(cache)
    return statuses


def resource_of(profile_id: str, registry: heads_mod.Registry | None = None) -> str | None:
    """`profile_id`'s resource id, or None if the profile is unknown to the registry (a reconciled
    record with no stored head, or a profile removed from heads.toml since bring-up) — callers
    treat that as "nothing to freeze/fall back on", never as an error mid-tick."""
    reg = registry or heads_mod.load_registry()
    try:
        return reg.profile(profile_id)["resource"]
    except heads_mod.HeadRegistryError:
        return None


def resolve_head(preferred: str, statuses: dict[str, str],
                 registry: heads_mod.Registry | None = None) -> str | None:
    """The profile to actually launch for `preferred`: itself if its resource is green, else the
    first profile — breadth-first over the ordered fallback chain, recursively — whose resource is
    green. None when `preferred` and everything reachable through its fallback chain sits on a red
    resource: the caller must not claim in that case (claim-skip, card stays Ready)."""
    reg = registry or heads_mod.load_registry()
    seen: set[str] = set()
    queue = [preferred]
    while queue:
        pid = queue.pop(0)
        if pid in seen:
            continue
        seen.add(pid)
        try:
            prof = reg.profile(pid)
        except heads_mod.HeadRegistryError:
            continue
        if statuses.get(prof["resource"], GREEN) == GREEN:
            return pid
        queue.extend(prof.get("fallback") or [])
    return None


def next_retry_head(current: str, tried: set[str], statuses: dict[str, str],
                    registry: heads_mod.Registry | None = None) -> tuple[str | None, bool]:
    """The next profile for a watchdog retry-switch to land `current` on: breadth-first over
    `current`'s own fallback chain (same walk as resolve_head), skipping anything already in
    `tried` (every head this card's watchdog has already used this life, `current` included) and
    any red resource. Returns (head, False) on a hit.

    A miss carries a second flag distinguishing why, so dispatcher._watchdog_retry can tell "spend
    the switch budget's one shot on nothing" from "there's a real target, just not up right now":
      (None, True)  nothing untried left to try at all (empty/exhausted chain, or `current` itself
                    unknown to the registry) — stop retrying, nothing here will ever turn green.
      (None, False) untried candidates exist but every one sits on a red resource right now —
                    requeue without spending the budget; the next claim's own red-skip
                    (_claim_next/resolve_head) picks the card up once a resource recovers."""
    reg = registry or heads_mod.load_registry()
    try:
        queue = list(reg.profile(current).get("fallback") or [])
    except heads_mod.HeadRegistryError:
        return None, True
    visited = {current}   # cycle guard only — a tried-but-not-visited node's OWN fallback is still
                          # worth walking, it may lead to something genuinely untried further out
    found_untried = False
    while queue:
        pid = queue.pop(0)
        if pid in visited:
            continue
        visited.add(pid)
        try:
            prof = reg.profile(pid)
        except heads_mod.HeadRegistryError:
            continue
        queue.extend(prof.get("fallback") or [])
        if pid in tried:
            continue
        found_untried = True
        if statuses.get(prof["resource"], GREEN) == GREEN:
            return pid, False
    return None, not found_untried


# Real, cheap probes for the two resources this repo actually names in heads.toml. Deliberately
# not resource-id-dispatched inside `refresh`/`_run_probe_cmd` above (those stay pure "run this
# shell command" — heads.toml's `probe` field is the single source of what each resource runs);
# these are just what that field's command, for these two ids, happens to invoke.
_OPENROUTER_ENV_FILE = Path(os.environ.get("TA_OPENROUTER_ENV_FILE",
                                          str(Path.home() / "projects" / "project_inspect" / ".env")))
_OPENROUTER_ENV_KEY = "open_router_key"


def _read_openrouter_key() -> str | None:
    override = os.environ.get("TA_OPENROUTER_KEY")
    if override:
        return override
    try:
        text = _OPENROUTER_ENV_FILE.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        if key.strip() == _OPENROUTER_ENV_KEY:
            return val.strip().strip('"').strip("'") or None
    return None


def probe_claude_sub() -> bool:
    """One haiku token through the shared OAuth-authenticated `claude` CLI (see heads.toml:
    claude-sub is one subscription, no per-profile credential) — red exactly when the
    subscription is rate-limited or the CLI can't reach the API, otherwise green. Costs one cheap
    call per PROBE_TTL_S, never per tick."""
    try:
        p = subprocess.run(
            ["claude", "-p", "ping", "--model", "haiku", "--dangerously-skip-permissions"],
            capture_output=True, text=True, timeout=PROBE_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return p.returncode == 0


def probe_openrouter() -> bool:
    """One 1-token chat completion against OpenRouter's gemini-flash — red on a missing key, a
    non-2xx response, a timeout, or any transport error; never raises."""
    key = _read_openrouter_key()
    if not key:
        return False
    body = json.dumps({
        "model": "google/gemini-2.5-flash",
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions", data=body, method="POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT_S) as resp:  # noqa: S310 (fixed host)
            return 200 <= resp.status < 300
    except Exception:  # noqa: BLE001 — any transport/HTTP-error outcome is just "red"
        return False


BUILTIN_PROBES = {
    "claude-sub": probe_claude_sub,
    "openrouter": probe_openrouter,
}


def run_builtin_probe(resource_id: str) -> bool:
    """Dispatch to the real check for `resource_id` — the thing heads.toml's `probe = "python3 -m
    triggered_agents pipeline probe --resource <id>"` command actually runs. Raises KeyError for
    an id with no builtin (a resource that only ever needs "true"/"false" has no reason to go
    through this CLI at all)."""
    return BUILTIN_PROBES[resource_id]()
