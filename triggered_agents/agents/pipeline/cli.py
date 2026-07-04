"""pipeline agent — the task-pipeline board CLI (PO / dispatcher / worker / steward share one
binary).

Role is a global `--role` (or env BOARD_ROLE) checked before the command runs: create is
PO-, steward-, or worker-only (a worker's own create is further gated in ops — straight to Ready
only as a continuation of its own chain, see ops._check_worker_continuation, otherwise Идеи like
any other agent idea), claim is dispatcher-only, report/feedback are worker-only, move/ready defer
to the transition matrix for the role, comment is open to any role (the role becomes the marker).
update accepts any role at this layer but is PO-only in ops (GuardError otherwise), same as
move's per-role matrix. steward gets every po transition (via move/ready) plus one more: Blocked
-> Done, which additionally needs `move --reason` (a non-empty justification, posted as a comment
in the same call) — see model.STEWARD_OVERRIDE and ops.move_card. idea is reviewer- or retro-only
(both file an Идеи-only card, never move anything — model.TRANSITIONS leaves each an empty set).
setup/list/show/probe need no role. Guards live in model/ops; this layer only wires argv to them
and maps failures to exit codes.

`probe --resource <id>` exits 0/1 for green/red (see health.run_builtin_probe), not the generic
KanboardError/GuardError table below — it is heads.toml's own probe command, run by
health.refresh, never touching the board at all.

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
    sub.add_parser("tick")       # dispatcher: one deterministic tick (claim/advance)
    sub.add_parser("precheck")   # dispatcher: exit 0 if there is work, non-zero to skip

    p_probe = sub.add_parser("probe")   # heads.toml's own probe command for a resource
    p_probe.add_argument("--resource", required=True)

    p_create = sub.add_parser("create")
    p_create.add_argument("--project", required=True)
    p_create.add_argument("--type", required=True, dest="task_type")
    p_create.add_argument("--title", required=True)
    p_create.add_argument("--ref")
    p_create.add_argument("--column", default="Идеи")
    p_create.add_argument("--blocked-by", dest="blocked_by")
    p_create.add_argument("--head", dest="head")
    p_create.add_argument("--slug")
    p_create.add_argument("--base-branch", dest="base_branch")
    p_create.add_argument("--description")
    p_create.add_argument("--description-file")
    p_create.add_argument("--own-ref", dest="own_ref")   # worker-only: its own card reference

    p_update = sub.add_parser("update")
    p_update.add_argument("--ref", required=True)
    p_update.add_argument("--blocked-by", dest="blocked_by")
    p_update.add_argument("--head", dest="head")
    p_update.add_argument("--slug")
    p_update.add_argument("--base-branch", dest="base_branch")

    p_ready = sub.add_parser("ready")
    p_ready.add_argument("--ref", required=True)

    p_move = sub.add_parser("move")
    p_move.add_argument("--ref", required=True)
    p_move.add_argument("--to", required=True, dest="to_column")
    p_move.add_argument("--reason")           # steward's Blocked->Done justification
    p_move.add_argument("--reason-file")

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

    p_verdict = sub.add_parser("verdict")     # reviewer: the layer-3 green/red verdict
    p_verdict.add_argument("--ref", required=True)
    p_verdict.add_argument("--kind", required=True, choices=("green", "red"))
    p_verdict.add_argument("--body")
    p_verdict.add_argument("--body-file")

    p_idea = sub.add_parser("idea")           # reviewer: file a finding as an Идеи card
    p_idea.add_argument("--project", required=True)
    p_idea.add_argument("--title", required=True)
    p_idea.add_argument("--type", default="code", dest="task_type")
    p_idea.add_argument("--ref")
    p_idea.add_argument("--head", dest="head")
    p_idea.add_argument("--slug")
    p_idea.add_argument("--description")
    p_idea.add_argument("--description-file")

    p_list = sub.add_parser("list")
    p_list.add_argument("--column")
    p_list.add_argument("--project")

    p_show = sub.add_parser("show")
    p_show.add_argument("--ref", required=True)

    return parser


def main(argv=None) -> int:
    from ...runtime.kanboard import KanboardError
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
        if args.cmd in ("tick", "precheck"):
            from . import dispatcher
            return dispatcher.tick() if args.cmd == "tick" else dispatcher.precheck()
        if args.cmd == "probe":
            from . import health
            try:
                ok = health.run_builtin_probe(args.resource)
            except KeyError:
                _err(f"no builtin probe for resource {args.resource!r} "
                    f"(known: {', '.join(sorted(health.BUILTIN_PROBES))})")
                return 2
            return 0 if ok else 1
        if args.cmd == "list":
            return _emit(ops.list_cards(column=args.column, project=args.project))
        if args.cmd == "show":
            return _emit(ops.show_card(args.ref))

        if args.cmd == "create":
            if not _need_role(role, ("po", "steward", "worker")):
                return 2
            desc = _text_arg(args.description, args.description_file)
            return _emit(ops.create_card(
                project=args.project, task_type=args.task_type, title=args.title,
                description=desc, ref=args.ref, column=args.column,
                blocked_by=args.blocked_by, head=args.head, slug=args.slug,
                base_branch=args.base_branch, role=role, own_ref=args.own_ref))
        if args.cmd == "update":
            if not _need_role(role, ROLES):
                return 2
            return _emit(ops.update_card(
                role, args.ref, slug=args.slug,
                head=args.head, blocked_by=args.blocked_by,
                base_branch=args.base_branch))
        if args.cmd == "ready":
            if not _need_role(role, ROLES):
                return 2
            return _emit(ops.move_card(role, args.ref, "Ready"))
        if args.cmd == "move":
            if not _need_role(role, ROLES):
                return 2
            reason = _text_arg(args.reason, args.reason_file)
            return _emit(ops.move_card(role, args.ref, args.to_column, reason=reason))
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
        if args.cmd == "verdict":
            if not _need_role(role, ("reviewer",)):
                return 2
            return _emit(ops.verdict(args.ref, args.kind, _text_arg(args.body, args.body_file)))
        if args.cmd == "idea":
            if not _need_role(role, ("reviewer", "retro")):
                return 2
            # The reviewer's one code-creation exception: findings out of the card's scope go to
            # Идеи (never Ready) so they enter the queue only via a human, not the reviewer.
            # retro's only board write is the same shape: a fail-pattern proposal, Идеи-only.
            desc = _text_arg(args.description, args.description_file)
            fn = ops.reviewer_idea if role == "reviewer" else ops.retro_idea
            return _emit(fn(
                project=args.project, task_type=args.task_type, title=args.title,
                description=desc, ref=args.ref, head=args.head, slug=args.slug))
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
