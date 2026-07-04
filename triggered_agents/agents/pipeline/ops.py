"""Pipeline Kanboard operations — the deterministic board side of the task pipeline.

One Kanboard project (model.BOARD_NAME) is the board, model.COLUMNS its columns, one
swimlane per source project. Cards are keyed by a board-wide unique `reference`
(`<project>-<task_id>` when the PO does not supply one). Card state that the dispatcher
reasons over (type, project, predecessor, model, claim owner) lives in task metadata, a
flat str->str dict, so a reader needs only getTaskByReference + getTaskMetadata.

Guards live here, not in prompts. `move_card` defers to model.check_move; `claim_card` is
the only way into "In progress" and runs the entry guards (Ready, unclaimed, predecessor
Done, one code task per project, global cap) under a host-local lock — Kanboard has no
compare-and-swap, so a single dispatcher plus the lock is what serializes claims.

Metadata is fetched per task (N+1) in the list/guards; the board is small, so this stays
cheap and keeps each helper self-contained.
"""
from __future__ import annotations

import os

from ...runtime.state import AgentState
from ..board.kanboard import KanboardError, call
from . import heads, model, naming, worker

STATE = AgentState("pipeline")


def board_id() -> int:
    """Kanboard project id of the board, creating it if absent.

    model.BOARD_NAME is read lazily (it comes from env at import) and no id is cached across
    names, so the e2e can flip TA_PIPELINE_BOARD to a throwaway board within one process.
    """
    name = model.BOARD_NAME
    for p in call("getAllProjects") or []:
        if p["name"] == name:
            return int(p["id"])
    return int(call("createProject", name=name))


def _ensure_admin_member(pid: int) -> str | None:
    """Add the admin user as project manager so the board shows on their Kanboard dashboard.

    A project created via the API has no members; Kanboard's dashboard ("My projects" / "My
    tasks") only lists projects the logged-in user belongs to, so without this the board looks
    empty in the UI even though it exists. Idempotent; skipped if KANBOARD_ADMIN_USER is unset.
    Copied from the legacy board agent on purpose — that showcase will be removed and this must
    not depend on it.
    """
    admin = os.environ.get("KANBOARD_ADMIN_USER")
    if not admin:
        return None
    members = call("getProjectUsers", project_id=pid) or {}
    if admin in members.values():
        return None
    user = call("getUserByName", username=admin)
    if not user:
        return None
    call("addProjectUser", project_id=pid, user_id=int(user["id"]), role="project-manager")
    return admin


def _column_id(pid: int, title: str) -> int:
    for c in call("getColumns", project_id=pid) or []:
        if c["title"] == title:
            return int(c["id"])
    raise KanboardError(f"no column {title!r} on board (run `pipeline setup`)")


def _column_title(pid: int, column_id: int) -> str:
    for c in call("getColumns", project_id=pid) or []:
        if int(c["id"]) == int(column_id):
            return c["title"]
    return ""


def _ensure_swimlane(pid: int, name: str) -> int:
    """Return the active swimlane id for `name`, creating it if absent."""
    for s in call("getActiveSwimlanes", project_id=pid) or []:
        if s["name"] == name:
            return int(s["id"])
    return int(call("addSwimlane", project_id=pid, name=name))


def ensure_structure() -> dict:
    """Idempotently bring the board's columns to exactly model.COLUMNS; add the admin member.

    Same reconcile as the legacy board: rename in place, append missing, drop extras beyond
    len(COLUMNS). Swimlanes are left alone here — a card gets its project swimlane created on
    demand at create time, and the default swimlane stays.
    """
    pid = board_id()
    current = sorted(call("getColumns", project_id=pid) or [], key=lambda c: c["position"])
    for i, title in enumerate(model.COLUMNS):
        if i < len(current):
            if current[i]["title"] != title:
                call("updateColumn", column_id=int(current[i]["id"]), title=title)
        else:
            call("addColumn", project_id=pid, title=title)
    for extra in current[len(model.COLUMNS):]:
        call("removeColumn", column_id=int(extra["id"]))
    admin_added = _ensure_admin_member(pid)
    return {"board_id": pid, "columns": model.COLUMNS, "admin_member_added": admin_added}


def _check_head(head: str) -> None:
    """Raise GuardError (not HeadRegistryError — everything a role sees from ops is a GuardError)
    unless `head` names a real profile in heads.toml. Shared by create/update (reject a bad head
    before anything is written) and claim (a card's stored head may have gone stale since it was
    set, e.g. the profile was later removed from the registry)."""
    try:
        heads.load_registry().profile(head)
    except heads.HeadRegistryError as e:
        raise model.GuardError(str(e)) from e


def _get_by_ref(reference: str) -> dict:
    """Task dict for `reference` on the board, or GuardError if there is no such card."""
    pid = board_id()
    t = call("getTaskByReference", project_id=pid, reference=reference)
    if not t:
        raise model.GuardError(f"no card {reference!r}")
    return t


def _is_done(task: dict, pid: int) -> bool:
    """A card counts as done when it sits in the Done column or has been closed."""
    if _column_title(pid, int(task["column_id"])) == "Done":
        return True
    active = task.get("is_active", task.get("status"))
    return active is not None and int(active) == 0


def create_card(project: str, task_type: str, title: str, description: str = "",
                ref: str | None = None, column: str = "Идеи",
                blocked_by: str | None = None, head: str | None = None,
                slug: str | None = None, role: str | None = None) -> dict:
    """PO/steward: create a spec card in Идеи or Ready, keyed by reference, with metadata.

    `slug` names the card's future worker/reviewer workspace (`<reference>-<slug>`); when
    omitted, claim falls back to a transliterated slug of the title (naming.fallback_slug) so an
    old/manual card without one still claims fine. `head`, when given, must name a profile in
    heads.toml (checked before anything is written); omitted, the card gets heads.DEFAULT_PROFILE
    at bring-up.

    `role="steward"` scrubs title/description the same way add_comment does for steward — the
    escalation/idea path SKILL.md sends steward through (create in Идеи/Ready, then move to
    Blocked) is exactly where a quoted transcript/journalctl/env line could carry a raw secret
    (2026-07-04 review, triggered-agents-244 blocker B1 third round). Every other caller
    (po, reviewer_idea — which scrubs itself before calling here) passes no role and stays
    verbatim, unchanged from before."""
    if task_type not in model.TASK_TYPES:
        raise model.GuardError(f"unknown task_type {task_type!r} (types: {', '.join(model.TASK_TYPES)})")
    if column not in ("Идеи", "Ready"):
        raise model.GuardError(f"cards are created only in 'Идеи' or 'Ready', not {column!r}")
    if slug is not None and not naming.SLUG_RE.match(slug):
        raise model.GuardError(f"slug {slug!r} must match [a-z0-9-]{{1,30}}")
    if head:
        _check_head(head)
    if role == "steward":
        title = worker.scrub_secrets(title)
        description = worker.scrub_secrets(description)
    pid = board_id()
    col_id = _column_id(pid, column)
    sw_id = _ensure_swimlane(pid, project)
    task_id = int(call("createTask", title=title, project_id=pid, column_id=col_id,
                       swimlane_id=sw_id, description=description,
                       **({"reference": ref} if ref else {})))
    if ref is None:
        ref = f"{project}-{task_id}"
        call("updateTask", id=task_id, reference=ref)
    values = {model.META_TASK_TYPE: task_type, model.META_PROJECT: project}
    if blocked_by:
        values[model.META_BLOCKED_BY] = blocked_by
    if head:
        values[model.META_HEAD] = head
    if slug:
        values[model.META_SLUG] = slug
    call("saveTaskMetadata", task_id=task_id, values=values)
    return {"action": "created", "id": task_id, "reference": ref, "column": column}


def update_card(role: str, reference: str, slug: str | None = None,
                head: str | None = None, blocked_by: str | None = None) -> dict:
    """PO-only: patch slug/head/blocked_by metadata on an existing card. Only the fields
    passed (not None) change; column and claim are never touched. Same validation as
    create_card (slug SLUG_RE, head against heads.toml, blocked_by pointing at an existing
    card), all checked before anything is written so a rejected update leaves metadata
    untouched."""
    if role != "po":
        raise model.GuardError(f"role {role!r} may not update card metadata (po only)")
    if slug is not None and not naming.SLUG_RE.match(slug):
        raise model.GuardError(f"slug {slug!r} must match [a-z0-9-]{{1,30}}")
    if head:
        _check_head(head)
    pid = board_id()
    task = _get_by_ref(reference)
    if blocked_by is not None and not call("getTaskByReference", project_id=pid, reference=blocked_by):
        raise model.GuardError(f"blocked_by {blocked_by!r} does not exist")
    values = {}
    if slug is not None:
        values[model.META_SLUG] = slug
    if head is not None:
        values[model.META_HEAD] = head
    if blocked_by is not None:
        values[model.META_BLOCKED_BY] = blocked_by
    if values:
        call("saveTaskMetadata", task_id=int(task["id"]), values=values)
    meta = call("getTaskMetadata", task_id=int(task["id"])) or {}
    return {
        "action": "updated",
        "reference": reference,
        "slug": meta.get(model.META_SLUG, ""),
        "head": meta.get(model.META_HEAD, ""),
        "blocked_by": meta.get(model.META_BLOCKED_BY, ""),
    }


def _move_position(pid: int, task_id: int, column_id: int, swimlane_id: int) -> None:
    """moveTaskPosition, raising if Kanboard reports failure. It returns false instead of an
    RPC error, so a bare call() would pass silently (e.g. claim stamped, card still Ready)."""
    ok = call("moveTaskPosition", project_id=pid, task_id=task_id,
              column_id=column_id, position=1, swimlane_id=swimlane_id)
    if not ok:
        raise KanboardError(f"moveTaskPosition failed for task {task_id} -> column {column_id}")


def move_card(role: str, reference: str, to_column: str, reason: str = "") -> dict:
    """Move a card per the role/transition matrix (never into In progress; that is claim).

    The claim persists across In progress<->Validate rework (the worker session still owns
    the card) and resets on arrival in Ready — from Blocked (a human's manual recovery) or from
    In progress (the dispatcher's own watchdog auto-retry requeue, model TRANSITIONS["dispatcher"])
    — to the unclaimed/fresh-retry-budget defaults (empty string; every guard that reads these
    checks truthiness, so empty means "unset"). A human recovering a Blocked card this way gets a
    full watchdog retry budget again, same as a brand new card; a watchdog requeue's own caller
    (dispatcher._watchdog_retry) restates the real counters (and, on a head switch, the new head)
    right after via set_retry_state, so this reset is never the last write for that path.

    `reason` only matters for model.STEWARD_OVERRIDE (Blocked->Done): it must be non-empty (the
    justification for skipping review) and is posted as a [steward:blocked-done] comment in this
    same call, right after the move succeeds — so a card never ends up in Done with the override
    used and no comment explaining why, and a rejected (empty-reason) call moves nothing.
    """
    pid = board_id()
    task = _get_by_ref(reference)
    cur = _column_title(pid, int(task["column_id"]))
    model.check_move(role, cur, to_column)
    if (cur, to_column) == model.STEWARD_OVERRIDE and not reason.strip():
        raise model.GuardError(
            "Blocked -> Done requires a non-empty justification comment (--reason), "
            "supplied in the same call"
        )
    if to_column == "Ready":
        call("saveTaskMetadata", task_id=int(task["id"]), values={
            model.META_CLAIM: "",
            model.META_RETRY_SAME: "",
            model.META_RETRY_SWITCH: "",
            model.META_RETRY_HEADS: "",
        })
    _move_position(pid, int(task["id"]), _column_id(pid, to_column), int(task["swimlane_id"]))
    if (cur, to_column) == model.STEWARD_OVERRIDE:
        add_comment(role, reference, reason, marker=model.MARKER_STEWARD_OVERRIDE)
    return {"action": "moved", "reference": reference, "from": cur, "to": to_column}


def get_metadata(reference: str) -> dict:
    """Raw metadata dict for `reference` — used by the watchdog retry path (dispatcher.
    _watchdog_retry) to read retry_same/retry_switch/retry_heads without show_card's extra
    getAllComments fetch."""
    task = _get_by_ref(reference)
    return call("getTaskMetadata", task_id=int(task["id"])) or {}


def set_retry_state(reference: str, *, retry_same: int, retry_switch: int, retry_heads: str,
                    head: str | None = None) -> dict:
    """Dispatcher-only: stamp the watchdog retry counters (model.META_RETRY_*) on a card, and its
    head too when a switch just picked a new one. Always called right after move_card(..., 'Ready')
    during a watchdog retry — that move already reset these fields to defaults; this restates the
    real values on top, in the same tick, so the reset is never the last write."""
    task = _get_by_ref(reference)
    values = {
        model.META_RETRY_SAME: str(retry_same),
        model.META_RETRY_SWITCH: str(retry_switch),
        model.META_RETRY_HEADS: retry_heads,
    }
    if head is not None:
        values[model.META_HEAD] = head
    call("saveTaskMetadata", task_id=int(task["id"]), values=values)
    return {"action": "retry-state", "reference": reference, **values}


def claim_card(reference: str, worker: str, cap: int = 3) -> dict:
    """Dispatcher-only entry into In progress: guard, stamp claim, then move.

    Guards (each its own message): the card is Ready; it is unclaimed; its head, if set, names a
    real profile in heads.toml (a stale reference — the profile was renamed/removed after the
    card was created — must GuardError here, not blow up bring-up mid-tick); its blocked_by
    predecessor, if any, is Done; if it is a code card, no other active code card for the
    same project sits in In progress or Validate (a Validate card still owns its worker
    session for rework, so it counts); and fewer than `cap` cards sit in In progress or
    Validate (both hold a live worker session).

    The whole thing runs under AgentState("pipeline").lock(): Kanboard offers no
    compare-and-swap, so with a single dispatcher the host-local lock is what closes the
    read-check-write race between two overlapping claims.
    """
    pid = board_id()
    with STATE.lock():
        task = _get_by_ref(reference)
        tid = int(task["id"])
        cur = _column_title(pid, int(task["column_id"]))
        if cur != "Ready":
            raise model.GuardError(f"claim needs card in 'Ready', {reference!r} is in {cur!r}")
        meta = call("getTaskMetadata", task_id=tid) or {}
        if meta.get(model.META_CLAIM):
            raise model.GuardError(f"{reference!r} already claimed by {meta[model.META_CLAIM]!r}")

        head = meta.get(model.META_HEAD)
        if head:
            _check_head(head)

        blocked_by = meta.get(model.META_BLOCKED_BY)
        if blocked_by:
            pred = call("getTaskByReference", project_id=pid, reference=blocked_by)
            if not pred:
                raise model.GuardError(f"blocked_by {blocked_by!r} of {reference!r} does not exist")
            if not _is_done(pred, pid):
                raise model.GuardError(f"blocked_by {blocked_by!r} of {reference!r} is not Done yet")

        task_type = meta.get(model.META_TASK_TYPE)
        project = meta.get(model.META_PROJECT)
        actives = call("getAllTasks", project_id=pid, status_id=1) or []
        if task_type == "code":
            for t in actives:
                if int(t["id"]) == tid:
                    continue
                col = _column_title(pid, int(t["column_id"]))
                if col not in (model.IN_PROGRESS, "Validate"):
                    continue
                tmeta = call("getTaskMetadata", task_id=int(t["id"])) or {}
                if tmeta.get(model.META_PROJECT) == project and tmeta.get(model.META_TASK_TYPE) == "code":
                    raise model.GuardError(
                        f"one code task per project: {t.get('reference') or t['id']!r} "
                        f"({project}) is already in {col!r}"
                    )

        # Validate counts toward the cap too: a card there still owns its worker session.
        wip = sum(1 for t in actives
                  if _column_title(pid, int(t["column_id"])) in (model.IN_PROGRESS, "Validate"))
        if wip >= cap:
            raise model.GuardError(f"cap reached: {wip} card(s) in In progress/Validate (cap {cap})")

        call("saveTaskMetadata", task_id=tid, values={model.META_CLAIM: worker})
        _move_position(pid, tid, _column_id(pid, model.IN_PROGRESS), int(task["swimlane_id"]))
    STATE.log_run("claim", reference=reference, worker=worker)
    return {"action": "claimed", "reference": reference, "worker": worker}


def add_comment(role: str, reference: str, body: str, marker: str | None = None) -> dict:
    """Post a comment as `[marker or role]\\n<body>`; user_id=0 (app-token author). Scrubbed for
    steward specifically (worker.scrub_secrets), same as the reviewer's verdict/reviewer_idea:
    steward reads more raw system surface than any other role (transcripts, journalctl, env
    files) and could quote a secret by accident (2026-07-04 review, triggered-agents-244 remark
    Z1). Every other role keeps its body verbatim, unchanged from before."""
    task = _get_by_ref(reference)
    tag = marker or role
    text = worker.scrub_secrets(body) if role == "steward" else body
    call("createComment", task_id=int(task["id"]), user_id=0, content=f"[{tag}]\n{text}")
    return {"action": "commented", "reference": reference, "marker": tag}


def report(reference: str, kind: str, body: str = "") -> dict:
    """Worker-only: post a `[report:done]`/`[report:blocked]` comment. Blocked needs a body."""
    if kind not in ("done", "blocked"):
        raise model.GuardError(f"report kind must be 'done' or 'blocked', not {kind!r}")
    if kind == "blocked" and not body.strip():
        raise model.GuardError("a blocked report requires a non-empty body (why it is blocked)")
    marker = model.MARKER_REPORT_DONE if kind == "done" else model.MARKER_REPORT_BLOCKED
    out = add_comment("worker", reference, body, marker=marker)
    out["action"] = "reported"
    out["kind"] = kind
    return out


def verdict(reference: str, kind: str, body: str = "") -> dict:
    """Reviewer-only: post the layer-3 verdict as `[review:green]`/`[review:red]`. A red verdict
    needs a body (the blocker findings) — the dispatcher returns the card on red, so an empty red
    would send a card back with nothing to fix. The body is scrubbed: the reviewer hunts for secret
    leaks and may quote one, so its own comment must not become the leak."""
    if kind not in ("green", "red"):
        raise model.GuardError(f"verdict kind must be 'green' or 'red', not {kind!r}")
    if kind == "red" and not body.strip():
        raise model.GuardError("a red verdict requires a non-empty body (the blocker findings)")
    marker = model.MARKER_REVIEW_GREEN if kind == "green" else model.MARKER_REVIEW_RED
    out = add_comment("reviewer", reference, worker.scrub_secrets(body), marker=marker)
    out["action"] = "verdict"
    out["kind"] = kind
    return out


def reviewer_idea(project: str, title: str, description: str = "", task_type: str = "code",
                  ref: str | None = None, head: str | None = None,
                  slug: str | None = None) -> dict:
    """Reviewer-only: file an out-of-scope finding as an Идеи card (the reviewer's single
    code-creation exception). Title and description are scrubbed for the same reason as a verdict."""
    return create_card(project=project, task_type=task_type,
                       title=worker.scrub_secrets(title),
                       description=worker.scrub_secrets(description),
                       ref=ref, column="Идеи", head=head, slug=slug)


def retro_idea(project: str, title: str, description: str = "", task_type: str = "code",
               ref: str | None = None, head: str | None = None,
               slug: str | None = None) -> dict:
    """Retro-only: file a fail-pattern proposal as an Идеи card — retro's only board write,
    same shape as reviewer_idea (never Ready, title/description scrubbed). Retro quotes redacted
    transcript excerpts; the harvest step already strips secrets, but this scrubs again for the
    same defense-in-depth reason add_comment does for steward."""
    return create_card(project=project, task_type=task_type,
                       title=worker.scrub_secrets(title),
                       description=worker.scrub_secrets(description),
                       ref=ref, column="Идеи", head=head, slug=slug)


def feedback(reference: str, body: str) -> dict:
    """Worker-only: post a `[feedback]` comment on the spec/process; requires a non-empty body."""
    if not body.strip():
        raise model.GuardError("feedback requires a non-empty body")
    out = add_comment("worker", reference, body, marker=model.MARKER_FEEDBACK)
    out["action"] = "feedback"
    return out


def _card_view(pid: int, task: dict, cols: dict, lanes: dict) -> dict:
    meta = call("getTaskMetadata", task_id=int(task["id"])) or {}
    try:
        date_moved = int(task["date_moved"]) or None
    except (KeyError, TypeError, ValueError):
        date_moved = None
    return {
        "id": int(task["id"]),
        "reference": task.get("reference") or "",
        "title": task["title"],
        "column": cols.get(int(task["column_id"]), ""),
        "swimlane": lanes.get(int(task["swimlane_id"]), ""),
        "position": int(task.get("position", 0) or 0),
        # Kanboard's own "last column move" unix timestamp — steward's staleness signal reads
        # this to age a card in its current column without keeping a parallel cursor of its own.
        "date_moved": date_moved,
        "task_type": meta.get(model.META_TASK_TYPE, ""),
        "project": meta.get(model.META_PROJECT, ""),
        "blocked_by": meta.get(model.META_BLOCKED_BY, ""),
        "head": meta.get(model.META_HEAD, ""),
        "claim": meta.get(model.META_CLAIM, ""),
        "slug": meta.get(model.META_SLUG, ""),
    }


def list_cards(column: str | None = None, project: str | None = None) -> list[dict]:
    """Active cards with their metadata fields, optionally filtered by column/project."""
    pid = board_id()
    cols = {int(c["id"]): c["title"] for c in call("getColumns", project_id=pid) or []}
    lanes = {int(s["id"]): s["name"] for s in call("getActiveSwimlanes", project_id=pid) or []}
    out = []
    for t in call("getAllTasks", project_id=pid, status_id=1) or []:  # N+1 metadata: board is small
        view = _card_view(pid, t, cols, lanes)
        if column and view["column"] != column:
            continue
        if project and view["project"] != project:
            continue
        out.append(view)
    return out


def show_card(reference: str) -> dict:
    """Full card view: fields, metadata dict, and comments as [{ts, text}]."""
    pid = board_id()
    task = _get_by_ref(reference)
    meta = call("getTaskMetadata", task_id=int(task["id"])) or {}
    comments = [
        {"ts": c.get("date_creation", ""), "text": c.get("comment", "")}
        for c in call("getAllComments", task_id=int(task["id"])) or []
    ]
    return {
        "id": int(task["id"]),
        "reference": task.get("reference") or "",
        "title": task["title"],
        "description": task.get("description", ""),
        "column": _column_title(pid, int(task["column_id"])),
        "metadata": meta,
        "comments": comments,
    }
