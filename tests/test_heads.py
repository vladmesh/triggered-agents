"""Unit tests for triggered_agents.agents.pipeline.heads — the head registry (heads.toml) and its
command adapters. No I/O beyond reading the real heads.toml (or a tempfile one for the malformed-
registry cases), no orca, no network.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

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
                         {"claude-sonnet", "claude-opus", "claude-fable", "hermes-flash"})

    def test_claude_profiles_share_the_subscription_resource(self):
        for pid in ("claude-sonnet", "claude-opus", "claude-fable"):
            self.assertEqual(self.reg.profile(pid)["resource"], "claude-sub")

    def test_claude_fable_falls_back_to_opus_then_hermes(self):
        # 2026-07-04 (vladmesh): the steward must survive the whole claude-sub resource going red,
        # so after claude-opus the chain leaves the subscription onto a non-Anthropic runtime.
        self.assertEqual(self.reg.profile("claude-fable").get("fallback"),
                         ["claude-opus", "hermes-flash"])

    def test_claude_sonnet_falls_back_to_hermes_flash_cross_runtime(self):
        # 2026-07-03 design session: head-technical retries must prove the switch on a genuinely
        # different, non-claude runtime — claude-opus is untouched (still claude-sub only).
        self.assertEqual(self.reg.profile("claude-sonnet").get("fallback"), ["hermes-flash"])
        self.assertEqual(self.reg.profile("claude-opus").get("fallback") or [], [])

    def test_hermes_flash_is_on_its_own_resource(self):
        self.assertEqual(self.reg.profile("hermes-flash")["resource"], "openrouter")

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
        cmd = heads.render_command("hermes-flash", role="worker", prompt="ping", registry=self.reg)
        self.assertIn("BOARD_ROLE=worker hermes", cmd)
        self.assertNotIn("claude", cmd)
        self.assertNotIn("--dangerously-skip-permissions", cmd)

    def test_carries_model_provider_and_yolo_flags(self):
        cmd = heads.render_command("hermes-flash", role="worker", prompt="ping", registry=self.reg)
        self.assertIn("-z " + repr("ping"), cmd)
        self.assertIn("-m google/gemini-2.5-flash", cmd)
        self.assertIn("--provider openrouter", cmd)
        self.assertIn("--yolo", cmd)
        self.assertIn("--cli", cmd)


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
adapter = "codex"
fallback = []
""")
        with self.assertRaises(heads.HeadRegistryError) as ctx:
            heads.load_registry(path)
        self.assertIn("codex", str(ctx.exception))

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


if __name__ == "__main__":
    unittest.main()
