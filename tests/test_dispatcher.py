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

from triggered_agents.agents.pipeline import dispatcher, model, ops, worker  # noqa: E402


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
        self.pr_status = None            # dict returned by poll_pr, or None for gh-unavailable
        self.polled = []                 # PR urls poll_pr was asked about
        self.notified = []               # (handle, text) nudges sent to worker terminals
        self.stand_config = None         # dict from read_stand_config, or None for no-stand project
        self.stand_branch = "pipeline/x"  # branch pr_branch returns (None -> gh can't answer)
        self.stand_result = None         # dict from run_stand, or None for unavailable
        self.stand_runs = []             # (project, branch) each run_stand was asked about
        self.reviewer_spawns = []        # (project, worker_id, base) each spawn_reviewer was asked
        self.reviewer_raises = None      # set to raise from spawn_reviewer (orca failure)
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

    def poll_pr(self, pr_url):
        self.polled.append(pr_url)
        return self.pr_status

    def notify(self, handle, text):
        self.notified.append((handle, text))
        return bool(handle)

    def read_stand_config(self, project):
        return self.stand_config

    def pr_branch(self, pr_url):
        return self.stand_branch

    def run_stand(self, project, branch, cfg):
        self.stand_runs.append((project, branch))
        return self.stand_result

    def spawn_reviewer(self, project, worker_id, base_branch, review_md):
        if self.reviewer_raises:
            raise self.reviewer_raises
        self.reviewer_spawns.append((project, worker_id, base_branch))
        return f"/rev/{worker_id}", f"rev-handle-{worker_id}"

    def teardown(self, workspace):
        self.torn_down.append(workspace)


class _DispatcherBase(unittest.TestCase):
    """Board+host fakes and helpers shared by the dispatcher test cases."""

    def setUp(self):
        # Fresh board transport + host stub for every test; clean the shared pipeline state.
        self.board = FakeBoard()
        p = mock.patch("triggered_agents.agents.pipeline.ops.call", self.board.call)
        p.start()
        self.addCleanup(p.stop)

        self.worker = FakeWorker()
        for name in ("read_base_branch", "create_workspace", "provision", "write_task",
                     "launch_worker", "activity", "poll_pr", "notify", "teardown",
                     "read_stand_config", "pr_branch", "run_stand", "spawn_reviewer"):
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

    def _claim_one(self, title="A", **kw):
        ref = self._ready_card(title, **kw)
        dispatcher.tick()
        return ref


class DispatcherTest(_DispatcherBase):
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

    # первый боевой Validate: карточка ждала поллинга, а precheck скипал тик
    def test_precheck_dispatch_when_validating(self):
        self.board.add_task("A", "Validate", swimlane="personal_site",
                            meta={model.META_TASK_TYPE: "code", model.META_PROJECT: "personal_site"})
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
    def test_advance_report_done_to_validate(self):
        ref = self._claim_one()
        ops.report(ref, "done", "shipped")
        dispatcher.tick()
        self.assertEqual(self._column(ref), "Validate")
        # The record survives into Validate: the worker session lives on for CI rework.
        self.assertIn(ref, dispatcher._load_cards())

    def test_advance_done_resets_stand_retry_budget(self):
        # Re-entry into Validate is a fresh code state: the stand fail-count from a prior stint
        # must not carry over and short-circuit the auto-retry.
        ref = self._claim_one()
        recs = dispatcher._load_cards()
        recs[ref]["stand_fails"] = 5
        dispatcher._save_cards(recs)
        ops.report(ref, "done", "готово\nPR: https://github.com/vladmesh/personal_site/pull/1")
        dispatcher.tick()
        self.assertEqual(self._column(ref), "Validate")
        self.assertEqual(dispatcher._load_cards()[ref]["stand_fails"], 0)

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

    # bring-up failure after claim (review #1) --------------------------------
    def test_launch_worker_fail_blocks_not_hangs(self):
        ref = self._ready_card("A")
        with mock.patch("triggered_agents.agents.pipeline.worker.launch_worker",
                        side_effect=RuntimeError("orca terminal create failed")):
            dispatcher.tick()  # must not raise
        self.assertEqual(self._column(ref), "Blocked")
        self.assertNotIn(ref, dispatcher._load_cards())
        posted = " ".join(c["comment"] for cl in self.board.comments.values() for c in cl)
        self.assertIn("bring-up", posted)

    def test_show_card_fail_after_claim_blocks(self):
        ref = self._ready_card("A")
        with mock.patch("triggered_agents.agents.pipeline.ops.show_card",
                        side_effect=RuntimeError("board hiccup")):
            dispatcher.tick()
        self.assertEqual(self._column(ref), "Blocked")

    def test_reconcile_adopts_claimed_card_without_record(self):
        # Simulate a kill between claim and _save_cards: claim on the board, no record on disk.
        ref = self._ready_card("A")
        ops.claim_card(ref, "w-crash")
        self.assertEqual(dispatcher._load_cards(), {})
        dispatcher.tick()
        records = dispatcher._load_cards()
        self.assertIn(ref, records)
        self.assertEqual(records[ref]["worker"], "w-crash")
        self.assertTrue(any(r["event"] == "reconcile" for r in self._runs()))
        # From here the normal machinery applies: a report moves it to Validate.
        ops.report(ref, "done", "shipped after crash")
        dispatcher.tick()
        self.assertEqual(self._column(ref), "Validate")

    def test_reconciled_card_hits_watchdog_on_silence(self):
        ref = self._ready_card("A")
        ops.claim_card(ref, "w-crash")
        dispatcher.tick()  # adopt
        dispatcher.WATCHDOG_SECONDS = -1
        dispatcher.tick()
        self.assertEqual(self._column(ref), "Blocked")
        self.assertEqual(self.worker.torn_down, [])

    def test_bringup_saves_records_before_tick_ends(self):
        # _save_cards runs inside _bring_up: a crash right after the head is up must still
        # find the record on disk. Simulate by crashing the tick right after _claim_next.
        ref = self._ready_card("A")
        orig = dispatcher._claim_next

        def claim_then_die(records):
            orig(records)
            raise KeyboardInterrupt("kill right after bring-up")

        with mock.patch.object(dispatcher, "_claim_next", claim_then_die):
            with self.assertRaises(KeyboardInterrupt):
                dispatcher.tick()
        self.assertIn(ref, dispatcher._load_cards())

    # stale tick lock (review #2) ---------------------------------------------
    def test_stale_lock_is_reclaimed(self):
        import subprocess
        p = subprocess.Popen(["true"])
        p.wait()
        lockfile = dispatcher.STATE.dir / "dispatch.lock"
        lockfile.write_text(str(p.pid))
        ref = self._ready_card("A")
        dispatcher.tick()  # must not SystemExit
        self.assertEqual(self._column(ref), model.IN_PROGRESS)
        self.assertFalse(lockfile.exists())
        self.assertTrue(any(r["event"] == "lock-reclaimed" for r in self._runs()))

    def test_garbled_lock_is_reclaimed(self):
        lockfile = dispatcher.STATE.dir / "dispatch.lock"
        lockfile.write_text("not-a-pid")
        dispatcher.tick()
        self.assertFalse(lockfile.exists())

    def test_live_lock_skips_with_exit_zero(self):
        # A busy lock (a long stand run holding the tick while the timer fires again) is a skip,
        # not a failure: it must exit 0 so systemd doesn't log a run of unit failures.
        lockfile = dispatcher.STATE.dir / "dispatch.lock"
        lockfile.write_text(str(os.getpid()))
        with self.assertRaises(SystemExit) as cm:
            dispatcher.tick()
        self.assertEqual(cm.exception.code, 0)
        lockfile.unlink()

    # secret scrubbing before board comments (review #3) ----------------------
    def test_smoke_fail_comment_is_scrubbed(self):
        ref = self._ready_card("A")
        self.worker.provision_ok = False
        self.worker.provision_log = ("[provision] env: KANBOARD_API_TOKEN=supersecretvalue123\n"
                                     "[provision] FAIL: smoke command failed (exit 1)")
        dispatcher.tick()
        posted = " ".join(c["comment"] for cl in self.board.comments.values() for c in cl)
        self.assertNotIn("supersecretvalue123", posted)
        self.assertIn("smoke", posted)

    # TASK.md несёт протокол done целиком (первый прогон: воркер сделал работу, но без PR) ----
    def test_task_md_carries_pr_protocol(self):
        ref = self._ready_card("A")
        dispatcher.tick()
        (_, content), = self.worker.tasks_written
        self.assertIn(f"pipeline/{ref}", content)
        self.assertIn("PR открыт", content)
        self.assertIn("ссылка на PR", content)

    # criterion 2: no direct Kanboard API from the dispatcher ----------------
    def test_dispatcher_does_not_touch_kanboard_directly(self):
        src = Path(dispatcher.__file__).read_text()
        self.assertNotIn("kanboard", src)
        self.assertNotIn("from ..board", src)


class ValidateTest(_DispatcherBase):
    """Validate layer 1: the dispatcher polls each Validate card's PR through worker.poll_pr
    (gh stubbed by FakeWorker) and drives merge/red/green without an LLM."""

    PR = "https://github.com/vladmesh/personal_site/pull/25"

    def _markers(self, ref):
        tid = next(t["id"] for t in self.board.tasks.values() if t["reference"] == ref)
        return " ".join(c["comment"] for c in self.board.comments.get(tid, []))

    def _to_validate(self, pr=PR, report="готово"):
        """Claim a card, have the worker report done (with a PR link), land it in Validate with
        its record intact. poll_pr returns None for this landing tick so the card stays put."""
        ref = self._claim_one()
        ops.report(ref, "done", f"{report}\nPR: {pr}" if pr else report)
        self.worker.pr_status = None
        dispatcher.tick()
        self.assertEqual(self._column(ref), "Validate")
        return ref

    def test_no_gh_call_without_validate_card(self):
        # A card only In progress (never reported done) must not trigger any gh poll.
        self._claim_one()
        self.worker.pr_status = {"merged": False, "state": "OPEN", "rollup": "SUCCESS",
                                 "failed_job": None, "failed_log": None}
        dispatcher.tick()
        self.assertEqual(self.worker.polled, [])

    def test_merged_pr_moves_to_done(self):
        ref = self._to_validate()
        self.worker.pr_status = {"merged": True, "state": "MERGED", "rollup": "SUCCESS",
                                 "failed_job": None, "failed_log": None}
        dispatcher.tick()
        self.assertEqual(self._column(ref), "Done")
        self.assertNotIn(ref, dispatcher._load_cards())    # workspace record dropped
        self.assertIn(self.PR, self.worker.polled)
        self.assertTrue(any(r["event"] == "validate" and r.get("to") == "Done" for r in self._runs()))

    def test_red_ci_returns_to_in_progress_notifies_and_scrubs(self):
        ref = self._to_validate()
        self.worker.pr_status = {
            "merged": False, "state": "OPEN", "rollup": "FAILURE",
            "failed_job": "Lint, Typecheck & Test",
            "failed_log": "E   assert 1 == 2\nleaked KANBOARD_API_TOKEN=supersecretvalue123",
        }
        dispatcher.tick()
        self.assertEqual(self._column(ref), model.IN_PROGRESS)
        journal = self._markers(ref)
        self.assertIn("Lint, Typecheck & Test", journal)
        self.assertNotIn("supersecretvalue123", journal)          # scrubbed before posting
        self.assertEqual(len(self.worker.notified), 1)            # live worker nudged
        self.assertIn("Lint, Typecheck & Test", self.worker.notified[0][1])
        self.assertTrue(any(r.get("reason") == "ci-red" for r in self._runs()))

    def test_red_ci_then_rework_done_returns_to_validate(self):
        # A stale done comment (below the new baseline) must not bounce the card straight back.
        ref = self._to_validate()
        self.worker.pr_status = {"merged": False, "state": "OPEN", "rollup": "FAILURE",
                                 "failed_job": "CI", "failed_log": "boom"}
        dispatcher.tick()
        self.assertEqual(self._column(ref), model.IN_PROGRESS)
        self.worker.pr_status = None
        dispatcher.tick()                                        # no new report -> stays put
        self.assertEqual(self._column(ref), model.IN_PROGRESS)
        ops.report(ref, "done", "починил")                       # fresh report, PR link still on card
        dispatcher.tick()
        self.assertEqual(self._column(ref), "Validate")

    def test_green_ci_comments_exactly_once(self):
        ref = self._to_validate()
        self.worker.pr_status = {"merged": False, "state": "OPEN", "rollup": "SUCCESS",
                                 "failed_job": None, "failed_log": None}
        dispatcher.tick()
        dispatcher.tick()                                        # second green tick must not re-post
        self.assertEqual(self._column(ref), "Validate")
        self.assertEqual(self._markers(ref).count(f"[{model.MARKER_VALIDATE_GREEN}]"), 1)

    def test_no_pr_link_warns_without_touching_card(self):
        ref = self._claim_one()
        ops.report(ref, "done", "готово, но ссылку на PR забыл")
        self.worker.pr_status = {"merged": True, "state": "MERGED", "rollup": "SUCCESS",
                                 "failed_job": None, "failed_log": None}
        dispatcher.tick()                                        # lands in Validate, no PR -> warn
        self.assertEqual(self._column(ref), "Validate")
        self.assertEqual(self.worker.polled, [])                 # never reached gh
        self.assertTrue(any(r["event"] == "validate" and r.get("result") == "no-pr-ref"
                            and r.get("level") == "warn" for r in self._runs()))

    def test_gh_unavailable_warns_without_touching_card(self):
        ref = self._to_validate()
        self.worker.pr_status = None                             # gh down / PR not found
        dispatcher.tick()
        self.assertEqual(self._column(ref), "Validate")
        self.assertIn(ref, dispatcher._load_cards())
        self.assertTrue(any(r["event"] == "validate" and r.get("result") == "gh-unavailable"
                            and r.get("level") == "warn" for r in self._runs()))


class ValidateStandTest(_DispatcherBase):
    """Validate layer 2: for a project with a [stand] manifest section, CI green is not enough —
    the dispatcher deploys the PR branch to the stand (worker.run_stand, stubbed) and gates on a
    green e2e run. Green comes only after a green stand run; a red run retries once then Blocks."""

    PR = "https://github.com/vladmesh/personal_site/pull/42"
    STAND = {"namespace": "personal_site_stand", "compose": ["infra/docker-compose.stand.yml"],
             "e2e_command": "bash infra/e2e/run.sh"}

    def setUp(self):
        super().setUp()
        self.worker.stand_config = self.STAND        # every card here is a stand project
        self.worker.stand_branch = "pipeline/A"

    def _markers(self, ref):
        tid = next(t["id"] for t in self.board.tasks.values() if t["reference"] == ref)
        return " ".join(c["comment"] for c in self.board.comments.get(tid, []))

    def _to_validate(self, pr=PR):
        ref = self._claim_one()
        ops.report(ref, "done", f"готово\nPR: {pr}")
        self.worker.pr_status = None
        dispatcher.tick()
        self.assertEqual(self._column(ref), "Validate")
        return ref

    def _ci_green(self):
        self.worker.pr_status = {"merged": False, "state": "OPEN", "rollup": "SUCCESS",
                                 "failed_job": None, "failed_log": None}

    def test_ci_green_runs_stand_not_ci_green_verdict(self):
        ref = self._to_validate()
        self._ci_green()
        self.worker.stand_result = {"ok": True, "stage": "e2e", "log": "e2e: all checks passed"}
        dispatcher.tick()
        self.assertEqual(self._column(ref), "Validate")
        self.assertEqual(self.worker.stand_runs, [("personal_site", "pipeline/A")])
        journal = self._markers(ref)
        self.assertIn(f"[{model.MARKER_STAND_GREEN}]", journal)
        self.assertNotIn(f"[{model.MARKER_VALIDATE_GREEN}]", journal)   # green only via the stand
        self.assertTrue(any(r["event"] == "stand" and r.get("result") == "green" for r in self._runs()))

    def test_stand_green_is_posted_once_and_not_rerun(self):
        ref = self._to_validate()
        self._ci_green()
        self.worker.stand_result = {"ok": True, "stage": "e2e", "log": "ok"}
        dispatcher.tick()
        dispatcher.tick()                        # already stand-green -> must not run the stand again
        self.assertEqual(len(self.worker.stand_runs), 1)
        self.assertEqual(self._markers(ref).count(f"[{model.MARKER_STAND_GREEN}]"), 1)

    def test_stand_red_once_retries_stays_in_validate(self):
        ref = self._to_validate()
        self._ci_green()
        self.worker.stand_result = {"ok": False, "stage": "e2e",
                                    "log": "FAIL: backend health\nleaked TOKEN=supersecretvalue123"}
        dispatcher.tick()
        self.assertEqual(self._column(ref), "Validate")           # one auto-retry, not blocked yet
        journal = self._markers(ref)
        self.assertIn(f"[{model.MARKER_STAND_RED}]", journal)
        self.assertNotIn("supersecretvalue123", journal)          # scrubbed before posting
        self.assertEqual(dispatcher._load_cards()[ref]["stand_fails"], 1)

    def test_stand_red_twice_blocks_with_log(self):
        ref = self._to_validate()
        self._ci_green()
        self.worker.stand_result = {"ok": False, "stage": "e2e", "log": "boom"}
        dispatcher.tick()                                          # fail 1 -> retry
        self.assertEqual(self._column(ref), "Validate")
        dispatcher.tick()                                          # fail 2 -> Blocked
        self.assertEqual(self._column(ref), "Blocked")
        self.assertNotIn(ref, dispatcher._load_cards())
        self.assertEqual(self._markers(ref).count(f"[{model.MARKER_STAND_RED}]"), 2)
        self.assertTrue(any(r["event"] == "stand" and r.get("to") == "Blocked" for r in self._runs()))

    def test_stand_green_then_merge_to_done(self):
        ref = self._to_validate()
        self._ci_green()
        self.worker.stand_result = {"ok": True, "stage": "e2e", "log": "ok"}
        dispatcher.tick()
        self.worker.pr_status = {"merged": True, "state": "MERGED", "rollup": "SUCCESS",
                                 "failed_job": None, "failed_log": None}
        dispatcher.tick()
        self.assertEqual(self._column(ref), "Done")

    def test_pr_branch_unknown_warns_without_running_stand(self):
        ref = self._to_validate()
        self._ci_green()
        self.worker.stand_branch = None                           # gh can't resolve the branch
        dispatcher.tick()
        self.assertEqual(self.worker.stand_runs, [])
        self.assertEqual(self._column(ref), "Validate")
        self.assertTrue(any(r["event"] == "stand" and r.get("result") == "no-branch" for r in self._runs()))

    def test_stand_unavailable_warns_without_verdict(self):
        ref = self._to_validate()
        self._ci_green()
        self.worker.stand_result = None                           # host/stand infra unknown
        dispatcher.tick()
        self.assertEqual(self._column(ref), "Validate")
        self.assertNotIn(f"[{model.MARKER_STAND_GREEN}]", self._markers(ref))
        self.assertTrue(any(r["event"] == "stand" and r.get("result") == "unavailable"
                            for r in self._runs()))

    def test_ci_red_resets_stand_retry_budget(self):
        ref = self._to_validate()
        self._ci_green()
        self.worker.stand_result = {"ok": False, "stage": "e2e", "log": "boom"}
        dispatcher.tick()                                         # stand fail 1
        self.assertEqual(dispatcher._load_cards()[ref]["stand_fails"], 1)
        # CI goes red -> card bounces to In progress; the stand fail-count must reset.
        self.worker.pr_status = {"merged": False, "state": "OPEN", "rollup": "FAILURE",
                                 "failed_job": "CI", "failed_log": "red"}
        dispatcher.tick()
        self.assertEqual(self._column(ref), model.IN_PROGRESS)
        self.assertEqual(dispatcher._load_cards()[ref]["stand_fails"], 0)
        # rework, back to Validate, CI green again, stand fails once more -> still just a retry.
        ops.report(ref, "done", "починил")
        self.worker.pr_status = None
        dispatcher.tick()
        self.assertEqual(self._column(ref), "Validate")
        self._ci_green()
        dispatcher.tick()
        self.assertEqual(self._column(ref), "Validate")           # first fail of the reworked PR
        self.assertEqual(dispatcher._load_cards()[ref]["stand_fails"], 1)

    def test_broken_manifest_is_localized_not_tick_crash(self):
        # An unparseable base workspace.toml must fail this one card (warn + one comment), not
        # crash the whole tick and starve the other Validate cards / the claim step.
        from triggered_agents.agents.pipeline import stand
        ref = self._to_validate()
        self._ci_green()
        with mock.patch("triggered_agents.agents.pipeline.worker.read_stand_config",
                        side_effect=stand.StandError("workspace.toml is not readable/valid TOML")):
            dispatcher.tick()                 # must not raise
            dispatcher.tick()                 # second failing tick must not re-post the comment
        self.assertEqual(self._column(ref), "Validate")
        self.assertEqual(self._markers(ref).count(f"[{model.MARKER_VALIDATE_ERROR}]"), 1)
        self.assertTrue(any(r["event"] == "validate" and r.get("result") == "error"
                            and r.get("level") == "warn" for r in self._runs()))

    def test_no_stand_project_keeps_layer1_green(self):
        # A project without a stand section skips layer 2: CI green notes ci-green, then layer 3.
        self.worker.stand_config = None
        ref = self._to_validate()
        self._ci_green()
        dispatcher.tick()
        self.assertEqual(self.worker.stand_runs, [])
        self.assertIn(f"[{model.MARKER_VALIDATE_GREEN}]", self._markers(ref))
        self.assertEqual(len(self.worker.reviewer_spawns), 1)   # layer 3 fired on green CI


class ValidateReviewTest(_DispatcherBase):
    """Validate layer 3: once the mechanical layers are green, the dispatcher spawns an independent
    reviewer head (worker.spawn_reviewer, stubbed) and drives the card by its verdict — green waits
    for merge, red returns it for rework up to a cap, a silent reviewer hits the watchdog. No stand
    here, so CI green is the last mechanical layer."""

    PR = "https://github.com/vladmesh/personal_site/pull/77"

    def _markers(self, ref):
        tid = next(t["id"] for t in self.board.tasks.values() if t["reference"] == ref)
        return " ".join(c["comment"] for c in self.board.comments.get(tid, []))

    def _to_validate(self, pr=PR):
        ref = self._claim_one()
        ops.report(ref, "done", f"готово\nPR: {pr}")
        self.worker.pr_status = None
        dispatcher.tick()
        self.assertEqual(self._column(ref), "Validate")
        return ref

    def _ci_green(self):
        self.worker.pr_status = {"merged": False, "state": "OPEN", "rollup": "SUCCESS",
                                 "failed_job": None, "failed_log": None}

    def _spawned(self, pr=PR):
        """Land a card in Validate with the reviewer head up: done -> Validate, CI green -> spawn."""
        ref = self._to_validate(pr)
        self._ci_green()
        dispatcher.tick()
        self.assertEqual(len(self.worker.reviewer_spawns), 1)
        return ref

    def _rev_ws_torndown(self):
        return [w for w in self.worker.torn_down if w.startswith("/rev/")]

    def test_spawns_reviewer_once_when_lower_layers_green(self):
        ref = self._spawned()
        self.assertEqual(self._column(ref), "Validate")
        self.assertIn("review_baseline", dispatcher._load_cards()[ref])
        dispatcher.tick()                                # verdict still pending -> no re-spawn
        self.assertEqual(len(self.worker.reviewer_spawns), 1)

    def test_green_verdict_waits_for_merge_and_tears_down_once(self):
        ref = self._spawned()
        ops.verdict(ref, "green", "каждый criterion реально выполнен")
        dispatcher.tick()
        self.assertEqual(self._column(ref), "Validate")          # waits for a human merge
        self.assertEqual(len(self._rev_ws_torndown()), 1)        # reviewer worktree cleaned up
        dispatcher.tick()                                        # no-op, no second teardown
        self.assertEqual(len(self._rev_ws_torndown()), 1)
        self.assertTrue(any(r["event"] == "review" and r.get("result") == "green" for r in self._runs()))

    def test_green_verdict_then_merge_to_done(self):
        ref = self._spawned()
        ops.verdict(ref, "green", "ок")
        dispatcher.tick()
        self.worker.pr_status = {"merged": True, "state": "MERGED", "rollup": "SUCCESS",
                                 "failed_job": None, "failed_log": None}
        dispatcher.tick()
        self.assertEqual(self._column(ref), "Done")

    def test_red_verdict_returns_to_in_progress_and_nudges(self):
        ref = self._spawned()
        ops.verdict(ref, "red", "блокер: проглоченное исключение на пути ошибки, foo.py:12, "
                    "при падении gh карточка зависает без сигнала")
        dispatcher.tick()
        self.assertEqual(self._column(ref), model.IN_PROGRESS)
        rec = dispatcher._load_cards()[ref]
        self.assertEqual(rec["review_returns"], 1)
        self.assertNotIn("review_baseline", rec)                 # cleared -> fresh review after rework
        self.assertEqual(len(self.worker.notified), 1)          # live worker nudged
        self.assertEqual(len(self._rev_ws_torndown()), 1)       # reviewer worktree torn down
        self.assertTrue(any(r.get("reason") == "review-red" for r in self._runs()))

    def test_red_then_rework_reruns_review_and_ignores_stale_verdict(self):
        ref = self._spawned()
        ops.verdict(ref, "red", "блокер: X")
        dispatcher.tick()
        self.assertEqual(self._column(ref), model.IN_PROGRESS)
        ops.report(ref, "done", "починил")                      # rework
        self.worker.pr_status = None
        dispatcher.tick()
        self.assertEqual(self._column(ref), "Validate")
        self._ci_green()
        dispatcher.tick()                                        # CI green -> a fresh reviewer
        self.assertEqual(len(self.worker.reviewer_spawns), 2)
        # the stale red verdict sits before the new baseline, so it must not bounce the card again
        self.assertEqual(self._column(ref), "Validate")
        ops.verdict(ref, "green", "теперь всё реально")
        dispatcher.tick()
        self.assertEqual(self._column(ref), "Validate")          # green -> waits for merge

    def test_return_cap_blocks_after_three_returns(self):
        ref = self._spawned()
        for i in range(dispatcher.REVIEW_RETURN_CAP):
            ops.verdict(ref, "red", f"блокер {i}")
            dispatcher.tick()
            self.assertEqual(self._column(ref), model.IN_PROGRESS)
            self.assertEqual(dispatcher._load_cards()[ref]["review_returns"], i + 1)
            ops.report(ref, "done", "fix")
            self.worker.pr_status = None
            dispatcher.tick()                                    # -> Validate
            self._ci_green()
            dispatcher.tick()                                    # -> reviewer up again
        ops.verdict(ref, "red", "четвёртый красный — сверх капа")
        dispatcher.tick()
        self.assertEqual(self._column(ref), "Blocked")
        self.assertNotIn(ref, dispatcher._load_cards())
        self.assertIn("кап возвратов", self._markers(ref))
        self.assertTrue(any(r["event"] == "review" and r.get("reason") == "return-cap"
                            for r in self._runs()))

    def test_reviewer_watchdog_blocks_on_silence(self):
        ref = self._spawned()
        self.worker.activity_ts = None
        dispatcher.WATCHDOG_SECONDS = -1
        dispatcher.tick()
        self.assertEqual(self._column(ref), "Blocked")
        self.assertNotIn(ref, dispatcher._load_cards())
        self.assertIn("watchdog", self._markers(ref))
        self.assertEqual(self._rev_ws_torndown(), [])           # reviewer ws left for a human
        self.assertTrue(any(r.get("reason") == "review-watchdog" for r in self._runs()))

    def test_spawn_failure_is_retried_next_tick(self):
        ref = self._to_validate()
        self._ci_green()
        self.worker.reviewer_raises = worker.WorkspaceError("orca terminal create failed")
        dispatcher.tick()
        self.assertEqual(self._column(ref), "Validate")
        self.assertNotIn("review_baseline", dispatcher._load_cards()[ref])
        self.assertEqual(self.worker.reviewer_spawns, [])
        self.worker.reviewer_raises = None
        dispatcher.tick()                                        # recovers
        self.assertEqual(len(self.worker.reviewer_spawns), 1)
        self.assertIn("review_baseline", dispatcher._load_cards()[ref])

    def test_red_return_note_uses_dedicated_marker_not_review_red(self):
        # The dispatcher's own return note must not carry [review:red] — only the reviewer's verdict
        # does, else a baseline shift would re-read the note as a fresh red and loop.
        ref = self._spawned()
        ops.verdict(ref, "red", "блокер: X")
        dispatcher.tick()
        markers = self._markers(ref)
        self.assertEqual(markers.count(f"[{model.MARKER_REVIEW_RED}]"), 1)   # just the verdict
        self.assertIn(f"[{model.MARKER_REVIEW_RETURN}]", markers)             # dispatcher's own note

    def test_spawn_cap_blocks_after_repeated_failures(self):
        ref = self._to_validate()
        self._ci_green()
        self.worker.reviewer_raises = worker.WorkspaceError("orca terminal create failed")
        for i in range(dispatcher.REVIEW_SPAWN_ATTEMPTS - 1):
            dispatcher.tick()
            self.assertEqual(self._column(ref), "Validate")
            self.assertEqual(dispatcher._load_cards()[ref]["review_spawn_fails"], i + 1)
        dispatcher.tick()                                       # the attempt that hits the cap
        self.assertEqual(self._column(ref), "Blocked")
        self.assertNotIn(ref, dispatcher._load_cards())
        self.assertTrue(any(r["event"] == "review" and r.get("reason") == "spawn-cap"
                            for r in self._runs()))

    def test_merge_during_review_tears_down_and_done(self):
        ref = self._spawned()
        self.worker.pr_status = {"merged": True, "state": "MERGED", "rollup": "SUCCESS",
                                 "failed_job": None, "failed_log": None}
        dispatcher.tick()
        self.assertEqual(self._column(ref), "Done")
        self.assertEqual(len(self._rev_ws_torndown()), 1)

    def test_spawn_persists_record_before_tick_ends(self):
        # A crash after the reviewer is up (but before the end-of-tick save) must still find the
        # baseline on disk, or the next tick spawns a second reviewer on the same PR.
        ref = self._to_validate()
        self._ci_green()

        def claim_then_die(records):
            raise KeyboardInterrupt("die after validate spawned the reviewer")

        with mock.patch.object(dispatcher, "_claim_next", claim_then_die):
            with self.assertRaises(KeyboardInterrupt):
                dispatcher.tick()
        self.assertIn("review_baseline", dispatcher._load_cards()[ref])

    def test_reconcile_adopts_validate_card_and_reviews(self):
        # cards.json lost (a dispatcher redeploy): a claimed Validate card with no record must be
        # adopted so layer 3 still runs, not left waiting for a review that never spawns.
        self.board.add_task("V", "Validate", swimlane="personal_site",
                            meta={model.META_TASK_TYPE: "code", model.META_PROJECT: "personal_site",
                                  model.META_CLAIM: "w-old"})
        ref = self._ref_of("V")
        ops.add_comment("po", ref, f"PR: {self.PR}")
        self._ci_green()
        self.assertEqual(dispatcher._load_cards(), {})
        dispatcher.tick()
        self.assertIn(ref, dispatcher._load_cards())
        self.assertEqual(len(self.worker.reviewer_spawns), 1)
        self.assertTrue(any(r["event"] == "reconcile" and r.get("column") == "Validate"
                            for r in self._runs()))

    def test_untracked_validate_card_skipped_with_warn(self):
        self.board.add_task("U", "Validate", swimlane="personal_site",
                            meta={model.META_TASK_TYPE: "code", model.META_PROJECT: "personal_site"})
        ref = self._ref_of("U")
        ops.add_comment("po", ref, f"PR: {self.PR}")
        self._ci_green()
        dispatcher.tick()
        self.assertEqual(self._column(ref), "Validate")
        self.assertEqual(self.worker.reviewer_spawns, [])
        self.assertTrue(any(r["event"] == "review" and r.get("result") == "untracked"
                            for r in self._runs()))


class OrcaJsonTimeoutTest(unittest.TestCase):
    """A hung orca CLI must surface as WorkspaceError (worker's spawn-failure contract), not a raw
    TimeoutExpired that escapes create_workspace/launch_worker/spawn_reviewer and lands in the
    caller's generic error path with a misleading message."""

    def test_timeout_becomes_workspace_error(self):
        import subprocess
        with mock.patch("subprocess.run",
                        side_effect=subprocess.TimeoutExpired(cmd="orca", timeout=1)):
            with self.assertRaises(worker.WorkspaceError):
                worker._orca_json(["terminal", "list"])


class ProjectRootTest(unittest.TestCase):
    """Резолв корня проекта: ~/projects/<name>, фоллбэк на ~/<name> (первый прогон на
    triggered-agents: репо живёт в ~, bring-up падал repo_not_found)."""

    def setUp(self):
        from triggered_agents.agents.pipeline import worker
        self.worker = worker
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        (self.home / "projects" / "site").mkdir(parents=True)
        (self.home / "infra-repo").mkdir()
        self._patch = mock.patch.object(worker, "PROJECTS_DIR", self.home / "projects")
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def test_prefers_projects_dir(self):
        self.assertEqual(self.worker.project_root("site"), self.home / "projects" / "site")

    def test_falls_back_to_home(self):
        self.assertEqual(self.worker.project_root("infra-repo"), self.home / "infra-repo")

    def test_missing_stays_in_projects_dir(self):
        self.assertEqual(self.worker.project_root("nope"), self.home / "projects" / "nope")


class EnsureTrustTest(unittest.TestCase):
    """Folder trust: без него голова виснет на интерактивном вопросе Claude Code
    (первый живой прогон, personal_site-198)."""

    def setUp(self):
        from triggered_agents.agents.pipeline import worker
        self.worker = worker
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.claude_json = Path(self.tmp.name) / ".claude.json"
        self._patch = mock.patch.object(worker, "CLAUDE_JSON", self.claude_json)
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def _trusted(self, ws):
        import json
        d = json.loads(self.claude_json.read_text())
        return d.get("projects", {}).get(ws, {}).get("hasTrustDialogAccepted")

    def test_sets_trust_for_fresh_workspace(self):
        self.worker.ensure_trust("/ws/x")
        self.assertTrue(self._trusted("/ws/x"))

    def test_keeps_existing_config_and_is_idempotent(self):
        self.claude_json.write_text('{"projects": {"/old": {"hasTrustDialogAccepted": true}}, "theme": "dark"}')
        self.worker.ensure_trust("/ws/x")
        self.worker.ensure_trust("/ws/x")
        import json
        d = json.loads(self.claude_json.read_text())
        self.assertTrue(self._trusted("/ws/x"))
        self.assertTrue(self._trusted("/old"))
        self.assertEqual(d["theme"], "dark")

    def test_garbage_config_raises_workspace_error(self):
        self.claude_json.write_text("{not json")
        with self.assertRaises(self.worker.WorkspaceError):
            self.worker.ensure_trust("/ws/x")


class EnsureThemeTest(unittest.TestCase):
    """Onboarding theme picker: unanswered, it hangs a fresh head the same way an untrusted
    folder does — found live as an orphaned terminal in the curator workspace."""

    def setUp(self):
        from triggered_agents.agents.pipeline import worker
        self.worker = worker
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.claude_json = Path(self.tmp.name) / ".claude.json"
        self._patch = mock.patch.object(worker, "CLAUDE_JSON", self.claude_json)
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def test_sets_theme_when_absent(self):
        self.worker.ensure_theme()
        import json
        self.assertEqual(json.loads(self.claude_json.read_text())["theme"], "dark")

    def test_leaves_existing_theme(self):
        self.claude_json.write_text('{"theme": "light"}')
        self.worker.ensure_theme()
        import json
        self.assertEqual(json.loads(self.claude_json.read_text())["theme"], "light")

    def test_garbage_config_raises_workspace_error(self):
        self.claude_json.write_text("{not json")
        with self.assertRaises(self.worker.WorkspaceError):
            self.worker.ensure_theme()


class WorkerHostCallsTest(unittest.TestCase):
    """The actual `orca` CLI args launch_worker/create_workspace build — everything above this
    stubs worker.py wholesale, so nothing else exercises the real argument lists."""

    def setUp(self):
        from triggered_agents.agents.pipeline import worker
        self.worker = worker
        self.calls = []

        def fake_orca_json(args):
            self.calls.append(args)
            if args[0] == "worktree":
                return {"worktree": {"path": "/ws/fresh"}}
            return {"terminal": {"handle": "term-1"}}

        p = mock.patch.object(worker, "_orca_json", fake_orca_json)
        p.start()
        self.addCleanup(p.stop)

        self.ensured = []
        for name in ("ensure_trust", "ensure_theme"):
            wp = mock.patch.object(worker, name, lambda *a, n=name: self.ensured.append(n))
            wp.start()
            self.addCleanup(wp.stop)

    def test_create_workspace_activates_the_worktree(self):
        self.worker.create_workspace("proj", "worker-1", "main")
        args = self.calls[0]
        self.assertIn("--activate", args)

    def test_launch_worker_ensures_trust_and_theme_before_terminal_create(self):
        self.worker.launch_worker("/ws/fresh", None, "worker-1")
        self.assertEqual(self.ensured, ["ensure_trust", "ensure_theme"])
        self.assertEqual(self.calls[0][0], "terminal")

    def test_spawn_reviewer_tears_down_worktree_on_launch_failure(self):
        # The worktree is created first; if the head fails to launch after that, the worktree must
        # not be left orphaned on disk.
        torn = []

        def orca_raise_on_terminal(args):
            self.calls.append(args)
            if args[0] == "worktree":
                return {"worktree": {"path": "/ws/rev"}}
            raise self.worker.WorkspaceError("terminal create boom")

        with mock.patch.object(self.worker, "_orca_json", orca_raise_on_terminal), \
             mock.patch.object(self.worker, "_write_excluded", lambda *a: "/ws/rev/REVIEW.md"), \
             mock.patch.object(self.worker, "teardown", lambda ws: torn.append(ws)):
            with self.assertRaises(self.worker.WorkspaceError):
                self.worker.spawn_reviewer("proj", "rev-1", "main", "REVIEW body")
        self.assertEqual(torn, ["/ws/rev"])


if __name__ == "__main__":
    unittest.main()
