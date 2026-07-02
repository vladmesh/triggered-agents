"""Unit tests for triggered_agents.agents.pipeline.reviewer — REVIEW.md text, no I/O besides the
thermo-nuclear skill file read (redirected to a tempfile here so the test never depends on the
real ~/.claude skill being present)."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from triggered_agents.agents.pipeline import reviewer  # noqa: E402


class BuildTaskTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        skill = Path(self.tmp.name) / "SKILL.md"
        skill.write_text("thermo-nuclear lens body")
        p = mock.patch.object(reviewer, "THERMO_SKILL", skill)
        p.start()
        self.addCleanup(p.stop)

    def _build(self, ref="triggered-agents-220", pr="https://github.com/vladmesh/x/pull/9"):
        card = {"project": "personal_site"}
        return reviewer.build_task(card, ref, pr, "spec text", "main")

    def test_never_suggests_gh_pr_checkout(self):
        # gh pr checkout tries to check out the PR's own branch locally, which clashes with the
        # worker's live worktree still sitting on it ("already used by worktree") — the reviewer
        # must never be told to run it.
        self.assertNotIn("gh pr checkout", self._build())

    def test_states_own_review_branch_already_checked_out(self):
        from triggered_agents.agents.pipeline import naming
        ref = "triggered-agents-220"
        text = self._build(ref=ref)
        self.assertIn(naming.reviewer_branch(ref), text)
        self.assertIn("уже стоит на состоянии PR", text)

    def test_still_offers_gh_pr_diff_for_the_diff_itself(self):
        pr = "https://github.com/vladmesh/x/pull/9"
        self.assertIn(f"gh pr diff {pr}", self._build(pr=pr))

    def test_forbids_pushing_the_review_branch(self):
        self.assertIn("не пушить", self._build())


if __name__ == "__main__":
    unittest.main()
