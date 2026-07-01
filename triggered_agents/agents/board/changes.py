"""Change-detection for the board precheck: did any project's plan-bearing git move?

The board sweep reconciles every project's plan into Kanboard. Running it hourly when nothing
moved just burns a headless session, so the run is gated on this: exit 0 (dispatch) when any
watched project's git HEAD differs from the last recorded sweep, exit non-zero (skip) when all
match.

Signal is the git HEAD sha of each plan project's repo (`~/projects/<name>`, with the few
out-of-tree roots overridden). Projects without a git root — `project_inspect` (non-git root),
`inspect_notebooks` (Trello) — can't be gated cheaply and are reported as `unwatched`: they
don't trigger a sweep on their own, but every sweep reconciles them anyway, so a change there
surfaces on the next sweep some watched project triggers. `orca`'s plan files are git-excluded,
so its HEAD only moves on code commits — over-triggering (harmless), not under.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from . import registry

_PROJECTS_ROOT = Path.home() / "projects"
# projects.md lists these plan sources outside ~/projects.
_ROOT_OVERRIDE = {"control-panel": Path.home() / "control-panel"}


def _repo_root(name: str) -> Path:
    return _ROOT_OVERRIDE.get(name, _PROJECTS_ROOT / name)


def _head_sha(root: Path) -> str | None:
    if not (root / ".git").exists():  # dir (repo) or file (worktree); absent -> not git
        return None
    p = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True
    )
    return p.stdout.strip() if p.returncode == 0 else None


def fingerprints() -> tuple[dict[str, str], list[str]]:
    """Return ({project: head_sha} for git-watched plan projects, [unwatched project names])."""
    watched: dict[str, str] = {}
    unwatched: list[str] = []
    for proj in registry.plan_projects():
        name = proj["name"]
        sha = _head_sha(_repo_root(name))
        if sha:
            watched[name] = sha
        else:
            unwatched.append(name)
    return watched, unwatched
