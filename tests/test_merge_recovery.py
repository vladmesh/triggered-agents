"""Unit tests for Validate base-freshness recovery (triggered-agents-442): poll_pr's mergeability
normalization (gh stubbed) and merge_recovery.merge_base_into_branch against real git repos — no
orca, no network. Kept in its own module (not folded into test_worker.py) so neither test file
crosses the 1000-line quality bar. The dispatcher-side orchestration is covered by
test_dispatcher.MergeRecoveryTest.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_STATE_DIR = tempfile.mkdtemp(prefix="ta-merge-recovery-test-")
os.environ["TA_STATE"] = _STATE_DIR

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from triggered_agents.agents.pipeline import merge_recovery, worker  # noqa: E402


def _git(cwd, *args, check=True):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=check)


class PollPrMergeStateTest(unittest.TestCase):
    """poll_pr normalizes GitHub's mergeable/mergeStateStatus into one merge-recovery signal, kept
    apart from the CI rollup, and carries the head/base SHAs and the PR's actual base ref."""

    def _poll(self, **fields):
        data = {"state": "OPEN", "mergedAt": None, "statusCheckRollup": [],
                "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN",
                "headRefOid": "hhh", "baseRefOid": "bbb", "baseRefName": "main"}
        data.update(fields)
        with mock.patch.object(worker, "_gh_json", return_value=data):
            return worker.poll_pr("https://github.com/x/y/pull/1")

    def test_clean_pr_carries_head_base_sha_and_base_branch(self):
        out = self._poll(baseRefName="release/9")
        self.assertEqual(out["mergeable"], "CLEAN")
        self.assertEqual(out["head_sha"], "hhh")
        self.assertEqual(out["base_sha"], "bbb")
        self.assertEqual(out["base_branch"], "release/9")

    def test_behind_base_is_its_own_state(self):
        self.assertEqual(self._poll(mergeStateStatus="BEHIND")["mergeable"], "BEHIND")

    def test_conflicting_is_a_conflict(self):
        self.assertEqual(self._poll(mergeable="CONFLICTING", mergeStateStatus="DIRTY")["mergeable"],
                         "CONFLICTING")

    def test_dirty_state_is_a_conflict_even_if_mergeable_still_unknown(self):
        self.assertEqual(self._poll(mergeable="UNKNOWN", mergeStateStatus="DIRTY")["mergeable"],
                         "CONFLICTING")

    def test_transient_unknown_is_neither_conflict_nor_clean(self):
        self.assertEqual(self._poll(mergeable="UNKNOWN", mergeStateStatus="UNKNOWN")["mergeable"],
                         "UNKNOWN")

    def test_rollup_stays_independent_of_the_merge_signal(self):
        # A conflicting PR with zero checks: the CI rollup is NONE, the merge signal is CONFLICTING —
        # two orthogonal axes, exactly the codegen_orchestrator-440 shape.
        out = self._poll(mergeable="CONFLICTING", mergeStateStatus="DIRTY", statusCheckRollup=[])
        self.assertEqual(out["rollup"], "NONE")
        self.assertEqual(out["mergeable"], "CONFLICTING")

    def test_gh_unavailable_returns_none(self):
        with mock.patch.object(worker, "_gh_json", return_value=None):
            self.assertIsNone(worker.poll_pr("https://github.com/x/y/pull/1"))


class MergeBaseIntoBranchTest(unittest.TestCase):
    """merge_recovery.merge_base_into_branch against real git repos: a clean behind branch is merged
    and pushed, a text conflict is aborted (tree left clean), a dirty worktree is refused untouched,
    and a worktree left on the wrong branch is moved onto the target before any merge. The op lives
    in merge_recovery (leaf module) but reuses worker._git."""

    def _cfg(self, tree):
        for c in (["config", "user.email", "t@t"], ["config", "user.name", "t"]):
            _git(tree, *c)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.bare = root / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.bare)], check=True)
        seed = root / "seed"
        subprocess.run(["git", "clone", "-q", str(self.bare), str(seed)], check=True)
        self._cfg(seed)
        (seed / "base.txt").write_text("base-v1\n")
        _git(seed, "add", "."); _git(seed, "commit", "-q", "-m", "v1")
        _git(seed, "push", "-q", "origin", "main")
        # feature branch off v1, then main advances past it (feature is now BEHIND)
        _git(seed, "checkout", "-q", "-b", "feature")
        (seed / "feat.txt").write_text("feature\n")
        _git(seed, "add", "."); _git(seed, "commit", "-q", "-m", "feat")
        _git(seed, "push", "-q", "origin", "feature")
        _git(seed, "checkout", "-q", "main")
        (seed / "base.txt").write_text("base-v2\n")
        _git(seed, "commit", "-q", "-am", "v2"); _git(seed, "push", "-q", "origin", "main")
        # the worker workspace: a clone checked out on the feature branch
        self.ws = root / "ws"
        subprocess.run(["git", "clone", "-q", "-b", "feature", str(self.bare), str(self.ws)],
                       check=True)
        self._cfg(self.ws)

    def _origin_feature(self):
        return _git(self.bare, "rev-parse", "feature").stdout.strip()

    def test_clean_behind_merges_the_base_and_pushes(self):
        r = merge_recovery.merge_base_into_branch(str(self.ws), "main", "feature")
        self.assertEqual(r["result"], "updated")
        self.assertEqual((self.ws / "base.txt").read_text(), "base-v2\n")   # base merged in
        self.assertEqual((self.ws / "feat.txt").read_text(), "feature\n")   # feature work kept
        self.assertEqual(_git(self.ws, "rev-parse", "HEAD").stdout.strip(), self._origin_feature())
        self.assertEqual(r["head"], self._origin_feature())

    def test_second_call_is_already_up_to_date(self):
        merge_recovery.merge_base_into_branch(str(self.ws), "main", "feature")
        r = merge_recovery.merge_base_into_branch(str(self.ws), "main", "feature")
        self.assertEqual(r["result"], "already")

    def test_worktree_on_wrong_branch_is_moved_onto_target_first(self):
        # B2: a worker that left the worktree on main (or detached) must not have the base merged
        # into the wrong checkout — the op checks out `feature` first, then merges and pushes it.
        _git(self.ws, "checkout", "-q", "main")
        r = merge_recovery.merge_base_into_branch(str(self.ws), "main", "feature")
        self.assertEqual(r["result"], "updated")
        self.assertEqual(_git(self.ws, "branch", "--show-current").stdout.strip(), "feature")
        self.assertEqual(_git(self.ws, "rev-parse", "HEAD").stdout.strip(), self._origin_feature())
        self.assertEqual((self.ws / "feat.txt").read_text(), "feature\n")   # feature work preserved

    def test_text_conflict_is_aborted_and_reports_files(self):
        (self.ws / "base.txt").write_text("feature-edits-base\n")
        _git(self.ws, "commit", "-q", "-am", "feature edits base too")
        _git(self.ws, "push", "-q", "origin", "feature")
        before = _git(self.ws, "rev-parse", "HEAD").stdout.strip()
        r = merge_recovery.merge_base_into_branch(str(self.ws), "main", "feature")
        self.assertEqual(r["result"], "conflict")
        self.assertIn("base.txt", r["conflict_files"])
        # the abort leaves the tree exactly as found — no conflict markers, no moved HEAD
        self.assertEqual(_git(self.ws, "status", "--porcelain").stdout.strip(), "")
        self.assertEqual(_git(self.ws, "rev-parse", "HEAD").stdout.strip(), before)

    def test_dirty_worktree_is_refused_untouched(self):
        (self.ws / "feat.txt").write_text("uncommitted worker change\n")
        origin_before = self._origin_feature()
        r = merge_recovery.merge_base_into_branch(str(self.ws), "main", "feature")
        self.assertEqual(r["result"], "dirty")
        self.assertEqual((self.ws / "feat.txt").read_text(), "uncommitted worker change\n")
        self.assertEqual(self._origin_feature(), origin_before)          # nothing pushed
        self.assertNotIn("base-v2", (self.ws / "base.txt").read_text())  # base never merged in

    def test_fetch_failure_is_named_not_a_conflict(self):
        r = merge_recovery.merge_base_into_branch(str(self.ws), "does-not-exist", "feature")
        self.assertEqual(r["result"], "fetch-failed")
        self.assertTrue(r["reason"])


if __name__ == "__main__":
    unittest.main()
