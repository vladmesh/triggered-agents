"""Unit tests for retro's deterministic precheck/harvest helpers."""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from triggered_agents.agents.retro import cli as retro_cli
from triggered_agents.agents.retro import search_log
from triggered_agents.runtime.state import AgentState, PRECHECK_SKIP


def _state(tmp: str) -> AgentState:
    return AgentState("retro-test", Path(tmp) / "retro-state")


class RetroDoneCleanupTest(unittest.TestCase):
    def test_quiet_precheck_runs_cleanup_before_skip(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = _state(tmp)
            order = []

            def cleanup():
                order.append("cleanup")
                return {"closed": ["triggered-agents-1"]}

            def harvest(_st):
                order.append("harvest")
                return {"sessions": [], "memory": [], "pending": {}}

            with mock.patch.object(retro_cli, "STATE", state), \
                    mock.patch.object(retro_cli.pipeline_ops, "close_old_done_cards",
                                      side_effect=cleanup), \
                    mock.patch.object(retro_cli.harvest, "harvest", side_effect=harvest):
                rc = retro_cli.cmd_precheck()

            self.assertEqual(rc, PRECHECK_SKIP)
            self.assertEqual(order, ["cleanup", "harvest"])
            runs = [
                json.loads(line)
                for line in (state.dir / "runs.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(runs[0]["event"], "done-cleanup")
            self.assertEqual(runs[0]["result"], "closed")
            self.assertEqual(runs[0]["closed_count"], 1)
            self.assertEqual(runs[0]["references"], "triggered-agents-1")
            self.assertEqual(runs[1]["event"], "precheck")
            self.assertEqual(runs[1]["result"], "no-change")

    def test_harvest_json_reports_cleanup_refs_and_logs_cleanup_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = _state(tmp)
            batch = {
                "sessions": [],
                "memory": [],
                "pending": {},
            }
            stdout = io.StringIO()

            with mock.patch.object(retro_cli, "STATE", state), \
                    mock.patch.object(retro_cli.pipeline_ops, "close_old_done_cards",
                                      return_value={"closed": ["triggered-agents-1"]}), \
                    mock.patch.object(retro_cli.harvest, "harvest", return_value=batch), \
                    mock.patch.object(retro_cli.search_log, "tail", return_value=[]), \
                    contextlib.redirect_stdout(stdout):
                rc = retro_cli.cmd_harvest(as_json=True)

            self.assertEqual(rc, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["done_cleanup"]["closed_refs"], ["triggered-agents-1"])
            self.assertEqual(payload["done_cleanup"]["closed_count"], 1)
            self.assertNotIn("title", json.dumps(payload["done_cleanup"]))
            self.assertNotIn("description", json.dumps(payload["done_cleanup"]))
            runs = [
                json.loads(line)
                for line in (state.dir / "runs.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(runs[0]["event"], "done-cleanup")
            self.assertEqual(runs[0]["references"], "triggered-agents-1")


class RetroSearchLogTest(unittest.TestCase):
    def test_default_log_follows_live_memory_storage(self):
        self.assertEqual(
            search_log.DEFAULT_SEARCH_LOG,
            Path.home() / "secretary-data" / "memory" / "search-log.jsonl",
        )


if __name__ == "__main__":
    unittest.main()
