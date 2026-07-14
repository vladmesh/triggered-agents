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

    def test_tells_reviewer_to_run_workers_claimed_live_checks(self):
        # triggered-agents-245: a human no longer double-checks a green verdict before merge, so
        # the reviewer itself must re-run whatever the worker's report claimed it verified live.
        text = self._build()
        self.assertIn("живые проверки", text)
        self.assertIn("прогнал", text)
        self.assertIn("не смог", text)

    def test_accepts_current_sha_mechanical_evidence_for_heavyweight_checks(self):
        text = self._build()
        self.assertIn("heavyweight-проверки", text)
        self.assertIn("ТЕКУЩЕМ head SHA", text)
        self.assertIn("workflow/команду", text)
        self.assertIn("отсутствие личного Docker-прогона само по себе не блокер", text)
        self.assertIn("ни безопасного rerun, ни подходящего механического evidence", text)

    def test_points_at_the_worker_report_via_show_command(self):
        ref = "triggered-agents-220"
        text = self._build(ref=ref)
        self.assertIn(f"pipeline show --ref {ref}", text)

    def test_carries_memory_block_scoped_to_project(self):
        text = self._build()
        self.assertIn("memory_search", text)
        self.assertIn('scope="project:personal_site"', text)
        self.assertIn('caller="reviewer"', text)


class ContribBuildTaskTest(unittest.TestCase):
    """Contrib (fork) cards have no PR in this pipeline — REVIEW.md points at the reported
    branch/head sha instead, and must never suggest a gh PR command."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        skill = Path(self.tmp.name) / "SKILL.md"
        skill.write_text("thermo-nuclear lens body")
        p = mock.patch.object(reviewer, "THERMO_SKILL", skill)
        p.start()
        self.addCleanup(p.stop)

    def _build(self, ref="agent-kanban-9", branch="pipeline/agent-kanban-9", head_sha="abc1234"):
        card = {"project": "agent-kanban"}
        return reviewer.build_task(card, ref, None, "spec text", "main",
                                   branch=branch, head_sha=head_sha)

    def test_never_mentions_a_pr(self):
        text = self._build()
        self.assertNotIn("gh pr checkout", text)
        self.assertNotIn("gh pr diff", text)
        self.assertNotIn("PR карточки", text)

    def test_states_branch_and_head_sha(self):
        text = self._build(branch="pipeline/agent-kanban-9", head_sha="abc1234")
        self.assertIn("pipeline/agent-kanban-9", text)
        self.assertIn("abc1234", text)

    def test_states_own_review_branch_already_checked_out(self):
        from triggered_agents.agents.pipeline import naming
        ref = "agent-kanban-9"
        text = self._build(ref=ref)
        self.assertIn(naming.reviewer_branch(ref), text)

    def test_forbids_pushing_the_review_branch(self):
        self.assertIn("не пушить", self._build())

    def test_points_at_the_worker_report_via_show_command(self):
        ref = "agent-kanban-9"
        text = self._build(ref=ref)
        self.assertIn(f"pipeline show --ref {ref}", text)


if __name__ == "__main__":
    unittest.main()
