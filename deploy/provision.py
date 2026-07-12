#!/usr/bin/env python3
"""Provision a triggered-agent's Orca automation + systemd timer from its in-repo spec.

The spec (triggered_agents/agents/<agent>/automation.toml) is the canon for how an agent is
scheduled and dispatched. Host bindings Orca generates (workspace path, repo/automation
UUIDs) are resolved here, not stored in the spec. This applies the spec idempotently:

  * register the repo in Orca (once),
  * mark the workspace folder trusted for Claude Code (else headless hangs on the dialog),
  * upsert the Orca automation matched BY NAME — edit in place so its id (and thus the
    systemd ExecStart) stays stable across re-provisions; create only if missing,
  * install the precheck gate script (deploy/ta-gate.sh) that the units' ExecStart references,
  * generate the ta-<agent> systemd service+timer from the spec (the real clock on this
    headless box), and remove a one-time legacy unit if the spec names one.

Re-runnable. Usage: python3 deploy/provision.py [<agent> ...]  (default: every agent with a spec)
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
# The only checkout provisioning may bind host state to. Run from anywhere else (a task
# workspace, an agent worktree) and ensure_repo_registered would register THAT checkout as a
# new Orca repo, fork agent worktrees off it and repoint the live ta-* units at them —
# 2026-07-04 a worker did exactly this from its card workspace and hijacked the whole runtime.
CANONICAL_ROOT = Path.home() / "triggered-agents"
AGENTS_DIR = REPO_ROOT / "triggered_agents" / "agents"
CLAUDE_JSON = Path.home() / ".claude.json"
SYSTEMD_DIR = Path("/etc/systemd/system")
# The precheck gate lives as a versioned script (deploy/ta-gate.sh), installed to a fixed host path
# so every unit's ExecStart can reference it by absolute path instead of an inline shell string.
# install_gate_script() sudo-writes it alongside the units.
GATE_SCRIPT_SRC = REPO_ROOT / "deploy" / "ta-gate.sh"
GATE_INSTALL_PATH = Path("/usr/local/bin/ta-gate.sh")
ORCA = os.environ.get("ORCA_BIN") or subprocess.run(
    ["bash", "-lc", "command -v orca"], capture_output=True, text=True
).stdout.strip() or "/home/dev/.local/bin/orca"


def log(msg: str) -> None:
    print(f"[provision] {msg}")


def run(cmd: list[str], check: bool = True, input_bytes: bytes | None = None) -> subprocess.CompletedProcess:
    p = subprocess.run(cmd, capture_output=True, text=True, input=input_bytes.decode() if input_bytes else None)
    if check and p.returncode != 0:
        raise SystemExit(f"[provision] command failed ({p.returncode}): {' '.join(cmd)}\n{p.stderr or p.stdout}")
    return p


def orca_json(args: list[str]) -> dict:
    p = run([ORCA, *args, "--json"])
    return json.loads(p.stdout)


def _unwrap(d: dict, key: str):
    # orca wraps payloads as {"ok":true,"result":{...}}; tolerate both shapes.
    res = d.get("result", d)
    return res.get(key, d.get(key))


# ---- steps --------------------------------------------------------------------------

def _repo_id(root: Path) -> str | None:
    for line in run([ORCA, "repo", "list"]).stdout.splitlines():
        f = line.split()
        if f and f[-1] == str(root):
            return f[0]
    return None


def ensure_repo_registered(root: Path) -> str:
    rid = _repo_id(root)
    if rid:
        log(f"repo already registered: {root}")
        return rid
    run([ORCA, "repo", "add", "--path", str(root)])
    rid = _repo_id(root)
    log(f"repo registered: {root} ({rid})")
    return rid


def ensure_worktree(agent: str, repo_id: str) -> Path:
    """One Orca worktree per agent (so each shows as its own named workspace, not 'main').

    Each is a git worktree on branch vladmesh/<agent> off origin/main. Agents never commit
    to this repo, so we pin the worktree to origin/main on every provision — code stays the
    deployed version, gitignored state/ survives the reset.
    """
    data = orca_json(["worktree", "list", "--repo", f"id:{repo_id}"])
    path = None
    for w in (_unwrap(data, "worktrees") or []):
        if w.get("displayName") == agent:
            path = Path(w["path"])
            break
    if path is None:
        created = orca_json(["worktree", "create", "--name", agent, "--repo", f"id:{repo_id}",
                             "--setup", "skip", "--no-parent"])
        path = Path((_unwrap(created, "worktree") or {})["path"])
        log(f"worktree created: {agent} -> {path}")
    else:
        log(f"worktree exists: {agent} -> {path}")
    run(["git", "-C", str(path), "fetch", "--quiet", "origin"], check=False)
    run(["git", "-C", str(path), "reset", "--hard", "origin/main"])
    log(f"worktree pinned to origin/main: {path}")
    return path


def migrate_state(agent: str, workspace: Path) -> None:
    """One-time: move an agent's gitignored state from the base checkout into its worktree."""
    src = REPO_ROOT / "state" / agent
    dst = workspace / "state" / agent
    if src.is_dir() and not dst.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        log(f"state migrated: {src} -> {dst}")


def ensure_trust(workspace: Path) -> None:
    key = str(workspace)
    d = json.loads(CLAUDE_JSON.read_text())
    projs = d.setdefault("projects", {})
    entry = projs.setdefault(key, {})
    if entry.get("hasTrustDialogAccepted") is True:
        log(f"folder trust already set: {key}")
        return
    entry["hasTrustDialogAccepted"] = True
    fd, tmp = tempfile.mkstemp(dir=str(CLAUDE_JSON.parent), prefix=".claude.json.")
    with os.fdopen(fd, "w") as f:
        json.dump(d, f, indent=2, ensure_ascii=False)
    os.replace(tmp, CLAUDE_JSON)
    log(f"folder trust set: {key}")


def _automation_id_by_name(name: str) -> str | None:
    data = orca_json(["automations", "list"])
    for a in (_unwrap(data, "automations") or []):
        if a.get("name") == name:
            return a.get("id")
    return None


def ensure_automation(spec: dict, workspace: Path) -> str | None:
    name = spec["name"]
    if spec.get("dispatcher") or not spec.get("skill"):
        # Deterministic agent (the pipeline dispatcher): the systemd timer drives its tick
        # directly, there is no claude head, so no Orca automation to register.
        log(f"automation skipped (deterministic dispatcher): {name}")
        return None
    common = [
        "--prompt", spec["skill"],
        "--provider", spec["provider"],
        "--trigger", spec.get("trigger", {}).get("orca", "hourly"),
        "--workspace", f"path:{workspace}",
        "--workspace-mode", "existing",
        "--reuse-session" if spec.get("reuse_session") else "--fresh-session",
    ]
    if spec.get("precheck"):
        common += ["--precheck", spec["precheck"]]

    aid = _automation_id_by_name(name)
    if aid:
        run([ORCA, "automations", "edit", aid, *common])
        log(f"automation edited (id preserved): {name} {aid}")
    else:
        data = orca_json(["automations", "create", "--name", name, *common, "--enabled"])
        aid = (_unwrap(data, "automation") or {}).get("id")
        if not aid:
            raise SystemExit(f"[provision] could not read new automation id for {name}")
        log(f"automation created: {name} {aid}")
    return aid


def _service_unit(agent: str, calendar: str, workspace: Path, _precheck: str) -> str:
    # ExecStart is the versioned gate script (deploy/ta-gate.sh, installed at GATE_INSTALL_PATH),
    # run as `ta-gate.sh <agent>`. It calls `<agent> precheck` and branches on the exit-code
    # protocol (0 dispatch / 100 skip / other = fail the unit), then execs `<agent> dispatch`.
    # Dispatch is our singleton terminal driver, not `orca automations run`: the latter dispatches
    # trigger=manual and spawns a fresh head every tick, so heads pile up. The driver converges the
    # workspace to one warm claude terminal and reuses it. See runtime/dispatch.py and ta-gate.sh.
    exec_start = f"{GATE_INSTALL_PATH} {agent}"
    return f"""[Unit]
Description=triggered-agents: {agent} {calendar} tick (precheck gate + singleton terminal dispatch)
Documentation=file:///home/dev/control-panel/docs/ARCHITECTURE.md
After=orca-server.service network-online.target
Wants=orca-server.service

[Service]
Type=oneshot
User=dev
Group=dev
WorkingDirectory={workspace}
Environment=HOME=/home/dev
ExecStart={exec_start}
"""


def _timer_unit(label: str, calendar: str, delay: int) -> str:
    # `calendar` may hold several whitespace-separated specs (systemd cannot express
    # sub-minute periods like "every 90s" in one OnCalendar line). `label` is descriptive text
    # only (e.g. "steward" or "steward deep-sweep") — the unit filename is chosen by the caller.
    on_calendar = "\n".join(f"OnCalendar={c}" for c in calendar.split())
    return f"""[Unit]
Description=triggered-agents: {label} {calendar} sweep

[Timer]
{on_calendar}
Persistent=true
RandomizedDelaySec={delay}

[Install]
WantedBy=timers.target
"""


def _variant_service_unit(agent: str, variant: str, calendar: str, workspace: Path) -> str:
    """Unconditional dispatch — no precheck gate at all (triggered-agents-254: the whole point of
    a second, differently-scheduled mode is to wake the head even when the deterministic signals
    stayed quiet, including the case where the signals themselves are blind). ExecStart is the
    script called with a variant arg (`ta-gate.sh <agent> <variant>`), whose two-arg form skips
    precheck entirely and execs `dispatch <variant>` directly; dispatch.py's own busy/idle check
    still protects it from colliding with a live hourly-tick session in the same workspace."""
    return f"""[Unit]
Description=triggered-agents: {agent} {variant} {calendar} unconditional sweep (no precheck gate)
Documentation=file:///home/dev/control-panel/docs/ARCHITECTURE.md
After=orca-server.service network-online.target
Wants=orca-server.service

[Service]
Type=oneshot
User=dev
Group=dev
WorkingDirectory={workspace}
Environment=HOME=/home/dev
ExecStart={GATE_INSTALL_PATH} {agent} {variant}
"""


def _sudo_write(path: Path, content: str) -> None:
    run(["sudo", "tee", str(path)], input_bytes=content.encode())


def install_gate_script() -> None:
    """Install deploy/ta-gate.sh to GATE_INSTALL_PATH so unit ExecStarts can reference it by
    absolute path. Idempotent: sudo-writes the current repo copy on every provision, same delivery
    channel as the unit files, so the script and the units that reference it cannot drift inside one
    provision run. The gate is a versioned script, not an inline `bash -lc` string in ExecStart, so
    this logic is reviewable and unit-tested (triggered-agents-276)."""
    _sudo_write(GATE_INSTALL_PATH, GATE_SCRIPT_SRC.read_text(encoding="utf-8"))
    run(["sudo", "chmod", "755", str(GATE_INSTALL_PATH)])
    log(f"gate script installed: {GATE_INSTALL_PATH}")


def remove_legacy_unit(legacy: str) -> None:
    if not legacy:
        return
    timer = f"{legacy}.timer"
    if subprocess.run(["systemctl", "cat", timer], capture_output=True).returncode != 0:
        return
    log(f"removing legacy unit: {legacy}")
    run(["sudo", "systemctl", "disable", "--now", timer], check=False)
    for suffix in ("service", "timer"):
        run(["sudo", "rm", "-f", str(SYSTEMD_DIR / f"{legacy}.{suffix}")], check=False)


def render_units(agent: str, spec: dict, workspace: Path) -> dict[str, str]:
    """unit filename ('ta-<agent>.service', ...) -> rendered content, for `agent`'s main
    service+timer and every `[variants.*]` pair declared in `spec` (the parsed automation.toml).
    Single source of truth for "what SHOULD this agent's units contain right now": ensure_systemd
    below writes exactly this to disk, and steward's drift.py (triggered-agents-256) compares it
    against what's actually there. Extracting the spec fields (calendar/delay defaults) a second
    time in drift.py would let that copy silently diverge from this one — the same
    two-renderers-drift-from-each-other risk the unit *text* builders (_service_unit etc.) already
    avoid by being called from both places instead of reimplemented."""
    sysd = spec.get("systemd", {})
    calendar = sysd.get("calendar", "hourly")
    units = {
        f"ta-{agent}.service": _service_unit(agent, calendar, workspace, spec.get("precheck", "")),
        f"ta-{agent}.timer": _timer_unit(agent, calendar, int(sysd.get("randomized_delay_sec", 0))),
    }
    for variant, vspec in spec.get("variants", {}).items():
        vsysd = vspec.get("systemd", {})
        vcalendar = vsysd.get("calendar", "daily")
        vunit = f"ta-{agent}-{variant}"
        units[f"{vunit}.service"] = _variant_service_unit(agent, variant, vcalendar, workspace)
        units[f"{vunit}.timer"] = _timer_unit(f"{agent} {variant}", vcalendar,
                                              int(vsysd.get("randomized_delay_sec", 0)))
    return units


def ensure_systemd(agent: str, spec: dict, workspace: Path) -> None:
    units = render_units(agent, spec, workspace)
    install_gate_script()   # the units below reference it by absolute path; write it first
    sysd = spec.get("systemd", {})
    unit = f"ta-{agent}"
    _sudo_write(SYSTEMD_DIR / f"{unit}.service", units[f"{unit}.service"])
    _sudo_write(SYSTEMD_DIR / f"{unit}.timer", units[f"{unit}.timer"])
    remove_legacy_unit(sysd.get("legacy_unit", ""))
    run(["sudo", "systemctl", "daemon-reload"])
    run(["sudo", "systemctl", "enable", "--now", f"{unit}.timer"])
    log(f"systemd unit active: {unit}.timer ({sysd.get('calendar', 'hourly')})")

    # A second, differently-scheduled mode of the same agent (spec's `[variants.<name>]` table,
    # e.g. the steward's "deep-sweep") — same worktree/workspace, its own systemd unit pair, no
    # precheck gate (see _variant_service_unit).
    for variant, vspec in spec.get("variants", {}).items():
        vcalendar = vspec.get("systemd", {}).get("calendar", "daily")
        vunit = f"ta-{agent}-{variant}"
        _sudo_write(SYSTEMD_DIR / f"{vunit}.service", units[f"{vunit}.service"])
        _sudo_write(SYSTEMD_DIR / f"{vunit}.timer", units[f"{vunit}.timer"])
        run(["sudo", "systemctl", "daemon-reload"])
        run(["sudo", "systemctl", "enable", "--now", f"{vunit}.timer"])
        log(f"systemd unit active: {vunit}.timer ({vcalendar})")


def provision(agent: str) -> None:
    spec_path = AGENTS_DIR / agent / "automation.toml"
    if not spec_path.is_file():
        raise SystemExit(f"[provision] no spec: {spec_path}")
    spec = tomllib.loads(spec_path.read_text())
    log(f"=== provisioning {agent} ===")
    repo_id = ensure_repo_registered(REPO_ROOT)
    workspace = ensure_worktree(agent, repo_id)   # per-agent named worktree, not the base 'main'
    migrate_state(agent, workspace)
    ensure_trust(workspace)
    # The Orca automation is now vestigial (the timer drives runtime/dispatch.py, not
    # `automations run`); kept so the agent still shows in Orca's GUI and its precheck/config
    # is recorded. Its own scheduler doesn't tick headless, so it won't fire on its own.
    ensure_automation(spec, workspace)
    ensure_systemd(agent, spec, workspace)
    log(f"=== {agent} done (workspace {workspace}) ===")


def main(argv: list[str]) -> int:
    unsafe = "--unsafe-root" in argv
    argv = [a for a in argv if a != "--unsafe-root"]
    if REPO_ROOT != CANONICAL_ROOT and not unsafe:
        raise SystemExit(
            f"[provision] refusing to run from non-canonical checkout {REPO_ROOT}: this would "
            f"register it as a new Orca repo and repoint live ta-* units at worktrees forked "
            f"off it. Run from {CANONICAL_ROOT}, or pass --unsafe-root to override."
        )
    agents = argv or sorted(p.name for p in AGENTS_DIR.iterdir() if (p / "automation.toml").is_file())
    if not agents:
        log("no agent specs found")
        return 1
    for a in agents:
        provision(a)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
