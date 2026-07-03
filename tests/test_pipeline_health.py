"""Unit tests for triggered_agents.agents.pipeline.health — TTL-cached resource probes, claim-time
fallback selection, and the two real (mocked-at-the-boundary) probes for claude-sub/openrouter.

Not to be confused with tests/test_health.py (runtime/health.py's unrelated per-agent liveness
check). No network, no orca, no real `claude`/OpenRouter calls: subprocess.run and
urllib.request.urlopen are mocked at their call sites.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_STATE_DIR = tempfile.mkdtemp(prefix="ta-health-test-")
os.environ["TA_STATE"] = _STATE_DIR

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from triggered_agents.agents.pipeline import health, heads  # noqa: E402


def _registry(resources, profiles=None):
    return heads.Registry(resources=resources, profiles=profiles or {})


class RefreshTtlTest(unittest.TestCase):
    def setUp(self):
        health.STATE.ensure_dir()
        for f in (health.STATE.dir / "runs.jsonl", health.HEALTH_FILE):
            if f.exists():
                f.unlink()

    def _runs(self):
        path = health.STATE.dir / "runs.jsonl"
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    def test_first_probe_sets_status_without_logging_a_transition(self):
        reg = _registry({"r1": {"probe": "true"}})
        statuses = health.refresh(reg)
        self.assertEqual(statuses, {"r1": "green"})
        self.assertFalse(any(r["event"] == "head-health" for r in self._runs()))

    def test_red_probe_sets_red_status(self):
        reg = _registry({"r1": {"probe": "false"}})
        statuses = health.refresh(reg)
        self.assertEqual(statuses, {"r1": "red"})

    def test_cache_reused_within_ttl_no_reprobe(self):
        calls = []

        def fake_probe(cmd):
            calls.append(cmd)
            return True

        reg = _registry({"r1": {"probe": "true"}})
        with mock.patch.object(health, "_run_probe_cmd", fake_probe):
            health.refresh(reg)
            health.refresh(reg)
        self.assertEqual(len(calls), 1)

    def test_probe_rerun_after_ttl_expires(self):
        calls = []

        def fake_probe(cmd):
            calls.append(cmd)
            return True

        reg = _registry({"r1": {"probe": "true"}})
        with mock.patch.object(health, "_run_probe_cmd", fake_probe), \
             mock.patch.object(health, "PROBE_TTL_S", 0):
            health.refresh(reg)
            health.refresh(reg)
        self.assertEqual(len(calls), 2)

    def test_green_to_red_transition_logs_one_event(self):
        reg = _registry({"r1": {"probe": "true"}})
        with mock.patch.object(health, "PROBE_TTL_S", 0):
            with mock.patch.object(health, "_run_probe_cmd", return_value=True):
                health.refresh(reg)
            with mock.patch.object(health, "_run_probe_cmd", return_value=False):
                health.refresh(reg)
        events = [r for r in self._runs() if r["event"] == "head-health"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["resource"], "r1")
        self.assertEqual(events[0]["from"], "green")
        self.assertEqual(events[0]["to"], "red")

    def test_same_status_reprobe_does_not_log_again(self):
        reg = _registry({"r1": {"probe": "true"}})
        with mock.patch.object(health, "PROBE_TTL_S", 0), \
             mock.patch.object(health, "_run_probe_cmd", return_value=True):
            health.refresh(reg)
            health.refresh(reg)
            health.refresh(reg)
        events = [r for r in self._runs() if r["event"] == "head-health"]
        self.assertFalse(events)

    def test_independent_redness_across_two_resources(self):
        # The health mechanic must not be implicitly keyed to a single resource id — claude red,
        # openrouter green (and the reverse) must both be representable at once.
        reg = _registry({"claude-sub": {"probe": "false"}, "openrouter": {"probe": "true"}})
        statuses = health.refresh(reg)
        self.assertEqual(statuses, {"claude-sub": "red", "openrouter": "green"})

        reg2 = _registry({"claude-sub": {"probe": "true"}, "openrouter": {"probe": "false"}})
        with mock.patch.object(health, "PROBE_TTL_S", 0):
            statuses2 = health.refresh(reg2)
        self.assertEqual(statuses2, {"claude-sub": "green", "openrouter": "red"})

    def test_probe_timeout_counts_as_red(self):
        reg = _registry({"r1": {"probe": "sleep 5"}})
        with mock.patch.object(health, "PROBE_TIMEOUT_S", 0.01):
            statuses = health.refresh(reg)
        self.assertEqual(statuses, {"r1": "red"})


class ResolveHeadTest(unittest.TestCase):
    """Fallback selection at claim time — pure function, no board/state involved."""

    def _reg(self):
        return _registry(
            resources={"res-a": {"probe": "true"}, "res-b": {"probe": "true"}, "res-c": {"probe": "true"}},
            profiles={
                "primary": {"resource": "res-a", "adapter": "claude", "fallback": ["secondary"]},
                "secondary": {"resource": "res-b", "adapter": "claude", "fallback": ["tertiary"]},
                "tertiary": {"resource": "res-c", "adapter": "claude", "fallback": []},
            },
        )

    def test_preferred_green_wins(self):
        reg = self._reg()
        statuses = {"res-a": "green", "res-b": "green", "res-c": "green"}
        self.assertEqual(health.resolve_head("primary", statuses, reg), "primary")

    def test_preferred_red_falls_back_to_first_green_in_chain(self):
        reg = self._reg()
        statuses = {"res-a": "red", "res-b": "green", "res-c": "green"}
        self.assertEqual(health.resolve_head("primary", statuses, reg), "secondary")

    def test_falls_back_recursively_through_the_whole_chain(self):
        reg = self._reg()
        statuses = {"res-a": "red", "res-b": "red", "res-c": "green"}
        self.assertEqual(health.resolve_head("primary", statuses, reg), "tertiary")

    def test_whole_chain_red_returns_none(self):
        reg = self._reg()
        statuses = {"res-a": "red", "res-b": "red", "res-c": "red"}
        self.assertIsNone(health.resolve_head("primary", statuses, reg))

    def test_resource_with_no_recorded_status_defaults_green(self):
        # A resource health.refresh hasn't gotten to yet (e.g. it errored) must not silently
        # block claims — fail open, not fail closed.
        reg = self._reg()
        self.assertEqual(health.resolve_head("primary", {}, reg), "primary")


class NextRetryHeadTest(unittest.TestCase):
    """Watchdog retry-switch target selection — breadth-first over `current`'s own fallback chain,
    skipping tried heads and red resources, with the exhausted-vs-waiting distinction
    dispatcher._watchdog_retry needs (spend the switch budget's one shot vs wait for free)."""

    def _reg(self):
        return _registry(
            resources={"res-a": {"probe": "true"}, "res-b": {"probe": "true"}, "res-c": {"probe": "true"}},
            profiles={
                "primary": {"resource": "res-a", "adapter": "claude", "fallback": ["secondary"]},
                "secondary": {"resource": "res-b", "adapter": "claude", "fallback": ["tertiary"]},
                "tertiary": {"resource": "res-c", "adapter": "claude", "fallback": []},
            },
        )

    def test_first_untried_green_wins(self):
        reg = self._reg()
        statuses = {"res-a": "green", "res-b": "green", "res-c": "green"}
        self.assertEqual(health.next_retry_head("primary", {"primary"}, statuses, reg),
                         ("secondary", False))

    def test_skips_already_tried_heads_even_if_green(self):
        reg = self._reg()
        statuses = {"res-a": "green", "res-b": "green", "res-c": "green"}
        self.assertEqual(health.next_retry_head("primary", {"primary", "secondary"}, statuses, reg),
                         ("tertiary", False))

    def test_skips_red_candidates_for_a_later_green_one(self):
        reg = self._reg()
        statuses = {"res-a": "green", "res-b": "red", "res-c": "green"}
        self.assertEqual(health.next_retry_head("primary", {"primary"}, statuses, reg),
                         ("tertiary", False))

    def test_untried_candidates_all_red_waits_not_exhausted(self):
        reg = self._reg()
        statuses = {"res-a": "green", "res-b": "red", "res-c": "red"}
        resolved, exhausted = health.next_retry_head("primary", {"primary"}, statuses, reg)
        self.assertIsNone(resolved)
        self.assertFalse(exhausted)

    def test_no_untried_candidates_left_is_exhausted(self):
        reg = self._reg()
        statuses = {"res-a": "green", "res-b": "green", "res-c": "green"}
        resolved, exhausted = health.next_retry_head(
            "primary", {"primary", "secondary", "tertiary"}, statuses, reg)
        self.assertIsNone(resolved)
        self.assertTrue(exhausted)

    def test_empty_fallback_chain_is_exhausted(self):
        reg = self._reg()
        resolved, exhausted = health.next_retry_head("tertiary", {"tertiary"}, {}, reg)
        self.assertIsNone(resolved)
        self.assertTrue(exhausted)

    def test_unknown_current_head_is_exhausted_not_a_crash(self):
        reg = self._reg()
        resolved, exhausted = health.next_retry_head("nope", set(), {}, reg)
        self.assertIsNone(resolved)
        self.assertTrue(exhausted)

    def test_resource_with_no_recorded_status_defaults_green(self):
        reg = self._reg()
        self.assertEqual(health.next_retry_head("primary", {"primary"}, {}, reg), ("secondary", False))


class ForceRedTest(unittest.TestCase):
    """TA_HEALTH_FORCE_RED: a live e2e's way to redden a resource without running its real probe
    or touching the shared credential."""

    def setUp(self):
        health.STATE.ensure_dir()
        for f in (health.STATE.dir / "runs.jsonl", health.HEALTH_FILE):
            if f.exists():
                f.unlink()

    def test_forced_resource_is_red_without_running_the_real_probe(self):
        reg = _registry({"claude-sub": {"probe": "true"}})
        with mock.patch.dict(os.environ, {"TA_HEALTH_FORCE_RED": "claude-sub"}):
            with mock.patch.object(health, "_run_probe_cmd") as probe:
                statuses = health.refresh(reg)
        self.assertEqual(statuses, {"claude-sub": "red"})
        probe.assert_not_called()

    def test_unforced_resource_probes_normally(self):
        reg = _registry({"claude-sub": {"probe": "true"}, "openrouter": {"probe": "true"}})
        with mock.patch.dict(os.environ, {"TA_HEALTH_FORCE_RED": "claude-sub"}):
            with mock.patch.object(health, "_run_probe_cmd", return_value=True) as probe:
                statuses = health.refresh(reg)
        self.assertEqual(statuses, {"claude-sub": "red", "openrouter": "green"})
        probe.assert_called_once()

    def test_force_red_bypasses_the_ttl_cache(self):
        reg = _registry({"claude-sub": {"probe": "true"}})
        with mock.patch.object(health, "_run_probe_cmd", return_value=True):
            health.refresh(reg)   # cached green
        with mock.patch.dict(os.environ, {"TA_HEALTH_FORCE_RED": "claude-sub"}):
            statuses = health.refresh(reg)   # must not reuse the still-fresh cached green
        self.assertEqual(statuses, {"claude-sub": "red"})

    def test_clearing_the_override_lets_the_real_probe_answer_again(self):
        reg = _registry({"claude-sub": {"probe": "true"}})
        with mock.patch.dict(os.environ, {"TA_HEALTH_FORCE_RED": "claude-sub"}):
            health.refresh(reg)
        with mock.patch.object(health, "_run_probe_cmd", return_value=True), \
             mock.patch.object(health, "PROBE_TTL_S", 0):
            statuses = health.refresh(reg)
        self.assertEqual(statuses, {"claude-sub": "green"})


class ResourceOfTest(unittest.TestCase):
    def test_known_profile_returns_its_resource(self):
        reg = _registry({"res-a": {"probe": "true"}},
                        {"p1": {"resource": "res-a", "adapter": "claude", "fallback": []}})
        self.assertEqual(health.resource_of("p1", reg), "res-a")

    def test_unknown_profile_returns_none(self):
        reg = _registry({}, {})
        self.assertIsNone(health.resource_of("nope", reg))


class ProbeClaudeSubTest(unittest.TestCase):
    """The real claude-sub probe: one haiku token through the `claude` CLI, mocked at
    subprocess.run so this never spends a real token or needs live credentials."""

    def test_exit_zero_is_green(self):
        with mock.patch.object(health.subprocess, "run",
                               return_value=subprocess.CompletedProcess([], 0)):
            self.assertTrue(health.probe_claude_sub())

    def test_nonzero_exit_is_red(self):
        with mock.patch.object(health.subprocess, "run",
                               return_value=subprocess.CompletedProcess([], 1)):
            self.assertFalse(health.probe_claude_sub())

    def test_timeout_is_red(self):
        with mock.patch.object(health.subprocess, "run",
                               side_effect=subprocess.TimeoutExpired("claude", 20)):
            self.assertFalse(health.probe_claude_sub())

    def test_missing_binary_is_red(self):
        with mock.patch.object(health.subprocess, "run", side_effect=FileNotFoundError()):
            self.assertFalse(health.probe_claude_sub())


class ProbeOpenrouterTest(unittest.TestCase):
    """The real openrouter probe: one 1-token completion against gemini-flash, mocked at
    urllib.request.urlopen and the key loader so this never spends real credit."""

    def setUp(self):
        p = mock.patch.object(health, "_read_openrouter_key", return_value="sk-or-v1-fake")
        p.start()
        self.addCleanup(p.stop)

    def test_missing_key_is_red_without_a_network_call(self):
        with mock.patch.object(health, "_read_openrouter_key", return_value=None):
            with mock.patch.object(health.urllib.request, "urlopen") as urlopen:
                self.assertFalse(health.probe_openrouter())
            urlopen.assert_not_called()

    def test_200_response_is_green(self):
        resp = mock.MagicMock()
        resp.status = 200
        resp.__enter__.return_value = resp
        with mock.patch.object(health.urllib.request, "urlopen", return_value=resp):
            self.assertTrue(health.probe_openrouter())

    def test_error_response_is_red(self):
        resp = mock.MagicMock()
        resp.status = 500
        resp.__enter__.return_value = resp
        with mock.patch.object(health.urllib.request, "urlopen", return_value=resp):
            self.assertFalse(health.probe_openrouter())

    def test_transport_error_is_red(self):
        with mock.patch.object(health.urllib.request, "urlopen", side_effect=OSError("no route")):
            self.assertFalse(health.probe_openrouter())


class RunBuiltinProbeTest(unittest.TestCase):
    def test_known_resource_dispatches(self):
        with mock.patch.object(health, "BUILTIN_PROBES", {"r1": lambda: True}):
            self.assertTrue(health.run_builtin_probe("r1"))

    def test_unknown_resource_raises_key_error(self):
        with self.assertRaises(KeyError):
            health.run_builtin_probe("no-such-resource")


if __name__ == "__main__":
    unittest.main()
