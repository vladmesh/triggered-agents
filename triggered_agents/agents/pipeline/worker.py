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

from ...runtime import claude_env, redact

ORCA = os.environ.get("ORCA_BIN") or shutil.which("orca") or str(Path.home() / ".local/bin/orca")
GH = os.environ.get("GH_BIN") or shutil.which("gh") or "gh"
PROJECTS_DIR = Path(os.environ.get("TA_PROJECTS_DIR", str(Path.home() / "projects")))
CLAUDE_JSON = Path(os.environ.get("TA_CLAUDE_JSON", str(Path.home() / ".claude.json")))
CONTROL_PANEL = Path(os.environ.get("TA_CONTROL_PANEL", str(Path.home() / "control-panel")))
PROVISION = CONTROL_PANEL / "pipeline" / "provision.py"
ORCA_TIMEOUT_S = 120       # worktree create runs repo git; give it room, but never hang forever
PROVISION_TIMEOUT_S = int(os.environ.get("TA_PROVISION_TIMEOUT_S", "900"))
GH_TIMEOUT_S = 60          # a gh call may hit the network; bounded so a tick never hangs on it
_GH_LOG_TAIL_LINES = 40


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
    """Корень репо проекта: ~/projects/<name>, иначе ~/<name> (там живут control-panel,
    triggered-agents и прочая инфраструктура секретаря)."""
    p = PROJECTS_DIR / project
    if p.is_dir():
        return p
    home = PROJECTS_DIR.parent / project
    return home if home.is_dir() else p


def read_base_branch(project: str) -> str:
    """base_branch from the project's committed workspace.toml, default 'main'."""
    manifest = project_root(project) / "workspace.toml"
    if not manifest.is_file():
        return "main"
    cfg = tomllib.loads(manifest.read_text(encoding="utf-8"))
    return cfg.get("workspace", {}).get("base_branch", "main")


def create_workspace(project: str, name: str, base_branch: str) -> str:
    """Create an Orca worktree off base_branch; return its path. Setup is skipped here — we run
    the provisioner ourselves next so we own its exit code and log.

    `--activate` reveals the new worktree in the Orca app. Without it a freshly created worktree
    has no window for the app to adopt a terminal into, so the head `launch_worker` starts next
    falls back to a background handle and the workspace shows empty in the GUI."""
    data = _orca_json([
        "worktree", "create", "--repo", f"path:{project_root(project)}",
        "--name", name, "--base-branch", base_branch, "--setup", "skip", "--no-parent",
        "--activate",
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


def ensure_trust(workspace: str) -> None:
    """Проставить folder trust Claude Code для свежего worktree. Без этого голова виснет на
    интерактивном вопросе «доверяешь ли папке» (та же логика — deploy/provision.py у агентов)."""
    try:
        claude_env.ensure_trust(CLAUDE_JSON, workspace)
    except claude_env.ClaudeConfigError as e:
        raise WorkspaceError(str(e)) from e


def ensure_theme() -> None:
    """Проставить тему в ~/.claude.json — иначе свежая голова виснет на первом онбординге
    («выбери стиль текста»), тот же класс бага, что и folder trust, но ключ глобальный, а не
    per-workspace."""
    try:
        claude_env.ensure_theme(CLAUDE_JSON)
    except claude_env.ClaudeConfigError as e:
        raise WorkspaceError(str(e)) from e


def launch_worker(workspace: str, model: str | None, worker_id: str) -> str:
    """Spawn the worker head in the workspace; return the terminal handle."""
    ensure_trust(workspace)
    ensure_theme()
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


def notify(handle: str, text: str) -> bool:
    """Nudge the live worker head: type `text` into its terminal (its Claude session) and Enter.
    Best-effort — a missing handle or an orca error is swallowed. The board comment is the durable
    record of a CI-red return; this is only a convenience so the worker sees it without a refresh."""
    if not handle:
        return False
    try:
        p = subprocess.run([ORCA, "terminal", "send", "--terminal", handle, "--text", text, "--enter"],
                           capture_output=True, text=True, timeout=ORCA_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return p.returncode == 0


# --- gh PR polling (Validate layer 1: mechanics, zero LLM) ------------------------------------
# The dispatcher polls each Validate card's PR here. Every gh touch returns None on any trouble
# (gh missing, non-zero exit, network/API error, unparsable output): None means "unknown, retry
# next tick", never a verdict — a transient gh outage must not move a card or ping a human.

_RUN_URL_RE = re.compile(r"github\.com/([\w.-]+/[\w.-]+)/actions/runs/(\d+)")
_FAIL_CONCLUSIONS = {"FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE"}


def _gh_json(args: list[str]):
    import json

    try:
        p = subprocess.run([GH, *args], capture_output=True, text=True, timeout=GH_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if p.returncode != 0:
        return None
    try:
        return json.loads(p.stdout)
    except (json.JSONDecodeError, ValueError):
        return None


def _check_result(item: dict) -> str:
    """One rollup entry -> 'pass' | 'fail' | 'pending'. Handles both a GitHub-Actions CheckRun
    (status/conclusion) and a legacy commit StatusContext (state)."""
    if item.get("__typename") == "StatusContext" or ("state" in item and "status" not in item):
        state = str(item.get("state", "")).upper()
        if state in ("FAILURE", "ERROR"):
            return "fail"
        if state == "SUCCESS":
            return "pass"
        return "pending"
    if str(item.get("status", "")).upper() != "COMPLETED":
        return "pending"
    return "fail" if str(item.get("conclusion", "")).upper() in _FAIL_CONCLUSIONS else "pass"


def _rollup(items: list[dict]) -> tuple[str, dict | None]:
    """Overall CI state from the rollup: 'FAILURE' (with the first failing entry), 'PENDING',
    'SUCCESS', or 'NONE' when the PR has no checks at all."""
    if not items:
        return "NONE", None
    first_fail = None
    pending = False
    for it in items:
        r = _check_result(it)
        if r == "fail" and first_fail is None:
            first_fail = it
        elif r == "pending":
            pending = True
    if first_fail is not None:
        return "FAILURE", first_fail
    return ("PENDING" if pending else "SUCCESS"), None


def _failed_log(item: dict, lines: int = _GH_LOG_TAIL_LINES) -> str | None:
    """Tail of the failed job's log via `gh run view --log-failed`, or None if not a Actions
    run (e.g. an external StatusContext) or gh cannot fetch it. Raw — the dispatcher scrubs."""
    m = _RUN_URL_RE.search(item.get("detailsUrl") or item.get("targetUrl") or "")
    if not m:
        return None
    repo, run_id = m.group(1), m.group(2)
    try:
        p = subprocess.run([GH, "run", "view", run_id, "-R", repo, "--log-failed"],
                           capture_output=True, text=True, timeout=GH_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if p.returncode != 0:
        return None
    tail = "\n".join((p.stdout or "").strip().splitlines()[-lines:])
    return tail or None


def poll_pr(pr_url: str) -> dict | None:
    """Poll a PR's merge state and CI rollup via gh. Returns
        {"merged": bool, "state": str, "rollup": "SUCCESS"|"FAILURE"|"PENDING"|"NONE",
         "failed_job": str|None, "failed_log": str|None}
    or None when gh cannot answer (missing, error, PR gone) — an unknown to retry, not a verdict.
    The failed job's log is fetched only on FAILURE, so a green/pending poll is a single gh call."""
    data = _gh_json(["pr", "view", pr_url, "--json", "state,mergedAt,statusCheckRollup"])
    if not isinstance(data, dict):
        return None
    rollup, failed = _rollup(data.get("statusCheckRollup") or [])
    out = {
        "merged": bool(data.get("mergedAt")),
        "state": data.get("state", ""),
        "rollup": rollup,
        "failed_job": None,
        "failed_log": None,
    }
    if failed is not None:
        out["failed_job"] = failed.get("name") or failed.get("context") or "?"
        out["failed_log"] = _failed_log(failed)
    return out


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
