"""Pipeline domain model — pure, no I/O.

The pipeline board is the operational task queue: the PO (secretary) writes specs into
cards, a deterministic dispatcher claims Ready cards and spawns workers, workers report
via comments. Column transitions are guarded here, in code, keyed by role — not by trusting
a prompt. A worker can never move a card; the dispatcher can never skip a guard.

The one path into "In progress" is the `claim` command, not a `move`. Claim is separate
because entering work is not a plain transition: it also picks a worker, checks the
predecessor is Done, enforces one code task per project, and honours a global cap. Folding
all that into the move matrix would hide it. So Ready->"In progress" is deliberately absent
from TRANSITIONS, and `move` to In progress errors with a pointer at `claim`.
"""
from __future__ import annotations

import os

# Env override exists so the e2e can drive a throwaway board without touching the real one.
BOARD_NAME = os.environ.get("TA_PIPELINE_BOARD", "Pipeline")

COLUMNS = ["Идеи", "Ready", "In progress", "Validate", "Blocked", "Done"]

ROLES = ("po", "dispatcher", "worker")

TASK_TYPES = ("code", "research", "debug")

# Metadata keys (flat str->str dict on the Kanboard card):
#   task_type  one of TASK_TYPES
#   project    source project name (also the swimlane)
#   blocked_by reference of a predecessor card that must be Done before claim
#   model      recommended model for the worker
#   claim      worker/workspace id, set by the claim command
META_TASK_TYPE = "task_type"
META_PROJECT = "project"
META_BLOCKED_BY = "blocked_by"
META_MODEL = "model"
META_CLAIM = "claim"

IN_PROGRESS = "In progress"

# Allowed (from, to) column moves per role, for the generic `move` command. Claim owns the
# only entry into In progress and is not listed here (see module docstring).
TRANSITIONS: dict[str, set[tuple[str, str]]] = {
    "po": {("Идеи", "Ready"), ("Blocked", "Ready")},
    "dispatcher": {
        ("In progress", "Validate"),
        ("In progress", "Blocked"),
        ("Validate", "In progress"),
        ("Validate", "Blocked"),
        ("Validate", "Done"),
    },
    "worker": set(),
}

# Comment markers, single source of truth for ops (and, later, the dispatcher reading back):
#   [report:done] / [report:blocked]  worker report
#   [feedback]                        worker's feedback on the spec/process, harvested by retro
#   [<role>]                          plain comment
MARKER_REPORT_DONE = "report:done"
MARKER_REPORT_BLOCKED = "report:blocked"
MARKER_FEEDBACK = "feedback"
# Dispatcher verdicts on a Validate card's CI (layer 1). ci-green is posted at most once per card
# (the dispatcher checks for it before posting) so repeated green ticks don't spam the journal.
MARKER_VALIDATE_GREEN = "validate:ci-green"
MARKER_VALIDATE_RED = "validate:ci-red"
# Dispatcher verdicts on the stand run (Validate layer 2: deploy the PR branch to the project's
# persistent stand and run e2e). Only for projects with a [stand] manifest section. stand-green is
# posted once and is the pre-merge verdict for such projects (it replaces ci-green as the
# "waiting for merge" signal — green comes only after a green stand run). stand-red is posted per
# failed run; two consecutive stand failures send the card to Blocked (one auto-retry).
MARKER_STAND_GREEN = "validate:stand-green"
MARKER_STAND_RED = "validate:stand-red"
# Posted once when validating a single card blows up unexpectedly (e.g. a base workspace.toml that
# won't parse). The failure is localized to that card — the tick keeps going for the others.
MARKER_VALIDATE_ERROR = "validate:error"


class GuardError(RuntimeError):
    """A role, transition, or claim guard was violated."""


def check_move(role: str, from_col: str, to_col: str) -> None:
    """Raise GuardError unless `role` may move a card from `from_col` to `to_col`."""
    if role not in ROLES:
        raise GuardError(f"unknown role {role!r} (roles: {', '.join(ROLES)})")
    for col in (from_col, to_col):
        if col not in COLUMNS:
            raise GuardError(f"unknown column {col!r} (columns: {', '.join(COLUMNS)})")
    if (from_col, to_col) in TRANSITIONS[role]:
        return
    # Validate->In progress is a legit rework transition (in the matrix); any OTHER move into
    # In progress is the fresh-claim case, which must go through `claim`, not `move`.
    if to_col == IN_PROGRESS:
        raise GuardError(
            "move into 'In progress' is not allowed; use `claim --ref R --worker ID` "
            "(dispatcher only): it also sets the claim and runs the entry guards"
        )
    raise GuardError(f"role {role!r} may not move {from_col!r} -> {to_col!r}")
