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

        p = mock.patch.object(dispatch, "_launch_cmd", lambda agent, variant=None: ("/skill", "claude ... /skill"))
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

    def test_variant_dispatch_logs_a_distinct_event_not_plain_dispatch(self):
        """triggered-agents-254: the deep-sweep timer's dispatch must be distinguishable in
        runs.jsonl from a regular signal-gated tick, not just another 'dispatch' line."""
        import json
        from triggered_agents.runtime.state import AgentState

        self.terminals = []
        dispatch.run("agent", "deep-sweep")
        runs = AgentState("agent").dir / "runs.jsonl"
        events = [json.loads(l)["event"] for l in runs.read_text(encoding="utf-8").splitlines()]
        self.assertIn("deep-sweep", events)
        self.assertNotIn("dispatch", events)

    def test_plain_dispatch_still_logs_dispatch_event(self):
        import json
        from triggered_agents.runtime.state import AgentState

        self.terminals = []
        dispatch.run("agent")
        runs = AgentState("agent").dir / "runs.jsonl"
        events = [json.loads(l)["event"] for l in runs.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(events, ["dispatch"])

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

    def test_steward_deep_sweep_variant_resolves_its_own_skill_through_the_same_head(self):
        from triggered_agents.agents.pipeline import health as pipeline_health

        with mock.patch.object(pipeline_health, "refresh", lambda: {"claude-sub": "green"}):
            skill, cmd = dispatch._launch_cmd("steward", "deep-sweep")
        self.assertEqual(skill, "/steward deep-sweep")
        self.assertIn("BOARD_ROLE=steward", cmd)
        self.assertIn("--model fable", cmd)

    def test_steward_head_keeps_original_name_when_the_whole_chain_is_red(self):
        from triggered_agents.agents.pipeline import health as pipeline_health

        # Every resource claude-fable's chain can reach (claude-sub, openrouter) is red -> nothing
        # to fall back onto; _launch_cmd must keep the originally-named profile rather than
        # silently downgrading to no model at all.
        with mock.patch.object(pipeline_health, "refresh",
                               lambda: {"claude-sub": "red", "openrouter": "red"}):
            skill, cmd = dispatch._launch_cmd("steward")
        self.assertIn("--model fable", cmd)

    def test_card_ref_is_appended_to_the_skill_before_rendering(self):
        """triggered-agents-255: the ref must land INSIDE the reprd prompt, not tacked onto the
        rendered command afterward where it could fall outside the quoted argument."""
        from triggered_agents.agents.pipeline import health as pipeline_health

        with mock.patch.object(pipeline_health, "refresh", lambda: {"claude-sub": "green"}):
            skill, cmd = dispatch._launch_cmd("steward", card_ref="triggered-agents-260")
        self.assertEqual(skill, "/steward --card triggered-agents-260")
        self.assertIn(repr(skill), cmd)

    def test_card_ref_with_deep_sweep_variant(self):
        from triggered_agents.agents.pipeline import health as pipeline_health

        with mock.patch.object(pipeline_health, "refresh", lambda: {"claude-sub": "green"}):
            skill, cmd = dispatch._launch_cmd("steward", "deep-sweep", card_ref="triggered-agents-261")
        self.assertEqual(skill, "/steward deep-sweep --card triggered-agents-261")

    def test_no_card_ref_leaves_skill_unchanged(self):
        skill, cmd = dispatch._launch_cmd("curator")
        self.assertEqual(skill, "/curate")


class StewardReportCardDispatchTest(unittest.TestCase):
    """dispatch.run() for the steward specifically (triggered-agents-255): a real dispatch must
    create the wake-up report card and embed its reference in the skill text sent to the head; a
    busy-skip tick must create none at all (nothing is being dispatched to close it)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._state_patch = mock.patch("triggered_agents.runtime.state.STATE_ROOT", Path(self.tmp.name))
        self._state_patch.start()
        self.addCleanup(self._state_patch.stop)

        self.terminals = []
        self.idle = True
        self.orca_calls = []
        self.created_cards = []

        def fake_create_report_card(project, title, slug, description=""):
            ref = f"triggered-agents-{len(self.created_cards) + 1}"
            self.created_cards.append({"project": project, "title": title, "slug": slug})
            return {"action": "created", "id": len(self.created_cards), "reference": ref,
                    "column": "In progress"}

        from triggered_agents.agents.pipeline import ops as pipeline_ops
        p = mock.patch.object(pipeline_ops, "create_report_card", fake_create_report_card)
        p.start()
        self.addCleanup(p.stop)

        p = mock.patch.object(dispatch, "_workspace", lambda agent: "/ws/steward")
        p.start()
        self.addCleanup(p.stop)

        p = mock.patch.object(dispatch, "_reap_ghosts", lambda ws: 0)
        p.start()
        self.addCleanup(p.stop)

        def fake_orca_json(args):
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

        p = mock.patch.object(dispatch, "_ensure_claude_ready", lambda ws: None)
        p.start()
        self.addCleanup(p.stop)

        p = mock.patch("time.sleep", lambda s: None)
        p.start()
        self.addCleanup(p.stop)

        # Keep _launch_cmd off the real head-resolution machinery entirely, same reasoning as
        # LaunchCmdTest's broken-resolution case — this class only cares about the report-card
        # wiring, not head resolution.
        from triggered_agents.agents.pipeline import health as pipeline_health
        p = mock.patch.object(pipeline_health, "refresh", side_effect=RuntimeError("not under test"))
        p.start()
        self.addCleanup(p.stop)

    def _launch_command_sent(self):
        creates = [c for c in self.orca_calls if c[:2] == ["terminal", "create"]]
        self.assertEqual(len(creates), 1)
        return creates[0][creates[0].index("--command") + 1]

    def _skill_text_sent(self):
        sends = [c for c in self.orca_calls if c[:2] == ["terminal", "send"] and c[-2] != "/clear"]
        self.assertEqual(len(sends), 1)
        return sends[0][sends[0].index("--text") + 1]

    def test_fresh_dispatch_creates_a_report_card_and_embeds_its_ref(self):
        self.terminals = []
        dispatch.run("steward")
        self.assertEqual(len(self.created_cards), 1)
        self.assertEqual(self.created_cards[0]["project"], "triggered-agents")
        self.assertIn("--card triggered-agents-1", self._launch_command_sent())

    def test_deep_sweep_variant_names_itself_in_the_card_title(self):
        self.terminals = []
        dispatch.run("steward", "deep-sweep")
        self.assertEqual(len(self.created_cards), 1)
        self.assertIn("deep-sweep sweep", self.created_cards[0]["title"])
        self.assertIn("--card triggered-agents-1", self._launch_command_sent())

    def test_busy_and_fresh_skip_creates_no_card(self):
        self.terminals = [{"handle": "h1", "title": "✳ Claude Code", "lastOutputAt": 1000}]
        self.idle = False
        with mock.patch.object(dispatch, "_quiet_seconds", lambda t, now: 5.0):
            dispatch.run("steward")
        self.assertEqual(self.created_cards, [])

    def test_watchdog_restart_creates_a_card(self):
        self.terminals = [{"handle": "h1", "title": "✳ Claude Code", "lastOutputAt": 1000}]
        self.idle = False
        with mock.patch.object(dispatch, "_quiet_seconds", lambda t, now: dispatch.WATCHDOG_SECONDS + 1):
            dispatch.run("steward")
        self.assertEqual(len(self.created_cards), 1)
        self.assertIn("--card triggered-agents-1", self._launch_command_sent())

    def test_idle_reuse_creates_a_card_and_sends_it_in_the_skill_text(self):
        self.terminals = [{"handle": "h1", "title": "✳ Claude Code", "lastOutputAt": 1000}]
        self.idle = True
        dispatch.run("steward")
        self.assertEqual(len(self.created_cards), 1)
        self.assertIn("--card triggered-agents-1", self._skill_text_sent())


if __name__ == "__main__":
    unittest.main()
