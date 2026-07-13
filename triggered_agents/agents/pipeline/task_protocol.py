"""Worker-side bridge to the Phase 5 ``secretary task`` write protocol."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROLLBACK_ENV = "TA_WORKER_LEGACY_BOARD_WRITES"
SECRETARY_REPO_ENV = "TA_SECRETARY_REPO"
DEFAULT_SECRETARY_REPO = Path("/home/dev/secretary")
_RUNTIME_CREDENTIALS = ("KANBOARD_URL", "KANBOARD_API_USER", "KANBOARD_API_TOKEN")


def use_legacy_path(environ: dict[str, str] | None = None) -> bool:
    """Whether the explicit temporary rollback routes worker writes to board-CLI."""
    env = os.environ if environ is None else environ
    return env.get(ROLLBACK_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def secretary_repo(environ: dict[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    return Path(env.get(SECRETARY_REPO_ENV, str(DEFAULT_SECRETARY_REPO))).expanduser()


def command_prefix() -> str:
    """Shell prefix that makes the provisioned secretary source importable to a worker."""
    return 'PYTHONPATH="${TA_SECRETARY_REPO:-/home/dev/secretary}${PYTHONPATH:+:$PYTHONPATH}" python3 -m secretary'


def preflight(environ: dict[str, str] | None = None) -> tuple[bool, str]:
    """Check the worker's selected write path without exposing runtime values in errors."""
    env = dict(os.environ if environ is None else environ)
    if use_legacy_path(env):
        return True, "worker task protocol rollback enabled; using legacy board-CLI"
    missing = [name for name in _RUNTIME_CREDENTIALS if not env.get(name)]
    if missing:
        return False, "secretary task runtime configuration is unavailable (missing " + ", ".join(missing) + ")"
    repo = secretary_repo(env)
    if not (repo / "secretary" / "__main__.py").is_file():
        return False, "secretary task runtime is unavailable; configure TA_SECRETARY_REPO with a compatible checkout"
    env["PYTHONPATH"] = str(repo) + (":" + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    try:
        result = subprocess.run(
            ["python3", "-m", "secretary", "task", "report", "--help"], cwd=repo,
            env=env, capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False, "secretary task runtime is unavailable or incompatible"
    if result.returncode != 0 or "--role" not in (result.stdout or ""):
        return False, "secretary task runtime is incompatible; it must support `task report --role worker`"
    return True, "secretary task protocol ready"
