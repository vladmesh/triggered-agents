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


if __name__ == "__main__":
    unittest.main()
