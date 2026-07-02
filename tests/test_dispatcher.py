"""Unit tests for the pipeline dispatcher — stdlib unittest, no network, no Orca.

TA_STATE points at a tempdir before any triggered_agents import (state.py reads it once at
import). The board goes through FakeBoard (reused from test_pipeline); the host side (worker.py:
worktree/head/activity) is stubbed by FakeWorker so the dispatcher's decisions run for real while
nothing leaves the process.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_STATE_DIR = tempfile.mkdtemp(prefix="ta-dispatcher-test-")
os.environ["TA_STATE"] = _STATE_DIR
os.environ.pop("KANBOARD_ADMIN_USER", None)

sys.path.insert(0, str(Path(__file__).resolve().parent))          # tests/ for test_pipeline
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))       # repo root

from test_pipeline import FakeBoard  # noqa: E402

from triggered_agents.agents.pipeline import dispatcher, model, ops  # noqa: E402


class FakeWorker:
    """Records host-side calls; provisioning result and activity are configurable per test."""

    def __init__(self):
        self.provision_ok = True
        self.provision_log = "[provision] done\n"
        self.create_raises = None
        self.activity_ts = None          # None -> dispatcher falls back to stored last_activity
        self.launched = []
        self.tasks_written = []
        self.torn_down = []
        self._n = 0

    def read_base_branch(self, project):
        return "main"

    def create_workspace(self, project, name, base_branch):
        if self.create_raises:
            raise self.create_raises
        self._n += 1
        return f"/ws/{name}"

    def provision(self, workspace):
        return self.provision_ok, self.provision_log

    def write_task(self, workspace, content):
        self.tasks_written.append((workspace, content))
        return workspace + "/TASK.md"

    def launch_worker(self, workspace, model_name, worker_id):
        self.launched.append({"ws": workspace, "model": model_name, "worker": worker_id})
        return f"handle-{worker_id}"

    def activity(self, workspace):
        return self.activity_ts

    def teardown(self, workspace):
        self.torn_down.append(workspace)


class DispatcherTest(unittest.TestCase):
    def setUp(self):
        # Fresh board transport + host stub for every test; clean the shared pipeline state.
        self.board = FakeBoard()
        p = mock.patch("triggered_agents.agents.pipeline.ops.call", self.board.call)
        p.start()
        self.addCleanup(p.stop)

        self.worker = FakeWorker()
        for name in ("read_base_branch", "create_workspace", "provision", "write_task",
                     "launch_worker", "activity", "teardown"):
            wp = mock.patch(f"triggered_agents.agents.pipeline.worker.{name}",
                            getattr(self.worker, name))
            wp.start()
            self.addCleanup(wp.stop)

        dispatcher.STATE.ensure_dir()
        for f in (dispatcher.CARDS_FILE, dispatcher.STATE.dir / "runs.jsonl",
                  dispatcher.STATE.dir / "dispatch.lock", dispatcher.STATE.dir / "lock"):
            if f.exists():
                f.unlink()
        self._orig_watchdog = dispatcher.WATCHDOG_SECONDS

    def tearDown(self):
        dispatcher.WATCHDOG_SECONDS = self._orig_watchdog

    # helpers ---------------------------------------------------------------
    def _ref_of(self, title):
        return next(t["reference"] for t in self.board.tasks.values() if t["title"] == title)

    def _column(self, ref):
        tid = next(t["id"] for t in self.board.tasks.values() if t["reference"] == ref)
        return self.board._column_title_for(tid)

    def _ready_card(self, title, project="personal_site", ttype="code", meta=None, model_name=None):
        m = {model.META_TASK_TYPE: ttype, model.META_PROJECT: project}
        if model_name:
            m[model.META_MODEL] = model_name
        if meta:
            m.update(meta)
        return self.board.add_task(title, "Ready", swimlane=project, meta=m)

    def _runs(self):
        path = dispatcher.STATE.dir / "runs.jsonl"
        if not path.is_file():
            return []
        import json
        return [json.loads(line) for line in path.read_text().splitlines()]

    # precheck --------------------------------------------------------------
    def test_precheck_skip_when_empty(self):
        rc = dispatcher.precheck()
        self.assertEqual(rc, 1)
        self.assertTrue(any(r["event"] == "precheck" and r.get("result") == "skip" for r in self._runs()))

    def test_precheck_dispatch_when_ready(self):
        self._ready_card("A")
        self.assertEqual(dispatcher.precheck(), 0)

    def test_precheck_dispatch_when_inflight(self):
        self.board.add_task("A", "In progress", swimlane="personal_site",
                            meta={model.META_TASK_TYPE: "code", model.META_PROJECT: "personal_site",
                                  model.META_CLAIM: "w0"})
        self.assertEqual(dispatcher.precheck(), 0)

    # claim + bring-up ------------------------------------------------------
    def test_tick_claims_and_launches(self):
        ref = self._ready_card("A", model_name="opus")
        dispatcher.tick()
        self.assertEqual(self._column(ref), model.IN_PROGRESS)
        self.assertEqual(len(self.worker.launched), 1)
        self.assertEqual(self.worker.launched[0]["model"], "opus")
        self.assertEqual(len(self.worker.tasks_written), 1)
        records = dispatcher._load_cards()
        self.assertIn(ref, records)
        self.assertEqual(records[ref]["comment_baseline"], 0)

    def test_tick_smoke_fail_blocks_and_no_head(self):
        ref = self._ready_card("A")
        self.worker.provision_ok = False
        self.worker.provision_log = "[provision] FAIL: smoke command failed (exit 1)"
        dispatcher.tick()
        self.assertEqual(self._column(ref), "Blocked")
        self.assertEqual(self.worker.launched, [])
        self.assertNotIn(ref, dispatcher._load_cards())
        posted = " ".join(c["comment"] for cl in self.board.comments.values() for c in cl)
        self.assertIn("smoke", posted)

    def test_tick_workspace_create_fail_blocks(self):
        ref = self._ready_card("A")
        self.worker.create_raises = RuntimeError("orca down")
        dispatcher.tick()
        self.assertEqual(self._column(ref), "Blocked")
        self.assertEqual(self.worker.launched, [])

    def test_claim_orders_by_position_and_skips_blocked_by(self):
        # pred not Done, so B is unclaimable; A (next in order) should be claimed instead.
        pred = self.board.add_task("PRED", "Идеи", swimlane="other",
                                   meta={model.META_TASK_TYPE: "code", model.META_PROJECT: "other"})
        ref_b = self.board.add_task("B", "Ready", swimlane="personal_site",
                                    meta={model.META_TASK_TYPE: "code", model.META_PROJECT: "other",
                                          model.META_BLOCKED_BY: pred})
        ref_a = self._ready_card("A", project="personal_site")
        # give B a lower position so it sorts first, forcing the skip-then-next path
        for t in self.board.tasks.values():
            if t["reference"] == ref_b:
                t["position"] = 1
            elif t["reference"] == ref_a:
                t["position"] = 2
        dispatcher.tick()
        self.assertEqual(self._column(ref_b), "Ready")       # skipped, predecessor not Done
        self.assertEqual(self._column(ref_a), model.IN_PROGRESS)

    def test_claim_stops_at_cap(self):
        self.board.add_task("busy", "In progress", swimlane="other",
                            meta={model.META_TASK_TYPE: "research", model.META_PROJECT: "other",
                                  model.META_CLAIM: "w0"})
        ref = self._ready_card("A")
        with mock.patch.object(dispatcher, "WORKER_CAP", 1):
            dispatcher.tick()
        self.assertEqual(self._column(ref), "Ready")
        self.assertEqual(self.worker.launched, [])
        self.assertTrue(any(r["event"] == "claim-skip" for r in self._runs()))

    # advance ---------------------------------------------------------------
    def _claim_one(self, title="A", **kw):
        ref = self._ready_card(title, **kw)
        dispatcher.tick()
        return ref

    def test_advance_report_done_to_validate(self):
        ref = self._claim_one()
        ops.report(ref, "done", "shipped")
        dispatcher.tick()
        self.assertEqual(self._column(ref), "Validate")
        self.assertNotIn(ref, dispatcher._load_cards())

    def test_advance_report_blocked_to_blocked(self):
        ref = self._claim_one()
        ops.report(ref, "blocked", "criterion 3 недостижим")
        dispatcher.tick()
        self.assertEqual(self._column(ref), "Blocked")
        self.assertNotIn(ref, dispatcher._load_cards())

    def test_advance_ignores_old_comments_before_baseline(self):
        # A comment posted before the claim must not be read as this worker's report.
        ref = self._ready_card("A")
        ops.add_comment("po", ref, "just a note")
        # a stale done-marker present before the claim must be ignored by the baseline
        tid = next(t["id"] for t in self.board.tasks.values() if t["reference"] == ref)
        self.board.comments.setdefault(tid, []).append(
            {"id": 99, "date_creation": 1, "user_id": 0, "comment": f"[{model.MARKER_REPORT_DONE}]\nold"})
        dispatcher.tick()  # claims; baseline = 2 existing comments
        self.assertEqual(self._column(ref), model.IN_PROGRESS)
        dispatcher.tick()  # advance: no NEW report -> stays In progress
        self.assertEqual(self._column(ref), model.IN_PROGRESS)

    def test_watchdog_blocks_but_keeps_workspace(self):
        ref = self._claim_one()
        dispatcher.WATCHDOG_SECONDS = -1        # any silence counts as over-threshold
        self.worker.activity_ts = None          # no fresh output
        dispatcher.tick()
        self.assertEqual(self._column(ref), "Blocked")
        self.assertEqual(self.worker.torn_down, [])   # workspace left alive
        posted = " ".join(c["comment"] for cl in self.board.comments.values() for c in cl)
        self.assertIn("watchdog", posted)
        self.assertTrue(any(r.get("reason") == "watchdog" for r in self._runs()))

    def test_watchdog_holds_when_worker_active(self):
        import time
        ref = self._claim_one()
        dispatcher.WATCHDOG_SECONDS = 600
        self.worker.activity_ts = time.time()   # fresh output within threshold
        dispatcher.tick()
        self.assertEqual(self._column(ref), model.IN_PROGRESS)

    # criterion 2: no direct Kanboard API from the dispatcher ----------------
    def test_dispatcher_does_not_touch_kanboard_directly(self):
        src = Path(dispatcher.__file__).read_text()
        self.assertNotIn("kanboard", src)
        self.assertNotIn("from ..board", src)


if __name__ == "__main__":
    unittest.main()
