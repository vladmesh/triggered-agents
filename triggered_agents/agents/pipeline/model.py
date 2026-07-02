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

ROLES = ("po", "dispatcher", "worker", "reviewer")

TASK_TYPES = ("code", "research", "debug")

# Metadata keys (flat str->str dict on the Kanboard card):
#   task_type  one of TASK_TYPES
#   project    source project name (also the swimlane)
#   blocked_by reference of a predecessor card that must be Done before claim
#   model      recommended model for the worker
#   claim      worker/workspace id, set by the claim command
#   slug       short [a-z0-9-]{1,30} tag naming the card's worker/reviewer workspace; a card
#              without one (old/manual) falls back to a transliterated slug of its title
META_TASK_TYPE = "task_type"
META_PROJECT = "project"
META_BLOCKED_BY = "blocked_by"
META_MODEL = "model"
META_CLAIM = "claim"
META_SLUG = "slug"

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
    # The layer-3 reviewer never moves cards (the dispatcher acts on its verdict, like it does on
    # a worker report). Its only artifacts are the verdict comment and Идеи cards.
    "reviewer": set(),
}

# Comment markers, single source of truth for ops (and, later, the dispatcher reading back):
#   [report:done] / [report:blocked]  worker report
#   [feedback]                        worker's feedback on the spec/process, harvested by retro
#   [review:green] / [review:red]     the layer-3 reviewer's verdict
#   [<role>]                          plain comment
MARKER_REPORT_DONE = "report:done"
MARKER_REPORT_BLOCKED = "report:blocked"
MARKER_FEEDBACK = "feedback"
# Dispatcher verdicts on a Validate card's CI (layer 1). ci-green marks "layer 1 passed, running
# the review" — posted once per code state (the dispatcher checks the comments after the card's
# current baseline before posting, so a rework that re-enters Validate gets a fresh note).
MARKER_VALIDATE_GREEN = "validate:ci-green"
MARKER_VALIDATE_RED = "validate:ci-red"
# Dispatcher verdicts on the stand run (Validate layer 2: deploy the PR branch to the project's
# persistent stand and run e2e). Only for projects with a [stand] manifest section. stand-green
# marks "layer 2 passed", posted once per code state; layer 3 (the reviewer) runs after it.
# stand-red is posted per failed run; two consecutive stand failures send the card to Blocked
# (one auto-retry).
MARKER_STAND_GREEN = "validate:stand-green"
MARKER_STAND_RED = "validate:stand-red"
# The layer-3 reviewer's verdict (Validate layer 3: an independent LLM head, not the worker, reads
# the whole repo + PR and posts one verdict). green = all layers clear, the card waits for a human
# merge. red = at least one blocker in any lens; the dispatcher returns the card to In progress
# with a nudge, up to a cap of returns (then Blocked до vladmesh).
MARKER_REVIEW_GREEN = "review:green"
MARKER_REVIEW_RED = "review:red"
# Posted once when a stand-project card's green review triggers the dispatcher's own squash merge
# (all three gates — CI, stand-green, review:green — cleared, per vladmesh's 2026-07-02 decision
# that a live-stand e2e run is enough assurance to drop the human from the merge for those projects).
MARKER_AUTOMERGE = "validate:automerge"
# The dispatcher's own note when a red verdict sends a card back for rework. Deliberately NOT a
# review:* marker: the invariant "only the reviewer posts a verdict" must not hinge on baseline
# arithmetic — if this carried [review:red] and any baseline shift re-read it, the card would loop.
MARKER_REVIEW_RETURN = "validate:review-return"
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
