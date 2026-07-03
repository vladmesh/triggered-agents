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


class LaunchCmdTest(unittest.TestCase):
    """_launch_cmd against the real automation.toml files on disk (no mocked spec) — curator has
    no `head` field (bare claude), steward names claude-fable and must resolve it through
    pipeline.health/heads, falling back to the bare invocation if that resolution blows up."""

    def test_agent_without_head_field_gets_bare_claude(self):
        skill, cmd = dispatch._launch_cmd("curator")
        self.assertEqual(skill, "/curate")
        self.assertEqual(cmd, "claude --dangerously-skip-permissions /curate")

    def test_steward_head_resolves_through_pipeline_health_and_heads(self):
        from triggered_agents.agents.pipeline import health as pipeline_health

        with mock.patch.object(pipeline_health, "refresh", lambda: {"claude-sub": "green"}):
            skill, cmd = dispatch._launch_cmd("steward")
        self.assertEqual(skill, "/steward")
        self.assertIn("BOARD_ROLE=steward", cmd)
        self.assertIn("--model fable", cmd)

    def test_steward_head_falls_back_to_bare_claude_on_broken_resolution(self):
        from triggered_agents.agents.pipeline import health as pipeline_health

        with mock.patch.object(pipeline_health, "refresh", side_effect=RuntimeError("boom")):
            skill, cmd = dispatch._launch_cmd("steward")
        self.assertEqual(skill, "/steward")
        self.assertEqual(cmd, "claude --dangerously-skip-permissions /steward")

    def test_steward_head_falls_back_to_next_profile_when_resource_red(self):
        from triggered_agents.agents.pipeline import health as pipeline_health

        # claude-fable's chain is claude-opus (same claude-sub resource) then hermes-flash (a
        # different, openrouter resource) — see heads.toml, PR #38: the steward is the watcher of
        # last resort and must wake even when the whole claude-sub resource is red. With claude-sub
        # red and openrouter unmentioned (defaults green — heads.health.resolve_head), resolution
        # walks past claude-opus (same red resource) onto hermes-flash.
        with mock.patch.object(pipeline_health, "refresh", lambda: {"claude-sub": "red"}):
            skill, cmd = dispatch._launch_cmd("steward")
        self.assertIn("BOARD_ROLE=steward", cmd)
        self.assertIn("hermes", cmd)
        self.assertIn("google/gemini-2.5-flash", cmd)

    def test_steward_head_keeps_original_name_when_the_whole_chain_is_red(self):
        from triggered_agents.agents.pipeline import health as pipeline_health

        # Every resource claude-fable's chain can reach (claude-sub, openrouter) is red -> nothing
        # to fall back onto; _launch_cmd must keep the originally-named profile rather than
        # silently downgrading to no model at all.
        with mock.patch.object(pipeline_health, "refresh",
                               lambda: {"claude-sub": "red", "openrouter": "red"}):
            skill, cmd = dispatch._launch_cmd("steward")
        self.assertIn("--model fable", cmd)


if __name__ == "__main__":
    unittest.main()
