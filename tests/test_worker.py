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

    def test_remote_head_sha_reads_current_branch_tip(self):
        self._push_new_commit_on("pipeline/triggered-agents-240", "v2-content")
        want = _git(self.bare, "rev-parse", "pipeline/triggered-agents-240").stdout.strip()
        self.assertEqual(worker.remote_head_sha("proj", "pipeline/triggered-agents-240"), want)

    def test_remote_head_sha_none_for_unknown_branch(self):
        self.assertIsNone(worker.remote_head_sha("proj", "pipeline/does-not-exist"))

    def test_land_pr_head_pins_to_expected_sha_ignoring_newer_push(self):
        # triggered-agents-240: a contrib worker's session lives on past report:done and could keep
        # pushing before the reviewer is actually spawned — land_pr_head must still land on the sha
        # the report claimed, not whatever origin's tip has become by the time it runs. The second
        # push is a real fast-forward on top of the first (not a force-push off main), the way a
        # worker that keeps committing after report:done actually behaves.
        branch = "pipeline/triggered-agents-220"
        tmp_co = Path(tempfile.mkdtemp(dir=self.tmp.name, prefix="chain-"))
        subprocess.run(["git", "clone", "-q", str(self.bare), str(tmp_co)], check=True)
        for cmd in (["config", "user.email", "t@t"], ["config", "user.name", "t"]):
            _git(tmp_co, *cmd)
        _git(tmp_co, "checkout", "-q", "-B", branch)
        (tmp_co / "f.txt").write_text("reported-content")
        _git(tmp_co, "commit", "-q", "-am", "reported-content")
        _git(tmp_co, "push", "-q", "-f", "origin", branch)
        reported_sha = _git(tmp_co, "rev-parse", "HEAD").stdout.strip()
        (tmp_co / "f.txt").write_text("content-pushed-after-report")
        _git(tmp_co, "commit", "-q", "-am", "content-pushed-after-report")
        _git(tmp_co, "push", "-q", "origin", branch)   # fast-forward, same lineage as reported_sha

        ws = worker.create_workspace("proj", "rev1", "main")
        worker.land_pr_head(ws, branch, "review/triggered-agents-220", expected_sha=reported_sha)
        self.assertEqual(_current_branch(ws), "review/triggered-agents-220")
        self.assertEqual((Path(ws) / "f.txt").read_text(), "reported-content")
        self.assertEqual(_git(ws, "rev-parse", "HEAD").stdout.strip(), reported_sha)

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


class ManifestLookupTest(unittest.TestCase):
    """_load_manifest (via read_base_branch/is_contrib) chains: workspace.toml in the project's
    own repo, else the central control-panel manifest for contrib forks that don't commit one to
    their own repo (agent-kanban-232), else plain defaults — the same three cases provision.py's
    own lookup has to cover, just for worker.py's slice of the decision (base_branch, contrib)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.projects_dir = root / "projects"
        self.projects_dir.mkdir()
        self.control_panel = root / "control-panel"
        (self.control_panel / "pipeline" / "manifests").mkdir(parents=True)

        p1 = mock.patch.object(worker, "PROJECTS_DIR", self.projects_dir)
        p1.start()
        self.addCleanup(p1.stop)
        p2 = mock.patch.object(worker, "CONTROL_PANEL", self.control_panel)
        p2.start()
        self.addCleanup(p2.stop)

    def _project_dir(self, name: str) -> Path:
        d = self.projects_dir / name
        d.mkdir()
        return d

    def _central_manifest(self, name: str) -> Path:
        return self.control_panel / "pipeline" / "manifests" / f"{name}.toml"

    def test_local_workspace_toml_wins_over_central_manifest(self):
        d = self._project_dir("local-proj")
        (d / "workspace.toml").write_text('[workspace]\nbase_branch = "develop"\ncontrib = true\n')
        self._central_manifest("local-proj").write_text(
            '[workspace]\nbase_branch = "central-should-not-win"\n')
        self.assertEqual(worker.read_base_branch("local-proj"), "develop")
        self.assertTrue(worker.is_contrib("local-proj"))

    def test_falls_back_to_central_manifest_when_no_local_one(self):
        self._project_dir("fork-proj")  # on disk, but carries no workspace.toml of its own
        self._central_manifest("fork-proj").write_text(
            '[workspace]\nbase_branch = "main"\ncontrib = true\n')
        self.assertEqual(worker.read_base_branch("fork-proj"), "main")
        self.assertTrue(worker.is_contrib("fork-proj"))

    def test_defaults_when_neither_manifest_exists(self):
        self._project_dir("bare-proj")
        self.assertEqual(worker.read_base_branch("bare-proj"), "main")
        self.assertFalse(worker.is_contrib("bare-proj"))


class ContribBaseRefTest(unittest.TestCase):
    """ensure_contrib_base_ref: idempotent `orca repo set-base-ref` against `orca repo show`'s
    current worktreeBaseRef — a contrib bring-up must converge Orca's host-local default to the
    manifest's declaration without hitting the write path when it's already converged."""

    def setUp(self):
        self.repo_state = {}
        self.calls = []

        def fake_orca_json(args):
            self.calls.append(args)
            if args[0] == "repo" and args[1] == "show":
                return {"repo": dict(self.repo_state)}
            if args[0] == "repo" and args[1] == "set-base-ref":
                ref = args[args.index("--ref") + 1]
                self.repo_state["worktreeBaseRef"] = ref
                return {"repo": dict(self.repo_state)}
            raise AssertionError(f"unexpected orca call: {args}")

        p = mock.patch.object(worker, "_orca_json", fake_orca_json)
        p.start()
        self.addCleanup(p.stop)

    def test_noop_when_already_set(self):
        self.repo_state = {"worktreeBaseRef": "upstream/main",
                            "gitRemoteIdentity": {"remoteName": "upstream"}}
        remote = worker.ensure_contrib_base_ref(Path("/repo"), "main")
        self.assertEqual(remote, "upstream")
        self.assertFalse(any(c[:2] == ["repo", "set-base-ref"] for c in self.calls))

    def test_sets_when_missing_or_stale(self):
        self.repo_state = {"worktreeBaseRef": "origin/main",
                            "gitRemoteIdentity": {"remoteName": "upstream"}}
        remote = worker.ensure_contrib_base_ref(Path("/repo"), "main")
        self.assertEqual(remote, "upstream")
        set_calls = [c for c in self.calls if c[:2] == ["repo", "set-base-ref"]]
        self.assertEqual(len(set_calls), 1)
        self.assertIn("upstream/main", set_calls[0])


class ContribForkBringUpTest(unittest.TestCase):
    """create_workspace for a contrib-fork project (manifest `[workspace] contrib = true`)
    branches the worktree off the upstream remote, never origin — the whole point being that a
    worker's branch (and thus its PR back to the fork) never carries the fork's own origin/main
    history forward (agent-kanban-232's precedent). Real git remotes, fake orca (same discipline
    as GitHygieneTest above)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)

        self.origin_bare = root / "origin.git"
        self.upstream_bare = root / "upstream.git"
        self.checkout = root / "checkout"
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.checkout)], check=True)
        for cmd in (["config", "user.email", "t@t"], ["config", "user.name", "t"]):
            _git(self.checkout, *cmd)
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.origin_bare)], check=True)
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.upstream_bare)], check=True)
        _git(self.checkout, "remote", "add", "origin", str(self.origin_bare))
        _git(self.checkout, "remote", "add", "upstream", str(self.upstream_bare))

        (self.checkout / "f.txt").write_text("v1")
        _git(self.checkout, "add", "f.txt")
        _git(self.checkout, "commit", "-q", "-m", "v1")
        _git(self.checkout, "push", "-q", "origin", "main")
        _git(self.checkout, "push", "-q", "upstream", "main")

        # Diverge: the fork (origin) races ahead with its own commit that upstream never sees —
        # a contrib worktree must be cut from upstream, never from this.
        (self.checkout / "f.txt").write_text("origin-only-v2")
        _git(self.checkout, "commit", "-q", "-am", "origin-only-v2")
        _git(self.checkout, "push", "-q", "origin", "main")
        _git(self.checkout, "reset", "-q", "--hard", "HEAD~1")
        (self.checkout / "f.txt").write_text("upstream-v2")
        _git(self.checkout, "commit", "-q", "-am", "upstream-v2")
        _git(self.checkout, "push", "-q", "upstream", "main")
        _git(self.checkout, "reset", "-q", "--hard", "HEAD~1")

        self.projects_dir = root / "projects"
        self.projects_dir.mkdir()
        (self.projects_dir / "cproj").symlink_to(self.checkout)
        self.control_panel = root / "control-panel"
        (self.control_panel / "pipeline" / "manifests").mkdir(parents=True)
        (self.control_panel / "pipeline" / "manifests" / "cproj.toml").write_text(
            '[workspace]\nbase_branch = "main"\ncontrib = true\n')

        for target, value in (("PROJECTS_DIR", self.projects_dir), ("WORKSPACES_ROOT", root),
                              ("CONTROL_PANEL", self.control_panel)):
            p = mock.patch.object(worker, target, value)
            p.start()
            self.addCleanup(p.stop)

        self.worktrees = []
        self.repo_state = {"worktreeBaseRef": "upstream/main",
                           "gitRemoteIdentity": {"remoteName": "upstream"}}
        self.set_base_ref_calls = []

        def fake_orca_json(args):
            if args[0] == "worktree":
                base_ref = args[args.index("--base-branch") + 1]
                wt_name = args[args.index("--name") + 1]
                path = root / "wt" / f"wt{len(self.worktrees)}"
                subprocess.run(["git", "-C", str(self.checkout), "worktree", "add", "-b", wt_name,
                               str(path), base_ref], check=True, capture_output=True)
                self.worktrees.append(path)
                return {"worktree": {"path": str(path)}}
            if args[0] == "repo" and args[1] == "show":
                return {"repo": dict(self.repo_state)}
            if args[0] == "repo" and args[1] == "set-base-ref":
                ref = args[args.index("--ref") + 1]
                self.set_base_ref_calls.append(ref)
                self.repo_state["worktreeBaseRef"] = ref
                return {"repo": dict(self.repo_state)}
            return {"terminal": {"handle": "term-1"}}

        oj = mock.patch.object(worker, "_orca_json", fake_orca_json)
        oj.start()
        self.addCleanup(oj.stop)

    def test_worktree_is_cut_from_upstream_not_origin(self):
        ws = worker.create_workspace("cproj", "w1", "main")
        self.assertEqual((Path(ws) / "f.txt").read_text(), "upstream-v2")

    def test_base_ref_already_set_is_left_alone(self):
        worker.create_workspace("cproj", "w1", "main")
        self.assertEqual(self.set_base_ref_calls, [])

    def test_base_ref_gets_set_when_missing(self):
        self.repo_state = {"worktreeBaseRef": "origin/main",
                           "gitRemoteIdentity": {"remoteName": "upstream"}}
        worker.create_workspace("cproj", "w1", "main")
        self.assertEqual(self.set_base_ref_calls, ["upstream/main"])


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


class CloseOrphanTerminalsTest(unittest.TestCase):
    """worker._close_orphan_terminals: `--activate` (create_workspace) leaves an empty default
    shell tab behind in every fresh worktree (confirmed empirically on triggered-agents-247 —
    `orca worktree create --agent claude` proved the only alternative, `--agent`, hardcodes its
    own launch command and drops our `--model`/`BOARD_ROLE` env, so it can't replace our own
    `terminal create`). launch_worker/spawn_reviewer call this right after creating their own
    terminal to close everything else in the workspace — these tests exercise that logic directly
    against a fake orca terminal list/close, no git, no real orca."""

    def test_closes_every_handle_except_the_one_to_keep(self):
        calls = []

        def fake_orca_json(args):
            if args[:2] == ["terminal", "list"]:
                return {"terminals": [{"handle": "term-default"}, {"handle": "term-mine"}]}
            if args[:2] == ["terminal", "close"]:
                calls.append(args[args.index("--terminal") + 1])
                return {"close": {"handle": args[args.index("--terminal") + 1]}}
            raise AssertionError(f"unexpected orca call: {args}")

        with mock.patch.object(worker, "_orca_json", fake_orca_json):
            worker._close_orphan_terminals("/ws", "term-mine")

        self.assertEqual(calls, ["term-default"])

    def test_empty_keep_handle_closes_nothing_including_the_head_itself(self):
        # Regression (review verdict, triggered-agents-247): a `terminal create` response with
        # neither "handle" nor "id" makes launch_worker/spawn_reviewer pass keep_handle="" — an
        # empty string never equals a real handle in the loop, so without the guard this would
        # close the just-spawned head along with the orphan shell instead of leaving both alone.
        def fake_orca_json(args):
            raise AssertionError(f"must not even list terminals with no keep_handle: {args}")

        with mock.patch.object(worker, "_orca_json", fake_orca_json):
            worker._close_orphan_terminals("/ws", "")

    def test_never_closes_the_kept_handle(self):
        def fake_orca_json(args):
            if args[:2] == ["terminal", "list"]:
                return {"terminals": [{"handle": "only-mine"}]}
            raise AssertionError(f"must not call close on the only, kept terminal: {args}")

        with mock.patch.object(worker, "_orca_json", fake_orca_json):
            worker._close_orphan_terminals("/ws", "only-mine")  # no close call, no raise

    def test_listing_failure_is_swallowed(self):
        def fake_orca_json(args):
            raise worker.WorkspaceError("boom")

        with mock.patch.object(worker, "_orca_json", fake_orca_json):
            worker._close_orphan_terminals("/ws", "term-mine")  # must not raise

    def test_one_orphans_close_failure_does_not_stop_the_others(self):
        calls = []

        def fake_orca_json(args):
            if args[:2] == ["terminal", "list"]:
                return {"terminals": [{"handle": "orphan-1"}, {"handle": "orphan-2"}, {"handle": "mine"}]}
            if args[:2] == ["terminal", "close"]:
                handle = args[args.index("--terminal") + 1]
                calls.append(handle)
                if handle == "orphan-1":
                    raise worker.WorkspaceError("close failed")
                return {"close": {"handle": handle}}
            raise AssertionError(f"unexpected orca call: {args}")

        with mock.patch.object(worker, "_orca_json", fake_orca_json):
            worker._close_orphan_terminals("/ws", "mine")

        self.assertEqual(calls, ["orphan-1", "orphan-2"])


class LaunchWorkerClosesOrphanTest(unittest.TestCase):
    """launch_worker must hand its own freshly created handle to _close_orphan_terminals as the
    one to keep — not close its own head's terminal along with the leftover default shell."""

    def test_launch_worker_closes_orphans_keeping_its_own_handle(self):
        with mock.patch.object(worker, "ensure_trust"), \
             mock.patch.object(worker, "ensure_theme"), \
             mock.patch.object(worker, "_orca_json", return_value={"terminal": {"handle": "term-new"}}), \
             mock.patch.object(worker, "_close_orphan_terminals") as close:
            handle = worker.launch_worker("/ws", None, "w1", "title")
        self.assertEqual(handle, "term-new")
        close.assert_called_once_with("/ws", "term-new")


class SpawnReviewerClosesOrphanTest(unittest.TestCase):
    """Same contract as LaunchWorkerClosesOrphanTest, for the reviewer's own bring-up path."""

    def test_spawn_reviewer_closes_orphans_keeping_its_own_handle(self):
        with mock.patch.object(worker, "create_workspace", return_value="/ws"), \
             mock.patch.object(worker, "land_pr_head"), \
             mock.patch.object(worker, "_write_excluded"), \
             mock.patch.object(worker, "ensure_trust"), \
             mock.patch.object(worker, "ensure_theme"), \
             mock.patch.object(worker, "_orca_json", return_value={"terminal": {"handle": "term-rev"}}), \
             mock.patch.object(worker, "_close_orphan_terminals") as close:
            ws, handle = worker.spawn_reviewer(
                "proj", "rev1", "main", "REVIEW.md content", "title", "pr-branch", "review-branch")
        self.assertEqual(ws, "/ws")
        self.assertEqual(handle, "term-rev")
        close.assert_called_once_with("/ws", "term-rev")


if __name__ == "__main__":
    unittest.main()
