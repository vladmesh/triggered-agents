"""Singleton terminal driver, shared by every triggered-agent.

Replaces `orca automations run` in the systemd trigger. One agent = one warm claude terminal
in its worktree. On a trigger (after precheck passes) this converges the workspace to a single
agent terminal and dispatches the skill into it:

  * no agent terminal open        -> create one running `claude ... <skill>`
  * an idle agent terminal open   -> `/clear` it and re-send <skill> (warm reuse, no re-init)
  * agent terminal busy (fresh)   -> leave it working, don't re-dispatch (and close any dups)
  * agent terminal busy but stuck -> watchdog: close it and start fresh

Why not just Orca's `reuse_session`: it spawns a NEW terminal when the prior session is busy, so
tick agents pile up warm/stuck duplicates (a leak). We want the opposite policy — busy means
skip, not spawn — which Orca doesn't do natively, so the trigger drives terminals directly.

"Busy vs idle" is Orca's tui-idle condition; "stuck" is busy with no terminal output for
WATCHDOG_SECONDS. Orca's own agent status is known to wedge on 'working' after a silent exit, so
a bare busy check would disable the agent forever — the watchdog is what makes "skip when busy"
safe.

Runs under the agent's run lock so a manual trigger can't race the timer into two terminals.
Dispatch only SENDS the skill and returns; the head reaches `advance` (same lock) minutes later,
so there's no deadlock.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import tomllib
from pathlib import Path

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
    """Live terminals in the workspace that are running a claude agent (title carries 'Claude')."""
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


def _close(handle: str) -> None:
    _orca(["terminal", "close", "--terminal", handle])


def _send(handle: str, text: str) -> None:
    _orca(["terminal", "send", "--terminal", handle, "--text", text, "--enter"])


def _create(ws: str, command: str) -> None:
    _orca(["terminal", "create", "--worktree", f"path:{ws}", "--command", command])


def run(agent: str) -> int:
    skill, launch = _launch_cmd(agent)
    ws = _workspace(agent)
    with AgentState(agent).lock():
        terms = _agent_terminals(ws)
        if not terms:
            _create(ws, launch)
            print(f"dispatch[{agent}]: no terminal — created fresh -> {skill}")
            return 0

        idle = [t for t in terms if _is_idle(t["handle"])]
        if idle:
            survivor = idle[0]
            for t in terms:
                if t["handle"] != survivor["handle"]:
                    _close(t["handle"])
            _send(survivor["handle"], "/clear")
            time.sleep(1.0)  # let /clear settle before the skill lands
            _send(survivor["handle"], skill)
            print(f"dispatch[{agent}]: reused idle terminal (/clear -> {skill}); closed {len(terms) - 1} extra")
            return 0

        # all busy — watchdog on the freshest; if even it is silent too long, everything's stuck.
        now = time.time()
        newest = max(terms, key=lambda t: t.get("lastOutputAt") or 0)
        quiet = _quiet_seconds(newest, now)
        if quiet > WATCHDOG_SECONDS:
            for t in terms:
                _close(t["handle"])
            _create(ws, launch)
            print(f"dispatch[{agent}]: busy but stuck ({int(quiet)}s silent) — watchdog restart -> {skill}")
            return 0
        for t in terms:  # genuinely working: keep the freshest, drop older duplicates, no re-dispatch
            if t["handle"] != newest["handle"]:
                _close(t["handle"])
        print(f"dispatch[{agent}]: busy ({int(quiet)}s silent) — left it running; closed {len(terms) - 1} dup(s)")
        return 0
