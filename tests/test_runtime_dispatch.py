"""Unit tests for the singleton terminal driver (runtime/dispatch.py) — no network, no Orca.

Focus: every path that spawns a NEW process (fresh create, watchdog restart) must
pre-answer folder trust + the onboarding theme picker first (`_ensure_claude_ready`), so a fresh
Claude head never lands on an interactive prompt and becomes an orphan invisible to the terminal
match. Warm reuse sends into an already-answered terminal, so it must not re-run prep.
"""
from __future__ import annotations

import os
import shlex
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ["TA_STATE"] = tempfile.mkdtemp(prefix="ta-runtime-dispatch-test-")
os.environ["TA_PIPELINE_STATE_DIR"] = tempfile.mkdtemp(prefix="ta-runtime-dispatch-live-state-test-")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root

from triggered_agents.runtime import dispatch  # noqa: E402


def _inner_shell(cmd: str) -> str:
    parts = shlex.split(cmd)
    idx = parts.index("--")
    assert parts[idx + 1:idx + 3] == ["/bin/sh", "-lc"]
    return parts[idx + 3]


def _role(cmd: str) -> str:
    parts = shlex.split(cmd)
    return parts[parts.index("--role") + 1]


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

        p = mock.patch.object(dispatch, "_launch_cmd",
                              lambda agent, variant=None: ("/skill", "claude ... /skill", "fake-profile"))
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
            if args[:2] == ["terminal", "create"]:
                return {"terminal": {"handle": "new-terminal"}}
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

    def _logged_actions(self):
        import json

        runs = dispatch.AgentState("agent").dir / "runs.jsonl"
        if not runs.is_file():
            return []
        return [json.loads(l).get("action") for l in runs.read_text(encoding="utf-8").splitlines()]


class DispatchRunTest(_DispatchBase):
    def test_fresh_create_ensures_claude_ready_first(self):
        self.terminals = []
        dispatch.run("agent")
        self.assertEqual(self.ready_calls, ["/ws/agent"])
        create_calls = [c for c in self.orca_json_calls if c[:2] == ["terminal", "create"]]
        self.assertEqual(len(create_calls), 1)
        self.assertIn("--title", create_calls[0])
        self.assertIn("triggered-agent:agent", create_calls[0])

    def test_fresh_create_records_resolved_profile(self):
        """triggered-agents-275: idle-reuse needs to know which profile a live terminal is
        actually running on, since it never re-resolves on its own -- recorded here at spawn."""
        self.terminals = []
        dispatch.run("agent")
        self.assertEqual(dispatch.AgentState("agent").load_head_profile(), "fake-profile")
        self.assertEqual(dispatch.AgentState("agent").load_terminal_handle(), "new-terminal")

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

    def test_idle_reuse_finds_codex_terminal_by_saved_handle_after_title_changes(self):
        dispatch.AgentState("agent").save_head_profile("fake-profile")
        dispatch.AgentState("agent").save_terminal_handle("h1")
        self.terminals = [{"handle": "h1", "title": "dev@host: ~/ws/agent", "lastOutputAt": 1000}]
        self.idle = True
        with mock.patch.object(dispatch, "_reuse_head_is_red", lambda agent, state: False):
            dispatch.run("agent")
        self.assertEqual(self.ready_calls, [])
        create_calls = [c for c in self.orca_json_calls if c[:2] == ["terminal", "create"]]
        self.assertEqual(create_calls, [])
        sent = [c for c in self.orca_calls if c[:2] == ["terminal", "send"]]
        self.assertEqual(len(sent), 2)

    def test_idle_reuse_diverts_to_fresh_terminal_when_head_is_red(self):
        """triggered-agents-274/275: a warm idle terminal keeps whatever profile it was spawned
        with, so a resource gone red since spawn must not receive the skill there. Dispatch must
        stop the red idle terminal and start a fresh one on the resolved fallback instead — the
        same shape as a watchdog restart — rather than leaving the dead terminal running
        alongside a new one (which would pile up one extra terminal per red tick)."""
        self.terminals = [{"handle": "h1", "title": "✳ Claude Code", "lastOutputAt": 1000}]
        self.idle = True
        with mock.patch.object(dispatch, "_reuse_head_is_red", lambda agent, state: True):
            dispatch.run("agent")
        self.assertEqual(self.ready_calls, ["/ws/agent"])
        stop_calls = [c for c in self.orca_calls if c[:2] == ["terminal", "stop"]]
        create_calls = [c for c in self.orca_json_calls if c[:2] == ["terminal", "create"]]
        self.assertEqual(len(stop_calls), 1)
        self.assertEqual(len(create_calls), 1)
        sent = [c for c in self.orca_calls if c[:2] == ["terminal", "send"]]
        self.assertEqual(sent, [])
        self.assertEqual(self._logged_actions(), ["reused-red-fallback"])
        self.assertEqual(dispatch.AgentState("agent").load_head_profile(), "fake-profile")

    def test_idle_reuse_stays_warm_when_head_is_green(self):
        """Counterpart of the red case above: a green head keeps the existing warm-reuse
        behavior — no extra terminal spawned, the idle one gets /clear + the skill."""
        self.terminals = [{"handle": "h1", "title": "✳ Claude Code", "lastOutputAt": 1000}]
        self.idle = True
        with mock.patch.object(dispatch, "_reuse_head_is_red", lambda agent, state: False):
            dispatch.run("agent")
        self.assertEqual(self.ready_calls, [])
        stop_calls = [c for c in self.orca_calls if c[:2] == ["terminal", "stop"]]
        create_calls = [c for c in self.orca_json_calls if c[:2] == ["terminal", "create"]]
        self.assertEqual(stop_calls, [])
        self.assertEqual(create_calls, [])
        sent = [c for c in self.orca_calls if c[:2] == ["terminal", "send"]]
        self.assertEqual(len(sent), 2)  # /clear, then the skill
        self.assertEqual(self._logged_actions(), ["reused"])

    def test_plain_idle_reuse_does_not_touch_recorded_profile(self):
        """A plain warm reuse doesn't spawn anything, so the profile the terminal is actually
        running on hasn't changed — dispatch must not overwrite what was recorded at its last
        create/restart."""
        dispatch.AgentState("agent").save_head_profile("already-running-profile")
        self.terminals = [{"handle": "h1", "title": "✳ Claude Code", "lastOutputAt": 1000}]
        self.idle = True
        with mock.patch.object(dispatch, "_reuse_head_is_red", lambda agent, state: False):
            dispatch.run("agent")
        self.assertEqual(dispatch.AgentState("agent").load_head_profile(), "already-running-profile")

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
        create_calls = [c for c in self.orca_json_calls if c[:2] == ["terminal", "create"]]
        self.assertEqual(len(stop_calls), 1)
        self.assertEqual(len(create_calls), 1)
        self.assertEqual(dispatch.AgentState("agent").load_head_profile(), "fake-profile")

    def test_agent_terminal_filter_ignores_onboarding_stuck_terminal(self):
        """A head stuck at the onboarding picker keeps the shell's default title, not
        'Claude' — exactly the orphan found live in the curator workspace. Without a real
        agent terminal, dispatch must treat the workspace as empty and create fresh."""
        self.terminals = [{"handle": "stuck", "title": "dev@host: ~/ws", "lastOutputAt": 999}]
        dispatch.run("agent")
        self.assertEqual(self.ready_calls, ["/ws/agent"])
        create_calls = [c for c in self.orca_json_calls if c[:2] == ["terminal", "create"]]
        self.assertEqual(len(create_calls), 1)


class PipelinePauseGateTest(_DispatchBase):
    """triggered-agents-281: steward/curator/retro dispatch must not spend a token while the
    pipeline is paused, in either mode — none of them carry an in-flight card the way a worker/
    reviewer head does, so there is no "let it finish" case here. Patches dispatch._pipeline_paused
    directly (rather than writing a real pause.json) so this stays independent of which
    agents.pipeline.pause.STATE happens to already be bound in a shared test process."""

    def test_paused_skips_every_dispatch_branch(self):
        self.terminals = []
        with mock.patch.object(dispatch, "_pipeline_paused", lambda: True):
            dispatch.run("agent")
        self.assertEqual(self.ready_calls, [])
        self.assertEqual(self.orca_calls, [])

    def test_paused_logs_a_distinct_action_not_created(self):
        import json

        from triggered_agents.runtime.state import AgentState

        self.terminals = []
        with mock.patch.object(dispatch, "_pipeline_paused", lambda: True):
            dispatch.run("agent")
        runs = AgentState("agent").dir / "runs.jsonl"
        actions = [json.loads(l)["action"] for l in runs.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(actions, ["paused"])

    def test_paused_skips_even_a_busy_stuck_watchdog_restart(self):
        self.terminals = [{"handle": "h1", "title": "✳ Claude Code", "lastOutputAt": 1000}]
        self.idle = False
        with mock.patch.object(dispatch, "_quiet_seconds", lambda t, now: dispatch.WATCHDOG_SECONDS + 1), \
             mock.patch.object(dispatch, "_pipeline_paused", lambda: True):
            dispatch.run("agent")
        self.assertEqual(self.orca_calls, [])

    def test_not_paused_dispatches_normally(self):
        self.terminals = []
        with mock.patch.object(dispatch, "_pipeline_paused", lambda: False):
            dispatch.run("agent")
        create_calls = [c for c in self.orca_json_calls if c[:2] == ["terminal", "create"]]
        self.assertEqual(len(create_calls), 1)


class LaunchCmdTest(unittest.TestCase):
    """_launch_cmd against the real automation.toml files on disk (no mocked spec)."""

    def test_curator_prefers_codex_with_claude_default_fallback(self):
        from triggered_agents.agents.pipeline import health as pipeline_health

        with mock.patch.object(pipeline_health, "refresh", lambda: {"openai-sub": "green"}):
            skill, cmd, profile = dispatch._launch_cmd("curator")
        self.assertEqual(skill, "/curate")
        self.assertEqual(_role(cmd), "curator")
        inner = _inner_shell(cmd)
        self.assertIn("codex exec", inner)
        self.assertIn("-m gpt-5.5", inner)
        self.assertIn("model_reasoning_effort=\"xhigh\"", inner)
        self.assertEqual(profile, "codex-curator")

    def test_curator_falls_back_to_bare_claude_when_openai_red(self):
        from triggered_agents.agents.pipeline import health as pipeline_health

        with mock.patch.object(pipeline_health, "refresh", lambda: {"openai-sub": "red", "claude-sub": "green"}):
            skill, cmd, profile = dispatch._launch_cmd("curator")
        self.assertEqual(skill, "/curate")
        self.assertEqual(_role(cmd), "curator")
        self.assertEqual(_inner_shell(cmd), "claude --dangerously-skip-permissions '/curate'")
        self.assertEqual(profile, "claude-default")

    def test_steward_head_resolves_through_pipeline_health_and_heads(self):
        from triggered_agents.agents.pipeline import health as pipeline_health

        with mock.patch.object(pipeline_health, "refresh", lambda: {"openai-sub": "green"}):
            skill, cmd, profile = dispatch._launch_cmd("steward")
        self.assertEqual(skill, "/steward")
        self.assertEqual(_role(cmd), "steward")
        self.assertIn("codex exec", _inner_shell(cmd))
        self.assertIn("model_reasoning_effort=\"xhigh\"", _inner_shell(cmd))
        self.assertEqual(profile, "codex-steward")

    def test_steward_head_falls_back_to_bare_claude_on_broken_resolution(self):
        from triggered_agents.agents.pipeline import health as pipeline_health

        with mock.patch.object(pipeline_health, "refresh", side_effect=RuntimeError("boom")):
            skill, cmd, profile = dispatch._launch_cmd("steward")
        self.assertEqual(skill, "/steward")
        self.assertEqual(_role(cmd), "steward")
        self.assertEqual(_inner_shell(cmd), "claude --dangerously-skip-permissions '/steward'")
        self.assertIsNone(profile)

    def test_steward_head_falls_back_to_next_profile_when_resource_red(self):
        from triggered_agents.agents.pipeline import health as pipeline_health

        # codex-steward falls back to the previous steward chain. With openai-sub red and
        # claude-sub green, resolution lands on claude-fable.
        with mock.patch.object(pipeline_health, "refresh", lambda: {"openai-sub": "red", "claude-sub": "green"}):
            skill, cmd, profile = dispatch._launch_cmd("steward")
        self.assertEqual(_role(cmd), "steward")
        self.assertIn("--model fable", _inner_shell(cmd))
        self.assertEqual(profile, "claude-fable")

    def test_steward_deep_sweep_variant_resolves_its_own_skill_through_the_same_head(self):
        from triggered_agents.agents.pipeline import health as pipeline_health

        with mock.patch.object(pipeline_health, "refresh", lambda: {"openai-sub": "green"}):
            skill, cmd, profile = dispatch._launch_cmd("steward", "deep-sweep")
        self.assertEqual(skill, "/steward deep-sweep")
        self.assertEqual(_role(cmd), "steward")
        self.assertEqual(profile, "codex-steward")

    def test_steward_head_keeps_original_name_when_the_whole_chain_is_red(self):
        from triggered_agents.agents.pipeline import health as pipeline_health

        # Every resource codex-steward's chain can reach (openai-sub, claude-sub, openrouter) is
        # red -> nothing to fall back onto; _launch_cmd must keep the originally-named profile.
        with mock.patch.object(pipeline_health, "refresh",
                               lambda: {"openai-sub": "red", "claude-sub": "red", "openrouter": "red"}):
            skill, cmd, profile = dispatch._launch_cmd("steward")
        self.assertIn("codex exec", _inner_shell(cmd))
        self.assertEqual(profile, "codex-steward")

    def test_card_ref_is_appended_to_the_skill_before_rendering(self):
        """triggered-agents-255: the ref must land INSIDE the reprd prompt, not tacked onto the
        rendered command afterward where it could fall outside the quoted argument."""
        from triggered_agents.agents.pipeline import health as pipeline_health

        with mock.patch.object(pipeline_health, "refresh", lambda: {"openai-sub": "green"}):
            skill, cmd, profile = dispatch._launch_cmd("steward", card_ref="triggered-agents-260")
        self.assertEqual(skill, "/steward --card triggered-agents-260")
        self.assertIn(repr(skill), _inner_shell(cmd))

    def test_card_ref_with_deep_sweep_variant(self):
        from triggered_agents.agents.pipeline import health as pipeline_health

        with mock.patch.object(pipeline_health, "refresh", lambda: {"openai-sub": "green"}):
            skill, cmd, profile = dispatch._launch_cmd("steward", "deep-sweep", card_ref="triggered-agents-261")
        self.assertEqual(skill, "/steward deep-sweep --card triggered-agents-261")

    def test_no_card_ref_leaves_skill_unchanged(self):
        skill, cmd, profile = dispatch._launch_cmd("curator")
        self.assertEqual(skill, "/curate")


class HeadProfileStateTest(unittest.TestCase):
    """`AgentState.save_head_profile`/`load_head_profile` round trip — the record idle-reuse
    reads to learn which resource a live terminal is actually running against
    (triggered-agents-275)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._state_patch = mock.patch("triggered_agents.runtime.state.STATE_ROOT", Path(self.tmp.name))
        self._state_patch.start()
        self.addCleanup(self._state_patch.stop)

    def test_nothing_recorded_yet_loads_none(self):
        self.assertIsNone(dispatch.AgentState("agent").load_head_profile())

    def test_saved_profile_round_trips(self):
        dispatch.AgentState("agent").save_head_profile("claude-fable")
        self.assertEqual(dispatch.AgentState("agent").load_head_profile(), "claude-fable")

    def test_saving_none_clears_a_previously_recorded_profile(self):
        state = dispatch.AgentState("agent")
        state.save_head_profile("hermes")
        state.save_head_profile(None)
        self.assertIsNone(state.load_head_profile())

    def test_separate_agents_do_not_share_a_recorded_profile(self):
        dispatch.AgentState("steward").save_head_profile("claude-fable")
        self.assertIsNone(dispatch.AgentState("curator").load_head_profile())


class ReuseHeadIsRedTest(unittest.TestCase):
    """`_reuse_head_is_red` against the real automation.toml files — the check idle-reuse makes
    before sending into a warm terminal (triggered-agents-274, triggered-agents-275)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._state_patch = mock.patch("triggered_agents.runtime.state.STATE_ROOT", Path(self.tmp.name))
        self._state_patch.start()
        self.addCleanup(self._state_patch.stop)

    def test_agent_without_head_field_is_never_red(self):
        state = dispatch.AgentState("agent")
        with mock.patch.object(dispatch, "_load_spec", lambda agent: {"skill": "/agent"}):
            self.assertFalse(dispatch._reuse_head_is_red("agent", state))

    def test_no_recorded_profile_falls_back_to_static_preferred_head_green(self):
        from triggered_agents.agents.pipeline import health as pipeline_health

        state = dispatch.AgentState("steward")
        with mock.patch.object(pipeline_health, "refresh", lambda: {"openai-sub": "green"}):
            self.assertFalse(dispatch._reuse_head_is_red("steward", state))

    def test_no_recorded_profile_falls_back_to_static_preferred_head_red(self):
        """State predating this tracking (or a first idle tick right after upgrade) has nothing
        recorded yet — same behavior as triggered-agents-274, checking the agent's static
        preferred head instead of a stored profile."""
        from triggered_agents.agents.pipeline import health as pipeline_health

        state = dispatch.AgentState("steward")
        with mock.patch.object(pipeline_health, "refresh", lambda: {"openai-sub": "red"}):
            self.assertTrue(dispatch._reuse_head_is_red("steward", state))

    def test_broken_resolution_defaults_to_not_red(self):
        from triggered_agents.agents.pipeline import health as pipeline_health

        state = dispatch.AgentState("steward")
        with mock.patch.object(pipeline_health, "refresh", side_effect=RuntimeError("boom")):
            self.assertFalse(dispatch._reuse_head_is_red("steward", state))

    def test_stored_fallback_profile_red_even_though_preferred_head_recovered(self):
        """triggered-agents-275: the terminal is actually running on hermes -- a fallback it was
        launched onto while openai-sub/claude-sub were red. Those have since recovered, but openrouter
        (hermes's own resource) is now red. Checking only the static preferred head
        (codex-steward/openai-sub, as triggered-agents-274 did) would miss this: the terminal is
        still dead even though its preferred resource looks green."""
        from triggered_agents.agents.pipeline import health as pipeline_health

        state = dispatch.AgentState("steward")
        state.save_head_profile("hermes")
        with mock.patch.object(pipeline_health, "refresh",
                               lambda: {"openai-sub": "green", "claude-sub": "green", "openrouter": "red"}):
            self.assertTrue(dispatch._reuse_head_is_red("steward", state))

    def test_stored_preferred_profile_green_even_though_its_own_fallback_is_red(self):
        """Counterpart: the terminal is running on the preferred codex-steward profile
        (openai-sub green) — a red openrouter (only hermes's resource, never reached) must not
        divert a perfectly live terminal."""
        from triggered_agents.agents.pipeline import health as pipeline_health

        state = dispatch.AgentState("steward")
        state.save_head_profile("codex-steward")
        with mock.patch.object(pipeline_health, "refresh",
                               lambda: {"openai-sub": "green", "openrouter": "red"}):
            self.assertFalse(dispatch._reuse_head_is_red("steward", state))


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
        self.orca_json_calls = []
        self.created_cards = []
        self.active_report_cards = []
        self.comments = []
        self.moves = []
        self.create_error = None

        def fake_create_report_card(project, title, slug, description=""):
            ref = f"triggered-agents-{len(self.created_cards) + 1}"
            self.created_cards.append({
                "reference": ref,
                "project": project,
                "title": title,
                "slug": slug,
                "column": "In progress",
                "date_moved": 1234,
                "steward_report": "1",
            })
            return {"action": "created", "id": len(self.created_cards), "reference": ref,
                    "column": "In progress"}

        from triggered_agents.agents.pipeline import ops as pipeline_ops
        p = mock.patch.object(pipeline_ops, "create_report_card", fake_create_report_card)
        p.start()
        self.addCleanup(p.stop)

        def fake_list_cards(column=None, project=None):
            cards = self.active_report_cards + self.created_cards
            out = []
            for card in cards:
                if column and card.get("column") != column:
                    continue
                if project and card.get("project", "triggered-agents") != project:
                    continue
                out.append(card)
            return out

        p = mock.patch.object(pipeline_ops, "list_cards", fake_list_cards)
        p.start()
        self.addCleanup(p.stop)

        def fake_add_comment(role, reference, body, marker=None):
            self.comments.append({
                "role": role,
                "reference": reference,
                "body": body,
                "marker": marker or role,
            })
            return {"action": "commented", "reference": reference, "marker": marker or role}

        p = mock.patch.object(pipeline_ops, "add_comment", fake_add_comment)
        p.start()
        self.addCleanup(p.stop)

        def fake_move_card(role, reference, to_column, reason=""):
            self.moves.append({
                "role": role,
                "reference": reference,
                "to": to_column,
                "reason": reason,
            })
            for card in self.created_cards + self.active_report_cards:
                if card.get("reference") == reference:
                    card["column"] = to_column
            if reason.strip():
                fake_add_comment(role, reference, reason)
            return {"action": "moved", "reference": reference, "to": to_column}

        p = mock.patch.object(pipeline_ops, "move_card", fake_move_card)
        p.start()
        self.addCleanup(p.stop)

        p = mock.patch.object(dispatch, "_workspace", lambda agent: "/ws/steward")
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
            if args[:2] == ["terminal", "create"]:
                if self.create_error:
                    raise self.create_error
                return {"terminal": {"handle": "new-terminal"}}
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
        creates = [c for c in self.orca_json_calls if c[:2] == ["terminal", "create"]]
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

    def test_fresh_active_report_card_skips_dispatch_without_creating_a_second_card(self):
        from triggered_agents.runtime.state import AgentState

        self.terminals = []
        self.active_report_cards = [{
            "reference": "triggered-agents-299",
            "project": "triggered-agents",
            "column": "In progress",
            "date_moved": 1234,
            "steward_report": "1",
        }]
        AgentState("steward").save_active_report("triggered-agents-299", "new-terminal")
        with mock.patch("time.time", lambda: 1235):
            dispatch.run("steward", "deep-sweep")
        self.assertEqual(self.created_cards, [])
        self.assertEqual([c for c in self.orca_calls if c[:2] == ["terminal", "create"]], [])

        runs = AgentState("steward").dir / "runs.jsonl"
        self.assertIn("active-report-skip", runs.read_text(encoding="utf-8"))
        self.assertIn("triggered-agents-299", runs.read_text(encoding="utf-8"))

    def test_fresh_report_card_without_run_identity_does_not_skip_dispatch(self):
        self.terminals = []
        self.active_report_cards = [{
            "reference": "triggered-agents-299",
            "project": "triggered-agents",
            "column": "In progress",
            "date_moved": 1234,
            "steward_report": "1",
        }]
        with mock.patch("time.time", lambda: 1235):
            dispatch.run("steward", "deep-sweep")
        self.assertEqual(len(self.created_cards), 1)
        self.assertIn("--card triggered-agents-1", self._launch_command_sent())

    def test_stale_active_report_card_does_not_skip_dispatch(self):
        from triggered_agents.agents.steward import signals as steward_signals

        self.terminals = []
        self.active_report_cards = [{
            "reference": "triggered-agents-299",
            "project": "triggered-agents",
            "column": "In progress",
            "date_moved": 1000,
            "steward_report": "1",
        }]
        now = 1000 + int(steward_signals.STALE_HOURS * 3600) + 1
        with mock.patch("time.time", lambda: now):
            dispatch.run("steward", "deep-sweep")
        self.assertEqual(len(self.created_cards), 1)
        self.assertIn("--card triggered-agents-1", self._launch_command_sent())

    def test_terminal_create_failure_recovers_report_card_and_next_dispatch_runs(self):
        from triggered_agents.runtime.state import AgentState

        self.terminals = []
        self.create_error = RuntimeError("orca terminal create failed: selector_not_found")
        with self.assertRaisesRegex(RuntimeError, "selector_not_found"):
            dispatch.run("steward")

        self.assertEqual(len(self.created_cards), 1)
        self.assertEqual(self.created_cards[0]["column"], "Done")
        self.assertEqual(self.moves[0]["reference"], "triggered-agents-1")
        self.assertEqual(self.moves[0]["to"], "Done")
        self.assertIn("selector_not_found", self.moves[0]["reason"])
        self.assertEqual(self.comments[0]["reference"], "triggered-agents-1")
        self.assertIn("selector_not_found", self.comments[0]["body"])
        self.assertIsNone(AgentState("steward").load_active_report())

        self.create_error = None
        dispatch.run("steward")
        self.assertEqual(len(self.created_cards), 2)
        creates = [c for c in self.orca_json_calls if c[:2] == ["terminal", "create"]]
        self.assertIn("--card triggered-agents-2", creates[-1][creates[-1].index("--command") + 1])

        runs = AgentState("steward").dir / "runs.jsonl"
        text = runs.read_text(encoding="utf-8")
        self.assertIn("dispatch-recovery", text)
        self.assertIn('"result": "done"', text)
        self.assertNotIn("active-report-skip", text)

    def test_recovery_failure_does_not_mask_terminal_create_failure(self):
        from triggered_agents.agents.pipeline import ops as pipeline_ops
        from triggered_agents.runtime.state import AgentState

        self.terminals = []
        self.create_error = RuntimeError("orca terminal create failed: selector_not_found")
        with mock.patch.object(pipeline_ops, "move_card", side_effect=RuntimeError("board down")):
            with self.assertRaisesRegex(RuntimeError, "selector_not_found"):
                dispatch.run("steward")

        runs = AgentState("steward").dir / "runs.jsonl"
        text = runs.read_text(encoding="utf-8")
        self.assertIn("dispatch-recovery", text)
        self.assertIn('"result": "failed"', text)
        self.assertIn("board down", text)

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
