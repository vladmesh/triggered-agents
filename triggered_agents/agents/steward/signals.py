"""Deterministic anomaly signals — what the steward's precheck gate and `/steward` skill both
read before anything judges.

Five signal kinds (2026-07-04 design grill, memory id 83 — "стюард присмотр пайплайн дизайн"):
new Blocked card, warn/error/head-health-flip line in the pipeline's own runs.jsonl since the
steward's watermark, a card sitting in an active column past STALE_HOURS, a resource health
flip, a worker/reviewer workspace on disk with no in-flight card record. Any one is enough for
precheck to spawn the head; finding none costs nothing (a few Kanboard reads and a couple of
local file stats, no LLM).

Every signal dedupes against a persisted "already notified" watermark (state/steward/
watermark.json), the same shape as curator/retro's watermark but keyed by anomaly kind rather
than by source: a condition that hasn't changed since the last run (a card still sitting in
Blocked, a resource still red) does not re-spawn the head every hour — the steward already
looked, and re-litigating an unresolved anomaly on an unchanged state is exactly the kind of
hourly LLM-cost sweep this agent is not meant to be. `scan` is read-only; `advance` (cli.py, only
after the skill has actually looked at the batch) folds the scanned state into the watermark —
two-phase like curator/retro, so a crash between scan and advance re-scans instead of silently
dropping a signal.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from ...runtime.state import AgentState
from ..pipeline import health as pipeline_health
from ..pipeline import ops as pipeline_ops

STATE = AgentState("steward")

# Columns where a long dwell is itself worth a look. "Идеи" (backlog, not yet triaged into Ready)
# and "Done" (terminal) are excluded — sitting there indefinitely is the expected shape, not an
# anomaly.
STALE_COLUMNS = ("Ready", "In progress", "Validate", "Blocked")
STALE_HOURS = float(os.environ.get("TA_STEWARD_STALE_HOURS", "24"))

_PIPELINE_STATE = AgentState("pipeline")
PIPELINE_RUNS = _PIPELINE_STATE.dir / "runs.jsonl"
PIPELINE_CARDS = _PIPELINE_STATE.dir / "cards.json"

WORKSPACES_ROOT = Path(os.environ.get("TA_WORKSPACES_ROOT") or Path.home() / "orca" / "workspaces").resolve()
# The runtime's own agent worktrees (board/curator/pipeline/retro/steward, one per agent, not one
# per task) live under this reserved project name — never a workspace-orphan candidate.
_AGENTS_PROJECT = "triggered-agents"


def _empty_watermark() -> dict:
    return {
        "pipeline_log_lines": 0,
        "notified_blocked": [],
        "notified_stale": {},
        "notified_orphans": [],
        "resource_status": {},
    }


def load_watermark() -> dict:
    mark = _empty_watermark()
    mark.update(STATE.load_watermark())
    return mark


def _log_signals(mark: dict) -> tuple[list[dict], int]:
    """(new warn/error/head-health lines past the watermark's cursor, new total line count)."""
    if not PIPELINE_RUNS.is_file():
        return [], mark["pipeline_log_lines"]
    lines = PIPELINE_RUNS.read_text(encoding="utf-8").splitlines()
    start = min(mark["pipeline_log_lines"], len(lines))
    hits = []
    for line in lines[start:]:
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("level") == "warn" or rec.get("result") == "error" or rec.get("event") == "head-health":
            hits.append(rec)
    return hits, len(lines)


def _blocked_signals(mark: dict) -> tuple[list[str], list[str]]:
    """(new Blocked refs since the watermark, every ref currently Blocked)."""
    blocked = [c["reference"] for c in pipeline_ops.list_cards(column="Blocked")]
    seen = set(mark["notified_blocked"])
    new = [r for r in blocked if r not in seen]
    return new, blocked


def _stale_signals(mark: dict) -> tuple[list[dict], dict]:
    """Cards past STALE_HOURS in their current column, excluding ones already notified at their
    current date_moved — a card that moves again re-arms the check; one that just sits still,
    already flagged once, does not re-fire every hour. (new stale hits, {ref: date_moved} for
    every card currently in a watched column — the next watermark)."""
    now = time.time()
    threshold = STALE_HOURS * 3600
    notified = mark["notified_stale"]
    hits = []
    current = {}
    for column in STALE_COLUMNS:
        for card in pipeline_ops.list_cards(column=column):
            moved = card.get("date_moved")
            if not moved:
                continue
            ref = card["reference"]
            current[ref] = moved
            if now - moved < threshold:
                continue
            if notified.get(ref) == moved:
                continue
            hits.append({"reference": ref, "column": column, "since": moved})
    return hits, current


def _resource_signals(mark: dict) -> tuple[dict, dict]:
    """(resources whose status differs from the watermark — a red->green recovery counts the same
    as a fresh red, both are worth a post-mortem look — current status map for every resource). A
    resource with no prior entry (first-ever scan, or a resource heads.toml just introduced) is a
    new baseline, not a flip — otherwise the very first cold-start scan would "flip" every
    currently-green resource and spawn a head for nothing to report."""
    try:
        current = pipeline_health.refresh()
    except Exception:
        current = {}
    prev = mark["resource_status"]
    changed = {r: s for r, s in current.items() if r in prev and prev[r] != s}
    return changed, current


def _in_flight_workspaces() -> set[str]:
    """Every worker/reviewer workspace path the dispatcher's own bookkeeping still tracks
    (state/pipeline/cards.json — cleared by worker.teardown on Done/Blocked-with-cleanup, so a
    path lingering here but gone from disk, or vice versa, is exactly what this module hunts)."""
    if not PIPELINE_CARDS.is_file():
        return set()
    try:
        records = json.loads(PIPELINE_CARDS.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return set()
    paths = set()
    for rec in records.values():
        for key in ("workspace", "review_ws"):
            p = rec.get(key)
            if p:
                paths.add(str(Path(p).resolve()))
    return paths


def _orphan_signals(mark: dict) -> tuple[list[str], list[str]]:
    """(new orphan workspace paths, every orphan path found this scan) — a directory under
    WORKSPACES_ROOT/<project>/* the dispatcher no longer (or never did) associate with an
    in-flight card: a tick killed between workspace-create and the cards.json save, a teardown
    that failed partway, a manual leftover."""
    if not WORKSPACES_ROOT.is_dir():
        return [], []
    in_flight = _in_flight_workspaces()
    orphans = []
    for project_dir in sorted(WORKSPACES_ROOT.iterdir()):
        if not project_dir.is_dir() or project_dir.name == _AGENTS_PROJECT:
            continue
        for ws in sorted(project_dir.iterdir()):
            if ws.is_dir() and str(ws.resolve()) not in in_flight:
                orphans.append(str(ws))
    notified = set(mark["notified_orphans"])
    new = [o for o in orphans if o not in notified]
    return new, orphans


def scan() -> dict:
    """Everything precheck/the skill need: signals since the watermark, plus the raw state to
    fold into the watermark on advance(). Read-only — never touches the watermark file itself."""
    mark = load_watermark()
    log_hits, log_lines = _log_signals(mark)
    new_blocked, all_blocked = _blocked_signals(mark)
    stale_hits, stale_current = _stale_signals(mark)
    changed_resources, resource_current = _resource_signals(mark)
    new_orphans, all_orphans = _orphan_signals(mark)
    return {
        "signals": {
            "log": log_hits,
            "new_blocked": new_blocked,
            "stale": stale_hits,
            "resource_flip": changed_resources,
            "new_orphan_workspaces": new_orphans,
        },
        "pending": {
            "pipeline_log_lines": log_lines,
            "notified_blocked": all_blocked,
            "notified_stale": stale_current,
            "notified_orphans": all_orphans,
            "resource_status": resource_current,
        },
    }


def has_signal(batch: dict) -> bool:
    s = batch["signals"]
    return bool(s["log"] or s["new_blocked"] or s["stale"] or s["resource_flip"]
                or s["new_orphan_workspaces"])


def render_markdown(batch: dict) -> str:
    s = batch["signals"]
    if not has_signal(batch):
        return "steward: нет сигналов с прошлого watermark.\n"
    lines = ["# steward: сигналы аномалий", ""]
    if s["new_blocked"]:
        lines.append(f"## Новые Blocked ({len(s['new_blocked'])})")
        lines += [f"- {ref}" for ref in s["new_blocked"]]
        lines.append("")
    if s["log"]:
        lines.append(f"## runs.jsonl пайплайна ({len(s['log'])})")
        lines += [f"- {rec.get('ts', '?')} [{rec.get('event', '?')}] "
                  f"{json.dumps(rec, ensure_ascii=False)}" for rec in s["log"]]
        lines.append("")
    if s["stale"]:
        lines.append(f"## Застряло в колонке дольше {STALE_HOURS:g}ч ({len(s['stale'])})")
        for hit in s["stale"]:
            since = datetime.fromtimestamp(hit["since"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            lines.append(f"- {hit['reference']} в {hit['column']!r} с {since}")
        lines.append("")
    if s["resource_flip"]:
        lines.append(f"## Флип здоровья ресурса ({len(s['resource_flip'])})")
        lines += [f"- {r}: -> {status}" for r, status in s["resource_flip"].items()]
        lines.append("")
    if s["new_orphan_workspaces"]:
        lines.append(f"## Воркспейс без карточки в полёте ({len(s['new_orphan_workspaces'])})")
        lines += [f"- {p}" for p in s["new_orphan_workspaces"]]
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"
