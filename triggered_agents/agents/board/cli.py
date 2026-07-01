"""board agent — deterministic helpers the /board skill drives via Bash.

PoC is one-way: project plans -> Kanboard, board read-only (no comments pulled back).
The agent gathers each project's heterogeneous plan by hand (Read/git), decides buckets and
card height, then drives these helpers:

  board setup                      idempotent: board project, columns, swimlane per project
  board projects                   plan-bearing projects to scan (from projects.md)
  board cards [--swimlane NAME]     current active cards, to reconcile against
  board upsert --swimlane .. --ref .. --column .. --title .. [--description ..]
  board archive --ref ..            close a card whose plan item is gone

Reference is the stable per-item key (board-wide unique); match by it, don't spawn dupes.
All output is JSON on stdout. No watermark/precheck yet — that lands when it goes on a timer.
"""
from __future__ import annotations

import argparse
import json
import sys

from .kanboard import KanboardError


def _emit(obj) -> int:
    print(json.dumps(obj, ensure_ascii=False, indent=2))
    return 0


def main(argv=None) -> int:
    from . import board, registry

    parser = argparse.ArgumentParser(prog="triggered_agents board", add_help=True)
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("setup")
    sub.add_parser("projects")

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
            return _emit(registry.plan_projects())
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
