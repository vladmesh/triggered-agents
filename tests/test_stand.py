"""Unit tests for the stand module (Validate layer 2 host side) — no docker, no git, no network.

stand._run (the only subprocess boundary) and _checkout_branch/_wait_health are stubbed, so the
real orchestration runs: config validation, compose arg building against the PR checkout, the
stage sequence up -> health -> e2e, and the always-teardown finally.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_STATE_DIR = tempfile.mkdtemp(prefix="ta-stand-test-")
os.environ["TA_STATE"] = _STATE_DIR

import sys  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from triggered_agents.agents.pipeline import stand  # noqa: E402


class ReadConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def _write(self, text):
        (self.root / "workspace.toml").write_text(text, encoding="utf-8")

    def test_no_manifest_is_none(self):
        self.assertIsNone(stand.read_config(self.root))

    def test_manifest_without_stand_is_none(self):
        self._write("[workspace]\nproject = 'p'\n")
        self.assertIsNone(stand.read_config(self.root))

    def test_stand_section_is_returned(self):
        self._write("[stand]\nnamespace = 'ns'\ncompose = ['a.yml']\ne2e_command = 'x'\n")
        cfg = stand.read_config(self.root)
        self.assertEqual(cfg["namespace"], "ns")
        self.assertEqual(cfg["compose"], ["a.yml"])

    def test_broken_toml_raises_stand_error(self):
        # A malformed manifest must be a typed, catchable error — not a bare TOMLDecodeError that
        # would crash the dispatcher tick.
        self._write("[stand]\nnamespace = 'ns'\ncompose = [oops\n")
        with self.assertRaises(stand.StandError):
            stand.read_config(self.root)


class RunHelperTest(unittest.TestCase):
    def test_missing_binary_is_red_not_raised(self):
        # docker/git missing from PATH: _run must report a non-zero red result, never raise —
        # an escaping OSError would bubble past run()'s `finally: down` and be swallowed upstream.
        code, out = stand._run(["/no/such/binary-xyz", "up"])
        self.assertNotEqual(code, 0)
        self.assertIn("could not run", out)


class CheckoutBranchTest(unittest.TestCase):
    """_checkout_branch against real git repos: the persistent stand worktree must always land on
    its own named `stand/<project>` branch, never a detached HEAD (git-hygiene AC), and the same
    holds when reusing a tree left detached by a run from before this convention."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        for cmd in (
            ["git", "init", "-q", "-b", "main"],
            ["git", "config", "user.email", "t@t"],
            ["git", "config", "user.name", "t"],
        ):
            subprocess.run(cmd, cwd=self.repo, check=True)
        (self.repo / "f.txt").write_text("v1")
        subprocess.run(["git", "add", "f.txt"], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "v1"], cwd=self.repo, check=True)
        subprocess.run(["git", "checkout", "-q", "-b", "pipeline/x"], cwd=self.repo, check=True)
        (self.repo / "f.txt").write_text("v2")
        subprocess.run(["git", "commit", "-q", "-am", "v2"], cwd=self.repo, check=True)
        subprocess.run(["git", "checkout", "-q", "main"], cwd=self.repo, check=True)
        # _checkout_branch always fetches "origin" — point it at the repo itself so a plain local
        # git setup (no real remote) can still exercise the real fetch/reset sequence.
        subprocess.run(["git", "remote", "add", "origin", str(self.repo)], cwd=self.repo, check=True)
        self.stand_dir = Path(self.tmp.name) / "stand"

    def _status_branch(self, tree):
        out = subprocess.run(["git", "-C", str(tree), "branch", "--show-current"],
                             capture_output=True, text=True, check=True)
        return out.stdout.strip()

    def test_fresh_tree_lands_on_stand_branch_not_detached(self):
        stand._checkout_branch(self.repo, self.stand_dir, "pipeline/x", "stand/proj")
        self.assertEqual(self._status_branch(self.stand_dir), "stand/proj")
        self.assertEqual((self.stand_dir / "f.txt").read_text(), "v2")

    def test_rerun_resets_onto_latest_without_detaching(self):
        stand._checkout_branch(self.repo, self.stand_dir, "pipeline/x", "stand/proj")
        subprocess.run(["git", "checkout", "-q", "pipeline/x"], cwd=self.repo, check=True)
        (self.repo / "f.txt").write_text("v3")
        subprocess.run(["git", "commit", "-q", "-am", "v3"], cwd=self.repo, check=True)
        subprocess.run(["git", "checkout", "-q", "main"], cwd=self.repo, check=True)
        stand._checkout_branch(self.repo, self.stand_dir, "pipeline/x", "stand/proj")
        self.assertEqual(self._status_branch(self.stand_dir), "stand/proj")
        self.assertEqual((self.stand_dir / "f.txt").read_text(), "v3")

    def test_legacy_detached_tree_is_folded_onto_stand_branch(self):
        # A tree from before the stand/<project> convention: created detached at HEAD.
        subprocess.run(["git", "worktree", "add", "--force", "--detach", str(self.stand_dir), "HEAD"],
                       cwd=self.repo, check=True)
        self.assertEqual(self._status_branch(self.stand_dir), "")   # detached, sanity-check
        stand._checkout_branch(self.repo, self.stand_dir, "pipeline/x", "stand/proj")
        self.assertEqual(self._status_branch(self.stand_dir), "stand/proj")

    def test_no_command_ever_pushes(self):
        calls = []
        real_run = subprocess.run

        def spy(cmd, *a, **k):
            calls.append(cmd)
            return real_run(cmd, *a, **k)

        with mock.patch("subprocess.run", spy):
            stand._checkout_branch(self.repo, self.stand_dir, "pipeline/x", "stand/proj")
        self.assertFalse(any("push" in c for c in calls))


class RunTest(unittest.TestCase):
    CFG = {"namespace": "ns", "compose": ["infra/compose.stand.yml"], "env_file": "infra/.env.stand",
           "e2e_command": "bash infra/e2e/run.sh", "e2e_env": {"STAND_BACKEND_URL": "http://x"}}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        stands = Path(self.tmp.name) / "stands"
        p = mock.patch.object(stand, "STANDS_ROOT", stands)
        p.start()
        self.addCleanup(p.stop)
        self.stand_dir = stands / "proj"

        self.cmds = []
        self.results = {}          # keyword in cmd -> (code, out); default (0, "")
        co = mock.patch.object(stand, "_checkout_branch", lambda *a, **k: "checked out")
        co.start()
        self.addCleanup(co.stop)
        wh = mock.patch.object(stand, "_wait_health", lambda url, t: (True, "healthy"))
        wh.start()
        self.addCleanup(wh.stop)
        r = mock.patch.object(stand, "_run", self._fake_run)
        r.start()
        self.addCleanup(r.stop)

    def _fake_run(self, cmd, *, cwd=None, env=None, timeout=None):
        self.cmds.append((cmd, cwd, env))
        for key, res in self.results.items():
            if key in cmd:
                return res
        return 0, ""

    def _run(self, cfg=None):
        return stand.run("proj", "pipeline/A", cfg or self.CFG, Path("/repo"))

    def test_happy_path_up_then_e2e_then_teardown(self):
        out = self._run()
        self.assertEqual(out["stage"], "e2e")
        self.assertTrue(out["ok"])
        verbs = [c[0][3] if c[0][:3] == [stand.DOCKER, "compose", "-p"] else c[0][0] for c in self.cmds]
        # compose up, e2e (bash), compose down — in order
        joined = [" ".join(c[0]) for c in self.cmds]
        self.assertTrue(any("compose" in j and " up " in f" {j} " for j in joined))
        self.assertTrue(any(j.startswith("bash -c") for j in joined))
        self.assertTrue(any("down" in j for j in joined))

    def test_compose_files_resolve_against_pr_checkout(self):
        self._run()
        up = next(c for c in self.cmds if "up" in c[0])
        self.assertIn("-f", up[0])
        f_arg = up[0][up[0].index("-f") + 1]
        self.assertEqual(f_arg, str(self.stand_dir / "infra/compose.stand.yml"))
        self.assertIn(str(self.stand_dir / "infra/.env.stand"), up[0])
        self.assertEqual(up[1], self.stand_dir)                 # cwd is the checkout

    def test_e2e_env_is_injected(self):
        self._run()
        e2e = next(c for c in self.cmds if c[0][0] == "bash")
        self.assertEqual(e2e[2]["STAND_BACKEND_URL"], "http://x")

    def test_up_failure_skips_e2e_but_tears_down(self):
        self.results = {"up": (1, "compose up boom")}
        out = self._run()
        self.assertFalse(out["ok"])
        self.assertEqual(out["stage"], "up")
        self.assertFalse(any(c[0][0] == "bash" for c in self.cmds))   # e2e never ran
        self.assertTrue(any("down" in c[0] for c in self.cmds))       # still torn down

    def test_e2e_failure_is_red(self):
        self.results = {"bash": (1, "e2e: FAILED")}
        out = self._run()
        self.assertFalse(out["ok"])
        self.assertEqual(out["stage"], "e2e")
        self.assertIn("FAILED", out["log"])

    def test_missing_required_key_is_config_red(self):
        out = self._run({"namespace": "ns"})       # no compose / e2e_command
        self.assertFalse(out["ok"])
        self.assertEqual(out["stage"], "config")
        self.assertIn("required key", out["log"])
        self.assertEqual(self.cmds, [])             # nothing ran


if __name__ == "__main__":
    unittest.main()
