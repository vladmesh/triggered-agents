"""Unit/integration tests for the ephemeral, cleanup-only and stray-sweep lifecycle
runtime/dispatch.py grew for triggered-agents-445 — no network, no real Orca (same convention as
test_runtime_dispatch.py, split out here per PR #95 review B4 so neither file grows unreviewably
large in one PR).
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("TA_STATE", tempfile.mkdtemp(prefix="ta-dispatch-lifecycle-test-"))
os.environ.setdefault("TA_PIPELINE_STATE_DIR", tempfile.mkdtemp(prefix="ta-dispatch-lifecycle-live-state-test-"))

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root

from triggered_agents.runtime import dispatch  # noqa: E402
from tests._dispatch_fixtures import _DispatchBase  # noqa: E402


class EphemeralLifecycleTest(_DispatchBase):
    """triggered-agents-445: an agent whose spec sets `ephemeral = true` (curator) must never
    take the /clear-and-reuse path. An idle terminal means the previous tick's provider session
    already finished (successfully or not), so the whole workspace is torn down and replaced with
    a fresh one instead of being `/clear`-ed and re-sent into."""

    def setUp(self):
        super().setUp()
        p = mock.patch.object(dispatch, "_load_spec", lambda agent: {"ephemeral": True})
        p.start()
        self.addCleanup(p.stop)

    def test_idle_ephemeral_tears_down_instead_of_clear_reuse(self):
        self.terminals = [{"handle": "h1", "title": "✳ Claude Code", "lastOutputAt": 1000}]
        self.idle = True
        dispatch.run("agent")
        self.assertEqual(self.ready_calls, ["/ws/agent"])
        stop_calls = [c for c in self.orca_calls if c[:2] == ["terminal", "stop"]]
        create_calls = [c for c in self.orca_json_calls if c[:2] == ["terminal", "create"]]
        sent = [c for c in self.orca_calls if c[:2] == ["terminal", "send"]]
        self.assertEqual(len(stop_calls), 1)
        self.assertEqual(len(create_calls), 1)
        self.assertEqual(sent, [])  # never /clear, never sent into the old terminal
        self.assertEqual(self._logged_actions(), ["ephemeral-restart"])

    def test_idle_ephemeral_reaps_the_ghost_it_just_made(self):
        """Teardown must reap its own ghost right away, not leave it for the top of the next
        run — otherwise `session.tabs.listAll` would still show the finished run's tab for up to
        a whole tick interval."""
        self.terminals = [{"handle": "h1", "title": "✳ Claude Code", "lastOutputAt": 1000}]
        self.idle = True
        reap_calls = []
        with mock.patch.object(dispatch, "_reap_ghosts", lambda ws: reap_calls.append(ws) or 1):
            dispatch.run("agent")
        # top-of-run reap, then the ephemeral branch's own reap right after the stop
        self.assertEqual(reap_calls, ["/ws/agent", "/ws/agent"])

    def test_idle_ephemeral_bypasses_the_red_head_check(self):
        """A fresh spawn always re-resolves the head profile, so ephemeral teardown has nothing
        to divert from — it must not even consult `_reuse_head_is_red`."""
        self.terminals = [{"handle": "h1", "title": "✳ Claude Code", "lastOutputAt": 1000}]
        self.idle = True
        with mock.patch.object(dispatch, "_reuse_head_is_red") as red_check:
            dispatch.run("agent")
        red_check.assert_not_called()

    def test_busy_and_fresh_still_skips_without_touching_the_running_terminal(self):
        """Concurrency: a second tick landing while the first is still working must not spawn a
        second head or kill the still-running one, ephemeral or not."""
        self.terminals = [{"handle": "h1", "title": "✳ Claude Code", "lastOutputAt": 1000}]
        self.idle = False
        with mock.patch.object(dispatch, "_quiet_seconds", lambda t, now: 5.0):
            dispatch.run("agent")
        self.assertEqual(self.ready_calls, [])
        self.assertEqual(self.orca_calls, [])
        self.assertEqual(self._logged_actions(), ["busy-skip"])

    def test_busy_and_stuck_watchdog_restart_reaps_immediately(self):
        self.terminals = [{"handle": "h1", "title": "✳ Claude Code", "lastOutputAt": 1000}]
        self.idle = False
        reap_calls = []
        with mock.patch.object(dispatch, "_quiet_seconds", lambda t, now: dispatch.WATCHDOG_SECONDS + 1), \
             mock.patch.object(dispatch, "_reap_ghosts", lambda ws: reap_calls.append(ws) or 1):
            dispatch.run("agent")
        self.assertEqual(reap_calls, ["/ws/agent", "/ws/agent"])
        self.assertEqual(self._logged_actions(), ["watchdog-restart"])

    def test_fresh_create_when_no_terminal_is_unaffected_by_ephemeral(self):
        self.terminals = []
        dispatch.run("agent")
        self.assertEqual(self._logged_actions(), ["created"])


class StopConfirmFailureTest(_DispatchBase):
    """triggered-agents-445, PR #95 review B3 (round 1): `_orca`'s `terminal stop` doesn't
    surface its own return code, so every "stop, then create" path must re-list and confirm the
    workspace is actually clear before spawning a replacement — otherwise a silently-failed stop
    plus an unconditional create risks two live sessions for one singleton agent. Overrides the
    shared fixture's stop handling with one that never actually clears `self.terminals`,
    simulating a stop Orca reports success for but that doesn't really kill the pty."""

    def setUp(self):
        super().setUp()

        def fake_orca_stop_never_confirms(args):
            self.orca_calls.append(args)
            # deliberately does NOT clear self.terminals -- the stop "succeeds" but the
            # workspace still shows the old terminal on the next `terminal list`

        p = mock.patch.object(dispatch, "_orca", fake_orca_stop_never_confirms)
        p.start()
        self.addCleanup(p.stop)

    def test_watchdog_restart_bails_without_creating(self):
        self.terminals = [{"handle": "h1", "title": "✳ Claude Code", "lastOutputAt": 1000}]
        self.idle = False
        with mock.patch.object(dispatch, "_quiet_seconds", lambda t, now: dispatch.WATCHDOG_SECONDS + 1):
            dispatch.run("agent")
        create_calls = [c for c in self.orca_json_calls if c[:2] == ["terminal", "create"]]
        self.assertEqual(create_calls, [])
        self.assertEqual(self._logged_actions(), ["watchdog-stop-failed"])

    def test_ephemeral_idle_bails_without_creating(self):
        with mock.patch.object(dispatch, "_load_spec", lambda agent: {"ephemeral": True}):
            self.terminals = [{"handle": "h1", "title": "✳ Claude Code", "lastOutputAt": 1000}]
            self.idle = True
            dispatch.run("agent")
        create_calls = [c for c in self.orca_json_calls if c[:2] == ["terminal", "create"]]
        self.assertEqual(create_calls, [])
        self.assertEqual(self._logged_actions(), ["ephemeral-stop-failed"])

    def test_red_fallback_bails_without_creating(self):
        self.terminals = [{"handle": "h1", "title": "✳ Claude Code", "lastOutputAt": 1000}]
        self.idle = True
        with mock.patch.object(dispatch, "_reuse_head_is_red", lambda agent, state: True):
            dispatch.run("agent")
        create_calls = [c for c in self.orca_json_calls if c[:2] == ["terminal", "create"]]
        self.assertEqual(create_calls, [])
        self.assertEqual(self._logged_actions(), ["red-fallback-stop-failed"])


class CreateVisibilityGuardTest(_DispatchBase):
    """triggered-agents-445, PR #95 review B2 (round 1): a terminal this agent just created can
    take a moment to show up in `terminal list`. A second dispatch landing in that gap must not
    read "no terminal visible" as "nothing was ever spawned" and create a duplicate."""

    def test_second_dispatch_within_grace_skips_instead_of_duplicating(self):
        self.terminals = []
        dispatch.run("agent")  # tick 1: genuinely nothing there -> creates
        self.terminals = []    # tick 2 lands before Orca shows the new terminal -- still empty
        dispatch.run("agent")
        create_calls = [c for c in self.orca_json_calls if c[:2] == ["terminal", "create"]]
        self.assertEqual(len(create_calls), 1)  # still just the one from tick 1
        self.assertEqual(self._logged_actions(), ["created", "recent-create-guard"])

    def test_dispatch_after_grace_expires_creates_again(self):
        self.terminals = []
        dispatch.run("agent")
        later = time.time() + dispatch.CREATE_VISIBILITY_GRACE_S + 1
        with mock.patch("time.time", lambda: later):
            self.terminals = []
            dispatch.run("agent")
        create_calls = [c for c in self.orca_json_calls if c[:2] == ["terminal", "create"]]
        self.assertEqual(len(create_calls), 2)
        self.assertEqual(self._logged_actions(), ["created", "created"])

    def test_first_ever_dispatch_is_not_blocked_by_the_guard(self):
        self.terminals = []
        dispatch.run("agent")
        self.assertEqual(self._logged_actions(), ["created"])


class StraySweepTest(_DispatchBase):
    """triggered-agents-445, PR #95 review B3 (round 2): `_agent_terminals`'s title/handle filter
    can miss a genuinely live terminal that isn't recognized as this agent's own (an orphan stuck
    on the shell's default title, e.g. from a past onboarding hang) — `_agent_terminals` then
    returns `[]` even though Orca's raw `terminal list` for the workspace is not empty. An
    ephemeral agent must sweep that stray before creating a fresh terminal (normal dispatch) or
    declaring the workspace clean (cleanup-only), or it never actually converges accumulated live
    orphans to zero as the acceptance criterion requires."""

    def setUp(self):
        super().setUp()
        p = mock.patch.object(dispatch, "_load_spec", lambda agent: {"ephemeral": True})
        p.start()
        self.addCleanup(p.stop)
        # `_agent_terminals` (filtered) sees nothing, but the raw fixture list still holds an
        # unrecognized terminal -- model that gap directly rather than via the shared fixture's
        # `_orca_json`, which drives both the filtered and "raw" list off the same `self.terminals`.
        self.raw_terminals = [{"handle": "orphan", "title": "dev@host: ~/ws/agent", "lastOutputAt": 1}]

        def fake_orca_json(args):
            self.orca_json_calls.append(args)
            if args[:2] == ["terminal", "list"]:
                return {"terminals": self.raw_terminals}
            if args[:2] == ["terminal", "wait"]:
                return {"wait": {"satisfied": self.idle}}
            if args[:2] == ["terminal", "create"]:
                return {"terminal": {"handle": "new-terminal"}}
            return {}

        p = mock.patch.object(dispatch, "_orca_json", fake_orca_json)
        p.start()
        self.addCleanup(p.stop)

        def fake_orca(args):
            self.orca_calls.append(args)
            if args[:2] == ["terminal", "stop"]:
                self.raw_terminals = []

        p = mock.patch.object(dispatch, "_orca", fake_orca)
        p.start()
        self.addCleanup(p.stop)

    def test_normal_dispatch_sweeps_the_stray_before_creating(self):
        dispatch.run("agent")
        stop_calls = [c for c in self.orca_calls if c[:2] == ["terminal", "stop"]]
        create_calls = [c for c in self.orca_json_calls if c[:2] == ["terminal", "create"]]
        self.assertEqual(len(stop_calls), 1)
        self.assertEqual(len(create_calls), 1)
        self.assertEqual(self._logged_actions(), ["created"])

    def test_cleanup_only_sweeps_the_stray_without_creating(self):
        dispatch.run("agent", cleanup_only=True)
        stop_calls = [c for c in self.orca_calls if c[:2] == ["terminal", "stop"]]
        create_calls = [c for c in self.orca_json_calls if c[:2] == ["terminal", "create"]]
        self.assertEqual(len(stop_calls), 1)
        self.assertEqual(create_calls, [])
        self.assertEqual(self._logged_actions(), ["cleanup-stray-swept"])

    def test_normal_dispatch_bails_without_creating_when_sweep_not_confirmed(self):
        def fake_orca_never_clears(args):
            self.orca_calls.append(args)
            # deliberately does not clear self.raw_terminals

        with mock.patch.object(dispatch, "_orca", fake_orca_never_clears):
            dispatch.run("agent")
        create_calls = [c for c in self.orca_json_calls if c[:2] == ["terminal", "create"]]
        self.assertEqual(create_calls, [])
        self.assertEqual(self._logged_actions(), ["stray-sweep-failed"])

    def test_genuinely_empty_workspace_is_not_swept(self):
        """No stray at all (raw list actually empty) must not trigger a pointless stop/reap --
        only a genuinely non-empty raw list is worth sweeping."""
        self.raw_terminals = []
        dispatch.run("agent")
        stop_calls = [c for c in self.orca_calls if c[:2] == ["terminal", "stop"]]
        self.assertEqual(stop_calls, [])
        self.assertEqual(self._logged_actions(), ["created"])


class FinalizerTest(_DispatchBase):
    """triggered-agents-445, PR #95 review B1 (round 3): every dispatch.run() cleanup path
    (idle-ephemeral, watchdog, cleanup-only, stray-sweep) is still only ever REACHED by some
    future tick's poll. `_create_terminal` closes that gap for an ephemeral agent by appending a
    self-teardown trailer (`_with_finalizer`) to the head's own launch command, so the terminal
    tears itself down (`dispatch.finalize`) the instant the head process exits, with no tick
    involved at all."""

    def setUp(self):
        super().setUp()
        p = mock.patch.object(dispatch, "_load_spec", lambda agent: {"ephemeral": True})
        p.start()
        self.addCleanup(p.stop)

    def _created_command(self):
        create_calls = [c for c in self.orca_json_calls if c[:2] == ["terminal", "create"]]
        self.assertEqual(len(create_calls), 1)
        args = create_calls[0]
        return args[args.index("--command") + 1]

    def test_ephemeral_create_embeds_a_self_teardown_trailer(self):
        self.terminals = []
        dispatch.run("agent")
        command = self._created_command()
        self.assertIn("dispatch --finalize", command)
        # the trailer carries the terminal's generation identity (review B2, round 4): first
        # create of this agent in a fresh state dir is generation 1.
        self.assertIn("--finalize --generation 1", command)
        # `;`, not `&&` -- the trailer must run regardless of the head's own exit code
        self.assertIn("; ", command)
        self.assertLess(command.index("claude"), command.index("dispatch --finalize"))

    def test_non_ephemeral_create_gets_no_trailer(self):
        with mock.patch.object(dispatch, "_load_spec", lambda agent: {}):
            self.terminals = []
            dispatch.run("agent")
        command = self._created_command()
        self.assertNotIn("--finalize", command)

    def test_finalize_tears_down_after_stop_confirms(self):
        self.terminals = [{"handle": "h1", "title": "✳ Claude Code", "lastOutputAt": 1000}]
        reap_calls = []
        with mock.patch.object(dispatch, "_reap_ghosts", lambda ws: reap_calls.append(ws) or 1):
            dispatch.finalize("agent")
        stop_calls = [c for c in self.orca_calls if c[:2] == ["terminal", "stop"]]
        self.assertEqual(len(stop_calls), 1)
        self.assertEqual(reap_calls, ["/ws/agent"])
        self.assertEqual(self._logged_actions(), ["self-teardown"])

    def test_finalize_never_creates_a_terminal(self):
        self.terminals = [{"handle": "h1", "title": "✳ Claude Code", "lastOutputAt": 1000}]
        dispatch.finalize("agent")
        create_calls = [c for c in self.orca_json_calls if c[:2] == ["terminal", "create"]]
        self.assertEqual(create_calls, [])

    def test_finalize_logs_failure_without_raising_when_stop_not_confirmed(self):
        def fake_orca_never_clears(args):
            self.orca_calls.append(args)
            # deliberately does not clear self.terminals

        with mock.patch.object(dispatch, "_orca", fake_orca_never_clears):
            self.terminals = [{"handle": "h1", "title": "✳ Claude Code", "lastOutputAt": 1000}]
            dispatch.finalize("agent")
        self.assertEqual(self._logged_actions(), ["self-teardown-failed"])

    def test_finalize_is_a_noop_for_a_non_ephemeral_agent(self):
        with mock.patch.object(dispatch, "_load_spec", lambda agent: {}):
            dispatch.finalize("agent")
        self.assertEqual(self.orca_calls, [])
        self.assertEqual(self._logged_actions(), [])

    def test_finalize_defers_when_a_live_tick_holds_the_lock_for_the_whole_window(self):
        """triggered-agents-445, PR #95 review B2, round 4: a tick wedged on the lock for the whole
        bounded retry window means it is itself actively working this workspace -- finalize must NOT
        stop anything (a blanket stop could kill a replacement that tick created) and must hand off
        cleanly rather than raise. `time.sleep` is a no-op in the fixture, so the retry loop runs
        instantly and lands on the deferred branch."""
        import os

        state = dispatch.AgentState("agent")
        state.ensure_dir()
        fd = os.open(state.lockfile, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, b"12345")
        os.close(fd)
        try:
            rc = dispatch.finalize("agent")  # no SystemExit: deferred, not abandoned
        finally:
            state.lockfile.unlink()
        self.assertEqual(rc, 0)
        self.assertEqual(self.orca_calls, [])
        self.assertEqual(self._logged_actions(), ["self-teardown-deferred"])

    def test_finalize_supersedes_without_stopping_a_replacement(self):
        """The core B2 fix: a late finalizer carrying an OLD generation, arriving after a concurrent
        tick already replaced this agent's terminal (a newer generation is now recorded), must never
        blanket-stop the workspace -- that would kill the live replacement. It only reaps its own
        ghost tab."""
        state = dispatch.AgentState("agent")
        state.save_terminal_handle("replacement", created_at=time.time(), generation=5)
        self.terminals = [{"handle": "replacement", "title": "triggered-agent:agent", "lastOutputAt": 1}]
        reap_calls = []
        with mock.patch.object(dispatch, "_reap_ghosts", lambda ws: reap_calls.append(ws) or 0):
            dispatch.finalize("agent", generation=4)
        stop_calls = [c for c in self.orca_calls if c[:2] == ["terminal", "stop"]]
        self.assertEqual(stop_calls, [])                  # replacement never stopped
        self.assertEqual(self.terminals, [{"handle": "replacement",
                                           "title": "triggered-agent:agent", "lastOutputAt": 1}])
        self.assertEqual(reap_calls, ["/ws/agent"])       # own ghost still reaped
        self.assertEqual(self._logged_actions(), ["self-teardown-superseded"])

    def test_finalize_stops_when_its_generation_is_still_current(self):
        """The complement: when the workspace's live terminal is still this finalizer's own
        generation, it does tear it down."""
        state = dispatch.AgentState("agent")
        state.save_terminal_handle("h1", created_at=time.time(), generation=7)
        self.terminals = [{"handle": "h1", "title": "triggered-agent:agent", "lastOutputAt": 1}]
        with mock.patch.object(dispatch, "_reap_ghosts", lambda ws: 0):
            dispatch.finalize("agent", generation=7)
        stop_calls = [c for c in self.orca_calls if c[:2] == ["terminal", "stop"]]
        self.assertEqual(len(stop_calls), 1)
        self.assertEqual(self._logged_actions(), ["self-teardown"])

    def test_finalize_reports_failure_when_raw_list_is_unavailable(self):
        """triggered-agents-445, PR #95 review B1, round 4: if the confirm re-list itself fails
        (`_raw_terminal_count` returns None, not 0), finalize must log a failure and let the next
        tick retry -- never a healthy self-teardown over a pty that may still be live."""
        state = dispatch.AgentState("agent")
        state.save_terminal_handle("h1", created_at=time.time(), generation=1)
        self.terminals = [{"handle": "h1", "title": "triggered-agent:agent", "lastOutputAt": 1}]
        with mock.patch.object(dispatch, "_raw_terminal_count", lambda ws: None), \
             mock.patch.object(dispatch, "_reap_ghosts", lambda ws: 0):
            dispatch.finalize("agent", generation=1)
        self.assertEqual(self._logged_actions(), ["self-teardown-failed"])


class CleanupOnlyTest(_DispatchBase):
    """triggered-agents-445, PR #95 review B1 (round 1): `dispatch.run(..., cleanup_only=True)` is
    `ta-gate.sh`'s call on a precheck skip (no new work). It must never dispatch a skill or
    create a terminal, but for an ephemeral agent it must still tear down a finished or stuck
    terminal instead of waiting for a tick that happens to have real work."""

    def setUp(self):
        super().setUp()
        p = mock.patch.object(dispatch, "_load_spec", lambda agent: {"ephemeral": True})
        p.start()
        self.addCleanup(p.stop)

    def test_no_terminal_is_a_pure_noop(self):
        self.terminals = []
        dispatch.run("agent", cleanup_only=True)
        self.assertEqual(self.orca_calls, [])
        self.assertEqual([c for c in self.orca_json_calls if c[:2] == ["terminal", "create"]], [])
        self.assertEqual(self._logged_actions(), [])

    def test_idle_finished_terminal_is_torn_down_without_recreating(self):
        self.terminals = [{"handle": "h1", "title": "✳ Claude Code", "lastOutputAt": 1000}]
        self.idle = True
        dispatch.run("agent", cleanup_only=True)
        stop_calls = [c for c in self.orca_calls if c[:2] == ["terminal", "stop"]]
        create_calls = [c for c in self.orca_json_calls if c[:2] == ["terminal", "create"]]
        self.assertEqual(len(stop_calls), 1)
        self.assertEqual(create_calls, [])
        self.assertEqual(self._logged_actions(), ["cleanup-teardown"])

    def test_busy_and_fresh_is_left_untouched(self):
        self.terminals = [{"handle": "h1", "title": "✳ Claude Code", "lastOutputAt": 1000}]
        self.idle = False
        with mock.patch.object(dispatch, "_quiet_seconds", lambda t, now: 5.0):
            dispatch.run("agent", cleanup_only=True)
        self.assertEqual(self.orca_calls, [])
        self.assertEqual(self._logged_actions(), [])

    def test_busy_and_stuck_is_stopped_without_recreating(self):
        self.terminals = [{"handle": "h1", "title": "✳ Claude Code", "lastOutputAt": 1000}]
        self.idle = False
        with mock.patch.object(dispatch, "_quiet_seconds", lambda t, now: dispatch.WATCHDOG_SECONDS + 1):
            dispatch.run("agent", cleanup_only=True)
        stop_calls = [c for c in self.orca_calls if c[:2] == ["terminal", "stop"]]
        create_calls = [c for c in self.orca_json_calls if c[:2] == ["terminal", "create"]]
        self.assertEqual(len(stop_calls), 1)
        self.assertEqual(create_calls, [])
        self.assertEqual(self._logged_actions(), ["cleanup-watchdog-stop"])

    def test_non_ephemeral_agent_makes_zero_orca_calls(self):
        """triggered-agents-445, PR #95 review B2 (round 2): a non-ephemeral agent's precheck-skip
        must stay the exact zero-Orca-calls no-op it always was BEFORE ta-gate.sh started calling
        `--cleanup-only` on every skip -- no ghost reap, no `terminal list`, nothing. Otherwise an
        Orca hiccup on a quiet retro/steward tick (which never touched Orca before this card) can
        now fail a unit that used to cleanly no-op."""
        with mock.patch.object(dispatch, "_load_spec", lambda agent: {}):
            self.terminals = [{"handle": "h1", "title": "✳ Claude Code", "lastOutputAt": 1000}]
            self.idle = True
            dispatch.run("agent", cleanup_only=True)
        self.assertEqual(self.orca_calls, [])
        self.assertEqual(self.orca_json_calls, [])
        self.assertEqual(self._logged_actions(), [])


class RawListFailureTest(_DispatchBase):
    """triggered-agents-445, PR #95 review B1 (round 4): a failed or timed-out `terminal list` must
    read as "unknown", never as "zero terminals". `_raw_terminal_count` returns None on failure;
    every confirmation/decision path treats None distinctly from 0 so an Orca hiccup can't confirm a
    teardown that didn't happen or declare a workspace clean it couldn't actually read."""

    def setUp(self):
        super().setUp()
        p = mock.patch.object(dispatch, "_load_spec", lambda agent: {"ephemeral": True})
        p.start()
        self.addCleanup(p.stop)

    def test_raw_terminal_count_returns_none_on_list_error(self):
        def boom(args):
            raise RuntimeError("orca unreachable")

        with mock.patch.object(dispatch, "_orca_json", boom):
            self.assertIsNone(dispatch._raw_terminal_count("/ws/agent"))

    def test_stop_and_confirm_workspace_empty_is_false_when_list_unknown(self):
        with mock.patch.object(dispatch, "_raw_terminal_count", lambda ws: None), \
             mock.patch.object(dispatch, "_orca", lambda args: None):
            self.assertFalse(dispatch._stop_and_confirm_workspace_empty("/ws/agent"))

    def test_stop_and_confirm_workspace_empty_true_only_on_real_zero(self):
        with mock.patch.object(dispatch, "_raw_terminal_count", lambda ws: 0), \
             mock.patch.object(dispatch, "_orca", lambda args: None):
            self.assertTrue(dispatch._stop_and_confirm_workspace_empty("/ws/agent"))

    def test_create_branch_bails_when_raw_count_unknown(self):
        """`_agent_terminals` recognized nothing, but the raw list failed -- can't rule out a stray,
        so don't pile a fresh session on top of a workspace we couldn't read."""
        self.terminals = []
        with mock.patch.object(dispatch, "_raw_terminal_count", lambda ws: None):
            dispatch.run("agent")
        create_calls = [c for c in self.orca_json_calls if c[:2] == ["terminal", "create"]]
        self.assertEqual(create_calls, [])
        self.assertEqual(self._logged_actions(), ["stray-check-failed"])

    def test_cleanup_only_bails_when_raw_count_unknown(self):
        self.terminals = []
        with mock.patch.object(dispatch, "_raw_terminal_count", lambda ws: None):
            dispatch.run("agent", cleanup_only=True)
        self.assertEqual(self._logged_actions(), ["cleanup-stray-check-failed"])


class EphemeralTwoTickLifecycleTest(unittest.TestCase):
    """Integration-style test (still no network, no real Orca — this file's own convention)
    simulating two sequential curator ticks against an in-memory terminal+tab store that mirrors
    real Orca semantics closely enough to prove triggered-agents-445's actual contract: every
    tick's provider session is a brand new process, never `/clear`-reused, and the run that just
    finished leaves neither a live terminal nor a lingering session tab for the next tick to
    inherit."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._state_patch = mock.patch("triggered_agents.runtime.state.STATE_ROOT", Path(self.tmp.name))
        self._state_patch.start()
        self.addCleanup(self._state_patch.stop)

        self.ws = "/ws/curator"
        self.idle = True
        self._next_handle = 0
        self.live: dict[str, dict] = {}     # handle -> terminal dict; a "stop" empties this
        self.tabs: dict[str, str] = {}      # handle -> "ready" | "pending-handle"
        self.sent_texts: list[tuple[str, str]] = []  # every `terminal send`, proves /clear never fires
        self.created_handles: list[str] = []

        p = mock.patch.object(dispatch, "_load_spec", lambda agent: {"ephemeral": True})
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(dispatch, "_launch_cmd",
                              lambda agent, variant=None: ("/curate", "claude ... /curate", "fake-profile"))
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(dispatch, "_workspace", lambda agent: self.ws)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(dispatch, "_ensure_claude_ready", lambda ws: None)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch("time.sleep", lambda s: None)
        p.start()
        self.addCleanup(p.stop)

        def fake_orca_json(args):
            if args[:2] == ["terminal", "list"]:
                return {"terminals": list(self.live.values())}
            if args[:2] == ["terminal", "wait"]:
                return {"wait": {"satisfied": self.idle}}
            if args[:2] == ["terminal", "create"]:
                self._next_handle += 1
                handle = f"term-{self._next_handle}"
                term = {"handle": handle, "title": "triggered-agent:curator", "lastOutputAt": 1000}
                self.live[handle] = term
                self.tabs[handle] = "ready"
                self.created_handles.append(handle)
                return {"terminal": term}
            return {}

        p = mock.patch.object(dispatch, "_orca_json", fake_orca_json)
        p.start()
        self.addCleanup(p.stop)

        def fake_orca(args):
            if args[:2] == ["terminal", "stop"]:
                # kills every live terminal in this workspace; each one's tab lingers as a ghost
                for handle in list(self.live.keys()):
                    del self.live[handle]
                    self.tabs[handle] = "pending-handle"
            elif args[:2] == ["terminal", "send"]:
                handle = args[args.index("--terminal") + 1]
                text = args[args.index("--text") + 1]
                self.sent_texts.append((handle, text))

        p = mock.patch.object(dispatch, "_orca", fake_orca)
        p.start()
        self.addCleanup(p.stop)

        def fake_reap_ghosts(ws):
            closed = 0
            for handle, status in list(self.tabs.items()):
                if status != "ready":
                    del self.tabs[handle]
                    closed += 1
            return closed

        p = mock.patch.object(dispatch, "_reap_ghosts", fake_reap_ghosts)
        p.start()
        self.addCleanup(p.stop)

    def _actions(self):
        import json

        runs = dispatch.AgentState("curator").dir / "runs.jsonl"
        return [json.loads(line)["action"] for line in runs.read_text(encoding="utf-8").splitlines()]

    def test_two_sequential_ticks_never_share_a_session_and_leave_no_artifacts(self):
        dispatch.run("curator")
        self.assertEqual(len(self.created_handles), 1)
        first_handle = self.created_handles[0]
        self.assertEqual(list(self.live), [first_handle])
        self.assertEqual(self.tabs, {first_handle: "ready"})

        # the first tick's session "finishes": its terminal is idle again by the next tick
        self.idle = True
        dispatch.run("curator")

        self.assertEqual(len(self.created_handles), 2)
        second_handle = self.created_handles[1]
        self.assertNotEqual(first_handle, second_handle)  # distinct provider session identifiers

        # the finished run's own terminal/tab are gone -- only the fresh one for tick 2 remains
        self.assertEqual(list(self.live), [second_handle])
        self.assertEqual(self.tabs, {second_handle: "ready"})

        # no /clear, no send of any kind into a reused terminal -- no context marker survives
        # from tick 1 into tick 2; each tick's skill only ever arrives via `terminal create
        # --command`, never `terminal send`
        self.assertEqual(self.sent_texts, [])

        self.assertEqual(self._actions(), ["created", "ephemeral-restart"])

    def test_finalize_tears_down_immediately_after_head_exit_without_a_second_tick(self):
        """PR #95 review B1, round 3: the review's core, repeated objection is that every
        teardown path is only ever reached by a FUTURE tick's poll, never by the head's own
        completion. Prove the opposite here: after tick 1 creates a terminal, simulate the head
        process exiting by calling `dispatch.finalize` directly -- exactly what `_create_terminal`
        appends to the launch command as its own trailer -- with NO second `dispatch.run` tick
        anywhere in between, and assert the workspace reaches zero terminals/tabs from that call
        alone."""
        dispatch.run("curator")
        self.assertEqual(len(self.created_handles), 1)
        handle = self.created_handles[0]
        self.assertEqual(list(self.live), [handle])
        self.assertEqual(self.tabs, {handle: "ready"})

        # the head process exits; its own launch command's trailer runs finalize() with the
        # generation the trailer carries (the first create is generation 1) -- no dispatch.run()
        # tick is involved at all
        dispatch.finalize("curator", generation=1)

        self.assertEqual(self.live, {})
        self.assertEqual(self.tabs, {})
        self.assertEqual(len(self.created_handles), 1)  # finalize never creates anything
        self.assertEqual(self._actions(), ["created", "self-teardown"])

    def test_third_tick_with_no_new_work_leaves_literally_zero_terminals_and_tabs(self):
        """PR #95 review B4: proving "the old terminal was replaced by a new one" on every tick
        is a weaker claim than the acceptance criterion ("zero terminals/tabs after each
        completion") — a workspace that always has exactly one live terminal never actually hits
        zero anywhere in that test. Drive a third tick as `ta-gate.sh` would on a precheck skip
        (`cleanup_only=True`, no new work for curator) after tick 2 finishes, and assert the
        workspace is verifiably empty: no live terminal, no tab, of any status."""
        dispatch.run("curator")
        self.idle = True
        dispatch.run("curator")
        self.assertEqual(len(self.live), 1)
        self.assertEqual(len(self.tabs), 1)

        # tick 3: precheck found no new work, ta-gate.sh calls cleanup_only instead of a real
        # dispatch -- the second tick's session already finished (idle), so it gets torn down
        # and nothing new is created in its place
        self.idle = True
        dispatch.run("curator", cleanup_only=True)

        self.assertEqual(self.live, {})
        self.assertEqual(self.tabs, {})
        self.assertEqual(len(self.created_handles), 2)  # no third session was spawned
        self.assertEqual(self._actions(), ["created", "ephemeral-restart", "cleanup-teardown"])


class EphemeralSessionIdentityTest(unittest.TestCase):
    """triggered-agents-445, PR #95 review B3 (round 4): the acceptance criterion is stronger than
    "two ticks used different fake terminal handles". It requires two sequential curator runs to use
    DISTINCT provider/session identifiers, NO context marker from the first run to reach the second,
    and ZERO terminals/tabs after EACH completion — not just eventually.

    This models an in-memory Orca that mints a fresh provider session id and an empty context store
    on every `terminal create`, and a head that stamps a marker into its own session's context. The
    only path that could carry a marker forward is warm reuse (`terminal send` a `/clear` into the
    SAME terminal/session), which an ephemeral agent must never take — so proving the session ids
    differ, the second session's context never contains the first's marker, and `terminal send`
    never fired is the criterion itself, not a proxy. Each run's completion is driven by the head's
    own `finalize` trailer (with the generation that trailer carries), and the workspace is asserted
    empty after each one."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._state_patch = mock.patch("triggered_agents.runtime.state.STATE_ROOT", Path(self.tmp.name))
        self._state_patch.start()
        self.addCleanup(self._state_patch.stop)

        self.ws = "/ws/curator"
        self.idle = True
        self._seq = 0
        self.live: dict[str, dict] = {}          # handle -> terminal dict
        self.tabs: dict[str, str] = {}           # handle -> "ready" | "pending-handle"
        self.session_of: dict[str, str] = {}     # handle -> provider session id
        self.context: dict[str, set] = {}        # session id -> markers the head wrote into it
        self.sent_texts: list[tuple[str, str]] = []
        self.created_handles: list[str] = []

        for target, repl in [
            ("_load_spec", lambda agent: {"ephemeral": True}),
            ("_launch_cmd", lambda agent, variant=None: ("/curate", "claude ... /curate", "fake-profile")),
            ("_workspace", lambda agent: self.ws),
            ("_ensure_claude_ready", lambda ws: None),
        ]:
            p = mock.patch.object(dispatch, target, repl)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch("time.sleep", lambda s: None)
        p.start()
        self.addCleanup(p.stop)

        def fake_orca_json(args):
            if args[:2] == ["terminal", "list"]:
                return {"terminals": list(self.live.values())}
            if args[:2] == ["terminal", "wait"]:
                return {"wait": {"satisfied": self.idle}}
            if args[:2] == ["terminal", "create"]:
                # a fresh process => a brand new provider session with an empty context store,
                # exactly what launching `claude` anew (never `/clear`-reusing) gives you
                self._seq += 1
                handle = f"term-{self._seq}"
                session = f"sess-{self._seq}"
                self.live[handle] = {"handle": handle, "title": "triggered-agent:curator", "lastOutputAt": 1000}
                self.tabs[handle] = "ready"
                self.session_of[handle] = session
                self.context[session] = set()
                self.created_handles.append(handle)
                return {"terminal": self.live[handle]}
            return {}

        p = mock.patch.object(dispatch, "_orca_json", fake_orca_json)
        p.start()
        self.addCleanup(p.stop)

        def fake_orca(args):
            if args[:2] == ["terminal", "stop"]:
                for handle in list(self.live.keys()):
                    del self.live[handle]
                    self.tabs[handle] = "pending-handle"
            elif args[:2] == ["terminal", "send"]:
                handle = args[args.index("--terminal") + 1]
                text = args[args.index("--text") + 1]
                self.sent_texts.append((handle, text))
                # model warm reuse honestly: a `/clear` into a live terminal keeps its provider
                # session (and thus its identity) -- only wipes the visible transcript. If the
                # ephemeral path ever regressed into reuse, the session id would repeat and this
                # test's distinct-id assertion would catch it.

        p = mock.patch.object(dispatch, "_orca", fake_orca)
        p.start()
        self.addCleanup(p.stop)

        def fake_reap_ghosts(ws):
            closed = 0
            for handle, status in list(self.tabs.items()):
                if status != "ready":
                    del self.tabs[handle]
                    closed += 1
            return closed

        p = mock.patch.object(dispatch, "_reap_ghosts", fake_reap_ghosts)
        p.start()
        self.addCleanup(p.stop)

    def _run_and_stamp_marker(self):
        """One curator run: dispatch creates the terminal, then simulate the head stamping a marker
        into its own provider session's context. Returns (session_id, marker)."""
        dispatch.run("curator")
        handle = self.created_handles[-1]
        session = self.session_of[handle]
        marker = f"context-marker-{session}"
        self.context[session].add(marker)
        return session, marker

    def test_two_runs_use_distinct_sessions_with_no_marker_transfer_and_zero_after_each(self):
        # --- run 1 ---
        session1, marker1 = self._run_and_stamp_marker()
        self.assertEqual(len(self.live), 1)
        self.assertEqual(list(self.tabs.values()), ["ready"])

        # head 1 exits -> its own trailer runs finalize with the generation it carries (gen 1)
        dispatch.finalize("curator", generation=1)
        self.assertEqual(self.live, {})   # zero terminals after the FIRST completion
        self.assertEqual(self.tabs, {})   # zero tabs after the FIRST completion

        # --- run 2 ---
        session2, marker2 = self._run_and_stamp_marker()
        self.assertEqual(len(self.live), 1)
        self.assertEqual(list(self.tabs.values()), ["ready"])

        # head 2 exits -> finalize with gen 2
        dispatch.finalize("curator", generation=2)
        self.assertEqual(self.live, {})   # zero terminals after the SECOND completion
        self.assertEqual(self.tabs, {})   # zero tabs after the SECOND completion

        # distinct provider/session identifiers
        self.assertNotEqual(session1, session2)

        # no context marker from run 1 ever reached run 2's session
        self.assertNotIn(marker1, self.context[session2])
        self.assertEqual(self.context[session2], {marker2})

        # the skill only ever arrived via `terminal create --command`; nothing was ever sent into a
        # reused terminal, so there was no `/clear`-and-reuse that could carry context forward
        self.assertEqual(self.sent_texts, [])


if __name__ == "__main__":
    unittest.main()
