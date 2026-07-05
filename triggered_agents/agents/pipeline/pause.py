"""Persistent pipeline-wide pause flag: `state/pipeline/pause.json`.

Absent (no file) = running. Present = paused, `{"mode": "soft"|"hard", "since": <iso ts>,
"stopped_worker": [<ref>, ...], "stopped_reviewer": [<ref>, ...]}`. The two `stopped_*` lists are
only ever populated by a hard pause (dispatcher.pause) and name exactly the cards whose live
terminal it stopped, so dispatcher.resume() knows precisely what to relaunch without re-deriving
it from cards.json — a card's column/record shape alone can't tell "a terminal was actually
running here when pause hit" from "this Validate card just hasn't spawned a reviewer yet". A
single Validate card can appear in both lists at once (its original worker terminal parked for CI
rework, and a live layer-3 reviewer) — each is stopped and relaunched independently.

Read from two places outside dispatcher.py itself: runtime/dispatch.py (steward/curator/retro
dispatch) and cli.py (pause-status) — both only ever call is_paused()/load()/status(), never
write. Kept in its own tiny module (state.py primitives only, no ops/worker/dispatcher import) so
runtime/dispatch.py can read it without pulling in dispatcher's board/host machinery — the same
reason it already lazy-imports agents.pipeline.health instead of importing dispatcher directly.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from ...runtime.state import AgentState

STATE = AgentState("pipeline")
PAUSE_FILE = STATE.dir / "pause.json"
MODES = ("soft", "hard")


def load() -> dict:
    if not PAUSE_FILE.is_file():
        return {}
    try:
        return json.loads(PAUSE_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save(mode: str, stopped_worker: list[str] | None = None,
        stopped_reviewer: list[str] | None = None) -> None:
    STATE.ensure_dir()
    state = {
        "mode": mode,
        "since": datetime.now(timezone.utc).isoformat(),
        "stopped_worker": stopped_worker or [],
        "stopped_reviewer": stopped_reviewer or [],
    }
    tmp = PAUSE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(PAUSE_FILE)


def clear() -> None:
    try:
        PAUSE_FILE.unlink()
    except FileNotFoundError:
        pass


def is_paused() -> bool:
    return bool(load())


def status() -> dict:
    state = load()
    if not state:
        return {"paused": False}
    return {
        "paused": True,
        "mode": state.get("mode"),
        "since": state.get("since"),
        "stopped_worker": state.get("stopped_worker") or [],
        "stopped_reviewer": state.get("stopped_reviewer") or [],
    }
