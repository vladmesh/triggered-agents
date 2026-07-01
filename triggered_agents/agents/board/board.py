"""Board operations — the single Kanboard board reconciled from project plans.

Layout: one Kanboard project (BOARD_NAME) is the whole board; COLUMNS are its columns; one
swimlane per source project. The agent drives the reconcile via cli.py; these are the
deterministic pieces (structure, name->id resolution, reference-keyed upsert/archive).

`reference` is board-wide unique — the agent assigns a stable key per plan item (e.g.
`dnd:sprint-007`) and matches by it, so no mapping table is needed. One-way for the PoC:
plans -> board, board read-only (no comments pulled back).
"""
from __future__ import annotations

import os

from . import registry
from .kanboard import KanboardError, call

BOARD_NAME = "Секретарь"
COLUMNS = ["Идеи", "To Do", "Готово"]

_pid_cache: int | None = None


def board_id() -> int:
    """Kanboard project id of the board, creating it if absent."""
    global _pid_cache
    if _pid_cache is not None:
        return _pid_cache
    for p in call("getAllProjects") or []:
        if p["name"] == BOARD_NAME:
            _pid_cache = int(p["id"])
            return _pid_cache
    _pid_cache = int(call("createProject", name=BOARD_NAME))
    return _pid_cache


def _column_id(pid: int, title: str) -> int:
    for c in call("getColumns", project_id=pid) or []:
        if c["title"] == title:
            return int(c["id"])
    raise KanboardError(f"no column {title!r} on board (run `board setup`)")


def _swimlane_id(pid: int, name: str) -> int:
    for s in call("getActiveSwimlanes", project_id=pid) or []:
        if s["name"] == name:
            return int(s["id"])
    raise KanboardError(f"no swimlane {name!r} on board (run `board setup`)")


def _ensure_admin_member(pid: int) -> str | None:
    """Add the admin user as project manager so the board shows on their Kanboard dashboard.

    A project created via the API has no members; Kanboard's dashboard ("My projects" / "My
    tasks") only lists projects the logged-in user belongs to, so without this the board looks
    empty in the UI even though it exists. Idempotent; skipped if KANBOARD_ADMIN_USER is unset.
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


def ensure_structure() -> dict:
    """Idempotently bring columns to COLUMNS and a swimlane to exist per plan project."""
    pid = board_id()
    current = sorted(call("getColumns", project_id=pid) or [], key=lambda c: c["position"])
    for i, title in enumerate(COLUMNS):
        if i < len(current):
            if current[i]["title"] != title:
                call("updateColumn", column_id=int(current[i]["id"]), title=title)
        else:
            call("addColumn", project_id=pid, title=title)
    for extra in current[len(COLUMNS):]:  # only fires on a fresh board (defaults are empty)
        call("removeColumn", column_id=int(extra["id"]))

    have = {s["name"] for s in call("getActiveSwimlanes", project_id=pid) or []}
    projects = [p["name"] for p in registry.plan_projects()]
    added = []
    for name in projects:
        if name not in have:
            call("addSwimlane", project_id=pid, name=name)
            added.append(name)
    # Board is a projection: keep only project swimlanes active. Disable strays — notably
    # Kanboard's built-in "Default swimlane", which otherwise sits empty taking board width.
    keep = set(projects)
    disabled = []
    for s in call("getActiveSwimlanes", project_id=pid) or []:
        if s["name"] not in keep:
            call("disableSwimlane", project_id=pid, swimlane_id=int(s["id"]))
            disabled.append(s["name"])

    admin_added = _ensure_admin_member(pid)
    return {"board_id": pid, "columns": COLUMNS, "swimlanes": projects, "swimlanes_added": added,
            "swimlanes_disabled": disabled, "admin_member_added": admin_added}


def list_cards(swimlane: str | None = None) -> list[dict]:
    """Active cards on the board as [{reference,title,column,swimlane,id}] (agent reconciles against these)."""
    pid = board_id()
    cols = {int(c["id"]): c["title"] for c in call("getColumns", project_id=pid) or []}
    lanes = {int(s["id"]): s["name"] for s in call("getActiveSwimlanes", project_id=pid) or []}
    out = []
    for t in call("getAllTasks", project_id=pid, status_id=1) or []:
        lane = lanes.get(int(t["swimlane_id"]), "")
        if swimlane and lane != swimlane:
            continue
        out.append({
            "id": int(t["id"]),
            "reference": t.get("reference") or "",
            "title": t["title"],
            "column": cols.get(int(t["column_id"]), ""),
            "swimlane": lane,
        })
    return out


def upsert_by_reference(swimlane: str, reference: str, column: str, title: str, description: str = "") -> dict:
    """Create the card, or update+move an existing one matched by reference."""
    pid = board_id()
    col_id = _column_id(pid, column)
    sw_id = _swimlane_id(pid, swimlane)
    existing = call("getTaskByReference", project_id=pid, reference=reference)
    if existing:
        tid = int(existing["id"])
        call("updateTask", id=tid, title=title, description=description)
        if int(existing["column_id"]) != col_id or int(existing["swimlane_id"]) != sw_id:
            call("moveTaskPosition", project_id=pid, task_id=tid,
                 column_id=col_id, position=1, swimlane_id=sw_id)
        return {"action": "updated", "id": tid, "reference": reference}
    tid = int(call("createTask", title=title, project_id=pid, reference=reference,
                   column_id=col_id, swimlane_id=sw_id, description=description))
    return {"action": "created", "id": tid, "reference": reference}


def archive_by_reference(reference: str) -> dict:
    """Close (archive) the card matched by reference; no-op if it's already gone."""
    pid = board_id()
    existing = call("getTaskByReference", project_id=pid, reference=reference)
    if not existing:
        return {"action": "absent", "reference": reference}
    call("closeTask", task_id=int(existing["id"]))
    return {"action": "archived", "id": int(existing["id"]), "reference": reference}
