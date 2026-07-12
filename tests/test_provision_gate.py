"""Unit tests for the versioned precheck gate script that systemd units run."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root

import deploy.provision as provision  # noqa: E402


GATE = Path(__file__).resolve().parents[1] / "deploy" / "ta-gate.sh"


def _run_gate(rc: int, *args: str) -> subprocess.CompletedProcess:
    with tempfile.TemporaryDirectory(prefix="ta-gate-test-") as d:
        root = Path(d)
        pkg = root / "triggered_agents"
        runtime = pkg / "runtime"
        pkg.mkdir()
        runtime.mkdir()
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        (runtime / "__init__.py").write_text("", encoding="utf-8")
        (runtime / "role_env.py").write_text(textwrap.dedent("""
            import os
            import sys

            def main():
                argv = sys.argv[1:]
                if argv[:1] != ["exec"]:
                    raise SystemExit(2)
                idx = argv.index("--")
                cmd = argv[idx + 1:]
                os.execvp(cmd[0], cmd)

            if __name__ == "__main__":
                main()
        """), encoding="utf-8")
        (pkg / "__main__.py").write_text(textwrap.dedent("""
            import os
            import sys

            agent, cmd, *rest = sys.argv[1:]
            if cmd == "precheck":
                print("PATH=" + os.environ.get("PATH", ""))
                print(f"PRECHECK {agent}")
                raise SystemExit(int(os.environ["FAKE_PRECHECK_RC"]))
            if cmd == "dispatch":
                print("DISPATCHED " + " ".join([agent, cmd, *rest]))
                raise SystemExit(int(os.environ.get("FAKE_DISPATCH_RC", "0")))
            raise SystemExit(98)
        """), encoding="utf-8")
        env = os.environ.copy()
        env["PYTHONPATH"] = str(root)
        env["FAKE_PRECHECK_RC"] = str(rc)
        return subprocess.run(["bash", str(GATE), *(args or ("pipeline",))],
                              cwd=root, env=env, capture_output=True, text=True)


class PrecheckGateTest(unittest.TestCase):
    def test_rc_zero_dispatches_and_unit_succeeds(self):
        p = _run_gate(0)
        self.assertEqual(p.returncode, 0)
        self.assertIn("DISPATCHED", p.stdout)

    def test_rc_100_skips_cleanly_without_dispatching(self):
        p = _run_gate(100)
        self.assertEqual(p.returncode, 0)  # a plain skip must not fail the unit
        self.assertNotIn("DISPATCHED", p.stdout)
        self.assertIn("no change, run skipped", p.stdout)

    def test_rc_one_fails_the_unit_distinctly_from_skip(self):
        p = _run_gate(1)
        self.assertEqual(p.returncode, 1)  # Python's default crash code must not be swallowed
        self.assertNotIn("DISPATCHED", p.stdout)
        self.assertIn("ERROR", p.stderr)

    def test_other_rc_fails_the_unit_distinctly_from_skip(self):
        p = _run_gate(2)
        self.assertEqual(p.returncode, 2)  # propagated, not swallowed
        self.assertNotIn("DISPATCHED", p.stdout)
        self.assertIn("ERROR", p.stderr)

    def test_variant_dispatches_without_precheck(self):
        p = _run_gate(1, "steward", "deep-sweep")
        self.assertEqual(p.returncode, 0)
        self.assertNotIn("PRECHECK", p.stdout)
        self.assertIn("DISPATCHED steward dispatch deep-sweep", p.stdout)

    def test_gate_exports_user_cli_path_for_health_probes(self):
        p = _run_gate(100)
        self.assertEqual(p.returncode, 0)
        self.assertTrue(p.stdout.startswith("PATH=/home/dev/.local/bin:/home/dev/bin:"), p.stdout)


class ServiceUnitRenderTest(unittest.TestCase):
    def test_main_unit_execstart_references_gate_script_directly(self):
        unit = provision._service_unit("pipeline", "*:0/3", Path("/ws/pipeline"),
                                       "python3 -m triggered_agents pipeline precheck")
        self.assertIn(f"ExecStart={provision.GATE_INSTALL_PATH} pipeline", unit)
        self.assertNotIn("bash -lc", unit)
        self.assertNotIn("rc=$?", unit)

    def test_install_gate_script_writes_repo_script_and_chmods(self):
        with tempfile.TemporaryDirectory(prefix="ta-gate-install-test-") as d:
            root = Path(d)
            src = root / "ta-gate.sh"
            src.write_text("#!/bin/sh\necho gate\n", encoding="utf-8")
            writes = []
            runs = []
            old_src = provision.GATE_SCRIPT_SRC
            old_path = provision.GATE_INSTALL_PATH
            old_sudo_write = provision._sudo_write
            old_run = provision.run
            try:
                provision.GATE_SCRIPT_SRC = src
                provision.GATE_INSTALL_PATH = Path("/usr/local/bin/ta-gate-test.sh")
                provision._sudo_write = lambda path, content: writes.append((path, content))
                provision.run = lambda cmd, **kwargs: runs.append(cmd)
                provision.install_gate_script()
            finally:
                provision.GATE_SCRIPT_SRC = old_src
                provision.GATE_INSTALL_PATH = old_path
                provision._sudo_write = old_sudo_write
                provision.run = old_run

        self.assertEqual(writes, [(Path("/usr/local/bin/ta-gate-test.sh"), "#!/bin/sh\necho gate\n")])
        self.assertIn(["sudo", "chmod", "755", "/usr/local/bin/ta-gate-test.sh"], runs)


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
