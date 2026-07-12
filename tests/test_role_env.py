"""Tests for role-scoped runtime env, the shared launch contract for heads and systemd gate."""
from __future__ import annotations

import os
import shlex
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from triggered_agents.runtime import role_env  # noqa: E402


class RoleEnvTest(unittest.TestCase):
    def _env_file(self, text: str) -> Path:
        d = tempfile.TemporaryDirectory(prefix="ta-role-env-test-")
        self.addCleanup(d.cleanup)
        path = Path(d.name) / ".env"
        path.write_text(text, encoding="utf-8")
        return path

    def test_worker_gets_board_env_and_not_full_control_panel_env(self):
        path = self._env_file("""
KANBOARD_URL=http://kanboard.invalid/jsonrpc.php
KANBOARD_API_USER=jsonrpc
KANBOARD_API_TOKEN=worker-token-value
KANBOARD_ADMIN_USER=admin
KANBOARD_ADMIN_PASSWORD=admin-password-value
SECRETARY_AGE_IDENTITY=age-secret-value
PANELMEM_KB_PAT=panelmem-pat-value
TA_CODEX_MODE=tui
""")
        base = {
            "PATH": "/bin",
            "KANBOARD_ADMIN_PASSWORD": "parent-admin-password",
            "GH_TOKEN": "parent-gh-token",
            "SECRETARY_AGE_IDENTITY": "parent-age",
        }
        env = role_env.runtime_env("worker", base_env=base, env_file=path, require=True)

        self.assertEqual(env["BOARD_ROLE"], "worker")
        self.assertEqual(env["KANBOARD_API_TOKEN"], "worker-token-value")
        self.assertEqual(env["TA_CODEX_MODE"], "tui")
        self.assertEqual(env["PATH"], "/bin")
        self.assertNotIn("KANBOARD_ADMIN_USER", env)
        self.assertNotIn("KANBOARD_ADMIN_PASSWORD", env)
        self.assertNotIn("SECRETARY_AGE_IDENTITY", env)
        self.assertNotIn("PANELMEM_KB_PAT", env)
        self.assertNotIn("GH_TOKEN", env)

    def test_curator_gets_panelmem_pat_without_board_credentials(self):
        path = self._env_file("""
KANBOARD_URL=http://kanboard.invalid/jsonrpc.php
KANBOARD_API_USER=jsonrpc
KANBOARD_API_TOKEN=board-token-value
PANELMEM_KB_PAT=panelmem-pat-value
TA_CODEX_MODE=exec
""")
        env = role_env.runtime_env("curator", base_env={"PATH": "/bin"}, env_file=path, require=True)

        self.assertNotIn("BOARD_ROLE", env)
        self.assertEqual(env["PANELMEM_KB_PAT"], "panelmem-pat-value")
        self.assertEqual(env["TA_CODEX_MODE"], "exec")
        self.assertNotIn("KANBOARD_URL", env)
        self.assertNotIn("KANBOARD_API_TOKEN", env)

    def test_pipeline_gets_board_credentials_without_board_role(self):
        path = self._env_file("""
KANBOARD_URL=http://kanboard.invalid/jsonrpc.php
KANBOARD_API_USER=jsonrpc
KANBOARD_API_TOKEN=pipeline-token-value
""")
        env = role_env.runtime_env("pipeline", base_env={"BOARD_ROLE": "stale"}, env_file=path,
                                   require=True)
        self.assertEqual(env["KANBOARD_API_TOKEN"], "pipeline-token-value")
        self.assertNotIn("BOARD_ROLE", env)

    def test_missing_required_env_points_to_launcher(self):
        path = self._env_file("KANBOARD_URL=http://kanboard.invalid/jsonrpc.php\n")
        with self.assertRaises(role_env.RoleEnvError) as ctx:
            role_env.runtime_env("worker", base_env={}, env_file=path, require=True)
        msg = str(ctx.exception)
        self.assertIn("check provisioning/launcher", msg)
        self.assertNotIn("source control-panel", msg)

    def test_wrap_shell_command_quotes_without_secret_values(self):
        cmd = role_env.wrap_shell_command(
            "worker",
            "codex exec 'prompt with spaces'",
            pythonpath="/repo path/triggered-agents",
            env_file="/secret dir/control-panel.env",
        )
        self.assertNotIn("worker-token-value", cmd)
        self.assertNotIn("admin-password-value", cmd)

        parts = shlex.split(cmd)
        self.assertEqual(parts[0], "PYTHONPATH=/repo path/triggered-agents")
        self.assertEqual(parts[1:5], ["python3", "-m", "triggered_agents.runtime.role_env", "exec"])
        self.assertEqual(parts[parts.index("--role") + 1], "worker")
        self.assertEqual(parts[parts.index("--env-file") + 1], "/secret dir/control-panel.env")
        idx = parts.index("--")
        self.assertEqual(parts[idx + 1:idx + 4], ["/bin/sh", "-lc", "codex exec 'prompt with spaces'"])

    def test_exec_cli_passes_sanitized_env_to_child_without_shell_source(self):
        path = self._env_file("""
KANBOARD_URL=http://kanboard.invalid/jsonrpc.php
KANBOARD_API_USER=jsonrpc
KANBOARD_API_TOKEN=worker-token-value
KANBOARD_ADMIN_PASSWORD=admin-password-value
""")
        captured = {}

        def fake_execvpe(file, argv, env):
            captured["file"] = file
            captured["argv"] = argv
            captured["env"] = env
            raise OSError("stop before exec")

        with mock.patch.object(os, "execvpe", fake_execvpe), redirect_stderr(StringIO()):
            rc = role_env.main([
                "exec", "--role", "worker", "--env-file", str(path), "--",
                "python3", "-c", "pass",
            ])

        self.assertEqual(rc, 126)
        self.assertEqual(captured["file"], "python3")
        self.assertEqual(captured["argv"], ["python3", "-c", "pass"])
        self.assertEqual(captured["env"]["KANBOARD_API_TOKEN"], "worker-token-value")
        self.assertNotIn("KANBOARD_ADMIN_PASSWORD", captured["env"])

    def test_apply_runtime_env_preserves_safe_ambient_env_before_clearing(self):
        path = self._env_file("""
KANBOARD_URL=http://kanboard.invalid/jsonrpc.php
KANBOARD_API_USER=jsonrpc
KANBOARD_API_TOKEN=pipeline-token-value
KANBOARD_ADMIN_PASSWORD=admin-password-value
""")
        env = {
            "PATH": "/usr/bin:/bin",
            "HOME": "/home/dev",
            "GH_TOKEN": "parent-gh-token",
            "TA_RUNTIME_ENV_FILE": str(path),
        }

        with mock.patch.dict(os.environ, env, clear=True):
            role_env.apply_runtime_env("pipeline", env_file=path)
            self.assertEqual(os.environ["PATH"], "/usr/bin:/bin")
            self.assertEqual(os.environ["HOME"], "/home/dev")
            self.assertEqual(os.environ["KANBOARD_API_TOKEN"], "pipeline-token-value")
            self.assertNotIn("KANBOARD_ADMIN_PASSWORD", os.environ)
            self.assertNotIn("GH_TOKEN", os.environ)


if __name__ == "__main__":
    unittest.main()
