"""Persistent pipeline-wide pause flag: `state/pipeline/pause.json`.

Absent (no file) = running. Present = paused, with internal `mode` stored as `"soft"` or `"hard"`,
plus the public reason/actor and the refs stopped by a hard pause. The two `stopped_*` lists are
only ever populated by a hard pause (dispatcher.pause) and name exactly the cards whose live
terminal it stopped, so dispatcher.resume() knows precisely what to relaunch without re-deriving
it from cards.json. A card's column/record shape alone can't tell "a terminal was actually
running here when pause hit" from "this Validate card just hasn't spawned a reviewer yet". A
single Validate card can appear in both lists at once (its original worker terminal parked for CI
rework, and a live layer-3 reviewer), each is stopped and relaunched independently.

Read from two places outside dispatcher.py itself: runtime/dispatch.py (steward/curator/retro
dispatch) and cli.py (pause-status) — both only ever call is_paused()/load()/status(), never
write. Kept in its own tiny module (state.py primitives only, no ops/worker/dispatcher import) so
runtime/dispatch.py can read it without pulling in dispatcher's board/host machinery — the same
reason it already lazy-imports agents.pipeline.health instead of importing dispatcher directly.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from .state import STATE

PAUSE_FILE = STATE.dir / "pause.json"
MODES = ("soft", "hard")
PUBLIC_MODES = ("drain", "freeze")
MODE_ALIASES = {
    "drain": "soft",
    "freeze": "hard",
    "soft": "soft",
    "hard": "hard",
}
DISPLAY_MODES = {
    "soft": "drain",
    "hard": "freeze",
}
DEFAULT_REASON = "admin pause"
DEFAULT_ACTOR = "pipeline"


def normalize_mode(mode: str | None) -> str | None:
    return MODE_ALIASES.get(mode or "")


def display_mode(mode: str | None) -> str | None:
    return DISPLAY_MODES.get(mode or "", mode)


def _resolve(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return path.absolute()


def _checkout_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _candidate_pause_files() -> list[Path]:
    """Likely legacy pause locations. This stays narrow so a read-only status call never turns
    into a host-wide filesystem walk."""
    paths: set[Path] = set()
    ta_state = os.environ.get("TA_STATE")
    if ta_state:
        paths.add(Path(ta_state) / "pipeline" / "pause.json")
    paths.add(_checkout_root() / "state" / "pipeline" / "pause.json")
    paths.add(Path.home() / "triggered-agents" / "state" / "pipeline" / "pause.json")

    workspaces_root = Path(os.environ.get("TA_WORKSPACES_ROOT") or Path.home() / "orca" / "workspaces")
    agents_root = workspaces_root / "triggered-agents"
    if agents_root.is_dir():
        paths.update(agents_root.glob("*/state/pipeline/pause.json"))
    return sorted(paths, key=lambda p: str(p))


def shadow_pause_files() -> list[str]:
    live = _resolve(PAUSE_FILE)
    out: list[str] = []
    for path in _candidate_pause_files():
        if not path.is_file():
            continue
        resolved = _resolve(path)
        if resolved != live:
            out.append(str(resolved))
    return out


def _on_resume(mode: str | None, stopped_worker: list[str], stopped_reviewer: list[str]) -> str:
    if mode == "hard":
        stopped = len(stopped_worker) + len(stopped_reviewer)
        return (
            f"resume clears freeze, relaunches {stopped} stopped worker/reviewer head(s) in "
            "their existing workspaces, and resets watchdog clocks; Validate workers stay parked "
            "until rework is needed"
        )
    if mode == "soft":
        return (
            "resume clears drain and allows new worker claims plus steward/curator/retro dispatch; "
            "cards already in progress kept running during the pause"
        )
    return "resume clears the pause flag"


def load() -> dict:
    """{} (not paused) when the file is absent or unreadable. A corrupt file fails toward "not
    paused" rather than wedging every caller (precheck/tick/dispatch.run all call this on every
    single tick) — but that fail-open is exactly backwards from the pause flag's own purpose, so
    it's not silent: logged as a warn every time it's hit, same discipline as any other
    recurring-until-fixed condition here (e.g. precheck's own head-health probe failure)."""
    if not PAUSE_FILE.is_file():
        return {}
    try:
        return json.loads(PAUSE_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        STATE.log_run("pause-flag", result="corrupt", level="warn", error=str(e))
        return {}


def save(mode: str, stopped_worker: list[str] | None = None,
        stopped_reviewer: list[str] | None = None, reason: str = DEFAULT_REASON,
        actor: str = DEFAULT_ACTOR) -> None:
    STATE.ensure_dir()
    state = {
        "mode": mode,
        "since": datetime.now(timezone.utc).isoformat(),
        "reason": reason,
        "actor": actor,
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
    live_path = str(_resolve(PAUSE_FILE))
    shadow_files = shadow_pause_files()
    base = {
        "live_state_path": live_path,
        "other_pause_files": shadow_files,
        "warnings": [
            f"other pause.json exists outside the live pipeline state: {path}"
            for path in shadow_files
        ],
    }
    state = load()
    if not state:
        return {"paused": False, **base}
    stopped_worker = state.get("stopped_worker") or []
    stopped_reviewer = state.get("stopped_reviewer") or []
    mode = state.get("mode")
    return {
        "paused": True,
        "mode": display_mode(mode),
        "internal_mode": mode,
        "since": state.get("since"),
        "reason": state.get("reason") or "",
        "actor": state.get("actor") or "",
        "stopped_worker": stopped_worker,
        "stopped_reviewer": stopped_reviewer,
        "on_resume": _on_resume(mode, stopped_worker, stopped_reviewer),
        **base,
    }
