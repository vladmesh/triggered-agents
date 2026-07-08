"""Singleton terminal driver, shared by every triggered-agent.

Replaces `orca automations run` in the systemd trigger. One agent = one warm terminal in
its worktree, reused across ticks. On a trigger (after precheck passes, under the run lock):

  * no agent terminal          -> create one running the agent's resolved head profile
  * one idle agent terminal    -> `/clear` it and re-send <skill> (warm reuse, kills nothing)
  * ...unless its head is red  -> stop it, start a fresh one on the resolved fallback instead
  * it's busy and fresh        -> leave it working, dispatch nothing
  * it's busy but stuck        -> watchdog: stop the workspace and start one fresh

Why warm reuse and not stop+create every run: Orca retains a dead pty as a ghost tab in the
workspace session after the process exits, so churning a terminal each tick piles up ghost tabs.
Reuse never kills the process, so no ghost is born — steady state stays at one terminal. The rare
kill paths (watchdog, a red idle head, closing legacy duplicates) do leave ghosts, so every run
first reaps them via `session.tabs.close` (`_reap_ghosts`) — the one lever that reaches the
session store; the `terminal` CLI can't. Together: steady state creates none, and any stray gets
swept next tick.

Why not `orca automations run`: it dispatches trigger=manual and spawns a NEW head every tick
(reuse only kicks in for scheduled runs, which don't tick headless), so heads piled up.

"Busy vs idle" is Orca's tui-idle condition; "stuck" is busy with no output for
WATCHDOG_SECONDS. Orca's agent status is known to wedge on 'working' after a silent exit, so a
bare busy check would freeze the agent forever — the watchdog makes "skip when busy" safe.
Dispatch only sends the skill and returns; the head reaches `advance` (same lock) minutes later,
so there's no deadlock.

Every fresh spawn (create, watchdog-restart) goes through `_ensure_claude_ready` first
(`claude_env.ensure_trust`/`ensure_theme`): a head that lands on the folder-trust dialog or the
onboarding theme picker hangs on stdin nobody sends, and never renames its tab away from the
shell default — invisible to the `Claude`-in-title match above, so it's neither reused nor
reaped and just sits there as a silent orphan (found live in the curator workspace: a terminal
stuck at "choose the text style" that `_agent_terminals` couldn't see).

A live terminal never re-resolves its head profile on its own, so every spawn that resolves one
(create, watchdog-restart, red-fallback) records it via `AgentState.save_head_profile` — the only
place idle-reuse can learn which resource the warm terminal is actually running against, since
that can already be a fallback and differ from the agent's static preferred head
(triggered-agents-275).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import tomllib

from . import claude_env, orca_rpc
from .state import AgentState

_REPO_ROOT = Path(__file__).resolve().parents[2]
ORCA = os.environ.get("ORCA_BIN") or shutil.which("orca") or str(Path.home() / ".local/bin/orca")
CLAUDE_JSON = Path(os.environ.get("TA_CLAUDE_JSON", str(Path.home() / ".claude.json")))
WATCHDOG_SECONDS = int(os.environ.get("TA_WATCHDOG_SECONDS", "1200"))  # busy + this quiet = stuck
IDLE_PROBE_MS = 2500        # tui-idle satisfied within this = idle; timeout = busy
ORCA_TIMEOUT_S = 20         # never let a hung orca call wedge dispatch while it holds the lock


def _orca_json(args: list[str]) -> dict:
    p = subprocess.run([ORCA, *args, "--json"], capture_output=True, text=True, timeout=ORCA_TIMEOUT_S)
    if p.returncode != 0:
        raise RuntimeError(f"orca {' '.join(args)} failed: {(p.stderr or p.stdout).strip()}")
    data = json.loads(p.stdout)
    return data.get("result", data)


def _orca(args: list[str]) -> None:
    subprocess.run([ORCA, *args], capture_output=True, text=True, timeout=ORCA_TIMEOUT_S)


def _workspace(agent: str) -> str:
    return os.environ.get("TA_WORKSPACE") or str(Path.home() / "orca/workspaces/triggered-agents" / agent)


def _load_spec(agent: str) -> dict:
    return tomllib.loads((_REPO_ROOT / "triggered_agents" / "agents" / agent / "automation.toml").read_text())


def _pipeline_paused() -> bool:
    """Whether the pipeline-wide pause flag (triggered-agents-281, agents/pipeline/pause.py) is
    set — checked first thing in run(), before the ghost reap or any of the four dispatch branches,
    so a paused pipeline never spends a token on steward/curator/retro either: none of them carry
    an in-flight card of their own the way a worker/reviewer head does, so pause has no "let it
    finish its cycle" case here in either mode, soft or hard. Lazy import, same reason as
    _reuse_head_is_red's own agents.pipeline.health import just below — this module is imported at
    process start by every agent, so a top-level import back into agents.pipeline would risk a
    circular import the first time either side changes its own imports. Best-effort: any failure
    (pause.py itself broken) defaults to not-paused rather than silently wedging every agent."""
    try:
        from ..agents.pipeline import pause as pipeline_pause
        return pipeline_pause.is_paused()
    except Exception:
        return False


def _reuse_head_is_red(agent: str, state: AgentState) -> bool:
    """Whether the profile the idle terminal was ACTUALLY launched with is currently sitting on a
    red resource — the check idle-reuse needs before sending into an already-warm terminal, since
    that terminal keeps whatever profile it was spawned with and never re-resolves on its own
    (only a fresh spawn does, via `_launch_cmd`).

    Reads the profile `state` recorded at the terminal's last create/restart/red-fallback
    (`AgentState.load_head_profile`) rather than re-reading `agent`'s static preferred head from
    automation.toml: the two can diverge (the terminal may already be running on a fallback), and
    checking the wrong one either misses a genuinely dead terminal (preferred head recovered while
    the terminal's actual fallback profile went red) or diverts needlessly (preferred head still
    red while the terminal is already happily running its fallback). Falls back to the static
    preferred head when nothing was recorded yet (state predates this tracking).

    Best-effort and defaults to green, matching `_launch_cmd`'s own fallback reasoning: a spec
    with no head, a broken heads.toml, or any resolution failure all mean "nothing to divert
    from", so idle-reuse only ever skips the warm terminal when a red resource is actually
    confirmed (triggered-agents-274, triggered-agents-275).
    """
    try:
        head = _load_spec(agent).get("head")
        if not head:
            return False
        profile = state.load_head_profile() or head
        from ..agents.pipeline import health as pipeline_health
        statuses = pipeline_health.refresh()
        resource = pipeline_health.resource_of(profile)
        return resource is not None and statuses.get(resource, pipeline_health.GREEN) == pipeline_health.RED
    except Exception:
        return False


def _launch_cmd(agent: str, variant: str | None = None,
                card_ref: str | None = None) -> tuple[str, str, str | None]:
    """(skill, full claude launch command, resolved head profile) from the agent's
    automation.toml. The third element is the profile id actually rendered into the launch
    command (None for a spec with no `head`, or when resolution raised) — the caller records it
    via `AgentState.save_head_profile` so a later idle-reuse tick can check the resource this very
    terminal is running against instead of just the agent's static preferred head
    (triggered-agents-275).

    A spec naming a `head` (a profile id in pipeline/heads.toml, e.g. the steward's claude-fable)
    launches through that registry: same adapter/model/fallback machinery a worker/reviewer head
    gets, resolved against this run's live resource health so a red claude-sub falls back to
    claude-opus instead of launching on a rate-limited account. curator/retro name no head
    and keep the bare default-model `claude` invocation they always had. Any failure to resolve
    (a broken heads.toml is itself the kind of anomaly the steward exists to catch) falls back to
    the same bare invocation rather than leaving the agent undispatched for the whole tick.

    `variant` (e.g. the steward's "deep-sweep", triggered-agents-254) reads `skill` from
    `spec["variants"][variant]` instead of the top-level one — a second, differently-scheduled
    mode of the same agent, same worktree/workspace/head, just a different prompt sent into it.

    `card_ref` (triggered-agents-255) appends `--card <ref>` to the skill text BEFORE it is handed
    to the head, the same way a hand-typed `/steward --card ...` would read — so the augmented text
    is what actually gets sent/embedded (heads.render_command reprs the whole prompt as one shell
    argument), not just tacked onto the rendered command afterward where it could land outside the
    quoted prompt.
    """
    spec = _load_spec(agent)
    skill = spec["variants"][variant]["skill"] if variant else spec["skill"]
    if card_ref:
        skill = f"{skill} --card {card_ref}"
    head = spec.get("head")
    if not head:
        return skill, f"claude --dangerously-skip-permissions {skill}", None
    try:
        from ..agents.pipeline import health as pipeline_health
        from ..agents.pipeline import heads as pipeline_heads
        statuses = pipeline_health.refresh()
        resolved = pipeline_health.resolve_head(head, statuses) or head
        return skill, pipeline_heads.render_command(resolved, role=agent, prompt=skill), resolved
    except Exception:
        return skill, f"claude --dangerously-skip-permissions {skill}", None


def _steward_report_card(agent: str, variant: str | None) -> str | None:
    """Create the steward's own wake-up report card (project triggered-agents, non-code type,
    straight into In progress, already claimed by itself — see pipeline.ops.create_report_card)
    right before a dispatch actually reaches the head. None for every agent but steward
    (triggered-agents-255): the rest keep their existing dispatch untouched.
    """
    if agent != "steward":
        return None
    from ..agents.pipeline import ops as pipeline_ops
    now = datetime.now(timezone.utc)
    kind = variant or "hourly"
    slug = f"steward-sweep-{now:%Y%m%d-%H%M%S}"
    card = pipeline_ops.create_report_card(
        project="triggered-agents",
        title=f"steward: {kind} sweep {now:%Y-%m-%d %H:%M UTC}",
        slug=slug,
    )
    return card["reference"]


def _dispatch_command(agent: str, variant: str | None) -> tuple[str, str, str | None]:
    """(skill, launch, resolved head profile) for a dispatch about to actually reach the head —
    the one spot that also creates the steward's report card, so every real dispatch (fresh
    create, watchdog restart, idle reuse) carries one and a busy-skip tick never does (no card,
    nobody to close it)."""
    card_ref = _steward_report_card(agent, variant)
    return _launch_cmd(agent, variant, card_ref=card_ref) if card_ref else _launch_cmd(agent, variant)


def _fresh_steward_report_in_progress(agent: str, now: float) -> dict | None:
    """A secondary run guard for steward dispatch.

    Orca terminal creation is not immediately visible in `terminal list` on every host. If two
    timers fire close together, the second dispatch can miss the first terminal and create a
    second report card/head. The report card is already the durable "this run exists" marker, so
    use it as a short-circuit while it is still younger than the steward stale threshold. Once it
    is stale, a later steward run must be allowed through to investigate and close/escalate it.
    """
    if agent != "steward":
        return None
    try:
        from ..agents.pipeline import ops as pipeline_ops
        from ..agents.steward import signals as steward_signals

        threshold = steward_signals.STALE_HOURS * 3600
        for card in pipeline_ops.list_cards(column="In progress", project="triggered-agents"):
            moved = card.get("date_moved")
            if card.get("steward_report") == "1" and moved and now - moved < threshold:
                return card
    except Exception:
        return None
    return None


def _ensure_claude_ready(ws: str) -> None:
    """Pre-answer folder trust + the onboarding theme picker before a fresh `claude` spawns.

    Without this a head can land on an interactive prompt, wait forever for input nobody sends,
    and never rename its terminal tab away from the shell default — invisible to
    `_agent_terminals`'s title match, so it's reused by nothing and reaped by nothing: an orphan
    every run creates that never dies (seen live in the curator workspace). Best-effort: a config
    hiccup here shouldn't block the tick, just risks the same hang it's meant to prevent.
    """
    try:
        claude_env.ensure_trust(CLAUDE_JSON, ws)
        claude_env.ensure_theme(CLAUDE_JSON)
    except claude_env.ClaudeConfigError as e:
        print(f"dispatch: claude config prep failed ({e})")


def _agent_terminals(ws: str, state: AgentState | None = None) -> list[dict]:
    """Live terminals in the workspace running this singleton agent.

    New spawns get an explicit `triggered-agent:<name>` title. The legacy `Claude` match keeps
    already-warm Claude terminals reusable until they are naturally restarted. Codex may rename
    its tab back to the shell cwd after startup, so the latest saved Orca handle is also accepted."""
    terms = _orca_json(["terminal", "list", "--worktree", f"path:{ws}", "--limit", "50"]).get("terminals", []) or []
    saved_handle = state.load_terminal_handle() if state else None
    return [
        t for t in terms
        if (saved_handle and (t.get("handle") or t.get("id")) == saved_handle)
        or (t.get("title") or "").startswith("triggered-agent:")
        or "Claude" in (t.get("title") or "")
    ]


def _create_terminal(agent: str, ws: str, launch: str, state: AgentState, profile: str | None) -> None:
    data = _orca_json(["terminal", "create", "--worktree", f"path:{ws}",
                       "--title", f"triggered-agent:{agent}", "--command", launch])
    term = data.get("terminal", data)
    state.save_terminal_handle(term.get("handle") or term.get("id"))
    state.save_head_profile(profile)


def _is_idle(handle: str) -> bool:
    try:
        res = _orca_json(["terminal", "wait", "--terminal", handle, "--for", "tui-idle",
                          "--timeout-ms", str(IDLE_PROBE_MS)])
    except (RuntimeError, subprocess.TimeoutExpired):
        return False
    return bool((res.get("wait") or {}).get("satisfied"))


def _quiet_seconds(term: dict, now: float) -> float:
    last = term.get("lastOutputAt")
    return (now - last / 1000.0) if last else 0.0


def _reap_ghosts(ws: str) -> int:
    """Close ghost tabs — ones whose pty died but linger in the workspace session store.

    `terminal list/stop/close` can't touch these (they only reach live ptys); the persisted
    `tabsByWorktree` keeps them as clutter until `session.tabs.close` prunes them (what the GUI
    tab-× does). Live tabs are status 'ready'; a dead pty leaves 'pending-handle'. Best-effort:
    if the RPC is unavailable (orca restarting), skip rather than fail the run.
    """
    try:
        snaps = (orca_rpc.call("session.tabs.listAll").get("result") or {}).get("snapshots", []) or []
    except Exception as e:
        print(f"dispatch: reap skipped ({e})")
        return 0
    closed = 0
    for snap in snaps:
        if snap.get("worktree", "").split("::", 1)[-1] != ws:
            continue
        for tab in snap.get("tabs", []) or []:
            if tab.get("status") != "ready":
                try:
                    orca_rpc.call("session.tabs.close", {"worktree": snap["worktree"], "tabId": tab["parentTabId"]})
                    closed += 1
                except Exception:
                    pass
    return closed


def run(agent: str, variant: str | None = None) -> int:
    """`variant` selects a differently-scheduled mode of the same agent (e.g. the steward's
    "deep-sweep", triggered-agents-254): a different prompt from `_launch_cmd`, and its own
    runs.jsonl event name (instead of the plain "dispatch" every hourly tick logs) so the two
    wake-up kinds stay distinguishable in the agent's own telemetry.

    `_dispatch_command` (not `_launch_cmd` directly) runs only in the three branches below that
    actually put the skill in front of a head (fresh create, watchdog restart, idle reuse) — never
    on a busy-skip, so a tick that dispatches nothing never creates the steward's report card
    either (triggered-agents-255)."""
    ws = _workspace(agent)
    state = AgentState(agent)
    event = variant or "dispatch"
    with state.lock():
        if _pipeline_paused():
            state.log_run(event, action="paused")
            print(f"dispatch[{agent}]: pipeline paused — no dispatch")
            return 0
        active_report = _fresh_steward_report_in_progress(agent, time.time())
        if active_report:
            state.log_run(event, action="active-report-skip", reference=active_report["reference"])
            print(
                f"dispatch[{agent}]: active steward report {active_report['reference']} "
                "is still fresh — no dispatch"
            )
            return 0
        reaped = _reap_ghosts(ws)  # prune dead-pty tabs so ghosts never accumulate
        if reaped:
            print(f"dispatch[{agent}]: reaped {reaped} ghost tab(s)")
        terms = _agent_terminals(ws, state)
        if not terms:
            skill, launch, profile = _dispatch_command(agent, variant)
            _ensure_claude_ready(ws)
            _create_terminal(agent, ws, launch, state, profile)
            state.log_run(event, action="created")
            print(f"dispatch[{agent}]: no terminal — created fresh -> {skill}")
            return 0

        survivor = max(terms, key=lambda t: t.get("lastOutputAt") or 0)
        if not _is_idle(survivor["handle"]):
            quiet = _quiet_seconds(survivor, time.time())
            if quiet <= WATCHDOG_SECONDS:  # a fresh, working agent — don't interrupt or pile on
                state.log_run(event, action="busy-skip")
                print(f"dispatch[{agent}]: agent busy ({int(quiet)}s silent) — left running, no dispatch")
                return 0
            # busy but silent too long -> stuck: sweep and restart (makes a ghost, but rare)
            _orca(["terminal", "stop", "--worktree", f"path:{ws}"])
            time.sleep(1.0)
            skill, launch, profile = _dispatch_command(agent, variant)
            _ensure_claude_ready(ws)
            _create_terminal(agent, ws, launch, state, profile)
            state.log_run(event, action="watchdog-restart")
            print(f"dispatch[{agent}]: busy but stuck ({int(quiet)}s silent) — watchdog restart -> {skill}")
            return 0

        # idle: a warm terminal keeps whatever profile it was spawned with, so a resource that's
        # gone red since spawn would otherwise get the skill anyway (only a fresh spawn
        # re-resolves). Stop it and start fresh on the resolved fallback instead — same shape as
        # the watchdog restart above — rather than leaving the red terminal running alongside a
        # new one, which would pile up one extra terminal per red tick (triggered-agents-274,
        # triggered-agents-275).
        if _reuse_head_is_red(agent, state):
            _orca(["terminal", "stop", "--worktree", f"path:{ws}"])
            time.sleep(1.0)
            skill, launch, profile = _dispatch_command(agent, variant)
            _ensure_claude_ready(ws)
            _create_terminal(agent, ws, launch, state, profile)
            state.log_run(event, action="reused-red-fallback")
            print(f"dispatch[{agent}]: idle terminal's head is red — stopped, fresh fallback terminal -> {skill}")
            return 0

        # idle: warm reuse, killing nothing -> no ghost. Close only legacy duplicates (one-time).
        state.save_terminal_handle(survivor.get("handle") or survivor.get("id"))
        extras = [t for t in terms if t["handle"] != survivor["handle"]]
        for t in extras:
            _orca(["terminal", "close", "--terminal", t["handle"]])
        _orca(["terminal", "send", "--terminal", survivor["handle"], "--text", "/clear", "--enter"])
        time.sleep(1.0)  # let /clear settle before the skill lands
        skill, _launch, _profile = _dispatch_command(agent, variant)
        _orca(["terminal", "send", "--terminal", survivor["handle"], "--text", skill, "--enter"])
        state.log_run(event, action="reused")
        tail = f"; closed {len(extras)} dup(s)" if extras else ""
        print(f"dispatch[{agent}]: reused idle terminal (/clear -> {skill}){tail}")
        return 0
