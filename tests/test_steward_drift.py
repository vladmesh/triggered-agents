"""Unit tests for triggered_agents.agents.steward.drift (triggered-agents-256): live ta-* systemd
units vs. the render deploy/provision.py would produce right now from the current automation.toml
specs. Fake AGENTS_DIR/SYSTEMD_DIR/AGENTS_ROOT throughout — no real host units are ever touched.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # repo root

import deploy.provision as provision  # noqa: E402
from triggered_agents.agents.pipeline import worker as pipeline_worker  # noqa: E402
from triggered_agents.agents.steward import drift  # noqa: E402

_SPEC = """
name = "{agent}"
skill = "/{agent}"
provider = "claude"
reuse_session = true
precheck = "python3 -m triggered_agents {agent} precheck"

[systemd]
calendar = "hourly"
randomized_delay_sec = 90
"""


class DriftCheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)

        self.agents_dir = root / "agents"
        self.agents_dir.mkdir()
        self.systemd_dir = root / "systemd"
        self.systemd_dir.mkdir()
        self.workspaces_root = root / "workspaces"

        for p, target in ((provision, "AGENTS_DIR"), ):
            self._patch(p, target, self.agents_dir)
        self._patch(drift, "SYSTEMD_DIR", self.systemd_dir)
        self._patch(pipeline_worker, "AGENTS_ROOT", self.workspaces_root)

    def _patch(self, target, attr, value) -> None:
        p = mock.patch.object(target, attr, value)
        p.start()
        self.addCleanup(p.stop)

    def _write_spec(self, agent: str) -> None:
        d = self.agents_dir / agent
        d.mkdir()
        (d / "automation.toml").write_text(_SPEC.format(agent=agent), encoding="utf-8")

    def _expected(self, agent: str) -> tuple[str, str]:
        ws = self.workspaces_root / agent
        service = provision._service_unit(agent, "hourly", ws,
                                          f"python3 -m triggered_agents {agent} precheck", "")
        timer = provision._timer_unit(agent, "hourly", 90)
        return service, timer

    def test_in_sync_when_live_matches_current_render(self):
        self._write_spec("curator")
        service, timer = self._expected("curator")
        (self.systemd_dir / "ta-curator.service").write_text(service, encoding="utf-8")
        (self.systemd_dir / "ta-curator.timer").write_text(timer, encoding="utf-8")

        result = drift.check()
        self.assertTrue(result["in_sync"])
        self.assertEqual(result["drift"], [])

    def test_content_drift_when_live_unit_is_stale(self):
        self._write_spec("curator")
        service, timer = self._expected("curator")
        (self.systemd_dir / "ta-curator.service").write_text(service.replace("hourly", "OLD-CAL"),
                                                             encoding="utf-8")
        (self.systemd_dir / "ta-curator.timer").write_text(timer, encoding="utf-8")

        result = drift.check()
        self.assertFalse(result["in_sync"])
        kinds = {h["unit"]: h["kind"] for h in result["drift"]}
        self.assertEqual(kinds["ta-curator.service"], "content")
        self.assertIn("OLD-CAL", result["drift"][0]["diff"])

    def test_missing_unit_when_spec_exists_but_never_provisioned(self):
        self._write_spec("curator")
        result = drift.check()
        self.assertFalse(result["in_sync"])
        kinds = {h["unit"]: h["kind"] for h in result["drift"]}
        self.assertEqual(kinds["ta-curator.service"], "missing")
        self.assertEqual(kinds["ta-curator.timer"], "missing")

    def test_extra_unit_when_agent_decommissioned_but_unit_left_on_host(self):
        # No spec at all for "board" any more — a live unit with no matching spec is exactly the
        # ta-board class this check exists to catch (2026-07-04).
        (self.systemd_dir / "ta-board.service").write_text("stale content", encoding="utf-8")
        (self.systemd_dir / "ta-board.timer").write_text("stale content", encoding="utf-8")

        result = drift.check()
        self.assertFalse(result["in_sync"])
        kinds = {h["unit"]: h["kind"] for h in result["drift"]}
        self.assertEqual(kinds["ta-board.service"], "extra")
        self.assertEqual(kinds["ta-board.timer"], "extra")

    def test_variant_unit_is_checked_too(self):
        d = self.agents_dir / "steward"
        d.mkdir()
        (d / "automation.toml").write_text(_SPEC.format(agent="steward") + """
[variants.deep-sweep]
skill = "/steward deep-sweep"

[variants.deep-sweep.systemd]
calendar = "03:47:00"
randomized_delay_sec = 600
""", encoding="utf-8")
        service, timer = self._expected("steward")
        (self.systemd_dir / "ta-steward.service").write_text(service, encoding="utf-8")
        (self.systemd_dir / "ta-steward.timer").write_text(timer, encoding="utf-8")
        # No ta-steward-deep-sweep unit written at all -> missing.

        result = drift.check()
        self.assertFalse(result["in_sync"])
        units = {h["unit"] for h in result["drift"]}
        self.assertIn("ta-steward-deep-sweep.service", units)
        self.assertIn("ta-steward-deep-sweep.timer", units)

    def test_render_markdown_reports_clean_when_in_sync(self):
        result = {"in_sync": True, "drift": []}
        self.assertIn("нет", drift.render_markdown(result))

    def test_render_markdown_lists_every_hit(self):
        result = {"in_sync": False, "drift": [
            {"unit": "ta-curator.service", "kind": "content", "diff": "- old\n+ new\n"},
        ]}
        out = drift.render_markdown(result)
        self.assertIn("ta-curator.service", out)
        self.assertIn("content", out)
        self.assertIn("- old", out)


if __name__ == "__main__":
    unittest.main()
