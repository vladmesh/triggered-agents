"""Worker migration to the Phase 5 secretary task write protocol."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from triggered_agents.agents.pipeline import task_protocol, taskdoc, worker  # noqa: E402


class TaskProtocolTest(unittest.TestCase):
    def _env(self, repo: Path | None = None) -> dict[str, str]:
        return {
            "KANBOARD_URL": "https://board.invalid/jsonrpc.php",
            "KANBOARD_API_USER": "jsonrpc",
            "KANBOARD_API_TOKEN": "not-a-real-token",
            "TA_SECRETARY_REPO": str(repo or Path("/secretary")),
        }

    def test_preflight_rejects_missing_credential_without_value(self):
        env = self._env()
        env.pop("KANBOARD_API_TOKEN")
        ok, message = task_protocol.preflight(env)
        self.assertFalse(ok)
        self.assertIn("KANBOARD_API_TOKEN", message)
        self.assertNotIn("not-a-real-token", message)

    def test_preflight_rejects_missing_or_incompatible_secretary(self):
        ok, message = task_protocol.preflight(self._env())
        self.assertFalse(ok)
        self.assertIn("secretary task runtime", message)

    def test_preflight_accepts_compatible_task_report_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "secretary").mkdir()
            (repo / "secretary" / "__main__.py").write_text("", encoding="utf-8")
            completed = mock.Mock(returncode=0, stdout="usage: secretary task report --role ROLE")
            with mock.patch.object(task_protocol.subprocess, "run", return_value=completed) as run:
                ok, message = task_protocol.preflight(self._env(repo))
        self.assertTrue(ok)
        self.assertIn("ready", message)
        self.assertEqual(run.call_args.args[0], ["python3", "-m", "secretary", "task", "report", "--help"])

    def test_rollback_uses_only_exact_value_one(self):
        self.assertTrue(task_protocol.use_legacy_path({task_protocol.ROLLBACK_ENV: "1"}))
        for value in ("", "true", "yes", "on", "0", " 1"):
            self.assertFalse(task_protocol.use_legacy_path({task_protocol.ROLLBACK_ENV: value}))

    def test_rollback_preflight_checks_legacy_cli_not_secretary(self):
        env = self._env()
        env[task_protocol.ROLLBACK_ENV] = "1"
        with mock.patch.object(task_protocol.subprocess, "run", return_value=mock.Mock(returncode=0, stdout="--kind")) as run:
            self.assertEqual(task_protocol.preflight(env), (True, "worker task protocol rollback enabled; using legacy board-CLI"))
        self.assertEqual(run.call_args.args[0], ["python3", "-m", "triggered_agents", "pipeline", "--role", "worker", "report", "--help"])

    def test_worker_prompt_uses_selected_path(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIn("secretary task", worker._worker_prompt())
            self.assertNotIn("board-CLI", worker._worker_prompt())
        with mock.patch.dict(os.environ, {task_protocol.ROLLBACK_ENV: "1"}, clear=True):
            self.assertIn("legacy board-CLI", worker._worker_prompt())
            self.assertNotIn("secretary task", worker._worker_prompt())

    def test_default_task_document_uses_secretary_for_comment_and_report(self):
        card = {"reference": "triggered-agents-470", "project": "triggered-agents", "task_type": "code", "title": "Test"}
        with mock.patch.dict(os.environ, {}, clear=True):
            rendered = taskdoc.render(card, {"description": "spec", "comments": []}, "main")
        self.assertIn("python3 -m secretary task report", rendered)
        self.assertIn("python3 -m secretary task comment", rendered)
        self.assertNotIn("triggered_agents pipeline --role worker report", rendered)
        self.assertNotIn("Kanboard API", rendered)

    def test_rollback_task_document_uses_legacy_for_comment_and_report(self):
        card = {"reference": "triggered-agents-470", "project": "triggered-agents", "task_type": "code", "title": "Test"}
        with mock.patch.dict(os.environ, {task_protocol.ROLLBACK_ENV: "1"}, clear=True):
            rendered = taskdoc.render(card, {"description": "spec", "comments": []}, "main")
        self.assertIn("triggered_agents pipeline --role worker report", rendered)
        self.assertIn("triggered_agents pipeline --role worker comment", rendered)
        self.assertNotIn("python3 -m secretary task report", rendered)
        self.assertNotIn("python3 -m secretary task comment", rendered)

    def test_provision_stops_before_project_setup_when_secretary_is_incompatible(self):
        with mock.patch.object(task_protocol, "preflight", return_value=(False, "secretary task runtime is incompatible")), \
             mock.patch.object(worker.subprocess, "run") as run:
            ok, log = worker.provision("/workspace")
        self.assertFalse(ok)
        self.assertIn("incompatible", log)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
