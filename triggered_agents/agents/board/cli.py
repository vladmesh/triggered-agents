"""board agent — deterministic helpers the /board skill drives via Bash.

PoC is one-way: project plans -> Kanboard, board read-only (no comments pulled back).
The agent gathers each project's heterogeneous plan by hand (Read/git), decides buckets and
card height, then drives these helpers:

  board setup                      idempotent: board project, columns, swimlane per project
  board projects                   plan-bearing projects to scan (from projects.md)
  board cards [--swimlane NAME]     current active cards, to reconcile against
  board upsert --swimlane .. --ref .. --column .. --title .. [--description ..]
  board archive --ref ..            close a card whose plan item is gone
  board advance                    record current project git shas (call at end of a sweep)

Reference is the stable per-item key (board-wide unique); match by it, don't spawn dupes.
Kanboard-touching commands emit JSON on stdout.

Change-detection (so the hourly timer doesn't reconcile unchanged plans):
  board precheck                   exit 0 if any plan project's git HEAD moved since the last
                                   `advance` (dispatch the sweep), non-zero if none did (skip).
The sweep ends with `advance`, which records the current shas as the new watermark. Single
phase: the sweep reconciles every project each run, so a change missed mid-run self-heals on
the next sweep. See changes.py for what's watched vs unwatched.
"""
from __future__ import annotations

import argparse
import json
import sys

from ...runtime.state import AgentState
from .kanboard import KanboardError

STATE = AgentState("board")


def _emit(obj) -> int:
    print(json.dumps(obj, ensure_ascii=False, indent=2))
    return 0


def cmd_precheck() -> int:
    """Exit 0 if a plan project's git moved since the last sweep, non-zero if none did."""
    from . import changes

    current, unwatched = changes.fingerprints()
    if unwatched:
        print(f"board: unwatched (no git gate): {', '.join(unwatched)}", file=sys.stderr)
    first = not STATE.watermark_file.is_file()
    mark = STATE.load_watermark()
    moved = sorted(p for p, sha in current.items() if mark.get(p) != sha)
    gone = sorted(p for p in mark if p not in current)
    if first or moved or gone:
        why = "first sweep" if first else "moved: " + ", ".join(moved + [f"-{g}" for g in gone])
        STATE.log_run("precheck", result="change")
        print(f"board: dispatch ({why})", file=sys.stderr)
        return 0
    STATE.log_run("precheck", result="no-change")
    print("board: no plan project moved since last sweep", file=sys.stderr)
    return 1


def cmd_advance() -> int:
    """Record current project shas as the watermark (the sweep calls this when it finishes)."""
    from . import changes

    current, _ = changes.fingerprints()
    with STATE.lock():
        STATE.save_watermark(current)
    STATE.log_run("advance")
    print(f"board: watermark recorded for {len(current)} project(s)")
    return 0


def main(argv=None) -> int:
    from . import board, registry

    if argv and argv[0] == "precheck":
        return cmd_precheck()
    if argv and argv[0] == "advance":
        return cmd_advance()

    parser = argparse.ArgumentParser(prog="triggered_agents board", add_help=True)
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("setup")
    p_proj = sub.add_parser("projects")
    p_proj.add_argument("--only", help="scope to a single project (for a targeted re-sync)")

    p_cards = sub.add_parser("cards")
    p_cards.add_argument("--swimlane")

    p_up = sub.add_parser("upsert")
    p_up.add_argument("--swimlane", required=True)
    p_up.add_argument("--ref", required=True)
    p_up.add_argument("--column", required=True)
    p_up.add_argument("--title", required=True)
    p_up.add_argument("--description", default="")

    p_arch = sub.add_parser("archive")
    p_arch.add_argument("--ref", required=True)

    args = parser.parse_args(argv)
    if not args.cmd:
        parser.print_help()
        return 0

    try:
        if args.cmd == "setup":
            return _emit(board.ensure_structure())
        if args.cmd == "projects":
            return _emit(registry.plan_projects(only=args.only))
        if args.cmd == "cards":
            return _emit(board.list_cards(swimlane=args.swimlane))
        if args.cmd == "upsert":
            return _emit(board.upsert_by_reference(
                swimlane=args.swimlane, reference=args.ref, column=args.column,
                title=args.title, description=args.description))
        if args.cmd == "archive":
            return _emit(board.archive_by_reference(reference=args.ref))
    except KanboardError as e:
        print(f"board: {e}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
