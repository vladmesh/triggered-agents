"""Unit tests for the steward agent (signals.py + cli.py) — stdlib unittest, no network, no Orca.

TA_STATE points at a tempdir before any triggered_agents import (state.py reads it once at
import). The board goes through FakeBoard (reused from test_pipeline, same pattern as
test_dispatcher); resource health and the workspace root are patched per test.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

_STATE_DIR = tempfile.mkdtemp(prefix="ta-steward-test-")
os.environ["TA_STATE"] = _STATE_DIR
os.environ.pop("TA_PIPELINE_STATE_DIR", None)
os.environ.pop("KANBOARD_ADMIN_USER", None)

sys.path.insert(0, str(Path(__file__).resolve().parent))          # tests/ for test_pipeline
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))       # repo root

from test_pipeline import FakeBoard  # noqa: E402

from triggered_agents.agents.pipeline import model  # noqa: E402
from triggered_agents.agents.steward import cli, signals  # noqa: E402
from triggered_agents.runtime import state as runtime_state  # noqa: E402
from triggered_agents.runtime.state import AgentState  # noqa: E402


class StewardTestBase(unittest.TestCase):
    """Fresh board + fresh steward/pipeline state dirs per test, real files on disk."""

    def _patch(self, target, attr, value) -> None:
        p = mock.patch.object(target, attr, value)
        p.start()
        self.addCleanup(p.stop)

    def setUp(self):
        self.board = FakeBoard()
        board_patcher = mock.patch("triggered_agents.agents.pipeline.ops.call", self.board.call)
        board_patcher.start()
        self.addCleanup(board_patcher.stop)

        # Give every test an isolated steward + pipeline state dir (tests share TA_STATE, so
        # a stale watermark/cards.json from an earlier test must never leak into the next one).
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        # AgentState reads STATE_ROOT at construction time — patch it just long enough to build
        # fresh steward/pipeline state objects pointed at this test's tempdir, then rebind
        # signals.STATE/cli.STATE/PIPELINE_* to them explicitly (both modules bound their own
        # copies against the real STATE_ROOT at import time). signals.py itself never resolves
        # PIPELINE_RUNS/PIPELINE_RESOURCE_HEALTH through STATE_ROOT any more (that was the prod
        # bug — see CrossWorkspaceStateTest below) — this AgentState("pipeline") only exists here
        # as a convenient throwaway directory for most tests to point PIPELINE_RUNS/
        # PIPELINE_RESOURCE_HEALTH at explicitly.
        self._patch(runtime_state, "STATE_ROOT", root)
        new_steward_state = AgentState("steward")
        new_pipeline_state = AgentState("pipeline")
        self._patch(signals, "STATE", new_steward_state)
        self._patch(cli, "STATE", new_steward_state)
        self._patch(signals.pipeline_ops, "STATE", new_pipeline_state)
        self._patch(signals, "PIPELINE_RUNS", new_pipeline_state.dir / "runs.jsonl")
        self._patch(signals, "PIPELINE_RESOURCE_HEALTH", new_pipeline_state.dir / "resource_health.json")
        self.pipeline_state = new_pipeline_state

        self.ws_root = root / "workspaces"
        self.ws_root.mkdir()
        self._patch(signals, "WORKSPACES_ROOT", self.ws_root)

        # Default steady state for every test but LogSignalsTest's missing-file case: the
        # dispatcher's runs.jsonl exists and is caught up, same as a live pipeline mid-tick — a
        # test that doesn't care about the log signal shouldn't have to know a missing file is
        # now itself a signal (triggered-agents-253).
        self._write_pipeline_runs([])

    def _write_pipeline_runs(self, records: list[dict]) -> None:
        self.pipeline_state.ensure_dir()
        with open(signals.PIPELINE_RUNS, "w", encoding="utf-8") as f:
            for rec in records:
                f.write(json.dumps(rec) + "\n")

    def _write_resource_health(self, statuses: dict) -> None:
        """`statuses` is the plain {resource_id: status} shape signals.py deals in — wrapped here
        into the {resource_id: {"status": ..., "checked_at": ...}} shape pipeline/health.py's own
        cache file actually uses on disk, since that's the format _resource_signals now reads."""
        self.pipeline_state.ensure_dir()
        cache = {rid: {"status": status, "checked_at": 0} for rid, status in statuses.items()}
        signals.PIPELINE_RESOURCE_HEALTH.write_text(json.dumps(cache), encoding="utf-8")


class LogSignalsTest(StewardTestBase):
    def test_missing_file_is_a_warn_signal_not_silence(self):
        """A missing PIPELINE_RUNS must never look like "checked, nothing new" — that ambiguity
        is exactly what let the cross-workspace path bug (triggered-agents-253) go unnoticed."""
        signals.PIPELINE_RUNS.unlink()  # setUp's default steady-state file — remove it for real
        batch = signals.scan()
        self.assertEqual(len(batch["signals"]["log"]), 1)
        self.assertEqual(batch["signals"]["log"][0]["level"], "warn")
        self.assertEqual(batch["pending"]["pipeline_log_lines"], 0)
        self.assertTrue(signals.has_signal(batch))
        # ... and it lands in the steward's OWN runs.jsonl too, not just this scan's in-memory batch.
        own_runs = signals.STATE.dir / "runs.jsonl"
        self.assertTrue(own_runs.is_file())
        own_events = [json.loads(line) for line in own_runs.read_text(encoding="utf-8").splitlines()]
        self.assertTrue(any(e.get("event") == "pipeline-log-missing" and e.get("level") == "warn"
                             for e in own_events))

    def test_warn_and_error_and_health_flip_are_signals_plain_events_are_not(self):
        self._write_pipeline_runs([
            {"ts": "t1", "event": "dispatch", "action": "reused"},          # plain, not a signal
            {"ts": "t2", "event": "ff-agents", "result": "error", "level": "warn", "error": "x"},
            {"ts": "t3", "event": "precheck", "result": "error", "error_class": "X"},
            {"ts": "t4", "event": "head-health", "resource": "claude-sub", "from": "green", "to": "red"},
        ])
        batch = signals.scan()
        self.assertEqual(len(batch["signals"]["log"]), 3)
        self.assertEqual(batch["pending"]["pipeline_log_lines"], 4)
        self.assertTrue(signals.has_signal(batch))

    def test_cursor_only_sees_lines_past_the_watermark(self):
        self._write_pipeline_runs([{"ts": "t1", "event": "x", "level": "warn"}])
        signals.STATE.save_watermark(signals.scan()["pending"])
        self._write_pipeline_runs([
            {"ts": "t1", "event": "x", "level": "warn"},
            {"ts": "t2", "event": "y", "level": "warn"},
        ])
        batch = signals.scan()
        self.assertEqual(len(batch["signals"]["log"]), 1)
        self.assertEqual(batch["signals"]["log"][0]["ts"], "t2")

    def test_shrunken_file_rescans_from_zero_with_reset_warn(self):
        """runs.jsonl shrinking below the cursor means it was truncated/replaced — the old
        min(cursor, len) clamp skipped the whole new file, and advance() then made that gap
        permanent (2026-07-09: a head-health flip in the fresh file went unreported)."""
        self._write_pipeline_runs([{"ts": f"t{i}", "event": "dispatch"} for i in range(5)])
        signals.STATE.save_watermark(signals.scan()["pending"])
        self._write_pipeline_runs([
            {"ts": "n1", "event": "dispatch"},
            {"ts": "n2", "event": "head-health", "resource": "openai-sub", "from": "green", "to": "red"},
        ])
        batch = signals.scan()
        events = {rec.get("event") for rec in batch["signals"]["log"]}
        self.assertIn("pipeline-log-reset", events)   # the reset itself is visible, not silent
        self.assertIn("head-health", events)          # and the new file is scanned from line 0
        self.assertEqual(batch["pending"]["pipeline_log_lines"], 2)
        self.assertTrue(signals.has_signal(batch))
        own_runs = signals.STATE.dir / "runs.jsonl"
        own_events = [json.loads(line) for line in own_runs.read_text(encoding="utf-8").splitlines()]
        self.assertTrue(any(e.get("event") == "pipeline-log-reset" and e.get("level") == "warn"
                             for e in own_events))

    def test_malformed_line_is_skipped_not_fatal(self):
        signals.PIPELINE_RUNS.parent.mkdir(parents=True, exist_ok=True)
        signals.PIPELINE_RUNS.write_text('not json\n{"ts": "t1", "event": "x", "level": "warn"}\n')
        batch = signals.scan()
        self.assertEqual(len(batch["signals"]["log"]), 1)


class BlockedSignalsTest(StewardTestBase):
    def test_new_blocked_card_is_a_signal(self):
        ref = self.board.add_task("A", "Blocked", meta={"project": "personal_site"})
        batch = signals.scan()
        self.assertEqual(batch["signals"]["new_blocked"], [ref])
        self.assertTrue(signals.has_signal(batch))

    def test_blocked_steward_report_card_is_not_a_signal(self):
        ref = self.board.add_task("steward: deep-sweep", "Blocked", swimlane="triggered-agents",
                                  meta={"task_type": "research", "project": "triggered-agents",
                                        "slug": "steward-sweep-1", "claim": "steward-sweep-1",
                                        "steward_report": "1"})
        batch = signals.scan()
        self.assertEqual(batch["signals"]["new_blocked"], [])
        self.assertFalse(signals.has_signal(batch))
        self.assertNotIn(ref, batch["pending"]["notified_blocked"])

    def test_already_notified_blocked_card_does_not_refire(self):
        ref = self.board.add_task("A", "Blocked", meta={"project": "personal_site"})
        signals.STATE.save_watermark(signals.scan()["pending"])
        batch = signals.scan()
        self.assertEqual(batch["signals"]["new_blocked"], [])
        self.assertFalse(signals.has_signal(batch))
        self.assertIn(ref, batch["pending"]["notified_blocked"])  # still tracked as Blocked

    def test_card_leaving_blocked_drops_out_of_the_next_watermark(self):
        ref = self.board.add_task("A", "Blocked", meta={"project": "personal_site"})
        signals.STATE.save_watermark(signals.scan()["pending"])
        self.board.tasks[1]["column_id"] = next(
            c["id"] for c in self.board.columns if c["title"] == "Ready")
        batch = signals.scan()
        self.assertNotIn(ref, batch["pending"]["notified_blocked"])


class StaleSignalsTest(StewardTestBase):
    def test_fresh_card_is_not_stale(self):
        self.board.add_task("A", "Ready", meta={"project": "personal_site"})
        self.board.tasks[1]["date_moved"] = int(time.time())
        batch = signals.scan()
        self.assertEqual(batch["signals"]["stale"], [])

    def test_old_card_past_threshold_is_a_signal(self):
        self.board.add_task("A", "Ready", meta={"project": "personal_site"})
        self.board.tasks[1]["date_moved"] = int(time.time()) - int(signals.STALE_HOURS * 3600) - 10
        batch = signals.scan()
        self.assertEqual(len(batch["signals"]["stale"]), 1)
        self.assertEqual(batch["signals"]["stale"][0]["column"], "Ready")

    def test_idea_column_is_never_stale(self):
        self.board.add_task("A", "Идеи", meta={"project": "personal_site"})
        self.board.tasks[1]["date_moved"] = 1  # ancient
        batch = signals.scan()
        self.assertEqual(batch["signals"]["stale"], [])

    def test_already_notified_at_same_date_moved_does_not_refire(self):
        self.board.add_task("A", "Blocked", meta={"project": "personal_site"})
        old = int(time.time()) - int(signals.STALE_HOURS * 3600) - 10
        self.board.tasks[1]["date_moved"] = old
        signals.STATE.save_watermark(signals.scan()["pending"])
        batch = signals.scan()
        self.assertEqual(batch["signals"]["stale"], [])
        # still Blocked -> also not "new_blocked" the second time around
        self.assertEqual(batch["signals"]["new_blocked"], [])

    def test_re_moved_card_re_arms_the_check(self):
        self.board.add_task("A", "Blocked", meta={"project": "personal_site"})
        old = int(time.time()) - int(signals.STALE_HOURS * 3600) - 10
        self.board.tasks[1]["date_moved"] = old
        signals.STATE.save_watermark(signals.scan()["pending"])
        # human recovers it (Blocked -> Ready is a legal po/steward move) then it re-stales
        self.board.tasks[1]["date_moved"] = old - 100  # a "new" stuck episode, distinct timestamp
        batch = signals.scan()
        self.assertEqual(len(batch["signals"]["stale"]), 1)

    def test_fresh_card_is_never_watermarked_just_by_an_unrelated_scan(self):
        """2026-07-04 review, triggered-agents-244 blocker B1 (second round): a NOT-yet-stale
        card's date_moved must never land in notified_stale just because some OTHER signal
        (an unrelated Blocked card, here) triggered a scan+advance while it was still fresh."""
        moved_at = int(time.time()) - 60  # just moved, one minute ago — nowhere near stale
        self.board.add_task("A", "Ready", meta={"project": "personal_site"})
        self.board.tasks[1]["date_moved"] = moved_at
        self.board.add_task("B", "Blocked", meta={"project": "personal_site"})  # unrelated signal
        signals.STATE.save_watermark(signals.scan()["pending"])
        mark = signals.load_watermark()
        self.assertNotIn("personal_site-1", mark["notified_stale"])

    def test_fresh_card_swept_by_an_unrelated_signal_still_fires_once_it_goes_stale(self):
        """The consequence of the bug above: without the fix, A's date_moved would already be
        cached as "seen" from the scan below, so it could never fire once genuinely stale."""
        moved_at = int(time.time()) - 60
        self.board.add_task("A", "Ready", meta={"project": "personal_site"})
        self.board.tasks[1]["date_moved"] = moved_at
        self.board.add_task("B", "Blocked", meta={"project": "personal_site"})
        signals.STATE.save_watermark(signals.scan()["pending"])
        # wall-clock now moves past the threshold from A's own, UNCHANGED date_moved
        future = moved_at + int(signals.STALE_HOURS * 3600) + 10
        with mock.patch("time.time", return_value=future):
            batch = signals.scan()
        self.assertEqual([h["reference"] for h in batch["signals"]["stale"]], ["personal_site-1"])


    def test_report_card_stuck_in_progress_is_caught_like_any_other_card(self):
        """triggered-agents-255: a dead steward head leaves its own wake-up report card sitting
        In progress forever — the generic stale detector must catch it exactly like an ordinary
        stuck card, with no special-casing needed (STALE_COLUMNS already covers In progress)."""
        self.board.add_task("steward: hourly sweep", "In progress", swimlane="triggered-agents",
                            meta={"task_type": "research", "project": "triggered-agents",
                                  "slug": "steward-sweep-1", "claim": "steward-sweep-1",
                                  "steward_report": "1"})
        self.board.tasks[1]["date_moved"] = int(time.time()) - int(signals.STALE_HOURS * 3600) - 10
        batch = signals.scan()
        self.assertEqual(len(batch["signals"]["stale"]), 1)
        self.assertEqual(batch["signals"]["stale"][0]["column"], model.IN_PROGRESS)


class ResourceSignalsTest(StewardTestBase):
    """_resource_signals reads the pipeline dispatcher's own resource_health.json cache (staged
    here via _write_resource_health) rather than probing itself — see signals.py's decision note
    on why (triggered-agents-253)."""

    def test_flip_is_a_signal_steady_state_is_not(self):
        self._write_resource_health({"claude-sub": "green"})
        signals.STATE.save_watermark(signals.scan()["pending"])
        self._write_resource_health({"claude-sub": "red"})
        batch = signals.scan()
        self.assertEqual(batch["signals"]["resource_flip"], {"claude-sub": "red"})
        signals.STATE.save_watermark(batch["pending"])
        batch = signals.scan()
        self.assertEqual(batch["signals"]["resource_flip"], {})

    def test_recovery_flip_also_counts(self):
        self._write_resource_health({"claude-sub": "red"})
        signals.STATE.save_watermark(signals.scan()["pending"])
        self._write_resource_health({"claude-sub": "green"})
        batch = signals.scan()
        self.assertEqual(batch["signals"]["resource_flip"], {"claude-sub": "green"})

    def test_missing_cache_file_keeps_previous_baseline(self):
        self._write_resource_health({"claude-sub": "green"})
        signals.STATE.save_watermark(signals.scan()["pending"])
        signals.PIPELINE_RESOURCE_HEALTH.unlink()
        batch = signals.scan()
        self.assertEqual(batch["signals"]["resource_flip"], {})
        self.assertEqual(batch["pending"]["resource_status"], {"claude-sub": "green"})


class OrphanSignalsTest(StewardTestBase):
    """2026-07-04 review, blocker B1: orphan-ness is matched against the board (any column,
    including Blocked — the pipeline deliberately leaves a Blocked card's workspace on disk with
    no cards.json record), not against the dispatcher's local cache."""

    def test_untracked_workspace_dir_is_an_orphan(self):
        (self.ws_root / "personal_site").mkdir()
        (self.ws_root / "personal_site" / "217-fix-bug").mkdir()
        batch = signals.scan()
        self.assertEqual(len(batch["signals"]["new_orphan_workspaces"]), 1)
        self.assertIn("217-fix-bug", batch["signals"]["new_orphan_workspaces"][0])

    def test_workspace_of_an_in_progress_card_is_not_an_orphan(self):
        ws = self.ws_root / "personal_site" / "217-fix-bug"
        ws.mkdir(parents=True)
        self.board.add_task("A", "In progress", reference="personal_site-217",
                            meta={"project": "personal_site"})
        batch = signals.scan()
        self.assertEqual(batch["signals"]["new_orphan_workspaces"], [])

    def test_workspace_of_a_blocked_card_with_no_cards_json_record_is_not_an_orphan(self):
        """The exact false-positive from B1: dispatcher/validate drop the cards.json record on
        Blocked but leave the workspace alive on disk for a human to inspect."""
        ws = self.ws_root / "personal_site" / "217-fix-bug"
        ws.mkdir(parents=True)
        self.board.add_task("A", "Blocked", reference="personal_site-217",
                            meta={"project": "personal_site"})
        batch = signals.scan()
        self.assertEqual(batch["signals"]["new_orphan_workspaces"], [])

    def test_dedupe_suffixed_workspace_still_matches_by_id_prefix(self):
        ws = self.ws_root / "personal_site" / "217-fix-bug-2"
        ws.mkdir(parents=True)
        self.board.add_task("A", "In progress", reference="personal_site-217",
                            meta={"project": "personal_site"})
        batch = signals.scan()
        self.assertEqual(batch["signals"]["new_orphan_workspaces"], [])

    def test_review_workspace_is_also_covered(self):
        ws = self.ws_root / "personal_site" / "review-217-fix-bug"
        ws.mkdir(parents=True)
        self.board.add_task("A", "Validate", reference="personal_site-217",
                            meta={"project": "personal_site"})
        batch = signals.scan()
        self.assertEqual(batch["signals"]["new_orphan_workspaces"], [])

    def test_workspace_of_a_different_card_id_is_still_an_orphan(self):
        (self.ws_root / "personal_site" / "218-other").mkdir(parents=True)
        self.board.add_task("A", "In progress", reference="personal_site-217",
                            meta={"project": "personal_site"})
        batch = signals.scan()
        self.assertEqual(len(batch["signals"]["new_orphan_workspaces"]), 1)
        self.assertIn("218-other", batch["signals"]["new_orphan_workspaces"][0])

    def test_manual_worktree_without_card_id_prefix_is_not_an_orphan(self):
        """2026-07-04, steward run 267: a human-made worktree (dnd-simulator/hook-path-filter,
        live session, uncommitted work) has no pipeline id-prefix at all — not an orphan, the
        pipeline never created it."""
        (self.ws_root / "personal_site" / "hook-path-filter").mkdir(parents=True)
        batch = signals.scan()
        self.assertEqual(batch["signals"]["new_orphan_workspaces"], [])

    def test_agent_worktrees_directory_is_excluded(self):
        (self.ws_root / "triggered-agents" / "curator").mkdir(parents=True)
        batch = signals.scan()
        self.assertEqual(batch["signals"]["new_orphan_workspaces"], [])

    def test_already_notified_orphan_does_not_refire(self):
        (self.ws_root / "personal_site").mkdir()
        (self.ws_root / "personal_site" / "217-fix-bug").mkdir()
        signals.STATE.save_watermark(signals.scan()["pending"])
        batch = signals.scan()
        self.assertEqual(batch["signals"]["new_orphan_workspaces"], [])

    def test_cleaned_up_orphan_drops_out_of_the_next_watermark(self):
        d = self.ws_root / "personal_site" / "217-fix-bug"
        d.mkdir(parents=True)
        signals.STATE.save_watermark(signals.scan()["pending"])
        import shutil
        shutil.rmtree(d)
        batch = signals.scan()
        self.assertEqual(batch["pending"]["notified_orphans"], [])


class NoSignalTest(StewardTestBase):
    def test_quiet_board_and_disk_has_no_signal(self):
        batch = signals.scan()
        self.assertFalse(signals.has_signal(batch))
        self.assertEqual(signals.render_markdown(batch), "steward: нет сигналов с прошлого watermark.\n")


class CliTest(StewardTestBase):
    def test_precheck_exits_nonzero_on_quiet_board(self):
        self.assertEqual(cli.cmd_precheck(), runtime_state.PRECHECK_SKIP)

    def test_precheck_exits_zero_on_a_signal(self):
        self.board.add_task("A", "Blocked", meta={"project": "personal_site"})
        self.assertEqual(cli.cmd_precheck(), 0)

    # Kanboard недоступен/битый env: исход error, rc=2 отличимый от dispatch(0) и skip(100).
    def test_precheck_error_when_scan_raises(self):
        with mock.patch.object(signals, "scan", side_effect=RuntimeError("connection refused")):
            rc = cli.cmd_precheck()
        self.assertEqual(rc, 2)
        self.assertNotIn(rc, (0, runtime_state.PRECHECK_SKIP))
        runs = [json.loads(l) for l in (cli.STATE.dir / "runs.jsonl").read_text().splitlines()]
        precheck_runs = [r for r in runs if r["event"] == "precheck"]
        self.assertTrue(any(r.get("result") == "error" and r.get("error_class") == "RuntimeError"
                             for r in precheck_runs))

    def test_advance_without_scan_first_fails(self):
        self.assertEqual(cli.cmd_advance(), 1)

    def test_scan_then_advance_persists_the_watermark_and_clears_pending(self):
        ref = self.board.add_task("A", "Blocked", meta={"project": "personal_site"})
        self.assertEqual(cli.cmd_scan(as_json=True), 0)
        self.assertTrue(cli.STATE.pending_file.is_file())
        self.assertEqual(cli.cmd_advance(), 0)
        self.assertFalse(cli.STATE.pending_file.is_file())
        mark = signals.load_watermark()
        self.assertIn(ref, mark["notified_blocked"])
        # the same still-Blocked card no longer trips precheck after advance
        self.assertEqual(cli.cmd_precheck(), runtime_state.PRECHECK_SKIP)

    def test_status_reports_the_persisted_watermark(self):
        self.board.add_task("A", "Blocked", meta={"project": "personal_site"})
        cli.cmd_scan(as_json=True)
        cli.cmd_advance()
        self.assertEqual(cli.cmd_status(), 0)

    def test_advance_folds_in_a_card_escalated_during_the_run(self):
        """2026-07-04 review, triggered-agents-244 note Z2: a card the skill itself escalates to
        Blocked AFTER scan() but BEFORE advance() must not look "new" again next hour."""
        # scan() sees a quiet board (nothing Blocked yet) ...
        self.assertEqual(cli.cmd_scan(as_json=True), 0)
        # ... then, still within the same run, the skill escalates a card to Blocked.
        ref = self.board.add_task("A", "Blocked", meta={"project": "personal_site"})
        self.assertEqual(cli.cmd_advance(), 0)
        mark = signals.load_watermark()
        self.assertIn(ref, mark["notified_blocked"])
        self.assertEqual(cli.cmd_precheck(), runtime_state.PRECHECK_SKIP)  # no fresh wake-up next hour


class DeepSweepWatermarkTest(StewardTestBase):
    """cli.py's deep-sweep-since/deep-sweep-advance (triggered-agents-254) — a separate
    watermark from signals.py's scan/advance, tracking only "when did the last unconditional
    sweep run", independent of the five-signal dedup."""

    def test_first_ever_run_reports_null(self):
        rc = cli.cmd_deep_sweep_since()
        self.assertEqual(rc, 0)

    def test_advance_then_since_reports_a_timestamp(self):
        self.assertEqual(cli.cmd_deep_sweep_advance(), 0)
        mark = json.loads(cli.STATE.dir.joinpath("deep_sweep_watermark.json").read_text())
        self.assertIn("last_run", mark)
        self.assertTrue(mark["last_run"])  # non-empty ISO string

    def test_deep_sweep_watermark_is_independent_of_the_signal_watermark(self):
        """Advancing the deep-sweep watermark must not touch signals.py's own watermark.json —
        the two gates must not be able to starve each other of a wake-up."""
        ref = self.board.add_task("A", "Blocked", meta={"project": "personal_site"})
        cli.cmd_deep_sweep_advance()
        # the Blocked card is still a fresh, un-notified signal — deep-sweep advancing must not
        # have folded it into signals.py's watermark.
        self.assertEqual(cli.cmd_precheck(), 0)
        batch = signals.scan()
        self.assertEqual(batch["signals"]["new_blocked"], [ref])

    def test_deep_sweep_advance_logs_its_own_event(self):
        cli.cmd_deep_sweep_advance()
        runs = [json.loads(l) for l in (cli.STATE.dir / "runs.jsonl").read_text().splitlines()]
        self.assertTrue(any(r["event"] == "deep-sweep-advance" for r in runs))


class CrossWorkspaceStateTest(unittest.TestCase):
    """Reproduces the actual prod layout (triggered-agents-253): the steward's own state root and
    the pipeline dispatcher's live state root are two genuinely disjoint directory trees — not
    nested subdirs of one shared tmp root like StewardTestBase's fixtures use for everything else
    (that nesting is exactly why the bug never showed up in the rest of this file). Uses two
    independent tempdirs instead, and never patches signals.PIPELINE_RUNS/PIPELINE_RESOURCE_HEALTH
    directly, so it exercises the module's actual default resolution.

    On the pre-fix code (PIPELINE_RUNS derived from AgentState("pipeline"), i.e. from STATE_ROOT)
    this fails: STATE_ROOT/pipeline/runs.jsonl is empty/absent, so the warn line staged at the
    real cross-workspace path is never seen and `scan()` reports no log signal at all.
    """

    def setUp(self):
        board = FakeBoard()
        board_patcher = mock.patch("triggered_agents.agents.pipeline.ops.call", board.call)
        board_patcher.start()
        self.addCleanup(board_patcher.stop)

        self._state_root = tempfile.TemporaryDirectory()
        self._workspaces_root = tempfile.TemporaryDirectory()
        self.addCleanup(self._state_root.cleanup)
        self.addCleanup(self._workspaces_root.cleanup)
        state_root = Path(self._state_root.name)
        workspaces_root = Path(self._workspaces_root.name)

        patcher_state = mock.patch.object(runtime_state, "STATE_ROOT", state_root)
        patcher_ws = mock.patch.object(signals, "WORKSPACES_ROOT", workspaces_root)
        patcher_pipeline_state_env = mock.patch.dict(os.environ, {"TA_PIPELINE_STATE_DIR": ""})
        patcher_state.start()
        patcher_ws.start()
        patcher_pipeline_state_env.start()
        self.addCleanup(patcher_state.stop)
        self.addCleanup(patcher_ws.stop)
        self.addCleanup(patcher_pipeline_state_env.stop)

        self.state_root = state_root
        self.workspaces_root = workspaces_root
        self._patch(signals, "STATE", AgentState("steward"))
        self._patch(signals.pipeline_ops, "STATE", AgentState("pipeline"))
        self._patch(signals, "PIPELINE_RUNS", signals.resolve_pipeline_state_dir() / "runs.jsonl")
        self._patch(signals, "PIPELINE_RESOURCE_HEALTH",
                    signals.resolve_pipeline_state_dir() / "resource_health.json")

    def _patch(self, target, attr, value) -> None:
        p = mock.patch.object(target, attr, value)
        p.start()
        self.addCleanup(p.stop)

    def test_pipeline_state_dir_is_under_workspaces_root_not_state_root(self):
        resolved = signals.resolve_pipeline_state_dir()
        self.assertEqual(resolved, self.workspaces_root / "triggered-agents" / "pipeline" / "state" / "pipeline")
        self.assertFalse(str(resolved).startswith(str(self.state_root)))

    def test_scan_reads_the_dispatchers_log_at_its_real_cross_workspace_path(self):
        # The old, wrong location this used to resolve to (AgentState("pipeline") under
        # STATE_ROOT) — deliberately left empty/absent, simulating the live host where it "не
        # существует и никогда не появится".
        wrong = self.state_root / "pipeline" / "runs.jsonl"
        self.assertFalse(wrong.exists())

        # The real location: the pipeline agent's own named worktree, sibling of the steward's
        # under the shared workspaces root — exactly where deploy/provision.py puts it and where
        # the live dispatcher actually appends.
        real = self.workspaces_root / "triggered-agents" / "pipeline" / "state" / "pipeline" / "runs.jsonl"
        real.parent.mkdir(parents=True)
        real.write_text(json.dumps({"ts": "t1", "event": "ff-agents", "result": "error",
                                    "level": "warn", "error": "cross-workspace e2e"}) + "\n",
                        encoding="utf-8")

        batch = signals.scan()
        self.assertTrue(signals.has_signal(batch))
        self.assertEqual(len(batch["signals"]["log"]), 1)
        self.assertEqual(batch["signals"]["log"][0]["error"], "cross-workspace e2e")


if __name__ == "__main__":
    unittest.main()
