"""Live smoke for Codex TUI trust in a linked git worktree.

NOT a unit test (name is e2e_*, so `unittest discover` skips it). It creates a disposable
Orca-managed linked worktree, launches Codex TUI through Orca with the pipeline launcher command,
waits for `tui-idle`, sends the initial prompt, and confirms a Codex user turn was written for that
worktree. The run uses a temporary CODEX_HOME copied from the pipeline auth but with no persisted
project trust, so the command-line trust override has to cover Codex' linked-worktree repo root.

Prep: Orca runtime reachable, `codex` logged in for the pipeline CODEX_HOME, then run:
`python3 tests/e2e_codex_tui_trust.py`.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from pathlib import Path

_STATE_DIR = tempfile.mkdtemp(prefix="ta-codex-tui-trust-state-")
os.environ["TA_STATE"] = _STATE_DIR
os.environ["TA_PIPELINE_STATE_DIR"] = tempfile.mkdtemp(prefix="ta-codex-tui-trust-pipeline-")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from triggered_agents.agents.pipeline import (  # noqa: E402
    codex_sessions,
    heads,
    prompt_delivery,
    terminal_session,
    worker,
)

PROMPT = "Ответь одной строкой: e2e-codex-tui-turn-start"
IDLE_TIMEOUT_MS = os.environ.get("TA_E2E_CODEX_TUI_IDLE_TIMEOUT_MS", "60000")
DELIVERY_TIMEOUT_S = float(os.environ.get("TA_E2E_CODEX_TUI_DELIVERY_TIMEOUT_S", "20"))
PROJECT = "triggered-agents"
BASE_BRANCH = "main"


def check(label: str, cond: bool) -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        raise SystemExit(1)


def read_screen(handle: str) -> str:
    return terminal_session.read_terminal_text(handle, worker._orca_json)


def make_codex_home(root: Path) -> Path:
    source = Path(heads.CODEX_HOME)
    auth = source / "auth.json"
    if not auth.is_file():
        print(f"e2e: missing Codex auth at {auth}", file=sys.stderr)
        raise SystemExit(2)
    home = root / "codex-home"
    home.mkdir()
    shutil.copy2(auth, home / "auth.json")
    for name in ("AGENTS.md", "installation_id"):
        src = source / name
        if src.is_file():
            shutil.copy2(src, home / name)
    memory_url = "http://127.0.0.1:8077/mcp"
    config = source / "config.toml"
    if config.is_file():
        data = tomllib.loads(config.read_text(encoding="utf-8"))
        memory_url = ((data.get("mcp_servers") or {}).get("memory") or {}).get("url") or memory_url
    (home / "config.toml").write_text(
        'personality = "pragmatic"\n'
        '[mcp_servers.memory]\n'
        f'url = "{memory_url}"\n',
        encoding="utf-8",
    )
    return home


def e2e_registry(codex_home: Path) -> heads.Registry:
    reg = heads.load_registry()
    profiles = dict(reg.profiles)
    profiles["codex-tui-e2e"] = dict(reg.profile("codex-tui"), codex_home=str(codex_home))
    return heads.Registry(reg.resources, profiles)


def main() -> int:
    if not shutil.which("orca"):
        print("e2e: orca not found on PATH", file=sys.stderr)
        return 2
    if not shutil.which("codex"):
        print("e2e: codex not found on PATH", file=sys.stderr)
        return 2

    handle = ""
    workspace = ""
    tmp = tempfile.TemporaryDirectory(prefix="ta-codex-tui-trust-")
    try:
        codex_home = make_codex_home(Path(tmp.name))
        codex_sessions.SESSIONS_ROOT = codex_home / "sessions"
        name = f"e2e-codex-tui-trust-{os.getpid()}"
        workspace = worker.create_workspace(PROJECT, name, BASE_BRANCH)
        repo = worker.project_root(PROJECT).resolve()
        linked = Path(workspace).resolve()
        trust_paths = heads._codex_tui_trust_paths(str(linked))
        check("linked worktree path differs from Codex repo trust root", linked != repo)
        check("launcher trusts linked worktree", str(linked) in trust_paths)
        check("launcher trusts common-dir repo root", str(repo) in trust_paths)

        launch = heads.render_launch("codex-tui", role="worker", prompt=PROMPT,
                                     workspace=str(linked), registry=e2e_registry(codex_home))
        check("launcher does not use wildcard trust", "projects.\"*\"" not in launch.command)
        data = worker._orca_json(["terminal", "create", "--worktree", f"path:{linked}",
                                  "--title", "e2e: codex tui trust",
                                  "--command", launch.command])
        term = data.get("terminal", data)
        handle = term.get("handle") or term.get("id") or ""
        check("terminal handle returned", bool(handle))

        def wait_idle() -> None:
            worker._orca_json(["terminal", "wait", "--terminal", handle, "--for", "tui-idle",
                               "--timeout-ms", IDLE_TIMEOUT_MS])
            screen = terminal_session.strip_ansi(read_screen(handle))
            trust_prompt = "Do you trust the contents of this directory?" in screen
            check("Codex TUI reached idle without trust prompt", not trust_prompt)

        result = prompt_delivery.deliver_initial_prompt(
            PROMPT,
            str(linked),
            handle,
            wait_idle,
            lambda: worker._orca_json(["terminal", "send", "--terminal", handle,
                                       "--text", PROMPT, "--enter"]),
            lambda: worker._orca_json(["terminal", "send", "--terminal", handle,
                                       "--text", "", "--enter"]),
            lambda: read_screen(handle),
            timeout_s=DELIVERY_TIMEOUT_S,
        )
        check(f"initial prompt started turn via {result.signal}", bool(result.signal))
        return 0
    finally:
        if handle:
            try:
                worker._orca_json(["terminal", "close", "--terminal", handle])
            except Exception as e:  # noqa: BLE001
                print(f"cleanup: terminal close failed: {e}", file=sys.stderr)
        if workspace:
            try:
                worker.teardown(workspace)
            except Exception as e:  # noqa: BLE001
                print(f"cleanup: workspace teardown failed: {e}", file=sys.stderr)
        try:
            tmp.cleanup()
        finally:
            shutil.rmtree(_STATE_DIR, ignore_errors=True)
            shutil.rmtree(os.environ["TA_PIPELINE_STATE_DIR"], ignore_errors=True)
        time.sleep(0.2)


if __name__ == "__main__":
    raise SystemExit(main())
