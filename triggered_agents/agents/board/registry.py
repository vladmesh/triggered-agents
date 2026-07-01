"""Read the project list from control-panel's projects.md.

The board plugin needs only the *names* of projects that have a plan — one Kanboard
swimlane per project. Parsing plan *contents* is deliberately left to the agent (plans are
heterogeneous: markdown, sprint dirs, Trello, non-git roots); doing it in Python would
reintroduce the rigid format the board was designed to avoid. So this stays narrow: the
first column of the "Планировочный источник" table, dropping rows whose plan cell says the
project has none ("нет ...").

Path is `$CONTROL_PANEL_PROJECTS` or ~/control-panel/docs/projects.md.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

_DEFAULT = Path.home() / "control-panel" / "docs" / "projects.md"
# Section whose table maps project -> where its plan lives.
_SECTION = "Планировочный источник"
_CELL_NAME = re.compile(r"`([^`]+)`")


def _projects_md() -> Path:
    return Path(os.environ.get("CONTROL_PANEL_PROJECTS", str(_DEFAULT)))


def plan_projects(only: str | None = None) -> list[dict]:
    """Projects that have a plan source, as [{"name", "plan"}].

    Rows whose plan cell starts with "нет" (no plan / frozen README) are skipped — nothing
    for the board to pull, so no swimlane. Pass `only` to scope to a single project (empty
    list if it has no plan / doesn't exist).
    """
    path = _projects_md()
    text = path.read_text(encoding="utf-8")
    out, in_section = [], False
    for line in text.splitlines():
        if line.startswith("## "):
            in_section = _SECTION in line
            continue
        if not in_section or not line.lstrip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        m = _CELL_NAME.search(cells[0])
        if not m:  # header / separator / prose row
            continue
        plan = cells[1]
        if plan.lower().startswith("нет"):
            continue
        name = m.group(1)
        if only and name != only:
            continue
        out.append({"name": name, "plan": plan})
    return out
