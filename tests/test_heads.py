"""Unit tests for triggered_agents.agents.pipeline.heads — the head registry (heads.toml) and its
command adapters. No I/O beyond reading the real heads.toml (or a tempfile one for the malformed-
registry cases), no orca, no network.
"""
from __future__ import annotations

import os
import shlex
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_STATE_DIR = tempfile.mkdtemp(prefix="ta-heads-test-")
os.environ["TA_STATE"] = _STATE_DIR

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from triggered_agents.agents.pipeline import heads  # noqa: E402


class RealRegistryTest(unittest.TestCase):
    """Against the actual shipped heads.toml — catches drift between its content and what the
    prod defaults (DEFAULT_PROFILE, worker.REVIEWER_HEAD) expect to find there."""

    def setUp(self):
        self.reg = heads.load_registry()

    def test_default_profile_exists(self):
        self.reg.profile(heads.DEFAULT_PROFILE)  # must not raise

    def test_starting_profiles_present(self):
        self.assertEqual(set(self.reg.known()),
                         {"claude-default", "claude-sonnet", "claude-opus", "claude-fable",
                          "hermes", "codex", "codex-high", "codex-extra", "codex-reviewer",
                          "codex-tui", "codex-high-tui", "codex-extra-tui",
                          "codex-reviewer-tui", "codex-curator", "codex-steward",
                          "codex-retro"})

    def test_claude_profiles_share_the_subscription_resource(self):
        for pid in ("claude-default", "claude-sonnet", "claude-opus", "claude-fable"):
            self.assertEqual(self.reg.profile(pid)["resource"], "claude-sub")

    def test_claude_fable_falls_back_to_opus_then_hermes(self):
        # 2026-07-04 (vladmesh): the steward must survive the whole claude-sub resource going red,
        # so after claude-opus the chain leaves the subscription onto a non-Anthropic runtime.
        self.assertEqual(self.reg.profile("claude-fable").get("fallback"),
                         ["claude-opus", "hermes"])

    def test_product_heads_have_no_cross_runtime_fallback(self):
        # 2026-07-04 (vladmesh): product tasks are Claude-only — overnight 04.07 the gemini-flash
        # fallback finished worker turns without push/PR/report, so a red claude-sub must mean
        # claim-skip (resolve_head -> None, card waits in Ready), never a hermes claim. Hermes
        # stays only in the steward's claude-fable chain above.
        self.assertEqual(self.reg.profile("claude-sonnet").get("fallback") or [], [])
        self.assertEqual(self.reg.profile("claude-opus").get("fallback") or [], [])
        self.assertEqual(self.reg.profile("codex").get("fallback") or [], [])
        self.assertEqual(self.reg.profile("codex-high").get("fallback") or [], [])
        self.assertEqual(self.reg.profile("codex-extra").get("fallback") or [], [])
        self.assertEqual(self.reg.profile("codex-tui").get("fallback") or [], [])
        self.assertEqual(self.reg.profile("codex-high-tui").get("fallback") or [], [])
        self.assertEqual(self.reg.profile("codex-extra-tui").get("fallback") or [], [])

    def test_codex_role_profiles_keep_claude_second_priority(self):
        self.assertEqual(self.reg.profile("codex-curator").get("fallback"), ["claude-default"])
        self.assertEqual(self.reg.profile("codex-steward").get("fallback"), ["claude-fable"])
        self.assertEqual(self.reg.profile("codex-reviewer").get("fallback"), ["claude-opus"])
        self.assertEqual(self.reg.profile("codex-reviewer-tui").get("fallback"), ["claude-opus"])

    def test_hermes_is_on_its_own_resource(self):
        self.assertEqual(self.reg.profile("hermes")["resource"], "openrouter")

    def test_unknown_profile_raises_with_known_ids_listed(self):
        with self.assertRaises(heads.HeadRegistryError) as ctx:
            self.reg.profile("codex-nope")
        msg = str(ctx.exception)
        self.assertIn("codex-nope", msg)
        self.assertIn("claude-sonnet", msg)


class RenderClaudeTest(unittest.TestCase):
    def setUp(self):
        self.reg = heads.load_registry()

    def test_renders_claude_binary_with_model_and_dangerous_skip(self):
        cmd = heads.render_command("claude-sonnet", role="worker", prompt="do the thing",
                                   registry=self.reg)
        self.assertIn("BOARD_ROLE=worker", cmd)
        self.assertIn("claude --dangerously-skip-permissions --model sonnet", cmd)
        self.assertIn(repr("do the thing"), cmd)

    def test_role_prefix_changes_with_role(self):
        cmd = heads.render_command("claude-opus", role="reviewer", prompt="review it",
                                   registry=self.reg)
        self.assertTrue(cmd.startswith("BOARD_ROLE=reviewer "))
        self.assertIn("--model opus", cmd)

    def test_renders_claude_fable(self):
        cmd = heads.render_command("claude-fable", role="steward", prompt="steward the board",
                                   registry=self.reg)
        self.assertTrue(cmd.startswith("BOARD_ROLE=steward "))
        self.assertIn("claude --dangerously-skip-permissions --model fable", cmd)
        self.assertIn(repr("steward the board"), cmd)


class RenderHermesTest(unittest.TestCase):
    """The hermes adapter is a genuinely different launch shape (not the claude template): no
    `--dangerously-skip-permissions`, a `-z`-seeded session instead of a bare positional prompt,
    model/provider as separate flags."""

    def setUp(self):
        self.reg = heads.load_registry()

    def test_renders_hermes_binary_not_claude(self):
        cmd = heads.render_command("hermes", role="worker", prompt="ping", registry=self.reg)
        self.assertIn("BOARD_ROLE=worker hermes", cmd)
        self.assertNotIn("claude", cmd)
        self.assertNotIn("--dangerously-skip-permissions", cmd)

    def test_carries_model_provider_and_yolo_flags(self):
        cmd = heads.render_command("hermes", role="worker", prompt="ping", registry=self.reg)
        self.assertIn("-z " + repr("ping"), cmd)
        self.assertIn("-m openai/gpt-5.5", cmd)
        self.assertIn("--provider openrouter", cmd)
        self.assertIn("--yolo", cmd)
        self.assertIn("--cli", cmd)


class RenderCodexTest(unittest.TestCase):
    """The codex adapter is the native OpenAI CLI's `exec` shape: no claude/hermes binary, a pinned
    CODEX_HOME (so the head finds its ChatGPT login, memory MCP, and global AGENTS.md), and the
    bypass-approvals flag standing in for claude's --dangerously-skip-permissions."""

    def setUp(self):
        self.reg = heads.load_registry()
        # Дефолтные exec-ассерты не должны зависеть от env хоста: smoke гоняет юниты
        # в окружении диспетчера, где прод-флаг TA_CODEX_MODE=tui может быть включён.
        env_guard = mock.patch.dict("os.environ")
        env_guard.start()
        self.addCleanup(env_guard.stop)
        os.environ.pop("TA_CODEX_MODE", None)
        os.environ.pop("TA_CODEX_TUI", None)

    def test_renders_codex_exec_not_claude_or_hermes(self):
        cmd = heads.render_command("codex", role="worker", prompt="ping", registry=self.reg)
        self.assertIn("BOARD_ROLE=worker ", cmd)
        self.assertIn("codex exec", cmd)
        self.assertNotIn("claude", cmd)
        self.assertNotIn("hermes", cmd)

    def test_pins_codex_home_and_bypass_flags(self):
        cmd = heads.render_command("codex", role="worker", prompt="ping", registry=self.reg)
        self.assertIn(f"CODEX_HOME={heads.CODEX_HOME} codex exec", cmd)
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", cmd)
        self.assertIn("--skip-git-repo-check", cmd)
        self.assertIn("-m gpt-5.5", cmd)
        self.assertNotIn("model_reasoning_effort", cmd)
        self.assertIn(repr("ping"), cmd)

    def test_renders_codex_effort(self):
        cmd = heads.render_command("codex-extra", role="worker", prompt="ping", registry=self.reg)
        self.assertIn("-m gpt-5.5", cmd)
        self.assertIn("model_reasoning_effort=\"xhigh\"", cmd)

    def test_profile_codex_home_overrides_pin(self):
        prof = dict(self.reg.profile("codex"), codex_home="/tmp/throwaway-home")
        cmd = heads._render_codex(prof, prompt="ping")
        self.assertIn("CODEX_HOME=/tmp/throwaway-home codex exec", cmd)

    def test_render_launch_keeps_codex_exec_as_default(self):
        launch = heads.render_launch("codex", role="worker", prompt="ping",
                                     workspace="/ws/fresh", registry=self.reg)
        self.assertIn("codex exec", launch.command)
        self.assertNotIn("trust_level", launch.command)
        self.assertIsNone(launch.initial_prompt)
        self.assertIsNone(launch.terminal_kind)

    def test_render_launch_codex_tui_splits_command_from_prompt(self):
        launch = heads.render_launch("codex-extra-tui", role="worker", prompt="ping",
                                     workspace="/ws/fresh", registry=self.reg)
        self.assertTrue(launch.command.startswith(f"BOARD_ROLE=worker CODEX_HOME={heads.CODEX_HOME} codex "))
        self.assertNotIn("codex exec", launch.command)
        self.assertNotIn(repr("ping"), launch.command)
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", launch.command)
        self.assertNotIn("--skip-git-repo-check", launch.command)
        self.assertIn("-m gpt-5.5", launch.command)
        self.assertIn("model_reasoning_effort=\"xhigh\"", launch.command)
        self.assertIn("'projects.\"/ws/fresh\".trust_level=\"trusted\"'", launch.command)
        self.assertEqual(launch.initial_prompt, "ping")
        self.assertEqual(launch.terminal_kind, "codex-tui")

    def test_render_launch_codex_tui_quotes_workspace_trust_override(self):
        launch = heads.render_launch("codex-tui", role="worker", prompt="ping",
                                     workspace="/tmp/work dir/that's \"quoted\"",
                                     registry=self.reg)
        parts = shlex.split(launch.command)
        conf = parts[parts.index("-c") + 1]
        self.assertEqual(conf,
                         "projects.\"/tmp/work dir/that's \\\"quoted\\\"\".trust_level=\"trusted\"")

    def test_render_launch_codex_tui_requires_workspace(self):
        with self.assertRaisesRegex(heads.HeadRegistryError, "requires workspace"):
            heads.render_launch("codex-tui", role="worker", prompt="ping", registry=self.reg)

    def test_codex_tui_feature_flag_can_enable_existing_profile(self):
        with mock.patch.dict(os.environ, {"TA_CODEX_MODE": "tui"}, clear=False):
            launch = heads.render_launch("codex", role="worker", prompt="ping",
                                         workspace="/ws/fresh", registry=self.reg)
        self.assertNotIn("codex exec", launch.command)
        self.assertIn("'projects.\"/ws/fresh\".trust_level=\"trusted\"'", launch.command)
        self.assertEqual(launch.initial_prompt, "ping")
        self.assertEqual(launch.terminal_kind, "codex-tui")


class RegistryValidationTest(unittest.TestCase):
    """Malformed heads.toml variants — each must fail to load with a message naming the bad
    reference, not a raw KeyError/tomllib traceback."""

    def _write(self, text: str) -> Path:
        tmp = Path(tempfile.mkdtemp(prefix="ta-heads-bad-")) / "heads.toml"
        tmp.write_text(text, encoding="utf-8")
        self.addCleanup(lambda: tmp.unlink(missing_ok=True))
        return tmp

    def test_missing_file_raises(self):
        with self.assertRaises(heads.HeadRegistryError):
            heads.load_registry(Path("/nonexistent/heads.toml"))

    def test_profile_with_unknown_resource_raises(self):
        path = self._write("""
[resources.claude-sub]
probe = "true"
[profiles.p1]
resource = "no-such-resource"
adapter = "claude"
fallback = []
""")
        with self.assertRaises(heads.HeadRegistryError) as ctx:
            heads.load_registry(path)
        self.assertIn("no-such-resource", str(ctx.exception))

    def test_profile_with_unknown_adapter_raises(self):
        path = self._write("""
[resources.claude-sub]
probe = "true"
[profiles.p1]
resource = "claude-sub"
adapter = "gemini"
fallback = []
""")
        with self.assertRaises(heads.HeadRegistryError) as ctx:
            heads.load_registry(path)
        self.assertIn("gemini", str(ctx.exception))

    def test_profile_with_unknown_fallback_raises(self):
        path = self._write("""
[resources.claude-sub]
probe = "true"
[profiles.p1]
resource = "claude-sub"
adapter = "claude"
fallback = ["p2"]
""")
        with self.assertRaises(heads.HeadRegistryError) as ctx:
            heads.load_registry(path)
        self.assertIn("p2", str(ctx.exception))

    def test_codex_profile_with_unknown_effort_raises(self):
        path = self._write("""
[resources.openai-sub]
probe = "true"
[profiles.p1]
resource = "openai-sub"
adapter = "codex"
model = "gpt-5.5"
effort = "too-much"
fallback = []
""")
        with self.assertRaises(heads.HeadRegistryError) as ctx:
            heads.load_registry(path)
        self.assertIn("too-much", str(ctx.exception))

    def test_codex_profile_with_unknown_launch_mode_raises(self):
        path = self._write("""
[resources.openai-sub]
probe = "true"
[profiles.p1]
resource = "openai-sub"
adapter = "codex"
model = "gpt-5.5"
effort = "default"
codex_mode = "sideways"
fallback = []
""")
        with self.assertRaises(heads.HeadRegistryError) as ctx:
            heads.load_registry(path)
        self.assertIn("sideways", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
