"""Unit tests for runtime/health.py — stdlib unittest, no systemd, no Orca.

`_timer_active`/`_workspace` are patched per test: the real ones shell out to systemctl / read
TA_WORKSPACE, neither of which a unit test should touch.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root

from triggered_agents.runtime import health  # noqa: E402


class MaxAgeTest(unittest.TestCase):
    """Freshness budget per agent: explicit [health] override > named cadence > default."""

    def test_pipeline_uses_explicit_health_override(self):
        # pipeline's calendar is a raw 3-min OnCalendar expression, not "hourly"/"daily", so it
        # relies on automation.toml's [health] max_age_s rather than the cadence table below.
        self.assertEqual(health._max_age_s("pipeline"), 900)

    def test_hourly_cadence_uses_named_default(self):
        self.assertEqual(health._max_age_s("curator"), 3 * 3600)

    def test_daily_cadence_uses_named_default(self):
        self.assertEqual(health._max_age_s("retro"), 26 * 3600)

    def test_env_override_wins_over_everything(self):
        with mock.patch.object(health, "_ENV_MAX_AGE", "123"):
            self.assertEqual(health._max_age_s("pipeline"), 123)


class CheckTest(unittest.TestCase):
    """check() flags an agent red on a stale (or missing) runs.jsonl. A precheck-nothing-to-do
    event is just as fresh a proof of life as a dispatched one — but a precheck-error is not: a
    permanently down Kanboard makes precheck log a fresh error every tick forever, so counting
    those as proof of life would keep a dead dispatcher looking green."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name)

        p = mock.patch.object(health, "_workspace", lambda agent: str(self.ws))
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(health, "_timer_active", lambda agent: True)
        p.start()
        self.addCleanup(p.stop)

    def _write_run(self, event: str, result: str, age_s: float) -> None:
        state_dir = self.ws / "state" / "pipeline"
        state_dir.mkdir(parents=True, exist_ok=True)
        ts = (datetime.now(timezone.utc) - timedelta(seconds=age_s)).isoformat()
        line = f'{{"ts": "{ts}", "event": "{event}", "result": "{result}"}}\n'
        with (state_dir / "runs.jsonl").open("a", encoding="utf-8") as f:
            f.write(line)

    def test_fresh_nothing_to_do_event_is_ok(self):
        self._write_run("precheck", "nothing-to-do", age_s=60)
        rc = health.check(("pipeline",))
        self.assertEqual(rc, 0)

    def test_stale_last_event_past_threshold_is_red(self):
        self._write_run("precheck", "nothing-to-do", age_s=901)  # > pipeline's 900s budget
        rc = health.check(("pipeline",))
        self.assertEqual(rc, 1)

    def test_no_runs_at_all_is_red(self):
        rc = health.check(("pipeline",))
        self.assertEqual(rc, 1)

    def test_only_fresh_error_events_is_red_not_ok(self):
        # A down Kanboard: precheck errors every tick, so the raw last event is always fresh.
        # Freshness must be judged on the last non-error event, else this looks perpetually OK.
        self._write_run("precheck", "nothing-to-do", age_s=901)  # last time it was actually healthy
        self._write_run("precheck", "error", age_s=60)
        rc = health.check(("pipeline",))
        self.assertEqual(rc, 1)

    def test_fresh_error_after_recent_healthy_tick_stays_ok(self):
        # A single transient error right after a healthy tick must not flip the check red.
        self._write_run("precheck", "nothing-to-do", age_s=60)
        self._write_run("precheck", "error", age_s=5)
        rc = health.check(("pipeline",))
        self.assertEqual(rc, 0)

    def test_only_ever_error_events_is_red(self):
        self._write_run("precheck", "error", age_s=5)
        rc = health.check(("pipeline",))
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
