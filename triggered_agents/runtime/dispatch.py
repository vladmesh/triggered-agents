"""Singleton terminal driver, shared by every triggered-agent.

Replaces `orca automations run` in the systemd trigger. One agent = one claude terminal in its
worktree. On a trigger (after precheck passes, under the run lock) it decides:

  * an agent terminal is busy and fresh  -> leave it working, dispatch nothing
  * otherwise (none / all idle / stuck)  -> reset the workspace and start one fresh agent

Why `orca automations run` was wrong: it dispatches trigger=manual and spawns a NEW head every
tick (reuse only kicks in for scheduled runs, which don't tick headless), so `/board`/`/curate`
piled up as warm/stuck duplicates — a leak.

Why reset-and-recreate instead of warm `/clear` reuse: Orca's per-terminal `close` doesn't
remove a pane, it kills the pty and respawns a bare shell in its place, so closing duplicates
just trades claude panes for orphan shells (UI noise). `terminal stop --worktree` is the only
clean sweep (down to Orca's one floor shell). So the steady state is exactly 1 floor shell + 1
agent terminal; a fresh claude per run is also more deterministic (no leaked cross-run context).

"Busy vs idle" is Orca's tui-idle condition; "stuck" is busy with no output for
WATCHDOG_SECONDS. Orca's agent status is known to wedge on 'working' after a silent exit, so a
bare busy check would freeze the agent forever — the watchdog is what makes "skip when busy"
safe. Dispatch only starts the agent and returns; the head reaches `advance` (same lock) minutes
later, so there's no deadlock.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import tomllib

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
    terms = _orca_json(["terminal", "list", "--worktree", f"path:{ws}"]).get("terminals", []) or []
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


def run(agent: str) -> int:
    skill, launch = _launch_cmd(agent)
    ws = _workspace(agent)
    with AgentState(agent).lock():
        now = time.time()
        for t in _agent_terminals(ws):
            if _is_idle(t["handle"]):
                continue
            quiet = _quiet_seconds(t, now)
            if quiet <= WATCHDOG_SECONDS:  # a fresh, working agent — don't interrupt or pile on
                print(f"dispatch[{agent}]: agent busy ({int(quiet)}s silent) — left running, no dispatch")
                return 0
            # else: busy but silent too long -> stuck; fall through to reset
        # none / all idle / stuck: sweep the workspace clean, start exactly one fresh agent
        _orca(["terminal", "stop", "--worktree", f"path:{ws}"])
        time.sleep(1.0)
        _orca(["terminal", "create", "--worktree", f"path:{ws}", "--command", launch])
        print(f"dispatch[{agent}]: reset workspace -> fresh {skill}")
        return 0
