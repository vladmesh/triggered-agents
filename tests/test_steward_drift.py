"""Unit tests for triggered_agents.agents.steward.drift (triggered-agents-256/276): live ta-*
systemd units and installed gate script vs. the render deploy/provision.py would produce right now
from the current checkout. Fake paths throughout, no real host units are ever touched.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # repo root

import deploy.provision as provision  # noqa: E402
from triggered_agents.agents.pipeline import worker as pipeline_worker  # noqa: E402
from triggered_agents.agents.steward import drift  # noqa: E402

_SPEC = """
name = "{agent}"
skill = "/{agent}"
provider = "claude"
reuse_session = true
precheck = "python3 -m triggered_agents {agent} precheck"

[systemd]
calendar = "hourly"
randomized_delay_sec = 90
"""


class DriftCheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)

        self.agents_dir = root / "agents"
        self.agents_dir.mkdir()
        self.systemd_dir = root / "systemd"
        self.systemd_dir.mkdir()
        self.workspaces_root = root / "workspaces"
        self.gate_source = root / "repo" / "deploy" / "ta-gate.sh"
        self.gate_source.parent.mkdir(parents=True)
        self.gate_source.write_text("#!/bin/sh\necho gate\n", encoding="utf-8")
        self.gate_install = root / "usr" / "local" / "bin" / "ta-gate.sh"
        self.gate_install.parent.mkdir(parents=True)
        self.gate_install.write_text(self.gate_source.read_text(encoding="utf-8"), encoding="utf-8")

        for p, target in ((provision, "AGENTS_DIR"), ):
            self._patch(p, target, self.agents_dir)
        self._patch(provision, "GATE_SCRIPT_SRC", self.gate_source)
        self._patch(provision, "GATE_INSTALL_PATH", self.gate_install)
        self._patch(drift, "SYSTEMD_DIR", self.systemd_dir)
        self._patch(pipeline_worker, "AGENTS_ROOT", self.workspaces_root)
        # Real orca is never reachable/relevant in these tests; default to "no automations known"
        # so the double-schedule check (triggered-agents-444) stays quiet unless a test opts in
        # below via self._fake_orca_automations({...}).
        self._patch(provision, "orca_json", lambda args: {"result": {"automations": []}})

    def _fake_orca_automations(self, states: dict) -> None:
        automations = [{"name": name, "enabled": enabled} for name, enabled in states.items()]
        self._patch(provision, "orca_json", lambda args: {"result": {"automations": automations}})

    def _patch(self, target, attr, value) -> None:
        p = mock.patch.object(target, attr, value)
        p.start()
        self.addCleanup(p.stop)

    def _write_spec(self, agent: str) -> None:
        d = self.agents_dir / agent
        d.mkdir()
        (d / "automation.toml").write_text(_SPEC.format(agent=agent), encoding="utf-8")

    def _write_spec_opted_into_single_scheduler(self, agent: str) -> None:
        # Real curator canon: `[trigger] enabled = false` (triggered-agents-444) — the
        # double-schedule check only ever applies to an agent whose own spec makes this promise.
        d = self.agents_dir / agent
        d.mkdir()
        (d / "automation.toml").write_text(
            _SPEC.format(agent=agent) + "\n[trigger]\nenabled = false\n", encoding="utf-8")

    def _expected(self, agent: str) -> tuple[str, str]:
        ws = self.workspaces_root / agent
        service = provision._service_unit(agent, "hourly", ws,
                                          f"python3 -m triggered_agents {agent} precheck")
        timer = provision._timer_unit(agent, "hourly", 90)
        return service, timer

    def test_in_sync_when_live_matches_current_render(self):
        self._write_spec("curator")
        service, timer = self._expected("curator")
        (self.systemd_dir / "ta-curator.service").write_text(service, encoding="utf-8")
        (self.systemd_dir / "ta-curator.timer").write_text(timer, encoding="utf-8")

        result = drift.check()
        self.assertTrue(result["in_sync"])
        self.assertEqual(result["drift"], [])

    def test_content_drift_when_live_unit_is_stale(self):
        self._write_spec("curator")
        service, timer = self._expected("curator")
        (self.systemd_dir / "ta-curator.service").write_text(service.replace("hourly", "OLD-CAL"),
                                                             encoding="utf-8")
        (self.systemd_dir / "ta-curator.timer").write_text(timer, encoding="utf-8")

        result = drift.check()
        self.assertFalse(result["in_sync"])
        kinds = {h["unit"]: h["kind"] for h in result["drift"]}
        self.assertEqual(kinds["ta-curator.service"], "content")
        self.assertIn("OLD-CAL", result["drift"][0]["diff"])

    def test_missing_unit_when_spec_exists_but_never_provisioned(self):
        self._write_spec("curator")
        result = drift.check()
        self.assertFalse(result["in_sync"])
        kinds = {h["unit"]: h["kind"] for h in result["drift"]}
        self.assertEqual(kinds["ta-curator.service"], "missing")
        self.assertEqual(kinds["ta-curator.timer"], "missing")

    def test_extra_unit_when_agent_decommissioned_but_unit_left_on_host(self):
        # No spec at all for "board" any more; a live unit with no matching spec is exactly the
        # ta-board class this check exists to catch (2026-07-04).
        (self.systemd_dir / "ta-board.service").write_text("stale content", encoding="utf-8")
        (self.systemd_dir / "ta-board.timer").write_text("stale content", encoding="utf-8")

        result = drift.check()
        self.assertFalse(result["in_sync"])
        kinds = {h["unit"]: h["kind"] for h in result["drift"]}
        self.assertEqual(kinds["ta-board.service"], "extra")
        self.assertEqual(kinds["ta-board.timer"], "extra")

    def test_variant_unit_is_checked_too(self):
        d = self.agents_dir / "steward"
        d.mkdir()
        (d / "automation.toml").write_text(_SPEC.format(agent="steward") + """
[variants.deep-sweep]
skill = "/steward deep-sweep"

[variants.deep-sweep.systemd]
calendar = "03:47:00"
randomized_delay_sec = 600
""", encoding="utf-8")
        service, timer = self._expected("steward")
        (self.systemd_dir / "ta-steward.service").write_text(service, encoding="utf-8")
        (self.systemd_dir / "ta-steward.timer").write_text(timer, encoding="utf-8")
        # No ta-steward-deep-sweep unit written at all -> missing.

        result = drift.check()
        self.assertFalse(result["in_sync"])
        units = {h["unit"] for h in result["drift"]}
        self.assertIn("ta-steward-deep-sweep.service", units)
        self.assertIn("ta-steward-deep-sweep.timer", units)

    def test_gate_script_content_drift_when_live_script_is_stale(self):
        self.gate_install.write_text("#!/bin/sh\necho stale\n", encoding="utf-8")

        result = drift.check()

        self.assertFalse(result["in_sync"])
        kinds = {h["unit"]: h["kind"] for h in result["drift"]}
        self.assertEqual(kinds[str(self.gate_install)], "content")
        self.assertIn("stale", result["drift"][0]["diff"])

    def test_missing_gate_script_when_source_exists_but_host_has_none(self):
        self.gate_install.unlink()

        result = drift.check()

        self.assertFalse(result["in_sync"])
        kinds = {h["unit"]: h["kind"] for h in result["drift"]}
        self.assertEqual(kinds[str(self.gate_install)], "missing")

    def test_render_markdown_reports_clean_when_in_sync(self):
        result = {"in_sync": True, "drift": []}
        self.assertIn("нет", drift.render_markdown(result))

    def test_render_markdown_lists_every_hit(self):
        result = {"in_sync": False, "drift": [
            {"unit": "ta-curator.service", "kind": "content", "diff": "- old\n+ new\n"},
        ]}
        out = drift.render_markdown(result)
        self.assertIn("ta-curator.service", out)
        self.assertIn("content", out)
        self.assertIn("- old", out)

    def _write_synced_curator(self) -> None:
        self._write_spec_opted_into_single_scheduler("curator")
        service, timer = self._expected("curator")
        (self.systemd_dir / "ta-curator.service").write_text(service, encoding="utf-8")
        (self.systemd_dir / "ta-curator.timer").write_text(timer, encoding="utf-8")

    def test_double_schedule_drift_when_timer_live_and_automation_still_enabled(self):
        # triggered-agents-444: ta-curator.timer on the host AND the Orca automation "curator"
        # still enabled — one hourly tick could dispatch /curate twice.
        self._write_synced_curator()
        self._fake_orca_automations({"curator": True})

        result = drift.check()

        self.assertFalse(result["in_sync"])
        hits = [h for h in result["drift"] if h["kind"] == "double-schedule"]
        self.assertEqual(len(hits), 1)
        self.assertIn("curator", hits[0]["unit"])

    def test_no_double_schedule_when_automation_is_disabled(self):
        self._write_synced_curator()
        self._fake_orca_automations({"curator": False})

        result = drift.check()

        self.assertTrue(result["in_sync"])
        self.assertEqual(result["drift"], [])

    def test_no_double_schedule_when_no_automation_of_that_name_exists(self):
        # Query succeeded but returned zero automations (or none named "curator") -> nothing to
        # double-fire from, correctly not flagged. Distinct from an actual query failure below.
        self._write_synced_curator()
        # no _fake_orca_automations call -> setUp default (query succeeds, empty list) applies

        result = drift.check()

        self.assertTrue(result["in_sync"])

    def test_double_schedule_unknown_when_orca_query_fails(self):
        # triggered-agents-444 review fixup (round 2): a query failure must not silently read as
        # "confirmed fine" for an agent that opted into single-scheduler ownership and has a live
        # timer — the whole point of this check is to catch curator's automation coming back
        # enabled, so an unverifiable state has to stay red, not turn quiet.
        self._write_synced_curator()

        def _boom(_args):
            raise RuntimeError("orca unreachable")
        self._patch(provision, "orca_json", _boom)

        result = drift.check()

        self.assertFalse(result["in_sync"])
        hits = [h for h in result["drift"] if h["kind"] == "double-schedule-unknown"]
        self.assertEqual(len(hits), 1)
        self.assertIn("curator", hits[0]["unit"])

    def test_no_double_schedule_unknown_for_agent_that_never_opted_in_when_query_fails(self):
        # A query failure must not spuriously flag retro/steward either — they never opted in, so
        # there is no invariant of theirs left unverified.
        self._write_spec("retro")
        service, timer = self._expected("retro")
        (self.systemd_dir / "ta-retro.service").write_text(service, encoding="utf-8")
        (self.systemd_dir / "ta-retro.timer").write_text(timer, encoding="utf-8")

        def _boom(_args):
            raise RuntimeError("orca unreachable")
        self._patch(provision, "orca_json", _boom)

        result = drift.check()

        kinds = {h["kind"] for h in result["drift"]}
        self.assertNotIn("double-schedule-unknown", kinds)
        self.assertNotIn("double-schedule", kinds)

    def test_no_double_schedule_when_timer_was_never_provisioned(self):
        # Spec exists but ta-curator.timer isn't live on the host yet -> reported as "missing"
        # unit drift, never as a double-schedule (there is only one live owner, not two).
        self._write_spec_opted_into_single_scheduler("curator")
        self._fake_orca_automations({"curator": True})

        result = drift.check()

        kinds = {h["kind"] for h in result["drift"]}
        self.assertNotIn("double-schedule", kinds)

    def test_no_double_schedule_for_agent_that_never_opted_in(self):
        # retro/steward keep today's default (Orca trigger enabled) — out of scope for this card
        # (triggered-agents-444). A live timer + a live enabled automation for one of them is their
        # unchanged, intended state, not drift: the check must not flag agents that never promised
        # single-scheduler ownership in their own spec (review fixup — a prior version of this
        # check flagged every agent unconditionally and would have misfired on retro/steward).
        self._write_spec("retro")   # plain _SPEC, no [trigger] section -> not opted in
        service, timer = self._expected("retro")
        (self.systemd_dir / "ta-retro.service").write_text(service, encoding="utf-8")
        (self.systemd_dir / "ta-retro.timer").write_text(timer, encoding="utf-8")
        self._fake_orca_automations({"retro": True})

        result = drift.check()

        kinds = {h["kind"] for h in result["drift"]}
        self.assertNotIn("double-schedule", kinds)

    def test_no_double_schedule_for_deterministic_dispatcher_agent(self):
        # A dispatcher-style agent (e.g. pipeline) never gets an Orca automation at all; an
        # unrelated automation happening to share its name must not be misread as a double owner.
        d = self.agents_dir / "pipeline"
        d.mkdir()
        (d / "automation.toml").write_text("""
name = "pipeline"
dispatcher = true
precheck = "python3 -m triggered_agents pipeline precheck"

[systemd]
calendar = "*:0/3:00"
randomized_delay_sec = 20
""", encoding="utf-8")
        service = provision._service_unit("pipeline", "*:0/3:00", self.workspaces_root / "pipeline", "")
        timer = provision._timer_unit("pipeline", "*:0/3:00", 20)
        (self.systemd_dir / "ta-pipeline.service").write_text(service, encoding="utf-8")
        (self.systemd_dir / "ta-pipeline.timer").write_text(timer, encoding="utf-8")
        self._fake_orca_automations({"pipeline": True})

        result = drift.check()

        kinds = {h["kind"] for h in result["drift"]}
        self.assertNotIn("double-schedule", kinds)

    def test_orca_automation_states_returns_none_on_query_failure(self):
        # None (not {}): a failed query is distinct from a successful query that found zero
        # automations — the double-schedule check needs to tell them apart (triggered-agents-444
        # review fixup, round 2).
        def _boom(_args):
            raise RuntimeError("orca unreachable")
        self._patch(provision, "orca_json", _boom)

        self.assertIsNone(drift._orca_automation_states())

    def test_orca_automation_states_returns_empty_dict_when_none_exist(self):
        self._patch(provision, "orca_json", lambda args: {"result": {"automations": []}})

        self.assertEqual(drift._orca_automation_states(), {})

    def test_orca_automation_states_returns_none_on_malformed_response(self):
        # No "automations" key at all in the response shape -> treated the same as a failure, not
        # coerced to {} (which would read as "confirmed zero automations").
        self._patch(provision, "orca_json", lambda args: {"result": {}})

        self.assertIsNone(drift._orca_automation_states())


if __name__ == "__main__":
    unittest.main()
