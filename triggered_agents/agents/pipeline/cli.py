"""pipeline agent — the task-pipeline board CLI (PO / dispatcher / worker share one binary).

Role is a global `--role` (or env BOARD_ROLE) checked before the command runs: create is
PO-only, claim is dispatcher-only, report/feedback are worker-only, move/ready defer to the
transition matrix for the role, comment is open to any role (the role becomes the marker).
setup/list/show need no role. Guards live in model/ops; this layer only wires argv to them
and maps failures to exit codes.

Exit codes: 0 ok, 1 KanboardError, 2 usage/bad-args/role, 3 GuardError. Kanboard-touching
commands emit JSON on stdout; errors go to stderr prefixed `pipeline: `.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from .model import GuardError, ROLES


def _emit(obj) -> int:
    print(json.dumps(obj, ensure_ascii=False, indent=2))
    return 0


def _err(msg: str) -> None:
    print(f"pipeline: {msg}", file=sys.stderr)


def _text_arg(inline: str | None, path: str | None) -> str:
    """Resolve a --body / --description pair: file wins, `-` means stdin, else the inline value."""
    if path is not None:
        if path == "-":
            return sys.stdin.read()
        with open(path, encoding="utf-8") as f:
            return f.read()
    return inline or ""


def _need_role(role: str | None, allowed: tuple[str, ...]) -> bool:
    """True if `role` is permitted; else print a stderr reason. Caller returns 2 on False."""
    if role is None:
        _err(f"this command needs --role (one of {', '.join(allowed)}) or env BOARD_ROLE")
        return False
    if role not in allowed:
        _err(f"role {role!r} may not run this command (needs one of {', '.join(allowed)})")
        return False
    return True


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="triggered_agents pipeline", add_help=True)
    parser.add_argument("--role", choices=ROLES, help="acting role (or env BOARD_ROLE)")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("setup")

    p_create = sub.add_parser("create")
    p_create.add_argument("--project", required=True)
    p_create.add_argument("--type", required=True, dest="task_type")
    p_create.add_argument("--title", required=True)
    p_create.add_argument("--ref")
    p_create.add_argument("--column", default="Идеи")
    p_create.add_argument("--blocked-by", dest="blocked_by")
    p_create.add_argument("--model", dest="model_name")
    p_create.add_argument("--description")
    p_create.add_argument("--description-file")

    p_ready = sub.add_parser("ready")
    p_ready.add_argument("--ref", required=True)

    p_move = sub.add_parser("move")
    p_move.add_argument("--ref", required=True)
    p_move.add_argument("--to", required=True, dest="to_column")

    p_claim = sub.add_parser("claim")
    p_claim.add_argument("--ref", required=True)
    p_claim.add_argument("--worker", required=True)
    p_claim.add_argument("--cap", type=int, default=3)

    p_report = sub.add_parser("report")
    p_report.add_argument("--ref", required=True)
    p_report.add_argument("--kind", required=True, choices=("done", "blocked"))
    p_report.add_argument("--body")
    p_report.add_argument("--body-file")

    p_comment = sub.add_parser("comment")
    p_comment.add_argument("--ref", required=True)
    p_comment.add_argument("--body")
    p_comment.add_argument("--body-file")

    p_feedback = sub.add_parser("feedback")
    p_feedback.add_argument("--ref", required=True)
    p_feedback.add_argument("--body")
    p_feedback.add_argument("--body-file")

    p_list = sub.add_parser("list")
    p_list.add_argument("--column")
    p_list.add_argument("--project")

    p_show = sub.add_parser("show")
    p_show.add_argument("--ref", required=True)

    return parser


def main(argv=None) -> int:
    from ..board.kanboard import KanboardError
    from . import ops

    parser = _build_parser()
    args = parser.parse_args(argv)
    if not args.cmd:
        parser.print_help()
        return 0
    role = args.role or os.environ.get("BOARD_ROLE") or None
    if role is not None and role not in ROLES:
        _err(f"unknown role {role!r} (roles: {', '.join(ROLES)})")
        return 2

    try:
        if args.cmd == "setup":
            return _emit(ops.ensure_structure())
        if args.cmd == "list":
            return _emit(ops.list_cards(column=args.column, project=args.project))
        if args.cmd == "show":
            return _emit(ops.show_card(args.ref))

        if args.cmd == "create":
            if not _need_role(role, ("po",)):
                return 2
            desc = _text_arg(args.description, args.description_file)
            return _emit(ops.create_card(
                project=args.project, task_type=args.task_type, title=args.title,
                description=desc, ref=args.ref, column=args.column,
                blocked_by=args.blocked_by, model_name=args.model_name))
        if args.cmd == "ready":
            if not _need_role(role, ROLES):
                return 2
            return _emit(ops.move_card(role, args.ref, "Ready"))
        if args.cmd == "move":
            if not _need_role(role, ROLES):
                return 2
            return _emit(ops.move_card(role, args.ref, args.to_column))
        if args.cmd == "claim":
            if not _need_role(role, ("dispatcher",)):
                return 2
            return _emit(ops.claim_card(args.ref, args.worker, cap=args.cap))
        if args.cmd == "report":
            if not _need_role(role, ("worker",)):
                return 2
            return _emit(ops.report(args.ref, args.kind, _text_arg(args.body, args.body_file)))
        if args.cmd == "feedback":
            if not _need_role(role, ("worker",)):
                return 2
            return _emit(ops.feedback(args.ref, _text_arg(args.body, args.body_file)))
        if args.cmd == "comment":
            if not _need_role(role, ROLES):
                return 2
            return _emit(ops.add_comment(role, args.ref, _text_arg(args.body, args.body_file)))
    except GuardError as e:
        _err(str(e))
        return 3
    except KanboardError as e:
        _err(str(e))
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
