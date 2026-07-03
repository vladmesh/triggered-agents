"""Unit tests for triggered_agents.agents.pipeline.worker's git-hygiene steps (create_workspace's
fetch-then-origin-base, set_branch, land_pr_head) against real git repos — no orca, no network.
The orca CLI boundary (_orca_json) is stubbed; every git call is real, so these catch actual git
semantics (branch renamed, worktree lands on a real ref, never detached) that an argv-only mock
would miss.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_STATE_DIR = tempfile.mkdtemp(prefix="ta-worker-test-")
os.environ["TA_STATE"] = _STATE_DIR

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from triggered_agents.agents.pipeline import worker  # noqa: E402


def _git(cwd, *args, check=True):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=check)


def _current_branch(tree) -> str:
    return _git(tree, "branch", "--show-current").stdout.strip()


class GitHygieneTest(unittest.TestCase):
    """A bare origin + a working checkout, wired the way a real project repo is: `project_root`
    resolves to the checkout, its `origin` remote points at the bare repo."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.bare = root / "origin.git"
        self.checkout = root / "checkout"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.bare)], check=True)
        subprocess.run(["git", "clone", "-q", str(self.bare), str(self.checkout)], check=True)
        for cmd in (["config", "user.email", "t@t"], ["config", "user.name", "t"]):
            _git(self.checkout, *cmd)
        (self.checkout / "f.txt").write_text("v1")
        _git(self.checkout, "add", "f.txt")
        _git(self.checkout, "commit", "-q", "-m", "v1")
        _git(self.checkout, "push", "-q", "origin", "main")

        self.projects_dir = root / "projects"
        self.projects_dir.mkdir()
        (self.projects_dir / "proj").symlink_to(self.checkout)
        p = mock.patch.object(worker, "PROJECTS_DIR", self.projects_dir)
        p.start()
        self.addCleanup(p.stop)

        # _claim_branch's fallback path (stale worktree still holding the branch) goes through
        # the real worker.teardown, which refuses any path outside WORKSPACES_ROOT.
        wr = mock.patch.object(worker, "WORKSPACES_ROOT", root)
        wr.start()
        self.addCleanup(wr.stop)

        self.worktrees = []

        def fake_orca_json(args):
            if args[0] == "worktree":
                # Mimic `orca worktree create --name <n> --base-branch <ref>`: a real orca worktree
                # always lands on *some* local branch (never detached) — here named after --name,
                # same as the real CLI, which is exactly the "wrong name" set_branch renames away.
                base_ref = args[args.index("--base-branch") + 1]
                wt_name = args[args.index("--name") + 1]
                path = root / "wt" / f"wt{len(self.worktrees)}"
                subprocess.run(["git", "-C", str(self.checkout), "worktree", "add", "-b", wt_name,
                               str(path), base_ref], check=True, capture_output=True)
                self.worktrees.append(path)
                return {"worktree": {"path": str(path)}}
            return {"terminal": {"handle": "term-1"}}

        oj = mock.patch.object(worker, "_orca_json", fake_orca_json)
        oj.start()
        self.addCleanup(oj.stop)

    def _push_new_commit_on(self, branch, content):
        tmp_co = Path(tempfile.mkdtemp(dir=self.tmp.name, prefix=f"push-{branch.replace('/', '-')}-"))
        subprocess.run(["git", "clone", "-q", str(self.bare), str(tmp_co)], check=True)
        for cmd in (["config", "user.email", "t@t"], ["config", "user.name", "t"]):
            _git(tmp_co, *cmd)
        _git(tmp_co, "checkout", "-q", "-B", branch)
        (tmp_co / "f.txt").write_text(content)
        _git(tmp_co, "commit", "-q", "-am", content)
        _git(tmp_co, "push", "-q", "-f", "origin", branch)

    def test_create_workspace_builds_off_freshly_fetched_origin_base(self):
        # A commit lands on origin's main *after* the checkout was cloned — create_workspace must
        # still see it, proving it fetches before cutting the worktree rather than trusting the
        # checkout's already-stale local main.
        self._push_new_commit_on("main", "v2-on-origin")
        ws = worker.create_workspace("proj", "w1", "main")
        self.assertEqual((Path(ws) / "f.txt").read_text(), "v2-on-origin")

    def test_set_branch_renames_without_detaching(self):
        ws = worker.create_workspace("proj", "w1", "main")
        worker.set_branch(ws, "pipeline/triggered-agents-220")
        self.assertEqual(_current_branch(ws), "pipeline/triggered-agents-220")

    def test_land_pr_head_fetches_worker_branch_onto_own_review_branch(self):
        self._push_new_commit_on("pipeline/triggered-agents-220", "worker-content")
        ws = worker.create_workspace("proj", "rev1", "main")
        worker.land_pr_head(ws, "pipeline/triggered-agents-220", "review/triggered-agents-220")
        self.assertEqual(_current_branch(ws), "review/triggered-agents-220")
        self.assertEqual((Path(ws) / "f.txt").read_text(), "worker-content")

    def _no_real_orca(self, real_run):
        """subprocess.run stand-in for tests that exercise worker.teardown for real: orca calls
        (there is no live orca/daemon in a unit test) report failure so teardown falls through to
        its real `rm -rf` fallback; every other command (git, rm) runs for real."""
        def fake_run(args, **kw):
            if args[:1] == [worker.ORCA]:
                return subprocess.CompletedProcess(args, 1, "", "no orca in test")
            return real_run(args, **kw)
        return fake_run

    def test_repeated_reviewer_spawn_reuses_the_review_branch(self):
        # Blocker A (review triggered-agents-220): a red verdict tears down the reviewer worktree
        # via worker.teardown, which drops the checkout but not the `review/<ref>` ref itself. The
        # *next* review spawn must still be able to claim that same branch name, not fail with
        # "a branch named ... already exists".
        self._push_new_commit_on("pipeline/triggered-agents-220", "v1-worker-content")
        real_run = subprocess.run
        with mock.patch("subprocess.run", side_effect=self._no_real_orca(real_run)):
            ws1 = worker.create_workspace("proj", "rev1", "main")
            worker.land_pr_head(ws1, "pipeline/triggered-agents-220", "review/triggered-agents-220")
            worker.teardown(ws1)   # red verdict -> _clear_review tears the reviewer worktree down
        self.assertFalse(Path(ws1).exists())

        self._push_new_commit_on("pipeline/triggered-agents-220", "v2-after-fix")
        ws2 = worker.create_workspace("proj", "rev2", "main")
        worker.land_pr_head(ws2, "pipeline/triggered-agents-220", "review/triggered-agents-220")
        self.assertEqual(_current_branch(ws2), "review/triggered-agents-220")
        self.assertEqual((Path(ws2) / "f.txt").read_text(), "v2-after-fix")

    def test_reclaim_frees_the_branch_from_a_still_alive_old_worktree(self):
        # Blocker B (worker triggered-agents-220): a Blocked card's worktree is deliberately left
        # alive for a human. When vladmesh moves the card back to Ready, bring-up creates a BRAND
        # NEW worktree (naming.dedupe) and must still land it on `pipeline/<ref>` even though the
        # old, still-alive worktree is sitting on that exact branch right now.
        real_run = subprocess.run
        with mock.patch("subprocess.run", side_effect=self._no_real_orca(real_run)):
            old_ws = worker.create_workspace("proj", "w1", "main")
            worker.set_branch(old_ws, "pipeline/triggered-agents-220")
            self.assertTrue(Path(old_ws).exists())   # never torn down while Blocked

            new_ws = worker.create_workspace("proj", "w1-2", "main")
            worker.set_branch(new_ws, "pipeline/triggered-agents-220")   # must not raise

        self.assertFalse(Path(old_ws).exists())   # reclaimed via the normal teardown path
        self.assertEqual(_current_branch(new_ws), "pipeline/triggered-agents-220")

    def test_land_pr_head_never_checks_out_the_pr_branch_name_itself(self):
        # The bug this replaces: `gh pr checkout` names the local branch after the PR's own head,
        # which collides with the worker's live worktree already sitting on that exact branch. A
        # second worktree must be able to land the same content under a different local name at
        # the same time the worker's worktree holds the real branch.
        self._push_new_commit_on("pipeline/triggered-agents-220", "worker-content")
        worker_ws = worker.create_workspace("proj", "w1", "main")
        worker.set_branch(worker_ws, "pipeline/triggered-agents-220")  # worker owns this branch
        review_ws = worker.create_workspace("proj", "rev1", "main")
        worker.land_pr_head(review_ws, "pipeline/triggered-agents-220", "review/triggered-agents-220")
        self.assertEqual(_current_branch(review_ws), "review/triggered-agents-220")
        self.assertNotEqual(_current_branch(worker_ws), _current_branch(review_ws))


class ScrubSecretsTest(unittest.TestCase):
    """worker.scrub_secrets' generic blob backstop must spare git shas and other hex-shaped
    identifiers while still catching base64/token-looking blobs — a comment full of
    «REDACTED»:blob is useless for debugging a CI failure."""

    def test_full_git_sha_survives(self):
        sha = "a" * 40
        text = f"fix landed in commit {sha}, see the diff"
        self.assertIn(sha, worker.scrub_secrets(text))

    def test_abbreviated_git_sha_survives(self):
        text = "bisected to 1a2b3c4"
        self.assertIn("1a2b3c4", worker.scrub_secrets(text))

    def test_hex_blob_past_git_sha_length_is_still_masked(self):
        # A sha256-shaped hex digest (64 chars) is past the git-sha bound (7-40) — the exemption
        # must not silently widen into "any hex, any length".
        digest = "a" * 64
        text = f"image digest sha256:{digest} pulled"
        out = worker.scrub_secrets(text)
        self.assertNotIn(digest, out)
        self.assertIn("blob", out)

    def test_token_like_blob_is_masked(self):
        # Mixed-case + digits, no known key prefix, not pure hex — the shape _BLOB_RE exists for.
        token = "Zk9mQwErTy1234567890AbCdEfGhIjKlMnOpQrSt"
        text = f"leaked token {token} in the log"
        out = worker.scrub_secrets(text)
        self.assertNotIn(token, out)
        self.assertIn("blob", out)


if __name__ == "__main__":
    unittest.main()
