"""Unit tests for triggered_agents.__main__'s `dispatch` argv parsing (triggered-agents-445):
`--cleanup-only` must reach `dispatch.run` as a flag, never as the variant positional, and must
compose correctly with a real variant like the steward's "deep-sweep". Also covers the `pipeline`
special case, which routes to the deterministic dispatcher instead of `dispatch.run` (PR #95
review B1, round 2): `--cleanup-only` there must be a genuine no-op, not fall through to a full
`dispatcher.tick()` just because the pipeline branch didn't know about the flag. The ephemeral
launch trailer uses `--spawn-finalizer`, which must route to the detached-helper launcher rather
than `dispatch.run`; the helper itself then invokes `--finalize`.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root

from triggered_agents import __main__ as ta_main  # noqa: E402
from triggered_agents.runtime import dispatch  # noqa: E402


class DispatchArgvParsingTest(unittest.TestCase):
    def setUp(self):
        self.calls = []
        p = mock.patch.object(dispatch, "run", lambda *a, **kw: self.calls.append((a, kw)) or 0)
        p.start()
        self.addCleanup(p.stop)

    def test_plain_dispatch_has_no_variant_and_is_not_cleanup_only(self):
        ta_main.main(["curator", "dispatch"])
        self.assertEqual(self.calls, [(("curator", None), {"cleanup_only": False})])

    def test_variant_is_passed_through(self):
        ta_main.main(["steward", "dispatch", "deep-sweep"])
        self.assertEqual(self.calls, [(("steward", "deep-sweep"), {"cleanup_only": False})])

    def test_cleanup_only_flag_is_not_read_as_a_variant(self):
        ta_main.main(["curator", "dispatch", "--cleanup-only"])
        self.assertEqual(self.calls, [(("curator", None), {"cleanup_only": True})])

    def test_cleanup_only_flag_composes_with_a_variant_in_either_order(self):
        ta_main.main(["steward", "dispatch", "--cleanup-only", "deep-sweep"])
        self.assertEqual(self.calls, [(("steward", "deep-sweep"), {"cleanup_only": True})])
        self.calls.clear()
        ta_main.main(["steward", "dispatch", "deep-sweep", "--cleanup-only"])
        self.assertEqual(self.calls, [(("steward", "deep-sweep"), {"cleanup_only": True})])


class PipelineDispatchCleanupOnlyTest(unittest.TestCase):
    """triggered-agents-445, PR #95 review B1 (round 2): ta-gate.sh now sends `--cleanup-only` to
    every agent on a precheck skip, including `pipeline` — a deterministic dispatcher with no
    terminal/PTY lifecycle at all. Before this fix the `pipeline` special case in `__main__.main`
    ignored the flag entirely and always called `dispatcher.tick()`, turning a quiet skip tick
    (every 3 minutes) into a full reconcile/advance/validate/claim pass."""

    def setUp(self):
        self.tick_calls = 0

        def fake_tick():
            self.tick_calls += 1
            return 0

        from triggered_agents.agents.pipeline import dispatcher
        p = mock.patch.object(dispatcher, "tick", fake_tick)
        p.start()
        self.addCleanup(p.stop)

    def test_cleanup_only_never_runs_a_dispatcher_tick(self):
        rc = ta_main.main(["pipeline", "dispatch", "--cleanup-only"])
        self.assertEqual(rc, 0)
        self.assertEqual(self.tick_calls, 0)

    def test_plain_dispatch_still_runs_a_tick_as_before(self):
        rc = ta_main.main(["pipeline", "dispatch"])
        self.assertEqual(rc, 0)
        self.assertEqual(self.tick_calls, 1)

    def test_finalize_never_runs_a_dispatcher_tick_either(self):
        rc = ta_main.main(["pipeline", "dispatch", "--finalize"])
        self.assertEqual(rc, 0)
        self.assertEqual(self.tick_calls, 0)

    def test_spawn_finalizer_never_runs_a_dispatcher_tick_either(self):
        rc = ta_main.main(["pipeline", "dispatch", "--spawn-finalizer"])
        self.assertEqual(rc, 0)
        self.assertEqual(self.tick_calls, 0)


class FinalizeArgvRoutingTest(unittest.TestCase):
    """triggered-agents-445, PR #95 review B1 (round 3): `--finalize` must route to
    `dispatch.finalize`, never to `dispatch.run` -- it isn't a dispatch decision, it's the
    self-teardown trailer a launch command runs against itself."""

    def setUp(self):
        self.finalize_calls = []
        self.spawn_calls = []
        self.run_calls = []
        p = mock.patch.object(dispatch, "finalize",
                              lambda agent, generation=None: self.finalize_calls.append((agent, generation)) or 0)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(dispatch, "spawn_finalizer",
                              lambda agent, generation=None: self.spawn_calls.append((agent, generation)) or 0)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(dispatch, "run", lambda *a, **kw: self.run_calls.append((a, kw)) or 0)
        p.start()
        self.addCleanup(p.stop)

    def test_finalize_flag_routes_to_dispatch_finalize_not_run(self):
        ta_main.main(["curator", "dispatch", "--finalize"])
        self.assertEqual(self.finalize_calls, [("curator", None)])
        self.assertEqual(self.run_calls, [])

    def test_finalize_generation_is_parsed_and_passed_through(self):
        """The self-teardown trailer carries `--generation <n>` (review B2, round 4); the CLI must
        parse it as an int and hand it to `dispatch.finalize` so the finalizer can tell its own
        terminal apart from a replacement."""
        ta_main.main(["curator", "dispatch", "--finalize", "--generation", "7"])
        self.assertEqual(self.finalize_calls, [("curator", 7)])
        self.assertEqual(self.run_calls, [])

    def test_spawn_finalizer_routes_to_the_detached_helper_launcher(self):
        ta_main.main(["curator", "dispatch", "--spawn-finalizer", "--generation", "7"])
        self.assertEqual(self.spawn_calls, [("curator", 7)])
        self.assertEqual(self.finalize_calls, [])
        self.assertEqual(self.run_calls, [])

    def test_plain_dispatch_still_routes_to_run_not_finalize(self):
        ta_main.main(["curator", "dispatch"])
        self.assertEqual(self.finalize_calls, [])
        self.assertEqual(len(self.run_calls), 1)


if __name__ == "__main__":
    unittest.main()
