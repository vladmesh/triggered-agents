"""Unit tests for triggered_agents.runtime.claude_env — no network, no Orca."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("TA_STATE", tempfile.mkdtemp(prefix="ta-claude-env-test-"))

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root

from triggered_agents.runtime import claude_env  # noqa: E402


class ClaudeEnvTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = Path(self.tmp.name) / ".claude.json"

    def _read(self) -> dict:
        return json.loads(self.config.read_text())

    def test_ensure_trust_sets_for_fresh_workspace(self):
        claude_env.ensure_trust(self.config, "/ws/x")
        self.assertTrue(self._read()["projects"]["/ws/x"]["hasTrustDialogAccepted"])

    def test_ensure_trust_keeps_other_projects_and_is_idempotent(self):
        self.config.write_text('{"projects": {"/old": {"hasTrustDialogAccepted": true}}}')
        claude_env.ensure_trust(self.config, "/ws/x")
        claude_env.ensure_trust(self.config, "/ws/x")
        d = self._read()
        self.assertTrue(d["projects"]["/ws/x"]["hasTrustDialogAccepted"])
        self.assertTrue(d["projects"]["/old"]["hasTrustDialogAccepted"])

    def test_ensure_theme_sets_when_absent(self):
        claude_env.ensure_theme(self.config)
        self.assertEqual(self._read()["theme"], "dark")

    def test_ensure_theme_leaves_existing_value(self):
        self.config.write_text('{"theme": "light"}')
        claude_env.ensure_theme(self.config)
        self.assertEqual(self._read()["theme"], "light")

    def test_ensure_theme_does_not_touch_projects(self):
        self.config.write_text('{"projects": {"/ws/x": {"hasTrustDialogAccepted": true}}}')
        claude_env.ensure_theme(self.config)
        d = self._read()
        self.assertEqual(d["theme"], "dark")
        self.assertTrue(d["projects"]["/ws/x"]["hasTrustDialogAccepted"])

    def test_garbage_config_raises(self):
        self.config.write_text("{not json")
        with self.assertRaises(claude_env.ClaudeConfigError):
            claude_env.ensure_trust(self.config, "/ws/x")
        with self.assertRaises(claude_env.ClaudeConfigError):
            claude_env.ensure_theme(self.config)


if __name__ == "__main__":
    unittest.main()
