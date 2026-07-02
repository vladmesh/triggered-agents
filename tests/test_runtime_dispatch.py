"""Unit tests for the singleton terminal driver (runtime/dispatch.py) — no network, no Orca.

Focus: every path that spawns a NEW claude process (fresh create, watchdog restart) must
pre-answer folder trust + the onboarding theme picker first (`_ensure_claude_ready`), so a fresh
head never lands on an interactive prompt and becomes an orphan invisible to the "Claude"-in-
title match. Warm reuse sends into an already-answered terminal, so it must not re-run prep.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ["TA_STATE"] = tempfile.mkdtemp(prefix="ta-runtime-dispatch-test-")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root

from triggered_agents.runtime import dispatch  # noqa: E402


class _DispatchBase(unittest.TestCase):
    """Fakes every Orca touch dispatch.run() makes; records the ones a test cares about."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._state_patch = mock.patch("triggered_agents.runtime.state.STATE_ROOT", Path(self.tmp.name))
        self._state_patch.start()
        self.addCleanup(self._state_patch.stop)

        self.terminals = []          # terminal list dispatch sees for the workspace
        self.orca_json_calls = []
        self.orca_calls = []
        self.ready_calls = []        # (workspace,) each time _ensure_claude_ready runs

        p = mock.patch.object(dispatch, "_launch_cmd", lambda agent: ("/skill", "claude ... /skill"))
        p.start()
        self.addCleanup(p.stop)

        p = mock.patch.object(dispatch, "_workspace", lambda agent: "/ws/agent")
        p.start()
        self.addCleanup(p.stop)

        p = mock.patch.object(dispatch, "_reap_ghosts", lambda ws: 0)
        p.start()
        self.addCleanup(p.stop)

        def fake_orca_json(args):
            self.orca_json_calls.append(args)
            if args[:2] == ["terminal", "list"]:
                return {"terminals": self.terminals}
            if args[:2] == ["terminal", "wait"]:
                return {"wait": {"satisfied": self.idle}}
            return {}

        p = mock.patch.object(dispatch, "_orca_json", fake_orca_json)
        p.start()
        self.addCleanup(p.stop)

        p = mock.patch.object(dispatch, "_orca", lambda args: self.orca_calls.append(args))
        p.start()
        self.addCleanup(p.stop)

        p = mock.patch.object(dispatch, "_ensure_claude_ready",
                              lambda ws: self.ready_calls.append(ws))
        p.start()
        self.addCleanup(p.stop)

        p = mock.patch("time.sleep", lambda s: None)
        p.start()
        self.addCleanup(p.stop)

        self.idle = True


class DispatchRunTest(_DispatchBase):
    def test_fresh_create_ensures_claude_ready_first(self):
        self.terminals = []
        dispatch.run("agent")
        self.assertEqual(self.ready_calls, ["/ws/agent"])
        create_calls = [c for c in self.orca_calls if c[:2] == ["terminal", "create"]]
        self.assertEqual(len(create_calls), 1)

    def test_idle_reuse_does_not_re_run_prep(self):
        self.terminals = [{"handle": "h1", "title": "✳ Claude Code", "lastOutputAt": 1000}]
        self.idle = True
        dispatch.run("agent")
        self.assertEqual(self.ready_calls, [])
        sent = [c for c in self.orca_calls if c[:2] == ["terminal", "send"]]
        self.assertEqual(len(sent), 2)  # /clear, then the skill

    def test_busy_and_fresh_skips_dispatch_without_prep(self):
        self.terminals = [{"handle": "h1", "title": "✳ Claude Code", "lastOutputAt": 1000}]
        self.idle = False
        with mock.patch.object(dispatch, "_quiet_seconds", lambda t, now: 5.0):
            dispatch.run("agent")
        self.assertEqual(self.ready_calls, [])
        self.assertEqual(self.orca_calls, [])

    def test_busy_and_stuck_watchdog_restarts_with_prep(self):
        self.terminals = [{"handle": "h1", "title": "✳ Claude Code", "lastOutputAt": 1000}]
        self.idle = False
        with mock.patch.object(dispatch, "_quiet_seconds", lambda t, now: dispatch.WATCHDOG_SECONDS + 1):
            dispatch.run("agent")
        self.assertEqual(self.ready_calls, ["/ws/agent"])
        stop_calls = [c for c in self.orca_calls if c[:2] == ["terminal", "stop"]]
        create_calls = [c for c in self.orca_calls if c[:2] == ["terminal", "create"]]
        self.assertEqual(len(stop_calls), 1)
        self.assertEqual(len(create_calls), 1)

    def test_agent_terminal_filter_ignores_onboarding_stuck_terminal(self):
        """A head stuck at the onboarding picker keeps the shell's default title, not
        'Claude' — exactly the orphan found live in the curator workspace. Without a real
        agent terminal, dispatch must treat the workspace as empty and create fresh."""
        self.terminals = [{"handle": "stuck", "title": "dev@host: ~/ws", "lastOutputAt": 999}]
        dispatch.run("agent")
        self.assertEqual(self.ready_calls, ["/ws/agent"])
        create_calls = [c for c in self.orca_calls if c[:2] == ["terminal", "create"]]
        self.assertEqual(len(create_calls), 1)


if __name__ == "__main__":
    unittest.main()
