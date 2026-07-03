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
from ...runtime.state import AgentState
from . import stand

ORCA = os.environ.get("ORCA_BIN") or shutil.which("orca") or str(Path.home() / ".local/bin/orca")
GH = os.environ.get("GH_BIN") or shutil.which("gh") or "gh"
PROJECTS_DIR = Path(os.environ.get("TA_PROJECTS_DIR", str(Path.home() / "projects")))
CLAUDE_JSON = Path(os.environ.get("TA_CLAUDE_JSON", str(Path.home() / ".claude.json")))
CONTROL_PANEL = Path(os.environ.get("TA_CONTROL_PANEL", str(Path.home() / "control-panel")))
PROVISION = CONTROL_PANEL / "pipeline" / "provision.py"
# Every workspace teardown lives under here — the guard in teardown() refuses anything outside it,
# and the same STATE (state/pipeline/) is where dispatcher.py logs, so a sudo-fallback trip shows
# up in the one runs.jsonl the pipeline already watches.
WORKSPACES_ROOT = Path(os.environ.get("TA_WORKSPACES_ROOT") or Path.home() / "orca" / "workspaces").resolve()
STATE = AgentState("pipeline")
ORCA_TIMEOUT_S = 120       # worktree create runs repo git; give it room, but never hang forever
PROVISION_TIMEOUT_S = int(os.environ.get("TA_PROVISION_TIMEOUT_S", "900"))
GH_TIMEOUT_S = 60          # a gh call may hit the network; bounded so a tick never hangs on it
_GH_LOG_TAIL_LINES = 40
# Model for the layer-3 reviewer head. A plain config knob (not a per-card choice): the reviewer is
# independent of the worker, so the card's `model` field is not reused here. Empty -> claude default.
REVIEWER_MODEL = os.environ.get("TA_REVIEWER_MODEL", "opus")


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

    try:
        p = subprocess.run([ORCA, *args, "--json"], capture_output=True, text=True, timeout=ORCA_TIMEOUT_S)
    except subprocess.TimeoutExpired as e:
        # A hung orca must surface as a WorkspaceError (the module's spawn-failure contract), not a
        # raw TimeoutExpired that escapes create_workspace/launch_worker/spawn_reviewer and lands in
        # the caller's generic error path with a misleading message.
        raise WorkspaceError(f"orca {' '.join(args)} timed out after {ORCA_TIMEOUT_S}s") from e
    if p.returncode != 0:
        raise WorkspaceError(f"orca {' '.join(args)} failed: {(p.stderr or p.stdout).strip()}")
    data = json.loads(p.stdout)
    return data.get("result", data)


def _git(cwd: str | Path, args: list[str], timeout: float = ORCA_TIMEOUT_S) -> subprocess.CompletedProcess:
    """Run `git -C cwd <args>`, bounded the same way `_orca_json` bounds the orca CLI — a hung git
    (a wedged fsmonitor, a stale lock) must never hang a dispatcher tick forever."""
    try:
        return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                              timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise WorkspaceError(f"git -C {cwd} {' '.join(args)} timed out after {timeout}s") from e


def _git_ok(cwd: str | Path, args: list[str], timeout: float = ORCA_TIMEOUT_S) -> str:
    """`_git`, raising WorkspaceError on a non-zero exit — the git-hygiene steps below (fetch,
    branch rename, landing a PR head) have no meaningful degraded path, so a failure here must
    abort bring-up the same way an orca failure does."""
    p = _git(cwd, args, timeout)
    if p.returncode != 0:
        raise WorkspaceError(f"git -C {cwd} {' '.join(args)} failed: {(p.stderr or p.stdout).strip()}")
    return p.stdout


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
    """Fetch base_branch fresh from origin, then create an Orca worktree off `origin/base_branch`
    (never the project's own local checkout state — a stale/switched local branch must not leak
    into a fresh worker/reviewer worktree). Returns the worktree path. Setup is skipped here — we
    run the provisioner ourselves next so we own its exit code and log.

    `--activate` reveals the new worktree in the Orca app. Without it a freshly created worktree
    has no window for the app to adopt a terminal into, so the head `launch_worker` starts next
    falls back to a background handle and the workspace shows empty in the GUI."""
    root = project_root(project)
    _git_ok(root, ["fetch", "origin", base_branch])
    data = _orca_json([
        "worktree", "create", "--repo", f"path:{root}",
        "--name", name, "--base-branch", f"origin/{base_branch}", "--setup", "skip", "--no-parent",
        "--activate",
    ])
    wt = data.get("worktree", data)
    path = wt.get("path")
    if not path:
        raise WorkspaceError(f"orca worktree create returned no path for {name!r}")
    return path


def _worktree_holding(repo_workspace: str, branch: str) -> str | None:
    """Path of another worktree of the same repo currently checked out on `branch`, via `git
    worktree list --porcelain`, or None if `branch` is free (or held only by `repo_workspace`
    itself). A stale ref from an earlier bring-up on the same card/PR is otherwise indistinguishable
    from a live one — this is how `_claim_branch` tells them apart."""
    out = _git_ok(repo_workspace, ["worktree", "list", "--porcelain"])
    me = str(Path(repo_workspace).resolve())
    path = None
    for line in out.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree "):]
        elif line == f"branch refs/heads/{branch}" and path and str(Path(path).resolve()) != me:
            return path
    return None


def _claim_branch(workspace: str, branch: str) -> None:
    """Make `workspace` the sole holder of `branch` (git branch -M), freeing it first from
    wherever an earlier bring-up on the same card/PR left it: a Blocked worker's worktree left
    alive for a human, or a reviewer branch that outlived its own worktree's teardown (worktree
    removal drops the checkout, not the ref). Origin is the pipeline's sync point — every actor's
    Done/verdict is already there before this ever runs — so reclaiming a stale local worktree
    loses nothing the pipeline promises to keep. The stale worktree goes through the normal
    teardown path (stop terminals, orca rm, rm -rf/sudo fallback), not a raw `git worktree
    remove`, so orca's own bookkeeping never points at a directory that's quietly gone; `worktree
    prune` after clears the now-stale administrative entry so the rename below never trips over
    it. A plain `-m` fails outright the moment `branch` already exists at all (even unheld) — `-M`
    is what makes the second and later bring-up on the same ref/PR idempotent."""
    other = _worktree_holding(workspace, branch)
    if other:
        teardown(other)
        _git_ok(workspace, ["worktree", "prune"])
    _git_ok(workspace, ["branch", "-M", branch])


def set_branch(workspace: str, branch: str) -> None:
    """Land the worktree on `branch` (see `_claim_branch`) before any head starts — every actor
    gets its own named ref (`pipeline/<ref>`, `review/<ref>`), never whatever name `orca worktree
    create` happened to pick, so no head ever has to create or rename its own branch."""
    _claim_branch(workspace, branch)


def land_pr_head(workspace: str, pr_branch: str, review_branch: str) -> None:
    """Fetch `pr_branch` (the worker's own branch, i.e. the PR's head — same-repo, never a fork)
    from origin into the reviewer's worktree and land it under `review_branch`. This is the
    fetch-not-checkout the reviewer uses instead of `gh pr checkout`: that command tries to check
    out the PR's own branch name locally, which collides ('already used by worktree') with the
    worker's live worktree still sitting on that exact branch."""
    _git_ok(workspace, ["fetch", "origin", pr_branch])
    _git_ok(workspace, ["reset", "--hard", "FETCH_HEAD"])
    _claim_branch(workspace, review_branch)


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


def _write_excluded(workspace: str, name: str, content: str) -> str:
    """Write <ws>/<name> and exclude it from git so a one-time head artifact never gets committed."""
    path = Path(workspace) / name
    path.write_text(content, encoding="utf-8")
    _git_exclude(workspace, name)
    return str(path)


def write_task(workspace: str, content: str) -> str:
    """Write the one-time spec to <ws>/TASK.md, excluded from git."""
    return _write_excluded(workspace, "TASK.md", content)


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


def workspace_exists(project: str, name: str) -> bool:
    """Whether `<WORKSPACES_ROOT>/<project>/<name>` already exists on disk — the collision check
    for a workspace name (naming.dedupe): a re-claim while the previous attempt's worktree is
    still alive (e.g. left on Blocked) must not collide with it."""
    return (WORKSPACES_ROOT / project / name).is_dir()


def rename_terminal(handle: str, title: str) -> bool:
    """Best-effort: (re)apply a human-readable title to `handle`'s tab. Claude Code's own
    session dynamically rewrites its tab title once it starts working (spinner + an inferred
    blurb of the current step — confirmed live, see PR description), so a rename at spawn alone
    does not stick; callers reapply this every tick to pin the pipeline's title back."""
    if not handle:
        return False
    try:
        p = subprocess.run([ORCA, "terminal", "rename", "--terminal", handle, "--title", title],
                           capture_output=True, text=True, timeout=ORCA_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return p.returncode == 0


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


def launch_worker(workspace: str, model: str | None, worker_id: str, title: str) -> str:
    """Spawn the worker head in the workspace; return the terminal handle. `title` seeds the
    tab's display name; the caller (dispatcher) pins it back every tick via rename_terminal since
    Claude Code overwrites it once the head starts working."""
    ensure_trust(workspace)
    ensure_theme()
    data = _orca_json(["terminal", "create", "--worktree", f"path:{workspace}",
                       "--title", title,
                       "--command", _launch_command(model, worker_id)])
    term = data.get("terminal", data)
    return term.get("handle") or term.get("id") or ""


def _reviewer_command(worker_id: str) -> str:
    model_flag = f" --model {REVIEWER_MODEL}" if REVIEWER_MODEL else ""
    prompt = (
        "Ты — независимая голова-ревьюер task-пайплайна (слой 3 валидации). Твоя работа целиком в "
        "REVIEW.md в корне воркспейса — прочти его первым и следуй ему. Роль на доске — reviewer "
        "(BOARD_ROLE уже выставлен): прав на код нет, не коммить и не пушь; артефакты — один "
        "вердикт-коммент и, при необходимости, карточки-идеи через board-CLI."
    )
    return f'BOARD_ROLE=reviewer claude --dangerously-skip-permissions{model_flag} {prompt!r}'


def spawn_reviewer(project: str, worker_id: str, base_branch: str, review_md: str, title: str,
                   pr_branch: str, review_branch: str) -> tuple[str, str]:
    """Bring up the layer-3 reviewer head: a fresh worktree off base_branch, landed on the PR's
    head content under its own `review_branch` (land_pr_head — fetch, not `gh pr checkout`, so the
    reviewer never touches the worker's own branch), the REVIEW.md prompt, then the head. No
    provisioning — the reviewer only reads code and drives gh/board-CLI, so it needs no app deps.
    Returns (workspace, terminal handle). `title` seeds the tab's display name (see launch_worker)."""
    ws = create_workspace(project, worker_id, base_branch)
    try:
        land_pr_head(ws, pr_branch, review_branch)
        _write_excluded(ws, "REVIEW.md", review_md)
        ensure_trust(ws)
        ensure_theme()
        data = _orca_json(["terminal", "create", "--worktree", f"path:{ws}",
                           "--title", title,
                           "--command", _reviewer_command(worker_id)])
    except Exception as e:
        teardown(ws)   # the worktree already exists; don't leave an orphan on a failed launch
        # Normalize to WorkspaceError so every bring-up failure (orca error/timeout, an OSError from
        # writing REVIEW.md) reaches the dispatcher's single spawn-failure path and its retry cap.
        if isinstance(e, WorkspaceError):
            raise
        raise WorkspaceError(f"reviewer head bring-up failed: {e}") from e
    term = data.get("terminal", data)
    return ws, (term.get("handle") or term.get("id") or "")


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
    """Overall CI state from the rollup: 'PENDING' while any job is still running (even next to an
    already-failed one — a flaky-looking early failure must not bounce the card before the rest of
    the suite finishes), then 'FAILURE' (with the first failing entry) once every job is terminal,
    'SUCCESS' if none failed, or 'NONE' when the PR has no checks at all."""
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
    if pending:
        return "PENDING", None
    if first_fail is not None:
        return "FAILURE", first_fail
    return "SUCCESS", None


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


def merge_pr(pr_url: str) -> dict:
    """Squash-merge a PR via gh. Returns {"ok": bool, "error": str|None}.

    Unlike poll_pr/pr_branch/run_stand, this is never a "retry next tick" call: the dispatcher
    calls it at most once per green review (see dispatcher._review_green), and any failure here —
    including gh being unavailable — is a final outcome the caller reports and Blocks on, not an
    unknown to poll again."""
    try:
        p = subprocess.run([GH, "pr", "merge", pr_url, "--squash"],
                           capture_output=True, text=True, timeout=GH_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"ok": False, "error": str(e)}
    if p.returncode == 0:
        return {"ok": True, "error": None}
    return {"ok": False, "error": (p.stderr or p.stdout).strip() or f"gh exit {p.returncode}"}


# --- Stand deploy + e2e (Validate layer 2) ----------------------------------------------------
# The dispatcher decides on the board; the heavy host work (git checkout, compose, e2e) lives in
# stand.py. These delegates keep the dispatcher talking to a single host boundary (worker.py) and
# stay stubbable in the dispatcher unit tests.


def read_stand_config(project: str) -> dict | None:
    """The project's `[stand]` manifest section, or None when it has no stand (layer 2 skipped)."""
    return stand.read_config(project_root(project))


def pr_branch(pr_url: str) -> str | None:
    """The PR's head branch via gh, or None if gh cannot answer (same contract as poll_pr)."""
    data = _gh_json(["pr", "view", pr_url, "--json", "headRefName"])
    if not isinstance(data, dict):
        return None
    return data.get("headRefName") or None


def run_stand(project: str, branch: str, cfg: dict) -> dict | None:
    """Deploy `branch` to the project's stand and run e2e. Returns stand.run's result dict, or
    None on an unexpected host error (treated as 'unknown, retry next tick', never a verdict)."""
    try:
        return stand.run(project, branch, cfg, project_root(project))
    except Exception:  # noqa: BLE001 — a stand host crash is an unknown, not a red verdict
        return None


def _guard_workspace_path(workspace: str) -> Path:
    """Refuse a path outside WORKSPACES_ROOT before teardown runs any rm — including the root
    itself, whose removal would take every other project's workspace with it. teardown ends in
    rm -rf/sudo rm -rf, so a wrong path here is a real deletion, not just a board mistake."""
    path = Path(workspace).resolve()
    if path == WORKSPACES_ROOT or WORKSPACES_ROOT not in path.parents:
        raise WorkspaceError(f"teardown refuses a path outside {WORKSPACES_ROOT}: {workspace}")
    return path


def stop_terminals(workspace: str) -> None:
    """Best-effort: close every terminal open in `workspace` before its worktree goes away, so the
    head is not left an orphan with a deleted cwd. Failure here is not fatal — teardown still
    proceeds to remove the worktree."""
    try:
        subprocess.run([ORCA, "terminal", "stop", "--worktree", f"path:{workspace}"],
                       capture_output=True, text=True, timeout=ORCA_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _rm_step(args: list[str]) -> subprocess.CompletedProcess:
    """Run one teardown removal step bounded by ORCA_TIMEOUT_S — the same discipline `_orca_json`
    and `stop_terminals` already apply to every other orca/host touch, so a stuck orca daemon or a
    wedged rm (e.g. a stale NFS handle) can never hang a dispatcher tick forever. A timeout counts
    as a failed step (non-zero return), not an exception, so the existing fallback chain still
    runs the next step instead of the tick crashing on it."""
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=ORCA_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired) as e:
        return subprocess.CompletedProcess(args, 1, "", str(e))


def teardown(workspace: str) -> None:
    """Stop the workspace's terminals, then best-effort remove the Orca worktree. A dev-stage
    backend without USER leaves root-owned __pycache__ in the tree, so a plain remove can fail on
    permissions — fall back to rm -rf, then sudo rm -rf (logged: after the personal_site PR#24
    non-root fix this should stay silent, so a trip here is a signal the root-owned grief is
    back)."""
    path = _guard_workspace_path(workspace)
    stop_terminals(workspace)
    r = _rm_step([ORCA, "worktree", "rm", "--worktree", f"path:{workspace}", "--force"])
    if r.returncode == 0 and not path.exists():
        return
    if path.exists():
        p = _rm_step(["rm", "-rf", workspace])
        if p.returncode != 0 and path.exists():
            STATE.log_run("teardown-sudo-fallback", workspace=workspace)
            _rm_step(["sudo", "rm", "-rf", workspace])
