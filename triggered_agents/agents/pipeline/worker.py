"""Worker-workspace side of the dispatcher — the Orca/host bits, kept apart from the board logic.

The dispatcher reasons over the board (ops.py) and delegates every side effect that touches a
worktree or a head to the functions here: read a project's base branch, create the Orca worktree,
run the provisioner (setup+smoke), drop the one-time TASK.md, launch the worker head, poll its
activity, tear the workspace down. Split out so the dispatcher's decisions stay unit-testable on a
fake board while these host calls are stubbed. No Kanboard here, no LLM.

Provisioning runs the SAME script `orca.yaml scripts.setup` points at
(`control-panel/pipeline/provision.py`), just invoked directly so we capture its exit code and log
and can move the card to Blocked before a head is spawned. Orca still owns worktree creation.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

from ...runtime import redact

ORCA = os.environ.get("ORCA_BIN") or shutil.which("orca") or str(Path.home() / ".local/bin/orca")
PROJECTS_DIR = Path(os.environ.get("TA_PROJECTS_DIR", str(Path.home() / "projects")))
CONTROL_PANEL = Path(os.environ.get("TA_CONTROL_PANEL", str(Path.home() / "control-panel")))
PROVISION = CONTROL_PANEL / "pipeline" / "provision.py"
ORCA_TIMEOUT_S = 120       # worktree create runs repo git; give it room, but never hang forever
PROVISION_TIMEOUT_S = int(os.environ.get("TA_PROVISION_TIMEOUT_S", "900"))


class WorkspaceError(RuntimeError):
    """A host-side workspace step failed (orca, git, provision transport)."""


# Board comments are the card's public journal, so provision logs / error texts get scrubbed
# before posting. runtime.redact catches known .env values and token shapes; on top of that:
# KEY=value assignments whose name smells like a secret, and long base64/hex-ish blobs
# (no `/`, so filesystem paths survive).
_ASSIGN_RE = re.compile(r"(?i)\b([A-Z0-9_]*(?:TOKEN|KEY|SECRET|PASSWORD|PASSWD)[A-Z0-9_]*)\s*=\s*\S+")
_BLOB_RE = re.compile(r"\b[A-Za-z0-9+=_-]{40,}\b")


def scrub_secrets(text: str) -> str:
    """Mask secret-looking material in `text` before it reaches a board comment."""
    if not text:
        return text
    text = redact.redact(text)
    text = _ASSIGN_RE.sub(rf"\1={redact.REDACTED}", text)
    return _BLOB_RE.sub(f"{redact.REDACTED}:blob", text)


def _orca_json(args: list[str]) -> dict:
    import json

    p = subprocess.run([ORCA, *args, "--json"], capture_output=True, text=True, timeout=ORCA_TIMEOUT_S)
    if p.returncode != 0:
        raise WorkspaceError(f"orca {' '.join(args)} failed: {(p.stderr or p.stdout).strip()}")
    data = json.loads(p.stdout)
    return data.get("result", data)


def project_root(project: str) -> Path:
    return PROJECTS_DIR / project


def read_base_branch(project: str) -> str:
    """base_branch from the project's committed workspace.toml, default 'main'."""
    manifest = project_root(project) / "workspace.toml"
    if not manifest.is_file():
        return "main"
    cfg = tomllib.loads(manifest.read_text(encoding="utf-8"))
    return cfg.get("workspace", {}).get("base_branch", "main")


def create_workspace(project: str, name: str, base_branch: str) -> str:
    """Create an Orca worktree off base_branch; return its path. Setup is skipped here — we run
    the provisioner ourselves next so we own its exit code and log."""
    data = _orca_json([
        "worktree", "create", "--repo", f"path:{project_root(project)}",
        "--name", name, "--base-branch", base_branch, "--setup", "skip", "--no-parent",
    ])
    wt = data.get("worktree", data)
    path = wt.get("path")
    if not path:
        raise WorkspaceError(f"orca worktree create returned no path for {name!r}")
    return path


def provision(workspace: str) -> tuple[bool, str]:
    """Run the workspace provisioner (setup+smoke). Return (ok, combined log)."""
    if not PROVISION.is_file():
        return False, f"provisioner missing: {PROVISION}"
    env = dict(os.environ, ORCA_WORKTREE_PATH=workspace)
    try:
        p = subprocess.run(
            ["python3", str(PROVISION), "--worktree", workspace],
            capture_output=True, text=True, env=env, timeout=PROVISION_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return False, f"provision timed out after {PROVISION_TIMEOUT_S}s"
    return p.returncode == 0, (p.stdout or "") + (p.stderr or "")


def write_task(workspace: str, content: str) -> str:
    """Write the one-time spec to <ws>/TASK.md and exclude it from git so it never gets committed."""
    path = Path(workspace) / "TASK.md"
    path.write_text(content, encoding="utf-8")
    _git_exclude(workspace, "TASK.md")
    return str(path)


def _git_exclude(workspace: str, name: str) -> None:
    r = subprocess.run(["git", "-C", workspace, "rev-parse", "--git-path", "info/exclude"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return
    rel = r.stdout.strip()
    exclude = Path(rel) if os.path.isabs(rel) else Path(workspace) / rel
    exclude.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude.read_text(encoding="utf-8").splitlines() if exclude.is_file() else []
    if name not in existing:
        with exclude.open("a", encoding="utf-8") as f:
            f.write(("" if not existing or existing[-1] == "" else "\n") + name + "\n")


def _launch_command(model: str | None, worker_id: str) -> str:
    model_flag = f" --model {model}" if model else ""
    prompt = (
        "Ты — воркер task-пайплайна. Твоя задача целиком в TASK.md в корне воркспейса — прочти "
        "его первым и следуй ему. Роль на доске — worker (BOARD_ROLE уже выставлен): карточку сам "
        "не двигаешь, только report/comment/feedback через board-CLI. TASK.md в репо не коммить."
    )
    return f'BOARD_ROLE=worker claude --dangerously-skip-permissions{model_flag} {prompt!r}'


def launch_worker(workspace: str, model: str | None, worker_id: str) -> str:
    """Spawn the worker head in the workspace; return the terminal handle."""
    data = _orca_json(["terminal", "create", "--worktree", f"path:{workspace}",
                       "--title", f"Claude worker {worker_id}",
                       "--command", _launch_command(model, worker_id)])
    term = data.get("terminal", data)
    return term.get("handle") or term.get("id") or ""


def activity(workspace: str) -> float | None:
    """Latest terminal output time in the workspace, epoch seconds, or None if unknown."""
    try:
        data = _orca_json(["terminal", "list", "--worktree", f"path:{workspace}", "--limit", "50"])
    except (WorkspaceError, subprocess.TimeoutExpired):
        return None
    last_ms = [t.get("lastOutputAt") for t in (data.get("terminals") or []) if t.get("lastOutputAt")]
    return max(last_ms) / 1000.0 if last_ms else None


def teardown(workspace: str) -> None:
    """Best-effort remove the Orca worktree. A dev-stage backend without USER leaves root-owned
    __pycache__ in the tree, so a plain remove can fail on permissions — fall back to sudo rm."""
    r = subprocess.run([ORCA, "worktree", "rm", "--worktree", f"path:{workspace}", "--force"],
                       capture_output=True, text=True)
    if r.returncode == 0 and not Path(workspace).exists():
        return
    if Path(workspace).exists():
        p = subprocess.run(["rm", "-rf", workspace], capture_output=True, text=True)
        if p.returncode != 0 and Path(workspace).exists():
            subprocess.run(["sudo", "rm", "-rf", workspace], capture_output=True, text=True)
