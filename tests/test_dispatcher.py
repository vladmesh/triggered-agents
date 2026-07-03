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
import time
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
        self.activity_polled = []        # every workspace worker.activity() was asked about
        self.launched = []
        self.tasks_written = []
        self.torn_down = []
        self.pr_status = None            # dict returned by poll_pr, or None for gh-unavailable
        self.polled = []                 # PR urls poll_pr was asked about
        self.merge_result = {"ok": True, "error": None}  # dict returned by merge_pr
        self.merged = []                 # PR urls merge_pr was asked to merge
        self.notified = []               # (handle, text) nudges sent to worker terminals
        self.stand_config = None         # dict from read_stand_config, or None for no-stand project
        self.stand_branch = "pipeline/x"  # branch pr_branch returns (None -> gh can't answer)
        self.stand_result = None         # dict from run_stand, or None for unavailable
        self.stand_runs = []             # (project, branch) each run_stand was asked about
        self.reviewer_spawns = []        # (project, worker_id, base, title, pr_branch, review_branch)
        self.reviewer_raises = None      # set to raise from spawn_reviewer (orca failure)
        self.existing_workspaces = set()  # (project, name) pairs treated as already on disk
        self.renamed = []                # (handle, title) every rename_terminal call
        self.branches_set = []           # (workspace, branch) every set_branch call
        self.agent_worktrees = []        # [(name, path), ...] list_agent_worktrees returns
        self.ff_results = {}             # path -> ff_worktree result dict (default: clean no-op ff)
        self.ff_calls = []               # (path, base_branch) every ff_worktree call
        self.contrib_projects = set()    # project names is_contrib() should report True for
        self._n = 0

    def read_base_branch(self, project):
        return "main"

    def is_contrib(self, project):
        return project in self.contrib_projects

    def list_agent_worktrees(self):
        return list(self.agent_worktrees)

    def ff_worktree(self, path, base_branch):
        self.ff_calls.append((path, base_branch))
        return self.ff_results.get(path, {"ok": True, "reason": None, "before": "x", "after": "x"})

    def create_workspace(self, project, name, base_branch):
        if self.create_raises:
            raise self.create_raises
        self._n += 1
        return f"/ws/{name}"

    def set_branch(self, workspace, branch):
        self.branches_set.append((workspace, branch))

    def provision(self, workspace):
        return self.provision_ok, self.provision_log

    def write_task(self, workspace, content):
        self.tasks_written.append((workspace, content))
        return workspace + "/TASK.md"

    def launch_worker(self, workspace, model_name, worker_id, title):
        self.launched.append({"ws": workspace, "model": model_name, "worker": worker_id, "title": title})
        return f"handle-{worker_id}"

    def workspace_exists(self, project, name):
        return (project, name) in self.existing_workspaces

    def workspace_path(self, project, name):
        return f"/ws/{name}"

    def rename_terminal(self, handle, title):
        self.renamed.append((handle, title))
        return bool(handle)

    def activity(self, workspace):
        self.activity_polled.append(workspace)
        return self.activity_ts

    def poll_pr(self, pr_url):
        self.polled.append(pr_url)
        return self.pr_status

    def merge_pr(self, pr_url):
        self.merged.append(pr_url)
        return self.merge_result

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

    def spawn_reviewer(self, project, worker_id, base_branch, review_md, title, pr_branch, review_branch):
        if self.reviewer_raises:
            raise self.reviewer_raises
        self.reviewer_spawns.append((project, worker_id, base_branch, title, pr_branch, review_branch))
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
        for name in ("read_base_branch", "is_contrib", "create_workspace", "set_branch", "provision",
                     "write_task", "launch_worker", "activity", "poll_pr", "notify", "teardown",
                     "read_stand_config", "pr_branch", "run_stand", "spawn_reviewer",
                     "workspace_exists", "workspace_path", "rename_terminal", "merge_pr",
                     "list_agent_worktrees", "ff_worktree"):
            wp = mock.patch(f"triggered_agents.agents.pipeline.worker.{name}",
                            getattr(self.worker, name))
            wp.start()
            self.addCleanup(wp.stop)

        dispatcher.STATE.ensure_dir()
        for f in (dispatcher.CARDS_FILE, dispatcher.STATE.dir / "runs.jsonl",
                  dispatcher.STATE.dir / "dispatch.lock", dispatcher.STATE.dir / "dispatch.lock.mutex",
                  dispatcher.STATE.dir / "lock"):
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
        self.assertTrue(any(r["event"] == "precheck" and r.get("result") == "nothing-to-do"
                             for r in self._runs()))

    def test_precheck_dispatch_when_ready(self):
        self._ready_card("A")
        self.assertEqual(dispatcher.precheck(), 0)
        self.assertTrue(any(r["event"] == "precheck" and r.get("result") == "dispatched"
                             for r in self._runs()))

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

    # Kanboard недоступен/битый env: исход error, ненулевой выход отличимый от nothing-to-do (1)
    def test_precheck_error_when_board_unreachable(self):
        with mock.patch.object(ops, "list_cards", side_effect=RuntimeError("connection refused")):
            rc = dispatcher.precheck()
        self.assertEqual(rc, 2)
        self.assertNotEqual(rc, 1)  # must differ from the plain-skip exit code
        runs = [r for r in self._runs() if r["event"] == "precheck"]
        self.assertTrue(any(r.get("result") == "error" and r.get("error_class") == "RuntimeError"
                             for r in runs))

    # ff agent worktrees ------------------------------------------------------
    def test_precheck_ffs_clean_agent_worktrees(self):
        self.worker.agent_worktrees = [("board", "/ws/agents/board")]
        self.worker.ff_results = {"/ws/agents/board": {"ok": True, "reason": None,
                                                        "before": "aaa", "after": "bbb"}}
        dispatcher.precheck()
        self.assertIn(("/ws/agents/board", "main"), self.worker.ff_calls)
        runs = [r for r in self._runs() if r["event"] == "ff-agents"]
        self.assertTrue(any(r.get("agent") == "board" and r.get("result") == "ff"
                             and r.get("before") == "aaa" and r.get("after") == "bbb" for r in runs))

    def test_precheck_ff_noop_when_already_up_to_date(self):
        self.worker.agent_worktrees = [("retro", "/ws/agents/retro")]
        self.worker.ff_results = {"/ws/agents/retro": {"ok": True, "reason": None,
                                                        "before": "aaa", "after": "aaa"}}
        dispatcher.precheck()
        runs = [r for r in self._runs() if r["event"] == "ff-agents"]
        self.assertFalse(runs)  # no log spam for a no-op ff

    # non-ff (local commits/conflict) warns by worktree name and never breaks the tick
    def test_precheck_ff_blocked_warns_and_does_not_break_tick(self):
        self.worker.agent_worktrees = [("curator", "/ws/agents/curator")]
        self.worker.ff_results = {"/ws/agents/curator": {"ok": False,
                                                          "reason": "local commits"}}
        self._ready_card("A")
        rc = dispatcher.precheck()
        self.assertEqual(rc, 0)  # board state still decides the exit code, unaffected by ff
        runs = [r for r in self._runs() if r["event"] == "ff-agents"]
        self.assertTrue(any(r.get("agent") == "curator" and r.get("result") == "blocked"
                             and r.get("level") == "warn" and "local commits" in r.get("reason", "")
                             for r in runs))

    def test_precheck_ff_one_bad_worktree_does_not_skip_the_rest(self):
        self.worker.agent_worktrees = [("board", "/ws/agents/board"), ("retro", "/ws/agents/retro")]
        self.worker.ff_results = {"/ws/agents/board": {"ok": False, "reason": "diverged"},
                                  "/ws/agents/retro": {"ok": True, "reason": None,
                                                        "before": "a", "after": "b"}}
        dispatcher.precheck()
        paths_called = [c[0] for c in self.worker.ff_calls]
        self.assertEqual(paths_called, ["/ws/agents/board", "/ws/agents/retro"])
        runs = [r for r in self._runs() if r["event"] == "ff-agents"]
        self.assertTrue(any(r.get("agent") == "board" and r.get("result") == "blocked" for r in runs))
        self.assertTrue(any(r.get("agent") == "retro" and r.get("result") == "ff" for r in runs))

    def test_precheck_ff_survives_read_base_branch_failure(self):
        with mock.patch.object(worker, "read_base_branch", side_effect=RuntimeError("no manifest")):
            rc = dispatcher.precheck()
        self.assertEqual(rc, 1)  # board still empty -> nothing-to-do, unaffected
        runs = [r for r in self._runs() if r["event"] == "ff-agents"]
        self.assertTrue(any(r.get("result") == "error" and r.get("level") == "warn" for r in runs))

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

    def test_tick_renames_branch_before_launching_the_head(self):
        # bring-up must land the worktree on its own pipeline/<ref> branch before the worker head
        # ever starts — no head creates or renames its own branch.
        from triggered_agents.agents.pipeline import naming
        ref = self._ready_card("A")
        dispatcher.tick()
        ws = self.worker.launched[0]["ws"]
        self.assertEqual(self.worker.branches_set, [(ws, naming.worker_branch(ref))])

    def test_task_md_states_branch_ready_not_create_it(self):
        from triggered_agents.agents.pipeline import naming
        ref = self._ready_card("A")
        dispatcher.tick()
        task_md = self.worker.tasks_written[0][1]
        self.assertIn(f"уже стоит на ветке `{naming.worker_branch(ref)}`", task_md)
        self.assertNotIn("git checkout -b", task_md)
        self.assertNotIn("git branch -m", task_md)

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

    def test_reconcile_restores_workspace_from_claim(self):
        # The claim IS the card's workspace base name (naming._worker_id) — reconcile must rebuild
        # the full path from it, not leave "workspace" empty and blind the activity watchdog.
        ref = self._ready_card("A")
        ops.claim_card(ref, "w-crash")
        dispatcher.tick()
        records = dispatcher._load_cards()
        self.assertEqual(records[ref]["workspace"], "/ws/w-crash")
        dispatcher.WATCHDOG_SECONDS = 600
        self.worker.activity_ts = None
        dispatcher.tick()  # advance polls activity for the watchdog clock
        self.assertIn("/ws/w-crash", self.worker.activity_polled)

    def test_restore_workspace_warns_on_empty_claim(self):
        # Nothing to rebuild a workspace name from — an explicit warn, not a silent "" adoption.
        self.assertEqual(dispatcher._restore_workspace("", "personal_site"), "")
        self.assertTrue(any(r["event"] == "reconcile" and r.get("result") == "workspace-unknown"
                            for r in self._runs()))

    def test_reconcile_restores_workspace_across_validate_rework(self):
        # A Validate card adopted after a lost cards.json, then bounced back to In progress by a
        # CI-red poll (the rework path) — the workspace reconcile just restored must survive it.
        self.board.add_task("V", "Validate", swimlane="personal_site",
                            meta={model.META_TASK_TYPE: "code", model.META_PROJECT: "personal_site",
                                  model.META_CLAIM: "w-old"})
        ref = self._ref_of("V")
        ops.add_comment("po", ref, "PR: https://github.com/vladmesh/personal_site/pull/9")
        self.worker.pr_status = {"merged": False, "state": "OPEN", "rollup": "FAILURE",
                                 "failed_job": "CI", "failed_log": "boom"}
        dispatcher.tick()  # reconcile adopts the Validate card, then _validate sees CI red
        self.assertEqual(self._column(ref), model.IN_PROGRESS)
        records = dispatcher._load_cards()
        self.assertIn(ref, records)
        self.assertEqual(records[ref]["workspace"], "/ws/w-old")

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

    # tick-lock TOCTOU (triggered-agents-229) ----------------------------------
    def test_concurrent_stale_reclaim_only_one_wins(self):
        # Two ticks racing over the same stale lock must not both conclude "stale" and both
        # reclaim it: exactly one wins, the other sees a live lock and skips without acting.
        import subprocess
        import threading

        p = subprocess.Popen(["true"])
        p.wait()
        lockfile = dispatcher.STATE.dir / "dispatch.lock"
        lockfile.write_text(str(p.pid))

        orig_stale = dispatcher._lock_stale

        def slow_stale(path):
            # Widen the stale-check window so a would-be racer has time to interleave while this
            # is mid-decision — the mutex around the whole decide-and-reclaim span must block it.
            result = orig_stale(path)
            time.sleep(0.1)
            return result

        results = []
        results_lock = threading.Lock()
        resolved = threading.Semaphore(0)  # released once per thread, after its outcome is final
        release = threading.Event()

        def contend():
            try:
                with dispatcher._tick_lock():
                    with results_lock:
                        results.append("entered")
                        hold = len(results) == 1  # the first to enter is the winner
                    resolved.release()
                    if hold:
                        # Keep the reclaimed lock visibly live until BOTH sides have resolved,
                        # instead of racing to unlink it the instant we're done — otherwise the
                        # loser might not even get a chance to observe it as live.
                        release.wait(timeout=5)
            except SystemExit:
                with results_lock:
                    results.append("skipped")
                resolved.release()

        with mock.patch.object(dispatcher, "_lock_stale", slow_stale):
            t1 = threading.Thread(target=contend)
            t2 = threading.Thread(target=contend)
            t1.start()
            t2.start()
            self.assertTrue(resolved.acquire(timeout=5))
            self.assertTrue(resolved.acquire(timeout=5))
            release.set()
            t1.join(timeout=5)
            t2.join(timeout=5)

        self.assertEqual(sorted(results), ["entered", "skipped"])
        reclaimed = [r for r in self._runs() if r["event"] == "lock-reclaimed"]
        self.assertEqual(len(reclaimed), 1)

    def test_concurrent_lock_creation_window_blocks_reader(self):
        # A lock file that exists but has no pid written yet (the O_EXCL-create-then-write gap)
        # must not be observable by a concurrent tick as stale/garbled — the writer holds the
        # decide-and-create span exclusively until the pid is in place.
        import threading

        orig_write = os.write
        write_started = threading.Event()
        release_write = threading.Event()
        paused = {"done": False}

        def paused_write(fd, data):
            if not paused["done"]:
                paused["done"] = True
                write_started.set()
                release_write.wait(timeout=5)
            return orig_write(fd, data)

        entered = []
        holder_ready = threading.Event()
        release_holder = threading.Event()

        def holder():
            with mock.patch.object(dispatcher.os, "write", paused_write):
                with dispatcher._tick_lock():
                    entered.append("holder")
                    holder_ready.set()
                    # Keep holding the now fully-written lock live until the contender has
                    # resolved, instead of racing to unlink it the instant we're done.
                    release_holder.wait(timeout=5)

        t1 = threading.Thread(target=holder)
        t1.start()
        self.assertTrue(write_started.wait(timeout=5))  # lockfile created, pid not written yet

        contender_done = threading.Event()
        contender_outcome = []

        def contender():
            try:
                with dispatcher._tick_lock():
                    entered.append("contender")
                    contender_outcome.append("entered")
            except SystemExit:
                contender_outcome.append("skipped")
            contender_done.set()

        t2 = threading.Thread(target=contender)
        t2.start()
        self.assertFalse(contender_done.wait(timeout=0.2))  # blocked behind the mutex, not racing in

        release_write.set()  # holder finishes writing its pid and enters its body
        self.assertTrue(holder_ready.wait(timeout=5))
        # The contender must now observe holder's fully-written, still-live lock (never the
        # empty pre-write file) and skip — never barge in and clobber it.
        self.assertTrue(contender_done.wait(timeout=5))
        release_holder.set()
        t1.join(timeout=5)
        t2.join(timeout=5)

        self.assertEqual(contender_outcome, ["skipped"])
        self.assertEqual(entered, ["holder"])

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


class TaskMdHistoryTest(_DispatcherBase):
    """TASK.md carries the whole card, not just header+spec (triggered-agents-222): metadata,
    the always-on force-push/own-branch protocol lines, and — only when the card already has
    comments — a chronological «История» section plus the git-fetch/don't-recreate warning."""

    def _seed_comment(self, ref, text, ts):
        tid = next(t["id"] for t in self.board.tasks.values() if t["reference"] == ref)
        self.board.comments.setdefault(tid, []).append(
            {"id": len(self.board.comments.get(tid, [])) + 1, "date_creation": ts,
             "user_id": 0, "comment": text})

    def test_fresh_card_has_no_history_section(self):
        self._ready_card("A")
        dispatcher.tick()
        (_, content), = self.worker.tasks_written
        self.assertNotIn("История", content)
        self.assertNotIn("git fetch", content)

    def test_always_on_protocol_lines_present_regardless_of_history(self):
        ref = self._ready_card("A")
        dispatcher.tick()
        (_, content), = self.worker.tasks_written
        self.assertIn("force-push запрещён", content)
        self.assertIn(f"pipeline/{ref}", content)

    # контриб-карточка: TASK.md явно говорит push только в origin, upstream не трогать -----------
    def test_contrib_project_task_md_warns_against_touching_upstream(self):
        self.worker.contrib_projects = {"agent-kanban"}
        self._ready_card("A", project="agent-kanban")
        dispatcher.tick()
        (_, content), = self.worker.tasks_written
        self.assertIn("upstream", content)
        self.assertIn("origin", content)

    def test_non_contrib_project_task_md_has_no_upstream_warning(self):
        self._ready_card("A")  # default project ("personal_site") is never in contrib_projects
        dispatcher.tick()
        (_, content), = self.worker.tasks_written
        self.assertNotIn("upstream", content)

    def test_metadata_section_has_type_model_slug_blocked_by(self):
        pred = self._ready_card("Pred")
        pred_tid = next(t["id"] for t in self.board.tasks.values() if t["reference"] == pred)
        self.board.tasks[pred_tid]["is_active"] = 0  # counts as Done for the blocked_by guard
        self._ready_card("B", model_name="opus",
                         meta={model.META_SLUG: "my-slug", model.META_BLOCKED_BY: pred})
        dispatcher.tick()
        (_, content), = self.worker.tasks_written
        self.assertIn("тип: code", content)
        self.assertIn("модель: opus", content)
        self.assertIn("слаг: my-slug", content)
        self.assertIn(f"blocked_by: {pred}", content)

    def test_history_present_triggers_fetch_dont_recreate_warning(self):
        ref = self._ready_card("A")
        self._seed_comment(ref, f"[{model.MARKER_REPORT_BLOCKED}]\nold blocker", 100)
        dispatcher.tick()
        (_, content), = self.worker.tasks_written
        self.assertIn("git fetch", content)
        self.assertIn("не пересоздавай", content)
        self.assertIn("История", content)

    def test_history_is_chronological_with_markers_and_dates(self):
        ref = self._ready_card("A")
        self._seed_comment(ref, f"[{model.MARKER_REPORT_BLOCKED}]\nfirst blocker", 100)
        self._seed_comment(ref, f"[{model.MARKER_REVIEW_RED}]\nsecond finding", 200)
        dispatcher.tick()
        (_, content), = self.worker.tasks_written
        first_pos = content.index("first blocker")
        second_pos = content.index("second finding")
        self.assertLess(first_pos, second_pos)
        self.assertIn(f"[{model.MARKER_REPORT_BLOCKED}]", content)
        self.assertIn(f"[{model.MARKER_REVIEW_RED}]", content)
        self.assertIn("1970-01-01", content)  # rendered date, not the raw unix int


class WorkspaceNamingTest(_DispatcherBase):
    """Slug -> workspace name, fallback for cards without one, collision suffix, human-readable
    tab titles, and pinning the title back every tick (Claude Code overwrites it on its own)."""

    def test_claim_uses_card_slug_in_workspace_name(self):
        from triggered_agents.agents.pipeline import naming
        ref = self._ready_card("A", meta={model.META_SLUG: "teardown-slug"})
        cid = naming.card_id(ref)
        dispatcher.tick()
        self.assertEqual(dispatcher._load_cards()[ref]["workspace"], f"/ws/{cid}-teardown-slug")
        self.assertEqual(dispatcher._load_cards()[ref]["worker"], f"{cid}-teardown-slug")

    def test_claim_without_slug_falls_back_to_title_transliteration(self):
        from triggered_agents.agents.pipeline import naming
        ref = self._ready_card("Тестовый Заголовок")
        cid = naming.card_id(ref)
        dispatcher.tick()
        expect = naming.fallback_slug("Тестовый Заголовок")
        self.assertEqual(dispatcher._load_cards()[ref]["worker"], f"{cid}-{expect}")

    def test_claim_name_collision_gets_dash_2_suffix(self):
        from triggered_agents.agents.pipeline import naming
        ref = self._ready_card("A", meta={model.META_SLUG: "dup-slug"})
        cid = naming.card_id(ref)
        self.worker.existing_workspaces.add(("personal_site", f"{cid}-dup-slug"))
        dispatcher.tick()
        self.assertEqual(dispatcher._load_cards()[ref]["worker"], f"{cid}-dup-slug-2")

    def test_claim_name_collision_keeps_incrementing(self):
        from triggered_agents.agents.pipeline import naming
        ref = self._ready_card("A", meta={model.META_SLUG: "dup-slug"})
        cid = naming.card_id(ref)
        for suffix in ("", "-2", "-3"):
            self.worker.existing_workspaces.add(("personal_site", f"{cid}-dup-slug{suffix}"))
        dispatcher.tick()
        self.assertEqual(dispatcher._load_cards()[ref]["worker"], f"{cid}-dup-slug-4")

    def test_launch_title_is_human_readable_worker_prefix(self):
        from triggered_agents.agents.pipeline import naming
        ref = self._ready_card("Fix the thing", meta={model.META_SLUG: "fix-thing"})
        cid = naming.card_id(ref)
        dispatcher.tick()
        self.assertEqual(self.worker.launched[0]["title"], f"worker {cid}: Fix the thing")

    def test_worker_title_pinned_every_tick_while_in_progress(self):
        from triggered_agents.agents.pipeline import naming
        ref = self._claim_one("A", meta={model.META_SLUG: "pin-me"})
        cid = naming.card_id(ref)
        handle = f"handle-{cid}-pin-me"
        dispatcher.tick()   # advance tick, no report yet -> stays In progress
        self.assertIn((handle, f"worker {cid}: A"), self.worker.renamed)

    def test_worker_title_pinned_while_in_validate(self):
        from triggered_agents.agents.pipeline import naming
        ref = self._claim_one("A", meta={model.META_SLUG: "pin-me"})
        cid = naming.card_id(ref)
        ops.report(ref, "done", "shipped")
        dispatcher.tick()
        self.assertEqual(self._column(ref), "Validate")
        handle = f"handle-{cid}-pin-me"
        title = f"worker {cid}: A"
        self.assertIn((handle, title), self.worker.renamed)
        before = len([c for c in self.worker.renamed if c == (handle, title)])
        dispatcher.tick()   # gh unavailable, card stays in Validate -> must pin again
        after = len([c for c in self.worker.renamed if c == (handle, title)])
        self.assertGreater(after, before)

    def test_reviewer_title_and_pinning(self):
        from triggered_agents.agents.pipeline import naming
        ref = self._claim_one("A", meta={model.META_SLUG: "rev-slug"})
        cid = naming.card_id(ref)
        ops.report(ref, "done", "готово\nPR: https://github.com/vladmesh/personal_site/pull/9")
        self.worker.pr_status = None
        dispatcher.tick()
        self.worker.pr_status = {"merged": False, "state": "OPEN", "rollup": "SUCCESS",
                                 "failed_job": None, "failed_log": None}
        dispatcher.tick()   # CI green -> spawns the reviewer
        self.assertEqual(len(self.worker.reviewer_spawns), 1)
        expect_title = naming.reviewer_title(cid, "A")
        _, worker_id, _, spawned_title, pr_branch, review_branch = self.worker.reviewer_spawns[0]
        self.assertEqual(spawned_title, expect_title)
        self.assertTrue(worker_id.startswith(f"review-{cid}-rev-slug"))
        self.assertEqual(pr_branch, naming.worker_branch(ref))
        self.assertEqual(review_branch, naming.reviewer_branch(ref))
        handle = f"rev-handle-{worker_id}"
        dispatcher.tick()   # no verdict yet -> watchdog path must pin the title again
        self.assertIn((handle, expect_title), self.worker.renamed)


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

    def test_merged_pr_tears_down_worker_workspace(self):
        # Done for a card must not leave its worker workspace/head behind — same tick tears it down.
        ref = self._to_validate()
        ws = dispatcher._load_cards()[ref]["workspace"]
        self.worker.pr_status = {"merged": True, "state": "MERGED", "rollup": "SUCCESS",
                                 "failed_job": None, "failed_log": None}
        dispatcher.tick()
        self.assertEqual(self.worker.torn_down, [ws])

    def test_closed_pr_without_merge_moves_to_blocked(self):
        # A PR closed by a human (or gone stale) without a merge must not hang the card in
        # Validate forever waiting for a merge that will never come.
        ref = self._to_validate()
        self.worker.pr_status = {"merged": False, "state": "CLOSED", "rollup": "SUCCESS",
                                 "failed_job": None, "failed_log": None}
        dispatcher.tick()
        self.assertEqual(self._column(ref), "Blocked")
        self.assertNotIn(ref, dispatcher._load_cards())
        self.assertIn("закрыт без мержа", self._markers(ref))
        self.assertTrue(any(r["event"] == "validate" and r.get("reason") == "pr-closed"
                            for r in self._runs()))

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

    def test_no_pr_ref_escalates_once_after_stall_cap(self):
        # A worker that never posts a PR link must not warn forever with no signal: past the cap,
        # a single Blocked escalation, not a repeated warn.
        ref = self._claim_one()
        ops.report(ref, "done", "готово, но ссылку на PR забыл")
        dispatcher.tick()                                        # -> Validate, no PR -> stall 1
        for i in range(2, dispatcher.VALIDATE_STALL_ATTEMPTS):
            dispatcher.tick()
            self.assertEqual(self._column(ref), "Validate")
            self.assertEqual(dispatcher._load_cards()[ref]["validate_stall_fails"], i)
        dispatcher.tick()                                        # cap reached -> escalate
        self.assertEqual(self._column(ref), "Blocked")
        self.assertNotIn(ref, dispatcher._load_cards())
        self.assertIn("не может определить статус", self._markers(ref))
        stalls = [r for r in self._runs() if r["event"] == "validate" and r.get("reason") == "no-pr-ref-stall"]
        self.assertEqual(len(stalls), 1)                          # escalation logged exactly once
        dispatcher.tick()                                        # card left Validate -> no more polling
        self.assertEqual(self._column(ref), "Blocked")

    def test_gh_unavailable_escalates_once_after_stall_cap(self):
        # _to_validate()'s own landing tick already polls once with gh down, so the counter starts
        # at 1 the moment the card settles in Validate.
        ref = self._to_validate()
        self.assertEqual(dispatcher._load_cards()[ref]["validate_stall_fails"], 1)
        self.worker.pr_status = None                              # gh stays down every tick
        for i in range(2, dispatcher.VALIDATE_STALL_ATTEMPTS):
            dispatcher.tick()
            self.assertEqual(dispatcher._load_cards()[ref]["validate_stall_fails"], i)
        dispatcher.tick()
        self.assertEqual(self._column(ref), "Blocked")
        self.assertNotIn(ref, dispatcher._load_cards())
        self.assertTrue(any(r["event"] == "validate" and r.get("reason") == "gh-unavailable-stall"
                            for r in self._runs()))

    def test_recovered_poll_resets_the_stall_counter(self):
        ref = self._to_validate()                                 # stall already 1 from landing
        self.worker.pr_status = None
        dispatcher.tick()
        self.assertEqual(dispatcher._load_cards()[ref]["validate_stall_fails"], 2)
        self.worker.pr_status = {"merged": False, "state": "OPEN", "rollup": "PENDING",
                                 "failed_job": None, "failed_log": None}
        dispatcher.tick()                                        # gh answers again -> counter drops
        self.assertNotIn("validate_stall_fails", dispatcher._load_cards()[ref])


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

    def test_green_verdict_logs_terminal_green_exactly_once(self):
        # Regression (review #1 on triggered-agents-221): a no-stand card idling in Validate on a
        # settled green verdict must log the terminal "review green" event once, not on every tick
        # it sits there waiting for a human merge.
        ref = self._spawned()
        ops.verdict(ref, "green", "ок")
        for _ in range(3):
            dispatcher.tick()
        self.assertEqual(self._column(ref), "Validate")
        greens = [r for r in self._runs() if r["event"] == "review" and r.get("result") == "green"]
        self.assertEqual(len(greens), 1)

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

    def test_stale_green_from_prior_cycle_not_reused_after_ci_regression(self):
        # A green verdict lives for its cycle, not the card's whole life: once a settled green
        # verdict is followed by a real CI regression (bounce back to In progress, rework), the next
        # trip through Validate must wait for a fresh review, not treat the old green as still good.
        ref = self._spawned()
        ops.verdict(ref, "green", "ок первый цикл")
        dispatcher.tick()
        self.assertEqual(self._column(ref), "Validate")          # waits for merge
        self.worker.pr_status = {"merged": False, "state": "OPEN", "rollup": "FAILURE",
                                 "failed_job": "CI", "failed_log": "regression"}
        dispatcher.tick()                                        # CI regresses -> back to rework
        self.assertEqual(self._column(ref), model.IN_PROGRESS)
        self.assertNotIn("review_baseline", dispatcher._load_cards()[ref])
        ops.report(ref, "done", "починил")
        self.worker.pr_status = None
        dispatcher.tick()
        self.assertEqual(self._column(ref), "Validate")
        self._ci_green()
        dispatcher.tick()                                        # CI green again -> a fresh reviewer
        self.assertEqual(len(self.worker.reviewer_spawns), 2)
        self.assertEqual(self._column(ref), "Validate")          # waits for the new cycle's verdict

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


class AutomergeTest(_DispatcherBase):
    """triggered-agents-221: for a project with a [stand] section, a green review verdict on top
    of already-green CI and stand triggers the dispatcher's own squash merge (worker.merge_pr)
    instead of waiting for a human — vladmesh's 2026-07-02 call that the live-stand e2e gate is
    enough assurance. Projects without a stand keep the old human-merge behaviour."""

    PR = "https://github.com/vladmesh/personal_site/pull/101"
    STAND = {"namespace": "personal_site_stand", "compose": ["infra/docker-compose.stand.yml"],
             "e2e_command": "bash infra/e2e/run.sh"}

    def _markers(self, ref):
        tid = next(t["id"] for t in self.board.tasks.values() if t["reference"] == ref)
        return " ".join(c["comment"] for c in self.board.comments.get(tid, []))

    def _ci_green(self):
        self.worker.pr_status = {"merged": False, "state": "OPEN", "rollup": "SUCCESS",
                                 "failed_job": None, "failed_log": None}

    def _to_review_green(self, pr=PR):
        """Stand project all the way through: done -> Validate, CI green -> stand run -> stand
        green, reviewer spawned, green verdict posted — not yet consumed by a tick."""
        self.worker.stand_config = self.STAND
        self.worker.stand_branch = "pipeline/A"
        ref = self._claim_one()
        ops.report(ref, "done", f"готово\nPR: {pr}")
        self.worker.pr_status = None
        dispatcher.tick()
        self.assertEqual(self._column(ref), "Validate")
        self._ci_green()
        self.worker.stand_result = {"ok": True, "stage": "e2e", "log": "ok"}
        dispatcher.tick()                     # stand run -> stand-green
        dispatcher.tick()                     # stand green noted -> spawn reviewer
        self.assertEqual(len(self.worker.reviewer_spawns), 1)
        ops.verdict(ref, "green", "все критерии реально выполнены")
        return ref

    def test_stand_project_green_review_triggers_squash_merge(self):
        ref = self._to_review_green()
        dispatcher.tick()
        self.assertEqual(self.worker.merged, [self.PR])
        journal = self._markers(ref)
        self.assertIn(f"[{model.MARKER_AUTOMERGE}]", journal)
        self.assertIn(self.PR, journal)
        self.assertEqual(self._column(ref), "Validate")   # still waits on gh to report merged
        self.assertTrue(any(r["event"] == "review" and r.get("result") == "green-automerge"
                            for r in self._runs()))

    def test_automerge_then_gh_reports_merged_moves_to_done(self):
        ref = self._to_review_green()
        dispatcher.tick()                                  # merges via gh
        self.worker.pr_status = {"merged": True, "state": "MERGED", "rollup": "SUCCESS",
                                 "failed_job": None, "failed_log": None}
        dispatcher.tick()
        self.assertEqual(self._column(ref), "Done")

    def test_automerge_is_attempted_once_across_ticks(self):
        # gh may not reflect the merge immediately: a later tick still sees the green verdict and
        # poll_pr still reports merged=False, but must not call gh pr merge a second time.
        ref = self._to_review_green()
        dispatcher.tick()
        self.assertEqual(self.worker.merged, [self.PR])
        dispatcher.tick()
        self.assertEqual(self.worker.merged, [self.PR])

    def test_merge_failure_blocks_with_reason_and_does_not_retry(self):
        ref = self._to_review_green()
        self.worker.merge_result = {"ok": False, "error": "PR is not mergeable: conflicting files"}
        dispatcher.tick()
        self.assertEqual(self._column(ref), "Blocked")
        self.assertNotIn(ref, dispatcher._load_cards())
        journal = self._markers(ref)
        self.assertIn("conflicting files", journal)
        self.assertEqual(self.worker.merged, [self.PR])
        self.assertTrue(any(r["event"] == "review" and r.get("reason") == "automerge-fail"
                            for r in self._runs()))
        dispatcher.tick()                                  # Blocked, off the board -> no retry
        self.assertEqual(self.worker.merged, [self.PR])

    def test_no_stand_project_green_review_waits_for_human(self):
        ref = self._claim_one()
        ops.report(ref, "done", f"готово\nPR: {self.PR}")
        self.worker.pr_status = None
        dispatcher.tick()
        self._ci_green()
        dispatcher.tick()                                  # no stand -> spawns the reviewer directly
        self.assertEqual(len(self.worker.reviewer_spawns), 1)
        ops.verdict(ref, "green", "ок")
        dispatcher.tick()
        self.assertEqual(self.worker.merged, [])
        self.assertEqual(self._column(ref), "Validate")


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
        self.git_calls = []

        def fake_orca_json(args):
            self.calls.append(args)
            if args[0] == "worktree":
                return {"worktree": {"path": "/ws/fresh"}}
            return {"terminal": {"handle": "term-1"}}

        p = mock.patch.object(worker, "_orca_json", fake_orca_json)
        p.start()
        self.addCleanup(p.stop)

        def fake_git_ok(cwd, args, timeout=None):
            self.git_calls.append((str(cwd), args))
            return ""

        g = mock.patch.object(worker, "_git_ok", fake_git_ok)
        g.start()
        self.addCleanup(g.stop)

        self.ensured = []
        for name in ("ensure_trust", "ensure_theme"):
            wp = mock.patch.object(worker, name, lambda *a, n=name: self.ensured.append(n))
            wp.start()
            self.addCleanup(wp.stop)

    def test_create_workspace_activates_the_worktree(self):
        self.worker.create_workspace("proj", "worker-1", "main")
        args = self.calls[0]
        self.assertIn("--activate", args)

    def test_create_workspace_fetches_base_and_creates_off_origin(self):
        # Git hygiene: the worktree is cut from a freshly fetched origin/<base>, never whatever
        # the project's own local checkout happens to have.
        self.worker.create_workspace("proj", "worker-1", "main")
        self.assertEqual(self.git_calls[0][1], ["fetch", "origin", "main"])
        args = self.calls[0]
        self.assertEqual(args[args.index("--base-branch") + 1], "origin/main")

    def test_no_step_ever_pushes(self):
        self.worker.create_workspace("proj", "worker-1", "main")
        self.worker.set_branch("/ws/fresh", "pipeline/proj-1")
        self.worker.land_pr_head("/ws/fresh", "pipeline/proj-1", "review/proj-1")
        self.assertFalse(any("push" in args for _, args in self.git_calls))

    def test_launch_worker_ensures_trust_and_theme_before_terminal_create(self):
        self.worker.launch_worker("/ws/fresh", None, "worker-1", "worker A-1: title")
        self.assertEqual(self.ensured, ["ensure_trust", "ensure_theme"])
        self.assertEqual(self.calls[0][0], "terminal")
        self.assertIn("worker A-1: title", self.calls[0])

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
                self.worker.spawn_reviewer("proj", "rev-1", "main", "REVIEW body", "review A-1: title",
                                           "pipeline/proj-1", "review/proj-1")
        self.assertEqual(torn, ["/ws/rev"])

    def test_spawn_reviewer_lands_pr_head_on_its_own_branch(self):
        with mock.patch.object(self.worker, "_write_excluded", lambda *a: "/ws/fresh/REVIEW.md"):
            self.worker.spawn_reviewer("proj", "rev-1", "main", "REVIEW body", "review A-1: title",
                                       "pipeline/proj-1", "review/proj-1")
        ws, args = self.git_calls[-1]
        self.assertEqual(ws, "/ws/fresh")
        self.assertEqual(args, ["branch", "-M", "review/proj-1"])
        self.assertIn(["fetch", "origin", "pipeline/proj-1"], [a for _, a in self.git_calls])
        self.assertIn(["reset", "--hard", "FETCH_HEAD"], [a for _, a in self.git_calls])
        self.assertFalse(any("push" in args for _, args in self.git_calls))


class RollupTest(unittest.TestCase):
    """worker._rollup(): the pure function that folds a PR's statusCheckRollup into one verdict.
    Must wait for every job to reach a terminal state before calling FAILURE — an early failed job
    next to a still-running one is a flaky-looking early return, not a settled verdict."""

    def _run(self, name, status, conclusion=None):
        return {"__typename": "CheckRun", "name": name, "status": status, "conclusion": conclusion}

    def test_all_success_is_success(self):
        items = [self._run("lint", "COMPLETED", "SUCCESS"), self._run("test", "COMPLETED", "SUCCESS")]
        self.assertEqual(worker._rollup(items), ("SUCCESS", None))

    def test_one_failed_all_terminal_is_failure(self):
        items = [self._run("lint", "COMPLETED", "SUCCESS"), self._run("test", "COMPLETED", "FAILURE")]
        rollup, failed = worker._rollup(items)
        self.assertEqual(rollup, "FAILURE")
        self.assertEqual(failed["name"], "test")

    def test_failed_job_next_to_running_job_stays_pending(self):
        # One job failed, the other is still running: the rollup must not call FAILURE before the
        # running job finishes.
        items = [self._run("test", "COMPLETED", "FAILURE"), self._run("e2e", "IN_PROGRESS")]
        self.assertEqual(worker._rollup(items), ("PENDING", None))

    def test_becomes_failure_once_the_running_job_finishes(self):
        items = [self._run("test", "COMPLETED", "FAILURE"), self._run("e2e", "COMPLETED", "SUCCESS")]
        rollup, failed = worker._rollup(items)
        self.assertEqual(rollup, "FAILURE")
        self.assertEqual(failed["name"], "test")

    def test_no_checks_is_none(self):
        self.assertEqual(worker._rollup([]), ("NONE", None))


class TeardownTest(unittest.TestCase):
    """worker.teardown(): guard the path before any rm runs, stop terminals before the worktree
    goes away, and log a sudo-fallback trip (should stay silent after the personal_site PR#24
    non-root fix — a trip here is the signal root-owned grief is back)."""

    def setUp(self):
        from triggered_agents.agents.pipeline import worker
        self.worker = worker
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = (Path(self.tmp.name) / "workspaces").resolve()
        self.root.mkdir()
        self._patch_root = mock.patch.object(worker, "WORKSPACES_ROOT", self.root)
        self._patch_root.start()
        self.addCleanup(self._patch_root.stop)
        self.calls = []

    def _ws(self, name="proj/worker-1"):
        ws = self.root / name
        ws.mkdir(parents=True)
        return ws

    def test_refuses_path_outside_root(self):
        outside = Path(self.tmp.name) / "elsewhere"
        outside.mkdir()
        with mock.patch("subprocess.run") as run:
            with self.assertRaises(self.worker.WorkspaceError):
                self.worker.teardown(str(outside))
            run.assert_not_called()          # guard fires before any orca/rm call
        self.assertTrue(outside.exists())    # nothing was touched

    def test_refuses_the_root_itself(self):
        # The root is not a workspace: removing it would take every project's workspace with it.
        with mock.patch("subprocess.run") as run:
            with self.assertRaises(self.worker.WorkspaceError):
                self.worker.teardown(str(self.root))
            run.assert_not_called()
        self.assertTrue(self.root.exists())

    def test_worktree_rm_timeout_falls_back_to_rm(self):
        # A wedged orca daemon must not hang the tick forever — orca worktree rm is bounded the
        # same way stop_terminals already is, and a timeout there falls through to rm -rf.
        import subprocess
        import shutil

        ws = self._ws()

        def fake_run(args, **kw):
            self.calls.append(args)
            if args[:2] == [self.worker.ORCA, "worktree"]:
                raise subprocess.TimeoutExpired(cmd=args, timeout=kw.get("timeout"))
            if args[:2] == ["rm", "-rf"]:
                shutil.rmtree(ws, ignore_errors=True)
            return subprocess.CompletedProcess(args, 0, "", "")

        with mock.patch("subprocess.run", side_effect=fake_run):
            self.worker.teardown(str(ws))
        self.assertFalse(ws.exists())
        self.assertTrue(any(a[:2] == ["rm", "-rf"] for a in self.calls))

    def test_stops_terminals_before_worktree_rm(self):
        import subprocess
        import shutil

        ws = self._ws()

        def fake_run(args, **kw):
            self.calls.append(args)
            if args[:2] == [self.worker.ORCA, "worktree"]:
                shutil.rmtree(ws, ignore_errors=True)
            return subprocess.CompletedProcess(args, 0, "", "")

        with mock.patch("subprocess.run", side_effect=fake_run):
            self.worker.teardown(str(ws))
        self.assertEqual(self.calls[0][:3], [self.worker.ORCA, "terminal", "stop"])
        self.assertEqual(self.calls[1][:2], [self.worker.ORCA, "worktree"])
        self.assertFalse(ws.exists())

    def test_sudo_fallback_is_logged(self):
        import json
        import subprocess

        ws = self._ws()
        state_dir = Path(self.tmp.name) / "state"

        def fake_run(args, **kw):
            self.calls.append(args)
            if args[:2] == [self.worker.ORCA, "worktree"]:
                return subprocess.CompletedProcess(args, 1, "", "denied")   # orca rm fails
            if args[:2] == ["rm", "-rf"]:
                return subprocess.CompletedProcess(args, 1, "", "denied")   # plain rm fails too
            if args[:1] == ["sudo"]:
                import shutil
                shutil.rmtree(ws, ignore_errors=True)
            return subprocess.CompletedProcess(args, 0, "", "")

        with mock.patch.object(self.worker.STATE, "dir", state_dir), \
             mock.patch("subprocess.run", side_effect=fake_run):
            self.worker.teardown(str(ws))

        runs = [json.loads(line) for line in (state_dir / "runs.jsonl").read_text().splitlines()]
        self.assertTrue(any(r["event"] == "teardown-sudo-fallback" and r.get("workspace") == str(ws)
                            for r in runs))
        self.assertTrue(any(a[:1] == ["sudo"] for a in self.calls))

    def test_clean_removal_does_not_trip_sudo_fallback(self):
        import subprocess
        import shutil

        ws = self._ws()
        state_dir = Path(self.tmp.name) / "state"

        def fake_run(args, **kw):
            self.calls.append(args)
            if args[:2] == [self.worker.ORCA, "worktree"]:
                shutil.rmtree(ws, ignore_errors=True)
            return subprocess.CompletedProcess(args, 0, "", "")

        with mock.patch.object(self.worker.STATE, "dir", state_dir), \
             mock.patch("subprocess.run", side_effect=fake_run):
            self.worker.teardown(str(ws))
        self.assertFalse((state_dir / "runs.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
