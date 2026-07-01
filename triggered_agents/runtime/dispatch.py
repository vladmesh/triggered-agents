"""Singleton terminal driver, shared by every triggered-agent.

Replaces `orca automations run` in the systemd trigger. One agent = one warm claude terminal in
its worktree, reused across ticks. On a trigger (after precheck passes, under the run lock):

  * no agent terminal          -> create one running `claude ... <skill>`
  * one idle agent terminal    -> `/clear` it and re-send <skill> (warm reuse, kills nothing)
  * it's busy and fresh        -> leave it working, dispatch nothing
  * it's busy but stuck        -> watchdog: stop the workspace and start one fresh

Why warm reuse and not stop+create every run: Orca retains a dead pty as a ghost tab in the
workspace session after the process exits, so churning a terminal each tick piles up ghost tabs.
Reuse never kills the process, so no ghost is born — steady state stays at one terminal. The rare
kill paths (watchdog, closing legacy duplicates) do leave ghosts, so every run first reaps them
via `session.tabs.close` (`_reap_ghosts`) — the one lever that reaches the session store; the
`terminal` CLI can't. Together: steady state creates none, and any stray gets swept next tick.

Why not `orca automations run`: it dispatches trigger=manual and spawns a NEW head every tick
(reuse only kicks in for scheduled runs, which don't tick headless), so heads piled up.

"Busy vs idle" is Orca's tui-idle condition; "stuck" is busy with no output for
WATCHDOG_SECONDS. Orca's agent status is known to wedge on 'working' after a silent exit, so a
bare busy check would freeze the agent forever — the watchdog makes "skip when busy" safe.
Dispatch only sends the skill and returns; the head reaches `advance` (same lock) minutes later,
so there's no deadlock.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import tomllib

from . import orca_rpc
from .state import AgentState

_REPO_ROOT = Path(__file__).resolve().parents[2]
ORCA = os.environ.get("ORCA_BIN") or shutil.which("orca") or str(Path.home() / ".local/bin/orca")
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


def _launch_cmd(agent: str) -> tuple[str, str]:
    """(skill, full claude launch command) from the agent's automation.toml."""
    spec = tomllib.loads((_REPO_ROOT / "triggered_agents" / "agents" / agent / "automation.toml").read_text())
    skill = spec["skill"]
    return skill, f"claude --dangerously-skip-permissions {skill}"


def _agent_terminals(ws: str) -> list[dict]:
    """Live terminals in the workspace running a claude agent (title carries 'Claude')."""
    terms = _orca_json(["terminal", "list", "--worktree", f"path:{ws}", "--limit", "50"]).get("terminals", []) or []
    return [t for t in terms if "Claude" in (t.get("title") or "")]


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


def run(agent: str) -> int:
    skill, launch = _launch_cmd(agent)
    ws = _workspace(agent)
    with AgentState(agent).lock():
        reaped = _reap_ghosts(ws)  # prune dead-pty tabs so ghosts never accumulate
        if reaped:
            print(f"dispatch[{agent}]: reaped {reaped} ghost tab(s)")
        terms = _agent_terminals(ws)
        if not terms:
            _orca(["terminal", "create", "--worktree", f"path:{ws}", "--command", launch])
            print(f"dispatch[{agent}]: no terminal — created fresh -> {skill}")
            return 0

        survivor = max(terms, key=lambda t: t.get("lastOutputAt") or 0)
        if not _is_idle(survivor["handle"]):
            quiet = _quiet_seconds(survivor, time.time())
            if quiet <= WATCHDOG_SECONDS:  # a fresh, working agent — don't interrupt or pile on
                print(f"dispatch[{agent}]: agent busy ({int(quiet)}s silent) — left running, no dispatch")
                return 0
            # busy but silent too long -> stuck: sweep and restart (makes a ghost, but rare)
            _orca(["terminal", "stop", "--worktree", f"path:{ws}"])
            time.sleep(1.0)
            _orca(["terminal", "create", "--worktree", f"path:{ws}", "--command", launch])
            print(f"dispatch[{agent}]: busy but stuck ({int(quiet)}s silent) — watchdog restart -> {skill}")
            return 0

        # idle: warm reuse, killing nothing -> no ghost. Close only legacy duplicates (one-time).
        extras = [t for t in terms if t["handle"] != survivor["handle"]]
        for t in extras:
            _orca(["terminal", "close", "--terminal", t["handle"]])
        _orca(["terminal", "send", "--terminal", survivor["handle"], "--text", "/clear", "--enter"])
        time.sleep(1.0)  # let /clear settle before the skill lands
        _orca(["terminal", "send", "--terminal", survivor["handle"], "--text", skill, "--enter"])
        tail = f"; closed {len(extras)} dup(s)" if extras else ""
        print(f"dispatch[{agent}]: reused idle terminal (/clear -> {skill}){tail}")
        return 0
