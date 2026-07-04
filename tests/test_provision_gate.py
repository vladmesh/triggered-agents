"""Unit tests for deploy/provision.py's precheck gate — the actual bash three-way branch that
runs on the host, not just the Python string that builds it. Runs the generated snippet for real
under `bash -c` with a fake precheck/dispatch, one exit code at a time.
"""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root

import deploy.provision as provision  # noqa: E402
from deploy.provision import _precheck_gate  # noqa: E402


def _run_gate(rc: int) -> subprocess.CompletedProcess:
    # A bare `exit N` as the fake precheck would hit bash's own exit builtin and abort the whole
    # gate script right there — real precheck is always a subprocess whose exit doesn't do that,
    # so the stand-in has to be one too (`sh -c 'exit N'` runs and exits as its own process).
    gate = _precheck_gate("pipeline", f"sh -c 'exit {rc}'", "echo DISPATCHED")
    return subprocess.run(["bash", "-c", gate], capture_output=True, text=True)


class PrecheckGateTest(unittest.TestCase):
    def test_rc_zero_dispatches_and_unit_succeeds(self):
        p = _run_gate(0)
        self.assertEqual(p.returncode, 0)
        self.assertIn("DISPATCHED", p.stdout)

    def test_rc_one_skips_cleanly_without_dispatching(self):
        p = _run_gate(1)
        self.assertEqual(p.returncode, 0)  # a plain skip must not fail the unit
        self.assertNotIn("DISPATCHED", p.stdout)
        self.assertIn("no change, run skipped", p.stdout)

    def test_other_rc_fails_the_unit_distinctly_from_skip(self):
        p = _run_gate(2)
        self.assertEqual(p.returncode, 2)  # propagated, not swallowed — the unit itself fails
        self.assertNotIn("DISPATCHED", p.stdout)
        self.assertIn("ERROR", p.stderr)


class CanonicalRootGuardTest(unittest.TestCase):
    """main() must refuse to provision from a checkout other than ~/triggered-agents: run from a
    task workspace it registers that checkout as a new Orca repo and repoints the live ta-* units
    at worktrees forked off it (2026-07-04, card triggered-agents-257)."""

    def setUp(self):
        self.calls = []
        self._root = provision.REPO_ROOT
        self._provision = provision.provision
        provision.provision = lambda a: self.calls.append(a)

    def tearDown(self):
        provision.REPO_ROOT = self._root
        provision.provision = self._provision

    def test_non_canonical_root_refuses_and_provisions_nothing(self):
        provision.REPO_ROOT = Path("/home/dev/orca/workspaces/triggered-agents/257-drop-board-agent")
        with self.assertRaises(SystemExit) as ctx:
            provision.main(["steward"])
        self.assertIn("non-canonical checkout", str(ctx.exception))
        self.assertEqual(self.calls, [])

    def test_unsafe_root_flag_overrides(self):
        provision.REPO_ROOT = Path("/somewhere/else")
        provision.main(["steward", "--unsafe-root"])
        self.assertEqual(self.calls, ["steward"])

    def test_canonical_root_proceeds(self):
        provision.REPO_ROOT = provision.CANONICAL_ROOT
        provision.main(["steward"])
        self.assertEqual(self.calls, ["steward"])


if __name__ == "__main__":
    unittest.main()
