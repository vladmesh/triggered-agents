"""Unit tests for deploy/provision.py's ensure_automation (triggered-agents-444): the embedded
Orca automation's own `enabled` trigger flag must be driven by the agent's automation.toml spec
on every create AND edit, so a re-provision reasserts it instead of only setting it once at
creation. Only exercises the pure command-building logic — orca/run are faked, same split as
test_provision_gate.py.
"""
from __future__ import annotations

import sys
import tomllib
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # repo root

import deploy.provision as provision  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]


class EnsureAutomationFlagTest(unittest.TestCase):
    def setUp(self):
        self.runs = []
        self.creates = []
        self._patch(provision, "run", lambda cmd, **kw: self.runs.append(cmd))
        self._patch(provision, "orca_json", self._fake_orca_json)
        self._patch(provision, "_automation_id_by_name", lambda name: self.existing_id)
        self.existing_id = None

    def _patch(self, target, attr, value) -> None:
        p = mock.patch.object(target, attr, value)
        p.start()
        self.addCleanup(p.stop)

    def _fake_orca_json(self, args):
        self.creates.append(args)
        return {"result": {"automation": {"id": "new-id"}}}

    def _spec(self, name: str, *, trigger_enabled=None) -> dict:
        spec = {
            "name": name,
            "skill": f"/{name}",
            "provider": "claude",
            "reuse_session": True,
            "precheck": f"python3 -m triggered_agents {name} precheck",
            "trigger": {"orca": "hourly"},
        }
        if trigger_enabled is not None:
            spec["trigger"]["enabled"] = trigger_enabled
        return spec

    def test_create_passes_disabled_when_spec_disables_orca_trigger(self):
        provision.ensure_automation(self._spec("curator", trigger_enabled=False), Path("/ws/curator"))
        args = self.creates[0]
        self.assertIn("--disabled", args)
        self.assertNotIn("--enabled", args)

    def test_create_passes_enabled_when_spec_is_silent_on_trigger(self):
        # Backward-compat default for agents that never opted into single-scheduler ownership
        # (retro, steward): today's behavior (always enabled) must not change under them.
        provision.ensure_automation(self._spec("retro"), Path("/ws/retro"))
        args = self.creates[0]
        self.assertIn("--enabled", args)
        self.assertNotIn("--disabled", args)

    def test_edit_reasserts_disabled_on_every_reprovision(self):
        # A stray manual re-enable in the Orca GUI must be corrected back on the next provision,
        # not just set once at creation time.
        self.existing_id = "existing-id"
        provision.ensure_automation(self._spec("curator", trigger_enabled=False), Path("/ws/curator"))
        edit_cmd = self.runs[0]
        self.assertIn("automations", edit_cmd)
        self.assertIn("edit", edit_cmd)
        self.assertIn("existing-id", edit_cmd)
        self.assertIn("--disabled", edit_cmd)
        self.assertNotIn("--enabled", edit_cmd)

    def test_edit_keeps_enabled_for_a_spec_that_never_disables(self):
        self.existing_id = "existing-id"
        provision.ensure_automation(self._spec("steward"), Path("/ws/steward"))
        edit_cmd = self.runs[0]
        self.assertIn("--enabled", edit_cmd)
        self.assertNotIn("--disabled", edit_cmd)


class CuratorSpecScopeTest(unittest.TestCase):
    """Agents that declare systemd as sole scheduler keep their Orca trigger disabled."""

    def test_curator_disables_its_own_orca_trigger(self):
        spec = tomllib.loads((REPO_ROOT / "triggered_agents" / "agents" / "curator"
                              / "automation.toml").read_text())
        self.assertIs(spec["trigger"]["enabled"], False)

    def test_steward_disables_its_own_orca_trigger(self):
        spec = tomllib.loads((REPO_ROOT / "triggered_agents" / "agents" / "steward"
                              / "automation.toml").read_text())
        self.assertIs(spec["trigger"]["enabled"], False)

    def test_retro_keeps_its_existing_scheduler_contract(self):
        spec = tomllib.loads((REPO_ROOT / "triggered_agents" / "agents" / "retro"
                              / "automation.toml").read_text())
        self.assertNotIn("enabled", spec.get("trigger", {}))


if __name__ == "__main__":
    unittest.main()
