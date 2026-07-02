"""Unit tests for the stand module (Validate layer 2 host side) — no docker, no git, no network.

stand._run (the only subprocess boundary) and _checkout_branch/_wait_health are stubbed, so the
real orchestration runs: config validation, compose arg building against the PR checkout, the
stage sequence up -> health -> e2e, and the always-teardown finally.
"""
from __future__ import annotations

import os
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
