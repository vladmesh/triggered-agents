"""Unit tests for deploy/provision.py's variant (second-timer) support (triggered-agents-254):
a second, differently-scheduled mode of the same agent (the steward's daily "deep-sweep"), with
no precheck gate at all. Only exercises the pure string builders — sudo/systemctl calls are left
to the live redeploy, same split as test_provision_gate.py.
"""
from __future__ import annotations

import sys
import tomllib
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root

from deploy.provision import _variant_service_unit, _timer_unit  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]


class VariantServiceUnitTest(unittest.TestCase):
    def test_no_precheck_gate_dispatch_is_unconditional(self):
        unit = _variant_service_unit("steward", "deep-sweep", "03:47:00", Path("/ws/steward"))
        self.assertNotIn("rc=$?", unit)
        self.assertIn("ExecStart=/bin/bash -lc 'exec python3 -m triggered_agents steward dispatch deep-sweep'", unit)

    def test_env_file_included_when_given(self):
        unit = _variant_service_unit("steward", "deep-sweep", "03:47:00", Path("/ws/steward"),
                                      env_file="/home/dev/control-panel/.env")
        self.assertIn("EnvironmentFile=/home/dev/control-panel/.env", unit)

    def test_env_file_omitted_when_blank(self):
        unit = _variant_service_unit("steward", "deep-sweep", "03:47:00", Path("/ws/steward"))
        self.assertNotIn("EnvironmentFile=", unit)

    def test_workspace_is_the_working_directory(self):
        unit = _variant_service_unit("steward", "deep-sweep", "03:47:00", Path("/ws/steward"))
        self.assertIn("WorkingDirectory=/ws/steward", unit)


class TimerUnitLabelTest(unittest.TestCase):
    def test_label_is_descriptive_only_not_the_filename(self):
        timer = _timer_unit("steward deep-sweep", "03:47:00", 600)
        self.assertIn("steward deep-sweep", timer)
        self.assertIn("OnCalendar=03:47:00", timer)
        self.assertIn("RandomizedDelaySec=600", timer)


class StewardSpecScheduleTest(unittest.TestCase):
    """The steward's real automation.toml — the deep-sweep timer must not collide with the
    hourly :00-:03 crowd (curator/board/pipeline) or retro's daily ~00:00-00:05 pass."""

    def setUp(self):
        self.spec = tomllib.loads((REPO_ROOT / "triggered_agents" / "agents" / "steward"
                                   / "automation.toml").read_text())

    def test_deep_sweep_variant_is_declared(self):
        self.assertIn("deep-sweep", self.spec["variants"])

    def test_deep_sweep_skips_precheck_entirely(self):
        # the variant table names no `precheck` key at all — dispatch is unconditional.
        self.assertNotIn("precheck", self.spec["variants"]["deep-sweep"])

    def test_deep_sweep_calendar_is_not_the_top_of_the_hour(self):
        cal = self.spec["variants"]["deep-sweep"]["systemd"]["calendar"]
        hh, mm, _ = cal.split(":")
        self.assertNotEqual(mm, "00")
        self.assertNotEqual((hh, mm), ("00", "00"))  # not retro's midnight either

    def test_deep_sweep_calendar_differs_from_retro_midnight(self):
        retro_cal = tomllib.loads((REPO_ROOT / "triggered_agents" / "agents" / "retro"
                                   / "automation.toml").read_text())["systemd"]["calendar"]
        deep_cal = self.spec["variants"]["deep-sweep"]["systemd"]["calendar"]
        self.assertNotEqual(retro_cal, deep_cal)


if __name__ == "__main__":
    unittest.main()
