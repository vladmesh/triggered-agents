"""Live e2e for role-scoped runtime env through real Orca terminals.

Run: `python3 tests/e2e_role_env_orca.py`. It creates a throwaway Kanboard project, then starts
three short-lived Orca terminals in a throwaway Orca worktree:

  * worker terminal posts a `[report:done]`;
  * reviewer terminal posts a `[review:green]`;
  * steward terminal closes its own service report-card.

No LLM head is invoked, so this spends no model tokens. The exercised boundary is the one this
card changes: `orca terminal create --command ...` starts a process that gets its role env before
the board CLI runs, without a manual shell source.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import shlex
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import redirect_stdout
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from triggered_agents.runtime import role_env  # noqa: E402

try:
    role_env.apply_runtime_env("pipeline")
except role_env.RoleEnvError as e:
    print(f"e2e: {e}", file=sys.stderr)
    raise SystemExit(2)

BOARD = f"__e2e_role_env_orca_{int(time.time())}__"
STATE_DIR = tempfile.mkdtemp(prefix="ta-role-env-orca-state-")
PIPELINE_STATE_DIR = tempfile.mkdtemp(prefix="ta-role-env-orca-pipeline-")
os.environ["TA_PIPELINE_BOARD"] = BOARD
os.environ["TA_STATE"] = STATE_DIR
os.environ["TA_PIPELINE_STATE_DIR"] = PIPELINE_STATE_DIR

from triggered_agents.runtime.kanboard import call  # noqa: E402
from triggered_agents.agents.pipeline import cli, model, ops  # noqa: E402

ORCA = os.environ.get("ORCA_BIN") or shutil.which("orca") or str(Path.home() / ".local/bin/orca")
ORCA_REPO = Path("/home/dev/projects/personal_site") \
    if Path("/home/dev/projects/personal_site").is_dir() else REPO_ROOT
WORKTREE_PATH: str | None = None
FAIL = False


def check(label: str, cond: bool) -> None:
    global FAIL
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        FAIL = True
        raise SystemExit(1)


def run_cli(role: str | None, argv: list[str]) -> dict | None:
    full = (["--role", role] if role else []) + argv
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli.main(full)
    check(f"pipeline {' '.join(argv[:1])} rc=0", rc == 0)
    out = buf.getvalue().strip()
    return json.loads(out) if out else None


def orca_json(args: list[str], timeout: int = 60) -> dict:
    p = subprocess.run([ORCA, *args, "--json"], capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError(f"orca {' '.join(args)} failed: {(p.stderr or p.stdout).strip()}")
    data = json.loads(p.stdout)
    return data.get("result", data)


def terminal_command(role: str, inner: str) -> str:
    env_prefix = " ".join([
        f"TA_PIPELINE_BOARD={shlex.quote(BOARD)}",
        f"TA_STATE={shlex.quote(STATE_DIR)}",
        f"TA_PIPELINE_STATE_DIR={shlex.quote(PIPELINE_STATE_DIR)}",
    ])
    return env_prefix + " " + role_env.wrap_shell_command(role, inner, pythonpath=str(REPO_ROOT))


def create_orca_worktree() -> str:
    name = f"role-env-orca-e2e-{int(time.time())}"
    data = orca_json([
        "worktree", "create",
        "--repo", f"path:{ORCA_REPO}",
        "--name", name,
        "--setup", "skip",
        "--no-parent",
    ], timeout=120)
    wt = data.get("worktree", data)
    path = wt.get("path")
    check("e2e worktree created", bool(path))
    return path


def run_orca_terminal(role: str, title: str, inner: str) -> str:
    if not WORKTREE_PATH:
        raise RuntimeError("WORKTREE_PATH is not initialized")
    marker = f"__TA_E2E_DONE_{uuid.uuid4().hex}__"
    command = f"{inner}; rc=$?; echo {marker}:$rc"
    data = orca_json([
        "terminal", "create",
        "--worktree", f"path:{WORKTREE_PATH}",
        "--title", title,
        "--command", terminal_command(role, command),
    ])
    term = data.get("terminal", data)
    handle = term.get("handle") or term.get("id")
    check(f"{title}: terminal handle returned", bool(handle))
    try:
        deadline = time.time() + 120
        text = ""
        while time.time() < deadline:
            shown = orca_json(["terminal", "show", "--terminal", handle])
            term_data = shown.get("terminal", shown)
            text = term_data.get("preview", "") or ""
            read = orca_json(["terminal", "read", "--terminal", handle, "--limit", "20000"])
            tail = (read.get("terminal", read).get("tail") or [])
            text += "\n" + "\n".join(str(line.get("text", line)) if isinstance(line, dict) else str(line)
                                     for line in tail)
            if marker in text:
                break
            time.sleep(1.0)
        check(f"{title}: completion marker printed", marker in text)
    finally:
        for _ in range(3):
            time.sleep(0.5)
            orca_json(["terminal", "close", "--terminal", handle])
            terms = orca_json(["terminal", "list", "--worktree", f"path:{WORKTREE_PATH}", "--limit", "50"])
            live = {t.get("handle") or t.get("id") for t in terms.get("terminals", [])}
            if handle not in live:
                break
    check(f"{title}: command completed", "Traceback" not in text and "role-env:" not in text)
    if f"{marker}:0" not in text:
        print(text[-4000:])
    check(f"{title}: exit code zero", f"{marker}:0" in text)
    return text


def comments(reference: str) -> str:
    return "\n".join(c["text"] for c in ops.show_card(reference)["comments"])


def main() -> int:
    global WORKTREE_PATH
    try:
        WORKTREE_PATH = create_orca_worktree()
        run_cli(None, ["setup"])
        card = run_cli("po", [
            "create", "--project", "triggered-agents", "--type", "code",
            "--title", "e2e role env orca worker/reviewer", "--column", "Ready",
        ])
        ref = card["reference"]

        worker_body = "role-env-orca worker report"
        run_orca_terminal(
            "worker",
            "e2e-role-env-worker",
            "python3 -m triggered_agents pipeline report "
            f"--ref {shlex.quote(ref)} --kind done --body {shlex.quote(worker_body)}",
        )
        text = comments(ref)
        check("worker report marker present", f"[{model.MARKER_REPORT_DONE}]" in text)
        check("worker report body present", worker_body in text)

        reviewer_body = "role-env-orca reviewer verdict"
        run_orca_terminal(
            "reviewer",
            "e2e-role-env-reviewer",
            "python3 -m triggered_agents pipeline verdict "
            f"--ref {shlex.quote(ref)} --kind green --body {shlex.quote(reviewer_body)}",
        )
        text = comments(ref)
        check("reviewer verdict marker present", f"[{model.MARKER_REVIEW_GREEN}]" in text)
        check("reviewer verdict body present", reviewer_body in text)

        report = ops.create_report_card(
            "triggered-agents",
            "e2e role env orca steward report",
            f"role-env-orca-{int(time.time())}",
        )
        steward_ref = report["reference"]
        run_orca_terminal(
            "steward",
            "e2e-role-env-steward",
            "python3 -m triggered_agents pipeline move "
            f"--ref {shlex.quote(steward_ref)} --to Done --reason "
            f"{shlex.quote('role-env-orca steward closed report')}",
        )
        steward_card = ops.show_card(steward_ref)
        check("steward report-card moved to Done", steward_card["column"] == "Done")

        print("\nALL PASS")
        return 0
    finally:
        if WORKTREE_PATH:
            try:
                orca_json(["terminal", "stop", "--worktree", f"path:{WORKTREE_PATH}"])
            except Exception as e:
                print(f"cleanup: failed to stop e2e terminals: {e}", file=sys.stderr)
            try:
                orca_json(["worktree", "rm", "--worktree", f"path:{WORKTREE_PATH}", "--force"], timeout=120)
                print(f"cleanup: removed worktree {WORKTREE_PATH}")
            except Exception as e:
                print(f"cleanup: failed to remove e2e worktree {WORKTREE_PATH}: {e}", file=sys.stderr)
        try:
            for project in call("getAllProjects") or []:
                if project["name"] == BOARD:
                    call("removeProject", project_id=int(project["id"]))
                    print(f"cleanup: removed board {BOARD} (id {project['id']})")
                    break
        except Exception as e:
            print(f"cleanup: failed to remove {BOARD}: {e}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
