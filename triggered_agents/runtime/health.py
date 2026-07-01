"""Health check for every registered triggered-agent.

One line per agent: is the systemd timer active, and how fresh is the last tick in
runs.jsonl (any event counts — precheck-skip still proves the timer fires and the
runtime runs). Last `advance` is informational: it can legitimately be days old when
nothing changed upstream. Exit non-zero if any agent is red.

State lives in the agent's worktree (the systemd unit's WorkingDirectory), not in this
checkout — so we look under the dispatch workspace, honoring TA_STATE the same way the
unit would only if it were set globally.
"""
from __future__ import annotations

import datetime
import json
import os
import subprocess
from pathlib import Path

from .dispatch import _workspace

MAX_AGE_S = int(os.environ.get("TA_HEALTH_MAX_AGE_S", str(3 * 3600)))  # hourly timers + slack


def _timer_active(agent: str) -> bool:
    p = subprocess.run(["systemctl", "is-active", f"ta-{agent}.timer"], capture_output=True, text=True)
    return p.stdout.strip() == "active"


def _runs(agent: str) -> list[dict]:
    path = Path(_workspace(agent)) / "state" / agent / "runs.jsonl"
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _age_s(ts: str) -> float:
    then = datetime.datetime.fromisoformat(ts)
    if then.tzinfo is None:
        then = then.replace(tzinfo=datetime.timezone.utc)
    return (datetime.datetime.now(datetime.timezone.utc) - then).total_seconds()


def check(agents: tuple[str, ...]) -> int:
    rc = 0
    for agent in agents:
        problems = []
        if not _timer_active(agent):
            problems.append(f"ta-{agent}.timer not active")
        runs = _runs(agent)
        if not runs:
            problems.append("no runs.jsonl yet")
            last_tick = last_advance = None
        else:
            last_tick = runs[-1]
            age = _age_s(last_tick["ts"])
            if age > MAX_AGE_S:
                problems.append(f"last tick {int(age / 60)}min ago (> {MAX_AGE_S // 60}min)")
            last_advance = next((r for r in reversed(runs) if r.get("event") == "advance"), None)
        status = "RED " if problems else "OK  "
        tick = last_tick["ts"] if last_tick else "-"
        adv = last_advance["ts"] if last_advance else "-"
        detail = "; ".join(problems) if problems else f"last tick {tick}, last advance {adv}"
        print(f"{status}{agent}: {detail}")
        if problems:
            rc = 1
    return rc
