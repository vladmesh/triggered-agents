"""Unit tests for the pipeline dispatcher — stdlib unittest, no network, no Orca.

TA_STATE and TA_PIPELINE_STATE_DIR point at tempdirs before any triggered_agents import. The board
goes through FakeBoard (reused from test_pipeline); the host side (worker.py: worktree/head/
activity) is stubbed by FakeWorker so the dispatcher's decisions run for real while nothing leaves
the process.
"""
from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

_STATE_DIR = tempfile.mkdtemp(prefix="ta-dispatcher-test-")
_PIPELINE_STATE_DIR = tempfile.mkdtemp(prefix="ta-dispatcher-live-state-test-")
os.environ["TA_STATE"] = _STATE_DIR
os.environ["TA_PIPELINE_STATE_DIR"] = _PIPELINE_STATE_DIR
os.environ.pop("KANBOARD_ADMIN_USER", None)

sys.path.insert(0, str(Path(__file__).resolve().parent))          # tests/ for test_pipeline
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))       # repo root

from test_pipeline import FakeBoard  # noqa: E402

from triggered_agents.agents.pipeline import (  # noqa: E402
    dispatcher, health, heads, model, ops, pause, validate, worker,
)
from triggered_agents.agents.pipeline import state as pipeline_state  # noqa: E402
from triggered_agents.runtime import state as runtime_state  # noqa: E402
from triggered_agents.runtime.state import PRECHECK_SKIP  # noqa: E402


class FakeWorker:
    """Records host-side calls; provisioning result and activity are configurable per test."""

    def __init__(self):
        self.provision_ok = True
        self.provision_log = "[provision] done\n"
        self.create_raises = None
        self.launch_raises = None
        self.activity_ts = None          # default tracked-terminal activity
        self.activity_by_handle = {}     # handle -> activity timestamp, overriding activity_ts
        self.activity_polled = []        # every workspace activity helper was asked about
        self.terminal_activity_polled = []  # (handle, workspace) for tracked activity polls
        self.launched = []
        self.tasks_written = []
        self.torn_down = []
        self.pr_status = None            # dict returned by poll_pr, or None for gh-unavailable
        self.polled = []                 # PR urls poll_pr was asked about
        self.merge_result = {"ok": True, "error": None}  # dict returned by merge_pr
        self.merged = []                 # PR urls merge_pr was asked to merge
        self.pr_bases = {}               # pr_url -> baseRefName pr_base_branch returns (default "main")
        self.pr_base_polled = []         # PR urls pr_base_branch was asked about
        self.notified = []               # (handle, text) nudges sent to worker terminals
        self.dead_handles = set()         # handles terminal_live should report as gone
        self.terminal_entries = {}        # handle -> Orca terminal-list entry for terminal_live
        self.terminal_status_polled = []  # (handle, workspace) for tracked status polls
        self.unique_launch_handles = False
        self.stand_config = None         # dict from read_stand_config, or None for no-stand project
        self.stand_branch = "pipeline/x"  # branch pr_branch returns (None -> gh can't answer)
        self.stand_result = None         # dict from run_stand, or None for unavailable
        self.stand_runs = []             # (project, branch) each run_stand was asked about
        self.reviewer_spawns = []        # (project, worker_id, base, title, pr_branch, review_branch)
        self.reviewer_heads = []         # review_head passed to each spawn_reviewer call
        self.reviewer_raises = None      # set to raise from spawn_reviewer (orca failure)
        self.existing_workspaces = set()  # (project, name) pairs treated as already on disk
        self.renamed = []                # (handle, title) every rename_terminal call
        self.branches_set = []           # (workspace, branch) every set_branch call
        self.agent_worktrees = []        # [(name, path), ...] list_agent_worktrees returns
        self.ff_results = {}             # path -> ff_worktree result dict (default: clean no-op ff)
        self.ff_calls = []               # (path, base_branch) every ff_worktree call
        self.contrib_projects = set()    # project names is_contrib() should report True for
        self.no_ci_projects = set()      # project names ci_expected() should report False for
        self.remote_head_shas = {}       # branch -> sha remote_head_sha returns; missing -> None
        self.stopped_terminals = []      # workspaces stop_terminals was asked to stop (no rm)
        self.workspace_calls = []        # (project, name, base_branch) every create_workspace call
        self.pr_files_result = None      # list[str] | None returned by pr_files (None -> gh down)
        self.pr_files_calls = []         # PR urls pr_files was asked about
        self.apply_provision_result = {"ok": True, "log": "provisioned"}
        self.apply_provision_calls = []  # each `agents` list apply_provision was called with
        self.relaunched_reviewers = []   # {"ws", "worker", "title"} each relaunch_reviewer call
        self._n = 0

    def read_base_branch(self, project):
        return "main"

    def is_contrib(self, project):
        return project in self.contrib_projects

    def ci_expected(self, project):
        return project not in self.no_ci_projects

    def list_agent_worktrees(self):
        return list(self.agent_worktrees)

    def ff_worktree(self, path, base_branch):
        self.ff_calls.append((path, base_branch))
        return self.ff_results.get(path, {"ok": True, "reason": None, "before": "x", "after": "x"})

    def create_workspace(self, project, name, base_branch):
        self.workspace_calls.append((project, name, base_branch))
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

    def launch_worker(self, workspace, head, worker_id, title):
        self.launched.append({"ws": workspace, "head": head, "worker": worker_id, "title": title})
        if self.launch_raises:
            raise self.launch_raises
        if self.unique_launch_handles:
            return f"handle-{worker_id}-{len(self.launched)}"
        return f"handle-{worker_id}"

    def workspace_exists(self, project, name):
        return (project, name) in self.existing_workspaces

    def workspace_path(self, project, name):
        return f"/ws/{name}"

    def rename_terminal(self, handle, title):
        self.renamed.append((handle, title))
        return bool(handle)

    def terminal_kind(self, head):
        return "codex-tui" if head and str(head).endswith("-tui") else None

    def reviewer_terminal_kind(self, head=None):
        return "codex-tui" if head and str(head).endswith("-tui") else None

    def activity(self, workspace):
        self.activity_polled.append(workspace)
        return self.activity_ts

    def terminal_activity(self, handle, workspace):
        self.activity_polled.append(workspace)
        self.terminal_activity_polled.append((handle, workspace))
        return self.activity_by_handle.get(handle, self.activity_ts)

    def terminal_status(self, handle, workspace=None, expected_kind=None):
        if workspace:
            self.activity_polled.append(workspace)
        self.terminal_activity_polled.append((handle, workspace))
        self.terminal_status_polled.append((handle, workspace))
        if not handle:
            return {"known": True, "live": False, "reason": "missing-handle"}
        if handle in self.dead_handles:
            return {"known": True, "live": False, "reason": "missing-terminal"}
        if handle in self.terminal_entries:
            entry = self.terminal_entries[handle]
            reason = worker._terminal_entry_dead_reason(entry)
            if not reason:
                reason = worker._terminal_expected_kind_dead_reason(
                    entry, expected_kind, handle, read_terminal_text=lambda _handle: "")
            if reason:
                return {"known": True, "live": False, "reason": reason}
            return {"known": True, "live": True, "reason": "live",
                    "last_activity": self.activity_by_handle.get(handle, self.activity_ts)}
        return {"known": True, "live": True, "reason": "live",
                "last_activity": self.activity_by_handle.get(handle, self.activity_ts)}

    def poll_pr(self, pr_url):
        self.polled.append(pr_url)
        return self.pr_status

    def merge_pr(self, pr_url):
        self.merged.append(pr_url)
        return self.merge_result

    def pr_base_branch(self, pr_url):
        self.pr_base_polled.append(pr_url)
        return self.pr_bases.get(pr_url, "main")

    def notify(self, handle, text):
        self.notified.append((handle, text))
        return bool(handle)

    def terminal_live(self, handle, workspace=None, expected_kind=None):
        if not handle or handle in self.dead_handles:
            return False
        if handle in self.terminal_entries:
            entry = self.terminal_entries[handle]
            if not worker._terminal_entry_live(entry):
                return False
            return worker._terminal_expected_kind_dead_reason(
                entry, expected_kind, handle, read_terminal_text=lambda _handle: "") is None
        return True

    def read_stand_config(self, project):
        return self.stand_config

    def pr_branch(self, pr_url):
        return self.stand_branch

    def run_stand(self, project, branch, cfg):
        self.stand_runs.append((project, branch))
        return self.stand_result

    def spawn_reviewer(self, project, worker_id, base_branch, review_md, title, pr_branch,
                       review_branch, head_sha=None, review_head=None):
        if self.reviewer_raises:
            raise self.reviewer_raises
        self.reviewer_spawns.append((project, worker_id, base_branch, title, pr_branch,
                                     review_branch, head_sha))
        self.reviewer_heads.append(review_head)
        return f"/rev/{worker_id}", f"rev-handle-{worker_id}"

    def remote_head_sha(self, project, branch):
        return self.remote_head_shas.get(branch)

    def pr_files(self, pr_url):
        self.pr_files_calls.append(pr_url)
        return self.pr_files_result

    def apply_provision(self, agents):
        self.apply_provision_calls.append(list(agents))
        return self.apply_provision_result

    def teardown(self, workspace):
        self.torn_down.append(workspace)

    def stop_terminals(self, workspace):
        self.stopped_terminals.append(workspace)

    def relaunch_reviewer(self, workspace, worker_id, title, review_head=None):
        self.relaunched_reviewers.append({
            "ws": workspace,
            "worker": worker_id,
            "title": title,
            "review_head": review_head,
        })
        return f"resumed-rev-handle-{worker_id}"


class _DispatcherBase(unittest.TestCase):
    """Board+host fakes and helpers shared by the dispatcher test cases."""

    def setUp(self):
        # Fresh board transport + host stub for every test; clean the shared pipeline state.
        self.board = FakeBoard()
        p = mock.patch("triggered_agents.agents.pipeline.ops.call", self.board.call)
        p.start()
        self.addCleanup(p.stop)

        self.worker = FakeWorker()
        for name in ("read_base_branch", "is_contrib", "ci_expected",
                     "create_workspace", "set_branch", "provision",
                     "write_task", "launch_worker", "activity", "terminal_activity", "poll_pr", "notify",
                     "terminal_status", "terminal_live", "teardown", "read_stand_config", "pr_branch", "run_stand",
                     "spawn_reviewer",
                     "workspace_exists", "workspace_path", "rename_terminal", "merge_pr",
                     "pr_base_branch", "list_agent_worktrees", "ff_worktree", "remote_head_sha",
                     "stop_terminals", "pr_files", "apply_provision", "relaunch_reviewer",
                     "terminal_kind", "reviewer_terminal_kind"):
            wp = mock.patch(f"triggered_agents.agents.pipeline.worker.{name}",
                            getattr(self.worker, name))
            wp.start()
            self.addCleanup(wp.stop)

        dispatcher.STATE.ensure_dir()
        for f in (dispatcher.CARDS_FILE, dispatcher.STATE.dir / "runs.jsonl",
                  dispatcher.STATE.dir / "dispatch.lock", dispatcher.STATE.dir / "lock",
                  pause.PAUSE_FILE):
            if f.exists():
                f.unlink()
        self._orig_watchdog = dispatcher.WATCHDOG_SECONDS
        self._orig_ci_pending_stall = validate.CI_PENDING_STALL_SECONDS

        # Resource health defaults to all-green (regular behavior, untouched by this card) unless
        # a test mutates self.statuses — real probing (subprocess/network) never runs in a unit
        # test. A test that needs a custom registry (fallback chains beyond claude-sonnet/opus'
        # real empty ones) patches heads.load_registry separately.
        self.statuses = {"claude-sub": "green", "openrouter": "green"}
        hp = mock.patch.object(health, "refresh", lambda registry=None: dict(self.statuses))
        hp.start()
        self.addCleanup(hp.stop)

    def tearDown(self):
        dispatcher.WATCHDOG_SECONDS = self._orig_watchdog
        validate.CI_PENDING_STALL_SECONDS = self._orig_ci_pending_stall

    # helpers ---------------------------------------------------------------
    def _ref_of(self, title):
        return next(t["reference"] for t in self.board.tasks.values() if t["title"] == title)

    def _column(self, ref):
        tid = next(t["id"] for t in self.board.tasks.values() if t["reference"] == ref)
        return self.board._column_title_for(tid)

    def _ready_card(self, title, project="personal_site", ttype="code", meta=None, head=None):
        m = {model.META_TASK_TYPE: ttype, model.META_PROJECT: project}
        if head:
            m[model.META_HEAD] = head
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
        self.assertEqual(rc, PRECHECK_SKIP)   # deliberate skip, distinct from a crash (1)
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

    # Kanboard недоступен/битый env: исход error, ненулевой выход отличимый от PRECHECK_SKIP.
    def test_precheck_error_when_board_unreachable(self):
        with mock.patch.object(ops, "list_cards", side_effect=RuntimeError("connection refused")):
            rc = dispatcher.precheck()
        self.assertEqual(rc, 2)
        self.assertNotEqual(rc, PRECHECK_SKIP)
        runs = [r for r in self._runs() if r["event"] == "precheck"]
        self.assertTrue(any(r.get("result") == "error" and r.get("error_class") == "RuntimeError"
                             for r in runs))

    # ff agent worktrees ------------------------------------------------------
    def test_precheck_ffs_clean_agent_worktrees(self):
        self.worker.agent_worktrees = [("curator", "/ws/agents/curator")]
        self.worker.ff_results = {"/ws/agents/curator": {"ok": True, "reason": None,
                                                          "before": "aaa", "after": "bbb"}}
        dispatcher.precheck()
        self.assertIn(("/ws/agents/curator", "main"), self.worker.ff_calls)
        runs = [r for r in self._runs() if r["event"] == "ff-agents"]
        self.assertTrue(any(r.get("agent") == "curator" and r.get("result") == "ff"
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
        self.worker.agent_worktrees = [("curator", "/ws/agents/curator"), ("retro", "/ws/agents/retro")]
        self.worker.ff_results = {"/ws/agents/curator": {"ok": False, "reason": "diverged"},
                                  "/ws/agents/retro": {"ok": True, "reason": None,
                                                        "before": "a", "after": "b"}}
        dispatcher.precheck()
        paths_called = [c[0] for c in self.worker.ff_calls]
        self.assertEqual(paths_called, ["/ws/agents/curator", "/ws/agents/retro"])
        runs = [r for r in self._runs() if r["event"] == "ff-agents"]
        self.assertTrue(any(r.get("agent") == "curator" and r.get("result") == "blocked" for r in runs))
        self.assertTrue(any(r.get("agent") == "retro" and r.get("result") == "ff" for r in runs))

    def test_precheck_ff_survives_read_base_branch_failure(self):
        with mock.patch.object(worker, "read_base_branch", side_effect=RuntimeError("no manifest")):
            rc = dispatcher.precheck()
        self.assertEqual(rc, PRECHECK_SKIP)  # board still empty -> nothing-to-do, unaffected
        runs = [r for r in self._runs() if r["event"] == "ff-agents"]
        self.assertTrue(any(r.get("result") == "error" and r.get("level") == "warn" for r in runs))

    # claim + bring-up ------------------------------------------------------
    def test_tick_claims_and_launches(self):
        ref = self._ready_card("A", head="claude-opus")
        dispatcher.tick()
        self.assertEqual(self._column(ref), model.IN_PROGRESS)
        self.assertEqual(len(self.worker.launched), 1)
        self.assertEqual(self.worker.launched[0]["head"], "claude-opus")
        self.assertEqual(len(self.worker.tasks_written), 1)
        records = dispatcher._load_cards()
        self.assertIn(ref, records)
        self.assertEqual(records[ref]["comment_baseline"], 1)

    def test_successful_claim_posts_start_comment(self):
        ref = self._ready_card("A")
        with mock.patch.object(dispatcher.time, "time", return_value=1714824240):
            dispatcher.tick()

        tid = next(t["id"] for t in self.board.tasks.values() if t["reference"] == ref)
        comments = [c["comment"] for c in self.board.comments.get(tid, [])]
        self.assertEqual(len(comments), 1)
        self.assertIn(f"[{model.MARKER_CLAIM_STARTED}]", comments[0])
        self.assertIn("Взята в работу 2024-05-04 12:04 UTC", comments[0])
        self.assertIn("воркер 1-a", comments[0])
        self.assertIn("воркспейс /ws/1-a", comments[0])
        self.assertEqual(dispatcher._load_cards()[ref]["comment_baseline"], 1)

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

    # card-level base_branch override (triggered-agents-260) ----------------
    def test_no_base_branch_uses_manifest_default(self):
        # unchanged behavior: no card override -> the project's manifest lookup (stubbed "main").
        ref = self._ready_card("A")
        dispatcher.tick()
        self.assertEqual(len(self.worker.workspace_calls), 1)
        project, _name, base = self.worker.workspace_calls[0]
        self.assertEqual((project, base), ("personal_site", "main"))
        task_md = self.worker.tasks_written[0][1]
        self.assertIn("- база: main", task_md)
        self.assertIn("base — `main`", task_md)

    def test_card_base_branch_overrides_manifest_at_bring_up(self):
        self.worker.remote_head_shas["sprint/007-dnd"] = "deadbeef"
        ref = self._ready_card("A", project="dnd-simulator",
                               meta={model.META_BASE_BRANCH: "sprint/007-dnd"})
        dispatcher.tick()
        self.assertEqual(len(self.worker.workspace_calls), 1)
        project, _name, base = self.worker.workspace_calls[0]
        self.assertEqual((project, base), ("dnd-simulator", "sprint/007-dnd"))
        self.assertEqual(self._column(ref), model.IN_PROGRESS)
        task_md = self.worker.tasks_written[0][1]
        self.assertIn("- база: sprint/007-dnd", task_md)
        self.assertIn("base — `sprint/007-dnd`", task_md)

    def test_card_base_branch_missing_on_origin_blocks_no_silent_main_fallback(self):
        # no self.worker.remote_head_shas entry -> remote_head_sha returns None (branch absent)
        ref = self._ready_card("A", project="dnd-simulator",
                               meta={model.META_BASE_BRANCH: "sprint/999-ghost"})
        dispatcher.tick()
        self.assertEqual(self._column(ref), "Blocked")
        self.assertEqual(self.worker.workspace_calls, [])   # never created off main, never at all
        self.assertEqual(self.worker.launched, [])
        posted = " ".join(c["comment"] for cl in self.board.comments.values() for c in cl)
        self.assertIn("sprint/999-ghost", posted)

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
        busy = self.board.add_task("busy", "In progress", swimlane="other",
                                   meta={model.META_TASK_TYPE: "research", model.META_PROJECT: "other",
                                         model.META_CLAIM: "w0"})
        now = time.time()
        dispatcher._save_cards({
            busy: {
                "workspace": "/ws/w0",
                "worker": "w0",
                "handle": "handle-w0",
                "title": "worker busy",
                "head": heads.DEFAULT_PROFILE,
                "claimed_at": now,
                "last_activity": now,
                "comment_baseline": 0,
            }
        })
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

    def test_watchdog_retries_same_head_first(self):
        # A single watchdog timeout with a live budget: teardown, Ready, reclaimed same tick
        # (FakeWorker never fails a claim) — In progress again, not Blocked, workspace torn down.
        ref = self._claim_one()
        first_ws = dispatcher._load_cards()[ref]["workspace"]
        dispatcher.WATCHDOG_SECONDS = -1        # any silence counts as over-threshold
        self.worker.activity_ts = None          # no fresh output
        dispatcher.tick()
        self.assertEqual(self._column(ref), model.IN_PROGRESS)
        self.assertIn(first_ws, self.worker.torn_down)
        self.assertEqual(ops.show_card(ref)["metadata"][model.META_RETRY_SAME], "1")
        self.assertEqual(ops.show_card(ref)["metadata"][model.META_RETRY_SWITCH], "0")
        posted = " ".join(c["comment"] for cl in self.board.comments.values() for c in cl)
        self.assertIn("watchdog", posted)
        self.assertIn(f"[{model.MARKER_WATCHDOG_RETRY}]", posted)
        self.assertTrue(any(r.get("reason") == "watchdog-retry-same" for r in self._runs()))
        # the claim guard was actually re-run (new worker id/claim), not a bare metadata edit
        self.assertGreaterEqual(len(self.worker.launched), 2)

    def test_watchdog_teardown_failure_does_not_crash_the_tick(self):
        # _advance has no per-card try/except (unlike validate.run) — a host-side teardown error
        # on one card's dead workspace must not stop every OTHER In-progress card from advancing
        # in the same tick, and must not prevent the retry requeue itself either.
        ref_broken = self._claim_one("A")
        ref_other = self._claim_one("B", project="other")
        # ref_other's budget is already exhausted (as if this were its third timeout), so its own
        # watchdog goes straight to Blocked this tick regardless of what happens to ref_broken.
        ops.set_retry_state(ref_other, retry_same=1, retry_switch=1, retry_heads="claude-sonnet")

        with mock.patch("triggered_agents.agents.pipeline.worker.teardown",
                        side_effect=RuntimeError("orca daemon wedged")):
            dispatcher.WATCHDOG_SECONDS = -1
            self.worker.activity_ts = None
            dispatcher.tick()   # must not raise
        self.assertEqual(self._column(ref_broken), model.IN_PROGRESS)   # retry still happened
        self.assertEqual(ops.show_card(ref_broken)["metadata"][model.META_RETRY_SAME], "1")
        self.assertEqual(self._column(ref_other), "Blocked")   # the other card's own watchdog still ran
        self.assertTrue(any(r.get("event") == "advance" and r.get("result") == "teardown-failed"
                            for r in self._runs()))

    def test_watchdog_full_cycle_same_then_switch_then_blocked(self):
        reg = heads.Registry(
            resources={"res-a": {"probe": "true"}, "res-b": {"probe": "true"}},
            profiles={
                "primary": {"resource": "res-a", "adapter": "claude", "fallback": ["secondary"]},
                "secondary": {"resource": "res-b", "adapter": "claude", "fallback": []},
            },
        )
        with mock.patch.object(heads, "load_registry", return_value=reg):
            ref = self._claim_one(head="primary")
            dispatcher.WATCHDOG_SECONDS = -1
            self.worker.activity_ts = None
            dispatcher.tick()   # same-head retry: primary -> primary again
            self.assertEqual(self._column(ref), model.IN_PROGRESS)
            self.assertEqual(self.worker.launched[-1]["head"], "primary")
            self.assertEqual(ops.show_card(ref)["metadata"][model.META_RETRY_SAME], "1")

            dispatcher.tick()   # same-head budget spent -> switch: primary -> secondary
            self.assertEqual(self._column(ref), model.IN_PROGRESS)
            self.assertEqual(self.worker.launched[-1]["head"], "secondary")
            meta = ops.show_card(ref)["metadata"]
            self.assertEqual(meta[model.META_RETRY_SWITCH], "1")
            self.assertEqual(meta[model.META_HEAD], "secondary")
            self.assertEqual(meta[model.META_RETRY_HEADS], "primary,secondary")

            dispatcher.tick()   # both budgets spent -> terminal Blocked, workspace kept for a human
            self.assertEqual(self._column(ref), "Blocked")
            # exactly the first two bring-ups were torn down (same-head retry, then switch retry);
            # the third (the one left In progress -> Blocked) is kept alive for a human to inspect.
            self.assertEqual(len(self.worker.torn_down), 2)
        posted = " ".join(c["comment"] for cl in self.board.comments.values() for c in cl)
        self.assertIn("бюджет авторетраев исчерпан", posted)
        self.assertIn("primary, secondary", posted)
        self.assertTrue(any(r.get("reason") == "watchdog" and r.get("reference") == ref
                            for r in self._runs()))

    def test_watchdog_switch_waits_without_burning_budget_when_chain_is_red(self):
        # Same-head retry already spent; the one switch candidate is red right now. The card must
        # keep cycling for free (teardown -> Ready -> reclaim on the still-green primary) rather
        # than either burning the switch budget or going Blocked.
        reg = heads.Registry(
            resources={"res-a": {"probe": "true"}, "res-b": {"probe": "true"}},
            profiles={
                "primary": {"resource": "res-a", "adapter": "claude", "fallback": ["secondary"]},
                "secondary": {"resource": "res-b", "adapter": "claude", "fallback": []},
            },
        )
        self.statuses = {"res-a": "green", "res-b": "red"}
        with mock.patch.object(heads, "load_registry", return_value=reg):
            ref = self._claim_one(head="primary")
            dispatcher.WATCHDOG_SECONDS = -1
            self.worker.activity_ts = None
            dispatcher.tick()   # same-head retry
            self.assertEqual(ops.show_card(ref)["metadata"][model.META_RETRY_SAME], "1")

            dispatcher.tick()   # switch candidate (secondary) is red -> wait, no budget spent
            self.assertEqual(self._column(ref), model.IN_PROGRESS)  # reclaimed on primary again
            self.assertEqual(self.worker.launched[-1]["head"], "primary")
            meta = ops.show_card(ref)["metadata"]
            self.assertEqual(meta[model.META_RETRY_SWITCH], "0")
            self.assertEqual(meta[model.META_HEAD], "primary")

            dispatcher.tick()   # still red -> still waiting, still not Blocked, still no budget burn
            self.assertEqual(self._column(ref), model.IN_PROGRESS)
            self.assertEqual(ops.show_card(ref)["metadata"][model.META_RETRY_SWITCH], "0")

            self.statuses["res-b"] = "green"    # resource recovers
            dispatcher.tick()   # now the switch actually happens
            self.assertEqual(self.worker.launched[-1]["head"], "secondary")
            self.assertEqual(ops.show_card(ref)["metadata"][model.META_RETRY_SWITCH], "1")

    def test_watchdog_switch_goes_straight_to_blocked_when_chain_has_no_candidate_at_all(self):
        # claude-opus (real registry) has no fallback at all: once the same-head retry is spent,
        # there is nothing to wait for, so the switch step must not stall forever — straight to
        # Blocked instead of an infinite free-retry loop.
        ref = self._claim_one(head="claude-opus")
        dispatcher.WATCHDOG_SECONDS = -1
        self.worker.activity_ts = None
        dispatcher.tick()   # same-head retry
        self.assertEqual(self._column(ref), model.IN_PROGRESS)
        dispatcher.tick()   # no fallback at all for claude-opus -> terminal Blocked
        self.assertEqual(self._column(ref), "Blocked")
        self.assertEqual(ops.show_card(ref)["metadata"][model.META_RETRY_SWITCH], "0")

    def test_retry_counters_survive_local_state_wipe(self):
        # A dispatcher redeploy only ever replaces the local cards.json — retry_same/retry_switch/
        # retry_heads live on the board (model.META_RETRY_*), so a card already one retry deep is
        # not treated as brand new after cards.json is lost and the card is re-adopted.
        reg = heads.Registry(
            resources={"res-a": {"probe": "true"}, "res-b": {"probe": "true"}},
            profiles={
                "primary": {"resource": "res-a", "adapter": "claude", "fallback": ["secondary"]},
                "secondary": {"resource": "res-b", "adapter": "claude", "fallback": []},
            },
        )
        with mock.patch.object(heads, "load_registry", return_value=reg):
            ref = self._claim_one(head="primary")
            dispatcher.WATCHDOG_SECONDS = -1
            self.worker.activity_ts = None
            dispatcher.tick()   # same-head retry: retry_same -> 1 on the board
            self.assertEqual(ops.show_card(ref)["metadata"][model.META_RETRY_SAME], "1")

            dispatcher._save_cards({})   # simulate the redeploy: local records gone
            self.assertEqual(dispatcher._load_cards(), {})
            self.worker.activity_ts = None
            # One tick both reconciles (fresh, head-less record) and fires the watchdog on it
            # (WATCHDOG_SECONDS=-1: any elapsed time trips it) — must read retry_same=1 off the
            # board, not a fresh 0, and go straight to switch instead of granting another same-head
            # retry the local state no longer remembers was already spent.
            dispatcher.tick()
        self.assertEqual(self.worker.launched[-1]["head"], "secondary")
        self.assertEqual(ops.show_card(ref)["metadata"][model.META_RETRY_SWITCH], "1")

    def test_env_technical_smoke_fail_is_never_retried(self):
        # Provision/smoke failure is env-technical, not head-technical: still a straight Blocked,
        # no retry budget, no retry_* metadata touched — the watchdog retry cycle must not blur
        # this line.
        self.worker.provision_ok = False
        self.worker.provision_log = "[provision] FAIL: smoke command failed (exit 1)\n"
        ref = self._ready_card("A")
        dispatcher.tick()
        self.assertEqual(self._column(ref), "Blocked")
        meta = ops.show_card(ref)["metadata"]
        self.assertFalse(meta.get(model.META_RETRY_SAME))
        self.assertFalse(meta.get(model.META_RETRY_SWITCH))

    def test_semantic_report_blocked_is_never_retried(self):
        # A worker's own [report:blocked] is semantic, not head-technical: straight to Blocked,
        # no watchdog retry involved at all.
        ref = self._claim_one()
        ops.report(ref, "blocked", "spec disagreement")
        dispatcher.tick()
        self.assertEqual(self._column(ref), "Blocked")
        meta = ops.show_card(ref)["metadata"]
        self.assertFalse(meta.get(model.META_RETRY_SAME))
        self.assertFalse(meta.get(model.META_RETRY_SWITCH))

    def test_human_recovery_from_blocked_resets_retry_budget(self):
        # po moving a Blocked card back to Ready is a fresh start: the retry budget already spent
        # in a prior life must not carry over and short-circuit the next watchdog cycle.
        reg = heads.Registry(
            resources={"res-a": {"probe": "true"}, "res-b": {"probe": "true"}},
            profiles={
                "primary": {"resource": "res-a", "adapter": "claude", "fallback": ["secondary"]},
                "secondary": {"resource": "res-b", "adapter": "claude", "fallback": []},
            },
        )
        with mock.patch.object(heads, "load_registry", return_value=reg):
            ref = self._claim_one(head="primary")
            dispatcher.WATCHDOG_SECONDS = -1
            self.worker.activity_ts = None
            dispatcher.tick()   # same-head retry -> retry_same=1
            self.assertEqual(ops.show_card(ref)["metadata"][model.META_RETRY_SAME], "1")
            dispatcher.tick()   # switch -> retry_switch=1
            self.assertEqual(ops.show_card(ref)["metadata"][model.META_RETRY_SWITCH], "1")
            dispatcher.tick()   # budgets spent -> terminal Blocked
            self.assertEqual(self._column(ref), "Blocked")

            ops.move_card("po", ref, "Ready")
            meta = ops.show_card(ref)["metadata"]
            self.assertFalse(meta.get(model.META_RETRY_SAME))
            self.assertFalse(meta.get(model.META_RETRY_SWITCH))
            self.assertFalse(meta.get(model.META_CLAIM))

            self.worker.activity_ts = None
            dispatcher.tick()   # re-claims; a fresh watchdog cycle gets the full budget again
            self.assertEqual(self._column(ref), model.IN_PROGRESS)
            dispatcher.tick()
            self.assertEqual(self._column(ref), model.IN_PROGRESS)   # same-head retry, not Blocked
            self.assertEqual(ops.show_card(ref)["metadata"][model.META_RETRY_SAME], "1")

    def test_watchdog_holds_when_worker_active(self):
        import time
        ref = self._claim_one()
        dispatcher.WATCHDOG_SECONDS = 600
        self.worker.activity_ts = time.time()   # fresh output within threshold
        dispatcher.tick()
        self.assertEqual(self._column(ref), model.IN_PROGRESS)

    def test_watchdog_uses_tracked_worker_handle_not_active_shell_in_workspace(self):
        ref = self._claim_one()
        rec = dispatcher._load_cards()[ref]
        tracked = rec["handle"]
        shell = "plain-shell"
        dispatcher.WATCHDOG_SECONDS = -1
        self.worker.activity_by_handle[tracked] = None
        self.worker.activity_by_handle[shell] = time.time()

        dispatcher.tick()

        self.assertIn((tracked, rec["workspace"]), self.worker.terminal_activity_polled)
        self.assertNotIn((shell, rec["workspace"]), self.worker.terminal_activity_polled)
        self.assertIn(rec["workspace"], self.worker.torn_down)
        self.assertEqual(ops.show_card(ref)["metadata"][model.META_RETRY_SAME], "1")

    def test_dead_worker_handle_retries_same_head_without_waiting_for_silence(self):
        ref = self._claim_one()
        rec = dispatcher._load_cards()[ref]
        tracked = rec["handle"]
        dispatcher.WATCHDOG_SECONDS = 3600
        self.worker.terminal_entries[tracked] = {
            "handle": tracked,
            "connected": True,
            "writable": False,
            "preview": "still looks busy",
        }

        dispatcher.tick()

        self.assertEqual(self._column(ref), model.IN_PROGRESS)
        self.assertIn(rec["workspace"], self.worker.torn_down)
        meta = ops.show_card(ref)["metadata"]
        self.assertEqual(meta[model.META_RETRY_SAME], "1")
        self.assertTrue(any(r.get("event") == "advance"
                            and r.get("reason") == "watchdog-retry-same"
                            and r.get("trigger") == dispatcher.WATCHDOG_TRIGGER_DEAD_HANDLE
                            and r.get("handle_status") == "unwritable"
                            for r in self._runs()))

    def test_missing_worker_handle_retries_same_head_without_waiting_for_silence(self):
        ref = self._claim_one()
        records = dispatcher._load_cards()
        rec = records[ref]
        rec["handle"] = ""
        rec["last_activity"] = time.time()
        dispatcher._save_cards(records)
        dispatcher.WATCHDOG_SECONDS = 3600

        dispatcher.tick()

        self.assertIn(("", rec["workspace"]), self.worker.terminal_status_polled)
        self.assertEqual(self._column(ref), model.IN_PROGRESS)
        self.assertIn(rec["workspace"], self.worker.torn_down)
        meta = ops.show_card(ref)["metadata"]
        self.assertEqual(meta[model.META_RETRY_SAME], "1")
        self.assertTrue(any(r.get("event") == "advance"
                            and r.get("reason") == "watchdog-retry-same"
                            and r.get("trigger") == dispatcher.WATCHDOG_TRIGGER_DEAD_HANDLE
                            and r.get("handle_status") == "missing-handle"
                            and r.get("handle") == ""
                            for r in self._runs()))

    def test_dead_worker_handle_follows_same_switch_then_blocked_budget(self):
        reg = heads.Registry(
            resources={"res-a": {"probe": "true"}, "res-b": {"probe": "true"}},
            profiles={
                "primary": {"resource": "res-a", "adapter": "claude", "fallback": ["secondary"]},
                "secondary": {"resource": "res-b", "adapter": "claude", "fallback": []},
            },
        )
        self.statuses = {"res-a": "green", "res-b": "green"}
        dispatcher.WATCHDOG_SECONDS = 3600
        with mock.patch.object(heads, "load_registry", return_value=reg):
            ref = self._claim_one(head="primary")
            first_handle = dispatcher._load_cards()[ref]["handle"]
            self.worker.dead_handles.add(first_handle)

            dispatcher.tick()
            self.assertEqual(self._column(ref), model.IN_PROGRESS)
            self.assertEqual(self.worker.launched[-1]["head"], "primary")
            self.assertEqual(ops.show_card(ref)["metadata"][model.META_RETRY_SAME], "1")

            dispatcher.tick()
            self.assertEqual(self._column(ref), model.IN_PROGRESS)
            self.assertEqual(self.worker.launched[-1]["head"], "secondary")
            meta = ops.show_card(ref)["metadata"]
            self.assertEqual(meta[model.META_RETRY_SWITCH], "1")
            self.assertEqual(meta[model.META_HEAD], "secondary")

            dispatcher.tick()
            self.assertEqual(self._column(ref), "Blocked")
            self.assertNotIn(ref, dispatcher._load_cards())
        self.assertTrue(any(r.get("event") == "advance"
                            and r.get("reason") == "watchdog"
                            and r.get("trigger") == dispatcher.WATCHDOG_TRIGGER_DEAD_HANDLE
                            and r.get("handle_status") == "missing-terminal"
                            for r in self._runs()))

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

    def test_launch_worker_inject_delivery_failure_is_explicit(self):
        ref = self._ready_card("A")
        self.worker.launch_raises = worker.InjectDeliveryError("inject не доставлен: TASK.md")

        dispatcher.tick()

        self.assertEqual(self._column(ref), "Blocked")
        self.assertNotIn(ref, dispatcher._load_cards())
        posted = " ".join(c["comment"] for cl in self.board.comments.values() for c in cl)
        self.assertIn("inject не доставлен", posted)
        self.assertTrue(any(r.get("event") == "bringup"
                            and r.get("reason") == "inject-delivery"
                            and r.get("reference") == ref
                            for r in self._runs()))

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
        self.assertEqual(records[ref]["worker"], "1-a")
        self.assertIn("/ws/w-crash", self.worker.torn_down)
        self.assertEqual(ops.show_card(ref)["metadata"][model.META_RETRY_SAME], "1")
        self.assertTrue(any(r["event"] == "reconcile" for r in self._runs()))
        # From here the normal machinery applies: a report moves it to Validate.
        ops.report(ref, "done", "shipped after crash")
        dispatcher.tick()
        self.assertEqual(self._column(ref), "Validate")

    def test_reconciled_card_hits_watchdog_on_silence(self):
        ref = self._ready_card("A")
        ops.claim_card(ref, "w-crash")
        dispatcher.WATCHDOG_SECONDS = 3600
        dispatcher.tick()
        self.assertEqual(self._column(ref), model.IN_PROGRESS)
        self.assertIn("/ws/w-crash", self.worker.torn_down)
        self.assertEqual(ops.show_card(ref)["metadata"][model.META_RETRY_SAME], "1")
        self.assertTrue(any(r.get("event") == "advance"
                            and r.get("reason") == "watchdog-retry-same"
                            and r.get("trigger") == dispatcher.WATCHDOG_TRIGGER_DEAD_HANDLE
                            and r.get("handle_status") == "missing-handle"
                            for r in self._runs()))

    def test_reconcile_restores_workspace_from_claim(self):
        # The claim IS the card's workspace base name (naming._worker_id) — reconcile must rebuild
        # the full path from it, not leave "workspace" empty and blind the activity watchdog.
        ref = self._ready_card("A")
        ops.claim_card(ref, "w-crash")
        dispatcher.tick()
        self.assertIn(("", "/ws/w-crash"), self.worker.terminal_status_polled)
        self.assertIn("/ws/w-crash", self.worker.torn_down)

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

    def test_reconcile_never_adopts_the_stewards_own_report_card(self):
        """triggered-agents-255: create_report_card puts its card straight into In progress with
        a claim already set — without the steward_report skip, _reconcile would adopt it as a lost
        worker record, poll a workspace that never existed, and eventually watchdog-requeue it to
        Ready with its claim cleared (letting a real worker get claimed against it)."""
        ref = ops.create_report_card("triggered-agents", "steward: hourly sweep",
                                     "steward-sweep-1")["reference"]
        dispatcher.tick()
        self.assertNotIn(ref, dispatcher._load_cards())
        self.assertEqual(self._column(ref), model.IN_PROGRESS)  # untouched by reconcile
        self.assertFalse(any(r["event"] == "reconcile" and r.get("reference") == ref
                             for r in self._runs()))
        # a real code card for the same project claims fine regardless of the report card
        code_ref = self._ready_card("B", project="triggered-agents")
        dispatcher.tick()
        self.assertEqual(self._column(code_ref), model.IN_PROGRESS)

    def test_bringup_saves_records_before_tick_ends(self):
        # _save_cards runs inside _bring_up: a crash right after the head is up must still
        # find the record on disk. Simulate by crashing the tick right after _claim_next.
        ref = self._ready_card("A")
        orig = dispatcher._claim_next

        def claim_then_die(records, statuses):
            orig(records, statuses)
            raise KeyboardInterrupt("kill right after bring-up")

        with mock.patch.object(dispatcher, "_claim_next", claim_then_die):
            with self.assertRaises(KeyboardInterrupt):
                dispatcher.tick()
        self.assertIn(ref, dispatcher._load_cards())

    # tick lock: flock on dispatch.lock itself, held for the whole tick (triggered-agents-236) ---
    def test_dead_holder_lock_released_by_kernel(self):
        # No more pid-liveness bookkeeping: a process that dies while holding the flock has the
        # kernel drop it immediately, so the very next tick just acquires it like any free lock —
        # there is nothing left to reclaim.
        lockfile = dispatcher.STATE.dir / "dispatch.lock"
        script = (
            "import fcntl\n"
            f"f = open({str(lockfile)!r}, 'w')\n"
            "fcntl.flock(f.fileno(), fcntl.LOCK_EX)\n"
            "f.write('99999')\n"
            "f.flush()\n"
        )
        subprocess.run([sys.executable, "-c", script], check=True)
        ref = self._ready_card("A")
        dispatcher.tick()  # must not SystemExit — the dead process's flock is already gone
        self.assertEqual(self._column(ref), model.IN_PROGRESS)

    def test_live_lock_skips_with_exit_zero(self):
        # A busy lock (a long stand run holding the tick while the timer fires again) is a skip,
        # not a failure: it must exit 0 so systemd doesn't log a run of unit failures. Simulated
        # with a real held flock, not just a pid written to the file — content alone means
        # nothing to the new lock now.
        lockfile = dispatcher.STATE.dir / "dispatch.lock"
        holder_fd = os.open(lockfile, os.O_CREAT | os.O_RDWR)
        fcntl.flock(holder_fd, fcntl.LOCK_EX)
        os.write(holder_fd, b"424242")
        try:
            with self.assertRaises(SystemExit) as cm:
                dispatcher.tick()
            self.assertEqual(cm.exception.code, 0)
        finally:
            fcntl.flock(holder_fd, fcntl.LOCK_UN)
            os.close(holder_fd)

    def test_concurrent_ticks_only_one_holds_lock(self):
        # Two ticks racing for dispatch.lock never both enter: the OS flock on the lock file
        # itself is the only serialization needed now, no companion mutex file.
        results = []
        results_lock = threading.Lock()
        first_entered = threading.Event()
        release = threading.Event()

        def contend():
            try:
                with dispatcher._tick_lock():
                    with results_lock:
                        results.append("entered")
                        first = len(results) == 1
                    if first:
                        first_entered.set()
                        release.wait(timeout=5)
            except SystemExit:
                with results_lock:
                    results.append("skipped")

        t1 = threading.Thread(target=contend)
        t1.start()
        self.assertTrue(first_entered.wait(timeout=5))
        t2 = threading.Thread(target=contend)
        t2.start()
        t2.join(timeout=5)
        release.set()
        t1.join(timeout=5)

        self.assertEqual(sorted(results), ["entered", "skipped"])

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

    # TASK.md carries the shared memory block, scoped to the card's own project -------------
    def test_task_md_carries_memory_block(self):
        ref = self._ready_card("A", project="personal_site")
        dispatcher.tick()
        (_, content), = self.worker.tasks_written
        self.assertIn("memory_search", content)
        self.assertIn('scope="project:personal_site"', content)
        self.assertIn('caller="worker"', content)

    # criterion 2: no direct Kanboard API from the dispatcher ----------------
    def test_dispatcher_does_not_touch_kanboard_directly(self):
        src = Path(dispatcher.__file__).read_text()
        self.assertNotIn("kanboard", src)
        self.assertNotIn("from ..board", src)


class LeftInProgressAnotherWayTest(_DispatcherBase):
    """2026-07-04 review, triggered-agents-244 blocker B2: a card that leaves In progress by a
    path other than the worker's own report (steward's escalation move, or a human) must not
    leave its worker head running unsupervised on a card that is no longer its concern — but the
    workspace itself stays on disk, same as every other Blocked path (no full teardown)."""

    def test_stops_the_terminal_but_does_not_remove_the_workspace(self):
        ref = self._claim_one("A")
        ws = self.worker.launched[-1]["ws"]
        ops.move_card("steward", ref, "Blocked",
                      reason="test setup: escalation happens outside a dispatcher tick")
        dispatcher.tick()
        self.assertIn(ws, self.worker.stopped_terminals)
        self.assertNotIn(ws, self.worker.torn_down)
        self.assertTrue(any(r.get("event") == "advance"
                            and r.get("result") == "left-in-progress-another-way"
                            and r.get("reference") == ref for r in self._runs()))

    def test_a_worker_self_report_blocked_is_unaffected(self):
        """The pre-existing intentional-preservation path (worker's own report:blocked) must
        still never call stop_terminals — it is a different branch entirely."""
        ref = self._claim_one("A")
        ws = self.worker.launched[-1]["ws"]
        ops.report(ref, "blocked", "не смог собрать")
        dispatcher.tick()
        self.assertEqual(self._column(ref), "Blocked")
        self.assertEqual(self.worker.stopped_terminals, [])
        self.assertEqual(self.worker.torn_down, [])

    def test_also_covers_a_card_that_left_validate(self):
        ref = self._claim_one("A")
        ops.report(ref, "done", "shipped")
        dispatcher.tick()   # -> Validate, record kept
        ws = self.worker.launched[-1]["ws"]
        self.assertEqual(self._column(ref), "Validate")
        ops.move_card("steward", ref, "Blocked",
                      reason="test setup: escalation happens outside a dispatcher tick")
        dispatcher.tick()
        self.assertIn(ws, self.worker.stopped_terminals)
        self.assertNotIn(ws, self.worker.torn_down)


class HeadHealthTest(_DispatcherBase):
    """Per-resource health at claim time (fallback selection, full-chain skip) and watchdog
    freeze/unfreeze for a head sitting on a red resource. self.statuses (from _DispatcherBase)
    starts all-green; each test reddens exactly the resource(s) it needs."""

    def _two_resource_registry(self):
        # primary/secondary sit on genuinely different resources, mirroring the real registry's
        # claude-sub/openrouter split — proves fallback selection isn't hardcoded to one resource.
        return heads.Registry(
            resources={"res-a": {"probe": "true"}, "res-b": {"probe": "true"}},
            profiles={
                "primary": {"resource": "res-a", "adapter": "claude", "fallback": ["secondary"]},
                "secondary": {"resource": "res-b", "adapter": "claude", "fallback": []},
            },
        )

    # claim-time fallback -----------------------------------------------------
    def test_claim_uses_preferred_head_when_its_resource_is_green(self):
        reg = self._two_resource_registry()
        with mock.patch.object(heads, "load_registry", return_value=reg):
            ref = self._ready_card("A", head="primary")
            dispatcher.tick()
        self.assertEqual(self._column(ref), model.IN_PROGRESS)
        self.assertEqual(self.worker.launched[0]["head"], "primary")

    def test_claim_falls_back_to_first_green_profile_in_chain(self):
        reg = self._two_resource_registry()
        self.statuses = {"res-a": "red", "res-b": "green"}
        with mock.patch.object(heads, "load_registry", return_value=reg):
            ref = self._ready_card("A", head="primary")
            dispatcher.tick()
        self.assertEqual(self._column(ref), model.IN_PROGRESS)
        self.assertEqual(self.worker.launched[0]["head"], "secondary")
        # the card's own metadata preference is untouched — once res-a is green again, a fresh
        # claim of a card asking for "primary" goes back to using it.
        self.assertEqual(ops.show_card(ref)["metadata"][model.META_HEAD], "primary")

    def test_claim_skips_card_when_whole_fallback_chain_is_red(self):
        reg = self._two_resource_registry()
        self.statuses = {"res-a": "red", "res-b": "red"}
        with mock.patch.object(heads, "load_registry", return_value=reg):
            ref = self._ready_card("A", head="primary")
            dispatcher.tick()
        self.assertEqual(self._column(ref), "Ready")   # not Blocked — waits for a resource to recover
        self.assertEqual(self.worker.launched, [])
        runs = [r for r in self._runs() if r["event"] == "claim-skip"]
        self.assertTrue(any(r.get("reference") == ref and "red" in r.get("reason", "") for r in runs))

    def test_unknown_head_still_reports_its_own_guard_error_not_a_red_resource(self):
        # An unknown/stale profile must not be misreported as "its resources are red" — the
        # claim-skip reason has to name the actual problem, same as ops.claim_card always did.
        ref = self._ready_card("A", head="codex-nope")
        dispatcher.tick()
        self.assertEqual(self._column(ref), "Ready")
        runs = [r for r in self._runs() if r["event"] == "claim-skip" and r.get("reference") == ref]
        self.assertTrue(runs)
        self.assertIn("codex-nope", runs[0]["reason"])
        self.assertNotIn("red", runs[0]["reason"])

    def test_red_head_does_not_block_a_green_card_behind_it(self):
        # Two Ready cards, first one's whole chain red: claim must skip it and still claim the
        # second (mirrors test_claim_orders_by_position_and_skips_blocked_by), whose head sits on
        # an unrelated, green resource.
        reg = self._two_resource_registry()
        reg.profiles["other"] = {"resource": "res-c", "adapter": "claude", "fallback": []}
        reg.resources["res-c"] = {"probe": "true"}
        self.statuses = {"res-a": "red", "res-b": "red", "res-c": "green"}
        with mock.patch.object(heads, "load_registry", return_value=reg):
            ref_a = self._ready_card("A", head="primary")
            ref_b = self._ready_card("B", head="other")
            for t in self.board.tasks.values():
                if t["reference"] == ref_a:
                    t["position"] = 1
                elif t["reference"] == ref_b:
                    t["position"] = 2
            dispatcher.tick()
        self.assertEqual(self._column(ref_a), "Ready")
        self.assertEqual(self._column(ref_b), model.IN_PROGRESS)

    # watchdog freeze -----------------------------------------------------------
    def test_watchdog_frozen_while_head_resource_is_red(self):
        ref = self._claim_one(head="claude-opus")   # real registry: resource claude-sub
        dispatcher.WATCHDOG_SECONDS = -1
        self.worker.activity_ts = None
        self.statuses["claude-sub"] = "red"
        dispatcher.tick()
        self.assertEqual(self._column(ref), model.IN_PROGRESS)   # frozen, not Blocked
        self.assertEqual(self.worker.torn_down, [])

    def test_dead_worker_handle_frozen_while_head_resource_is_red(self):
        ref = self._claim_one(head="claude-opus")   # real registry: resource claude-sub
        rec = dispatcher._load_cards()[ref]
        ops.set_retry_state(ref, retry_same=1, retry_switch=1, retry_heads="claude-opus")
        self.worker.dead_handles.add(rec["handle"])
        self.statuses["claude-sub"] = "red"
        before = rec["last_activity"]

        dispatcher.tick()

        rec_after = dispatcher._load_cards()[ref]
        self.assertEqual(self._column(ref), model.IN_PROGRESS)
        self.assertEqual(self.worker.torn_down, [])
        self.assertEqual(ops.show_card(ref)["metadata"][model.META_RETRY_SAME], "1")
        self.assertGreaterEqual(rec_after["last_activity"], before)
        self.assertFalse(any(r.get("reference") == ref and r.get("reason") == "watchdog"
                             for r in self._runs()))

        self.statuses["claude-sub"] = "green"
        dispatcher.tick()
        self.assertEqual(self._column(ref), "Blocked")
        self.assertTrue(any(r.get("reference") == ref and r.get("reason") == "watchdog"
                            and r.get("trigger") == dispatcher.WATCHDOG_TRIGGER_DEAD_HANDLE
                            for r in self._runs()))

    def test_watchdog_resumes_once_resource_goes_green_again(self):
        ref = self._claim_one(head="claude-opus")
        dispatcher.WATCHDOG_SECONDS = -1
        self.worker.activity_ts = None
        self.statuses["claude-sub"] = "red"
        dispatcher.tick()
        self.assertEqual(self._column(ref), model.IN_PROGRESS)   # still frozen
        self.statuses["claude-sub"] = "green"
        dispatcher.tick()   # clock resumes counting from "now" (the last frozen tick), still
                            # silent -> the watchdog actually fires (live budget: same-head retry)
        self.assertEqual(self._column(ref), model.IN_PROGRESS)   # reclaimed, not Blocked
        self.assertEqual(ops.show_card(ref)["metadata"][model.META_RETRY_SAME], "1")
        runs = [r for r in self._runs()
               if r["event"] == "advance" and r.get("reason") == "watchdog-retry-same"]
        self.assertTrue(any(r.get("reference") == ref for r in runs))

    def test_unfrozen_watchdog_still_holds_when_worker_is_actually_active(self):
        ref = self._claim_one(head="claude-opus")
        self.statuses["claude-sub"] = "red"
        dispatcher.WATCHDOG_SECONDS = 600
        dispatcher.tick()   # frozen tick
        self.statuses["claude-sub"] = "green"
        self.worker.activity_ts = time.time()   # fresh output once the resource recovers
        dispatcher.tick()
        self.assertEqual(self._column(ref), model.IN_PROGRESS)


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

    def test_task_md_says_pipeline_pause_is_not_a_card_blocker(self):
        self._ready_card("A")
        dispatcher.tick()
        (_, content), = self.worker.tasks_written
        self.assertIn("Пауза пайплайна", content)
        self.assertIn("Не репорти `blocked` только из-за паузы", content)
        self.assertIn("после `resume` продолжай ту же карточку", content)

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

    # контриб-карточка: report:done несёт ветку/head sha вместо ссылки на PR --------------------
    def test_contrib_project_task_md_carries_branch_head_protocol_not_pr(self):
        self.worker.contrib_projects = {"agent-kanban"}
        ref = self._ready_card("A", project="agent-kanban")
        dispatcher.tick()
        (_, content), = self.worker.tasks_written
        self.assertIn(f"branch: pipeline/{ref}", content)
        self.assertIn("head:", content)
        self.assertNotIn("PR открыт", content)
        self.assertNotIn("ссылка на PR", content)

    def test_metadata_section_has_type_head_slug_blocked_by(self):
        pred = self._ready_card("Pred")
        pred_tid = next(t["id"] for t in self.board.tasks.values() if t["reference"] == pred)
        self.board.tasks[pred_tid]["is_active"] = 0  # counts as Done for the blocked_by guard
        self._ready_card("B", head="claude-opus",
                         meta={model.META_SLUG: "my-slug", model.META_BLOCKED_BY: pred})
        dispatcher.tick()
        (_, content), = self.worker.tasks_written
        self.assertIn("тип: code", content)
        self.assertIn("голова worker: claude-opus", content)
        self.assertIn("голова reviewer: codex-reviewer", content)
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
        _, worker_id, _, spawned_title, pr_branch, review_branch, head_sha = self.worker.reviewer_spawns[0]
        self.assertEqual(spawned_title, expect_title)
        self.assertTrue(worker_id.startswith(f"review-{cid}-rev-slug"))
        self.assertEqual(pr_branch, naming.worker_branch(ref))
        self.assertEqual(review_branch, naming.reviewer_branch(ref))
        self.assertIsNone(head_sha)   # a regular PR card never pins a sha (out of scope)
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

    def _to_validate(self, pr=PR, report="готово", head=None):
        """Claim a card, have the worker report done (with a PR link), land it in Validate with
        its record intact. poll_pr returns None for this landing tick so the card stays put."""
        ref = self._claim_one(head=head)
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

    def test_red_ci_relaunches_shell_prompt_handle(self):
        self.worker.unique_launch_handles = True
        ref = self._to_validate()
        rec_before = dispatcher._load_cards()[ref]
        old_handle = rec_before["handle"]
        launched_before = len(self.worker.launched)
        tasks_before = len(self.worker.tasks_written)
        self.worker.terminal_entries[old_handle] = {
            "handle": old_handle,
            "connected": True,
            "writable": True,
            "preview": "dev@host:~/orca/workspaces/triggered-agents/card$",
        }
        self.worker.pr_status = {
            "merged": False, "state": "OPEN", "rollup": "FAILURE",
            "failed_job": "CI", "failed_log": "boom",
        }

        dispatcher.tick()

        self.assertEqual(self._column(ref), model.IN_PROGRESS)
        rec_after = dispatcher._load_cards()[ref]
        self.assertNotEqual(rec_after["handle"], old_handle)
        self.assertEqual(len(self.worker.launched), launched_before + 1)
        self.assertEqual(len(self.worker.tasks_written), tasks_before + 1)
        self.assertEqual(self.worker.notified[-1][0], rec_after["handle"])
        self.assertNotEqual(self.worker.notified[-1][0], old_handle)
        self.assertIn("CI красный", self.worker.tasks_written[-1][1])

    def test_red_ci_relaunches_codex_tui_non_agent_handle(self):
        self.worker.unique_launch_handles = True
        ref = self._to_validate(head="codex-tui")
        rec_before = dispatcher._load_cards()[ref]
        old_handle = rec_before["handle"]
        self.assertEqual(rec_before["terminal_kind"], "codex-tui")
        self.worker.terminal_entries[old_handle] = {
            "handle": old_handle,
            "connected": True,
            "writable": True,
            "preview": "python3 -m unittest discover",
        }
        self.worker.pr_status = {
            "merged": False, "state": "OPEN", "rollup": "FAILURE",
            "failed_job": "CI", "failed_log": "boom",
        }

        dispatcher.tick()

        rec_after = dispatcher._load_cards()[ref]
        self.assertEqual(rec_after["terminal_kind"], "codex-tui")
        self.assertNotEqual(rec_after["handle"], old_handle)
        self.assertEqual(self.worker.notified[-1][0], rec_after["handle"])
        self.assertNotIn(old_handle, [handle for handle, _ in self.worker.notified])

    def test_red_ci_relaunches_codex_tui_codex_help_shell_handle(self):
        self.worker.unique_launch_handles = True
        ref = self._to_validate(head="codex-tui")
        rec_before = dispatcher._load_cards()[ref]
        old_handle = rec_before["handle"]
        self.assertEqual(rec_before["terminal_kind"], "codex-tui")
        self.worker.terminal_entries[old_handle] = {
            "handle": old_handle,
            "connected": True,
            "writable": True,
            "preview": "watch 'codex --help'\nOpenAI Codex",
        }
        self.worker.pr_status = {
            "merged": False, "state": "OPEN", "rollup": "FAILURE",
            "failed_job": "CI", "failed_log": "boom",
        }

        dispatcher.tick()

        rec_after = dispatcher._load_cards()[ref]
        self.assertEqual(rec_after["terminal_kind"], "codex-tui")
        self.assertNotEqual(rec_after["handle"], old_handle)
        self.assertEqual(self.worker.notified[-1][0], rec_after["handle"])
        self.assertNotIn(old_handle, [handle for handle, _ in self.worker.notified])

    def test_red_ci_move_failure_does_not_nudge_live_worker(self):
        ref = self._to_validate()
        rec_before = dispatcher._load_cards()[ref]
        launched_before = len(self.worker.launched)
        notified_before = len(self.worker.notified)
        self.board.move_fails = True
        self.worker.pr_status = {
            "merged": False, "state": "OPEN", "rollup": "FAILURE",
            "failed_job": "CI", "failed_log": "boom",
        }

        dispatcher.tick()

        self.assertEqual(self._column(ref), "Validate")
        rec_after = dispatcher._load_cards()[ref]
        self.assertEqual(rec_after["handle"], rec_before["handle"])
        self.assertEqual(rec_after.get("comment_baseline"), rec_before.get("comment_baseline"))
        self.assertEqual(len(self.worker.launched), launched_before)
        self.assertEqual(len(self.worker.notified), notified_before)
        self.assertFalse(any(r.get("reason") == "ci-red" for r in self._runs()))

    def test_red_ci_relaunch_failure_blocks_dead_worker_after_move(self):
        ref = self._to_validate()
        rec_before = dispatcher._load_cards()[ref]
        old_handle = rec_before["handle"]
        launched_before = len(self.worker.launched)
        tasks_before = len(self.worker.tasks_written)
        notified_before = len(self.worker.notified)
        self.worker.dead_handles.add(old_handle)
        self.worker.pr_status = {
            "merged": False, "state": "OPEN", "rollup": "FAILURE",
            "failed_job": "CI", "failed_log": "boom",
        }

        with mock.patch.object(worker, "launch_worker",
                               side_effect=RuntimeError("orca create failed")):
            dispatcher.tick()

        self.assertEqual(self._column(ref), "Blocked")
        self.assertNotIn(ref, dispatcher._load_cards())
        self.assertEqual(len(self.worker.launched), launched_before)
        self.assertEqual(len(self.worker.tasks_written), tasks_before + 1)
        self.assertEqual(len(self.worker.notified), notified_before)
        self.assertIn(rec_before["workspace"], self.worker.stopped_terminals)
        journal = self._markers(ref)
        self.assertIn("CI красный", journal)
        self.assertIn("orca create failed", journal)
        self.assertTrue(any(r["event"] == "rework-worker" and r.get("to") == "Blocked"
                            and r.get("reason") == "ci-red" for r in self._runs()))
        self.assertFalse(any(r["event"] == "validate" and r.get("to") == model.IN_PROGRESS
                             and r.get("reason") == "ci-red" for r in self._runs()))

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

    def test_ci_pending_starts_stall_clock_without_escalating(self):
        # gh answers fine (unlike gh-unavailable above) but CI hasn't reached a terminal rollup —
        # the first such tick just starts the clock, no escalation.
        ref = self._to_validate()
        self.worker.pr_status = {"merged": False, "state": "OPEN", "rollup": "PENDING",
                                 "failed_job": None, "failed_log": None}
        dispatcher.tick()
        self.assertEqual(self._column(ref), "Validate")
        self.assertIn("ci_pending_since", dispatcher._load_cards()[ref])
        self.assertTrue(any(r["event"] == "validate" and r.get("result") == "ci-pending"
                            for r in self._runs()))

    def test_ci_none_without_manifest_flag_stays_in_validate(self):
        # NONE (no checks configured at all) is the same non-terminal case as PENDING; several
        # ticks under the (generous default) budget must not touch the card.
        ref = self._to_validate()
        self.worker.pr_status = {"merged": False, "state": "OPEN", "rollup": "NONE",
                                 "failed_job": None, "failed_log": None}
        for _ in range(3):
            dispatcher.tick()
        self.assertEqual(self._column(ref), "Validate")
        self.assertIn(ref, dispatcher._load_cards())

    def test_ci_none_with_manifest_flag_reaches_review(self):
        self.worker.no_ci_projects.add("personal_site")
        ref = self._to_validate()
        self.worker.pr_status = {"merged": False, "state": "OPEN", "rollup": "NONE",
                                 "failed_job": None, "failed_log": None}

        dispatcher.tick()

        self.assertEqual(self._column(ref), "Validate")
        rec = dispatcher._load_cards()[ref]
        self.assertNotIn("ci_pending_since", rec)
        self.assertNotIn("validate_stall_fails", rec)
        self.assertIn("review_ws", rec)
        self.assertEqual(len(self.worker.reviewer_spawns), 1)
        journal = self._markers(ref)
        self.assertIn("CI не ожидается", journal)
        self.assertTrue(any(r["event"] == "validate" and r.get("result") == "ci-none-declared"
                            for r in self._runs()))

    def test_no_ci_project_pending_rollup_still_waits(self):
        self.worker.no_ci_projects.add("personal_site")
        ref = self._to_validate()
        self.worker.pr_status = {"merged": False, "state": "OPEN", "rollup": "PENDING",
                                 "failed_job": None, "failed_log": None}

        dispatcher.tick()

        self.assertEqual(self._column(ref), "Validate")
        self.assertIn("ci_pending_since", dispatcher._load_cards()[ref])
        self.assertEqual(self.worker.reviewer_spawns, [])

    def test_no_ci_project_red_ci_still_returns_to_in_progress(self):
        self.worker.no_ci_projects.add("personal_site")
        ref = self._to_validate()
        self.worker.pr_status = {"merged": False, "state": "OPEN", "rollup": "FAILURE",
                                 "failed_job": "CI", "failed_log": "boom"}

        dispatcher.tick()

        self.assertEqual(self._column(ref), model.IN_PROGRESS)
        self.assertIn("CI красный", self._markers(ref))

    def test_ci_pending_escalates_once_past_stall_budget(self):
        ref = self._to_validate()
        self.worker.pr_status = {"merged": False, "state": "OPEN", "rollup": "PENDING",
                                 "failed_job": None, "failed_log": None}
        dispatcher.tick()                                        # starts the clock
        validate.CI_PENDING_STALL_SECONDS = -1                   # any elapsed time counts as over
        dispatcher.tick()
        self.assertEqual(self._column(ref), "Blocked")
        self.assertNotIn(ref, dispatcher._load_cards())
        self.assertIn("висит в статусе PENDING", self._markers(ref))
        stalls = [r for r in self._runs()
                  if r["event"] == "validate" and r.get("reason") == "ci-pending-stall"]
        self.assertEqual(len(stalls), 1)                          # escalation logged exactly once
        dispatcher.tick()                                        # card left Validate -> no polling
        self.assertEqual(self._column(ref), "Blocked")

    def test_ci_red_after_pending_resets_the_stall_clock(self):
        # A red CI result is a fresh code state (the card returns to In progress for rework) — the
        # pending clock from the previous, stuck attempt must not carry over to the next one.
        ref = self._to_validate()
        self.worker.pr_status = {"merged": False, "state": "OPEN", "rollup": "PENDING",
                                 "failed_job": None, "failed_log": None}
        dispatcher.tick()
        self.assertIn("ci_pending_since", dispatcher._load_cards()[ref])
        self.worker.pr_status = {"merged": False, "state": "OPEN", "rollup": "FAILURE",
                                 "failed_job": "CI", "failed_log": "boom"}
        dispatcher.tick()
        self.assertEqual(self._column(ref), model.IN_PROGRESS)
        self.assertNotIn("ci_pending_since", dispatcher._load_cards()[ref])

    def test_ci_success_resets_the_pending_clock(self):
        # Regression: SUCCESS is a terminal rollup too (symmetric to FAILURE above). A card that
        # sat on PENDING for a while, then went green, must not carry that stale clock into a LATER
        # PENDING spell (a post-report push, or a human re-running the workflow) — that would burn
        # the fresh restart's own budget with old, already-green elapsed time.
        ref = self._to_validate()
        self.worker.pr_status = {"merged": False, "state": "OPEN", "rollup": "PENDING",
                                 "failed_job": None, "failed_log": None}
        dispatcher.tick()
        self.assertIn("ci_pending_since", dispatcher._load_cards()[ref])
        self.worker.pr_status = {"merged": False, "state": "OPEN", "rollup": "SUCCESS",
                                 "failed_job": None, "failed_log": None}
        dispatcher.tick()
        self.assertEqual(self._column(ref), "Validate")
        self.assertNotIn("ci_pending_since", dispatcher._load_cards()[ref])

    def test_escalation_tears_down_a_review_already_in_flight(self):
        # Regression: SUCCESS spawns the layer-3 reviewer; if CI then drops back to PENDING (a
        # post-report push, or a re-run of the workflow) and the pending watchdog later escalates,
        # the reviewer's throwaway worktree must be torn down same as _validate_stall does — not
        # leaked because the escalation only knew about the worker's own workspace.
        ref = self._to_validate()
        self.worker.pr_status = {"merged": False, "state": "OPEN", "rollup": "SUCCESS",
                                 "failed_job": None, "failed_log": None}
        dispatcher.tick()                                        # spawns the reviewer
        self.assertEqual(len(self.worker.reviewer_spawns), 1)
        self.assertIn("review_ws", dispatcher._load_cards()[ref])
        self.worker.pr_status = {"merged": False, "state": "OPEN", "rollup": "PENDING",
                                 "failed_job": None, "failed_log": None}
        dispatcher.tick()                                        # starts a fresh pending clock
        validate.CI_PENDING_STALL_SECONDS = -1
        dispatcher.tick()                                        # escalates -> must clear_review
        self.assertEqual(self._column(ref), "Blocked")
        self.assertTrue(any(w.startswith("/rev/") for w in self.worker.torn_down))


class PostMergeProvisionApplyTest(_DispatcherBase):
    """triggered-agents-256/276: a merged PR that touches deploy/provision.py, deploy/ta-gate.sh or
    an agent's own automation.toml gets its live systemd artifacts re-provisioned right away,
    one-shot, without touching the (already Done) card on failure. Only the triggered-agents
    project itself has these paths, so every non-triggered-agents merge must never even call gh for
    the file list."""

    PR = "https://github.com/vladmesh/triggered-agents/pull/99"

    def _to_validate(self, project="triggered-agents", pr=PR):
        ref = self._claim_one(project=project)
        ops.report(ref, "done", f"готово\nPR: {pr}")
        self.worker.pr_status = None
        dispatcher.tick()
        self.assertEqual(self._column(ref), "Validate")
        return ref

    def _merge(self, ref):
        self.worker.pr_status = {"merged": True, "state": "MERGED", "rollup": "SUCCESS",
                                 "failed_job": None, "failed_log": None}
        dispatcher.tick()
        self.assertEqual(self._column(ref), "Done")

    def test_other_project_never_calls_gh_for_files(self):
        ref = self._to_validate(project="personal_site")
        self._merge(ref)
        self.assertEqual(self.worker.pr_files_calls, [])
        self.assertEqual(self.worker.apply_provision_calls, [])

    def test_unrelated_files_skip_apply(self):
        ref = self._to_validate()
        self.worker.pr_files_result = ["README.md", "tests/test_pipeline.py"]
        self._merge(ref)
        self.assertEqual(self.worker.pr_files_calls, [self.PR])
        self.assertEqual(self.worker.apply_provision_calls, [])

    def test_provision_py_change_applies_to_every_agent(self):
        ref = self._to_validate()
        self.worker.pr_files_result = ["deploy/provision.py", "deploy/README.md"]
        self._merge(ref)
        self.assertEqual(self.worker.apply_provision_calls, [[]])   # [] -> every agent

    def test_gate_script_change_applies_to_every_agent(self):
        ref = self._to_validate()
        self.worker.pr_files_result = ["deploy/ta-gate.sh"]
        self._merge(ref)
        self.assertEqual(self.worker.apply_provision_calls, [[]])   # [] -> every agent

    def test_automation_toml_change_applies_only_that_agent(self):
        ref = self._to_validate()
        self.worker.pr_files_result = ["triggered_agents/agents/curator/automation.toml"]
        self._merge(ref)
        self.assertEqual(self.worker.apply_provision_calls, [["curator"]])

    def test_multiple_automation_toml_changes_dedup_and_sort(self):
        ref = self._to_validate()
        self.worker.pr_files_result = [
            "triggered_agents/agents/steward/automation.toml",
            "triggered_agents/agents/curator/automation.toml",
            "triggered_agents/agents/steward/automation.toml",
        ]
        self._merge(ref)
        self.assertEqual(self.worker.apply_provision_calls, [["curator", "steward"]])

    def test_gh_unavailable_warns_and_never_applies(self):
        ref = self._to_validate()
        self.worker.pr_files_result = None
        self._merge(ref)
        self.assertEqual(self.worker.apply_provision_calls, [])
        self.assertTrue(any(r["event"] == "postmerge-apply" and r.get("result") == "gh-unavailable"
                            and r.get("level") == "warn" for r in self._runs()))

    def test_apply_failure_is_logged_but_card_stays_done(self):
        ref = self._to_validate()
        self.worker.pr_files_result = ["deploy/provision.py"]
        self.worker.apply_provision_result = {"ok": False, "log": "systemctl daemon-reload failed"}
        self._merge(ref)
        self.assertEqual(self._column(ref), "Done")                # merge already happened
        self.assertTrue(any(r["event"] == "postmerge-apply" and r.get("result") == "error"
                            and r.get("level") == "error" for r in self._runs()))

    def test_apply_success_logs_ok_not_a_signal(self):
        ref = self._to_validate()
        self.worker.pr_files_result = ["triggered_agents/agents/retro/automation.toml"]
        self._merge(ref)
        hits = [r for r in self._runs() if r["event"] == "postmerge-apply"]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["result"], "ok")
        self.assertNotEqual(hits[0].get("level"), "warn")
        self.assertNotEqual(hits[0].get("result"), "error")

    def test_one_shot_not_retried_next_tick(self):
        # Once Done, the card has left Validate entirely — a later tick must not re-derive/re-apply.
        ref = self._to_validate()
        self.worker.pr_files_result = ["deploy/provision.py"]
        self._merge(ref)
        self.assertEqual(len(self.worker.apply_provision_calls), 1)
        dispatcher.tick()
        self.assertEqual(len(self.worker.apply_provision_calls), 1)


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

    def _to_validate(self, pr=PR, head=None, review_head=None):
        meta = {model.META_REVIEW_HEAD: review_head} if review_head else None
        ref = self._claim_one(head=head, meta=meta)
        ops.report(ref, "done", f"готово\nPR: {pr}")
        self.worker.pr_status = None
        dispatcher.tick()
        self.assertEqual(self._column(ref), "Validate")
        return ref

    def _ci_green(self):
        self.worker.pr_status = {"merged": False, "state": "OPEN", "rollup": "SUCCESS",
                                 "failed_job": None, "failed_log": None}

    def _spawned(self, pr=PR, head=None, review_head=None):
        """Land a card in Validate with the reviewer head up: done -> Validate, CI green -> spawn."""
        ref = self._to_validate(pr, head=head, review_head=review_head)
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

    def test_reviewer_spawn_uses_card_base_branch_override(self):
        # triggered-agents-260: the reviewer's own worktree must land on the card's base_branch
        # override too, not the project manifest default ("main" here, stubbed read_base_branch).
        self.worker.remote_head_shas["sprint/007-dnd"] = "deadbeef"
        ref = self._claim_one(project="dnd-simulator",
                              meta={model.META_BASE_BRANCH: "sprint/007-dnd"})
        pr = "https://github.com/vladmesh/dnd-simulator/pull/1"
        ops.report(ref, "done", f"готово\nPR: {pr}")
        self.worker.pr_status = None
        dispatcher.tick()
        self._ci_green()
        dispatcher.tick()
        self.assertEqual(len(self.worker.reviewer_spawns), 1)
        base_used = self.worker.reviewer_spawns[0][2]
        self.assertEqual(base_used, "sprint/007-dnd")

    def test_codex_worker_can_use_claude_reviewer_from_card_metadata(self):
        ref = self._spawned(head="codex-extra", review_head="claude-opus")
        rec = dispatcher._load_cards()[ref]
        self.assertEqual(self.worker.launched[0]["head"], "codex-extra")
        self.assertEqual(self.worker.reviewer_heads, ["claude-opus"])
        self.assertEqual(rec["head"], "codex-extra")
        self.assertEqual(rec["review_head"], "claude-opus")
        self.assertIn("`claude-opus`", self._markers(ref))

    def test_claude_worker_can_use_codex_reviewer_from_card_metadata(self):
        ref = self._spawned(head="claude-opus", review_head="codex-reviewer")
        rec = dispatcher._load_cards()[ref]
        self.assertEqual(self.worker.launched[0]["head"], "claude-opus")
        self.assertEqual(self.worker.reviewer_heads, ["codex-reviewer"])
        self.assertEqual(rec["head"], "claude-opus")
        self.assertEqual(rec["review_head"], "codex-reviewer")

    def test_missing_review_head_uses_default_reviewer(self):
        ref = self._spawned(head="codex-extra")
        rec = dispatcher._load_cards()[ref]
        self.assertEqual(self.worker.reviewer_heads, [worker.REVIEWER_HEAD])
        self.assertEqual(rec["review_head"], worker.REVIEWER_HEAD)

    def _automerge_off(self):
        os.environ["TA_AUTOMERGE"] = "off"
        self.addCleanup(lambda: os.environ.pop("TA_AUTOMERGE", None))

    def test_green_verdict_automerges_by_default(self):
        # Default behavior (this card): a no-stand project's green review is no longer left for a
        # human — the dispatcher squash-merges it itself, same as a stand project always has.
        ref = self._spawned()
        ops.verdict(ref, "green", "каждый criterion реально выполнен")
        dispatcher.tick()
        self.assertEqual(self.worker.merged, [self.PR])
        self.assertEqual(self._column(ref), "Validate")   # still waits on gh to report merged
        self.assertEqual(len(self._rev_ws_torndown()), 1)        # reviewer worktree cleaned up
        dispatcher.tick()                                        # no-op, no second merge/teardown
        self.assertEqual(self.worker.merged, [self.PR])
        self.assertEqual(len(self._rev_ws_torndown()), 1)
        self.assertTrue(any(r["event"] == "review" and r.get("result") == "green-automerge"
                            for r in self._runs()))

    def test_green_verdict_with_automerge_off_waits_for_merge_and_tears_down_once(self):
        self._automerge_off()
        ref = self._spawned()
        ops.verdict(ref, "green", "каждый criterion реально выполнен")
        dispatcher.tick()
        self.assertEqual(self._column(ref), "Validate")          # waits for a human merge
        self.assertEqual(self.worker.merged, [])                 # switch off -> gh pr merge never called
        self.assertEqual(len(self._rev_ws_torndown()), 1)        # reviewer worktree cleaned up
        dispatcher.tick()                                        # no-op, no second teardown
        self.assertEqual(len(self._rev_ws_torndown()), 1)
        self.assertTrue(any(r["event"] == "review" and r.get("result") == "green" for r in self._runs()))

    def test_green_verdict_with_automerge_off_logs_terminal_green_exactly_once(self):
        # Regression (review #1 on triggered-agents-221): a no-stand card idling in Validate on a
        # settled green verdict must log the terminal "review green" event once, not on every tick
        # it sits there waiting for a human merge.
        self._automerge_off()
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

    def test_red_verdict_relaunches_dead_worker_and_rewrites_task(self):
        # Regression for triggered-agents-276: a review/CI return hands work back to a live
        # worker terminal with refreshed TASK.md, not just to the In progress column.
        self.worker.unique_launch_handles = True
        ref = self._spawned()
        rec_before = dispatcher._load_cards()[ref]
        old_handle = rec_before["handle"]
        launched_before = len(self.worker.launched)
        tasks_before = len(self.worker.tasks_written)
        self.worker.dead_handles.add(old_handle)

        ops.verdict(ref, "red", "блокер: TASK.md должен содержать свежую историю")
        dispatcher.tick()

        self.assertEqual(self._column(ref), model.IN_PROGRESS)
        rec_after = dispatcher._load_cards()[ref]
        self.assertEqual(rec_after["workspace"], rec_before["workspace"])
        self.assertEqual(rec_after["worker"], rec_before["worker"])
        self.assertNotEqual(rec_after["handle"], old_handle)
        self.assertEqual(len(self.worker.launched), launched_before + 1)
        self.assertEqual(self.worker.launched[-1]["ws"], rec_before["workspace"])
        self.assertEqual(self.worker.notified[-1][0], rec_after["handle"])
        self.assertNotIn(old_handle, [handle for handle, _ in self.worker.notified])
        self.assertEqual(len(self.worker.tasks_written), tasks_before + 1)
        task_workspace, task_md = self.worker.tasks_written[-1]
        self.assertEqual(task_workspace, rec_before["workspace"])
        self.assertIn(f"[{model.MARKER_REVIEW_RED}]", task_md)
        self.assertIn("блокер: TASK.md должен содержать свежую историю", task_md)
        self.assertIn(f"[{model.MARKER_REVIEW_RETURN}]", task_md)
        self.assertGreaterEqual(rec_after["last_activity"], rec_before["last_activity"])
        self.assertTrue(any(r["event"] == "rework-worker" and r.get("result") == "relaunched"
                            and r.get("reason") == "review-red"
                            and r.get("old_handle") == old_handle
                            and r.get("new_handle") == rec_after["handle"]
                            for r in self._runs()))

    def test_red_verdict_relaunches_shell_prompt_handle(self):
        self.worker.unique_launch_handles = True
        ref = self._spawned()
        rec_before = dispatcher._load_cards()[ref]
        old_handle = rec_before["handle"]
        launched_before = len(self.worker.launched)
        tasks_before = len(self.worker.tasks_written)
        self.worker.terminal_entries[old_handle] = {
            "handle": old_handle,
            "connected": True,
            "writable": True,
            "preview": "dev@host:~/orca/workspaces/triggered-agents/card$",
        }

        ops.verdict(ref, "red", "блокер: shell handle должен перезапустить воркера")
        dispatcher.tick()

        self.assertEqual(self._column(ref), model.IN_PROGRESS)
        rec_after = dispatcher._load_cards()[ref]
        self.assertNotEqual(rec_after["handle"], old_handle)
        self.assertEqual(len(self.worker.launched), launched_before + 1)
        self.assertEqual(len(self.worker.tasks_written), tasks_before + 1)
        self.assertEqual(self.worker.notified[-1][0], rec_after["handle"])
        self.assertNotEqual(self.worker.notified[-1][0], old_handle)
        self.assertIn("блокер: shell handle должен перезапустить воркера",
                      self.worker.tasks_written[-1][1])

    def test_red_verdict_relaunches_codex_tui_shell_handle_with_same_kind(self):
        self.worker.unique_launch_handles = True
        ref = self._spawned(head="codex-tui")
        rec_before = dispatcher._load_cards()[ref]
        old_handle = rec_before["handle"]
        self.assertEqual(rec_before["terminal_kind"], "codex-tui")
        self.worker.terminal_entries[old_handle] = {
            "handle": old_handle,
            "connected": True,
            "writable": True,
            "preview": "dev@host:~/orca/workspaces/triggered-agents/card$",
        }

        ops.verdict(ref, "red", "блокер: shell handle не должен получать follow-up")
        dispatcher.tick()

        rec_after = dispatcher._load_cards()[ref]
        self.assertEqual(rec_after["terminal_kind"], "codex-tui")
        self.assertNotEqual(rec_after["handle"], old_handle)
        self.assertEqual(self.worker.notified[-1][0], rec_after["handle"])
        self.assertNotIn(old_handle, [handle for handle, _ in self.worker.notified])

    def test_red_verdict_relaunches_codex_tui_non_agent_handle_with_same_kind(self):
        self.worker.unique_launch_handles = True
        ref = self._spawned(head="codex-tui")
        rec_before = dispatcher._load_cards()[ref]
        old_handle = rec_before["handle"]
        self.assertEqual(rec_before["terminal_kind"], "codex-tui")
        self.worker.terminal_entries[old_handle] = {
            "handle": old_handle,
            "connected": True,
            "writable": True,
            "preview": "python3 -m unittest discover",
        }

        ops.verdict(ref, "red", "блокер: non-agent handle не должен получать follow-up")
        dispatcher.tick()

        rec_after = dispatcher._load_cards()[ref]
        self.assertEqual(rec_after["terminal_kind"], "codex-tui")
        self.assertNotEqual(rec_after["handle"], old_handle)
        self.assertEqual(self.worker.notified[-1][0], rec_after["handle"])
        self.assertNotIn(old_handle, [handle for handle, _ in self.worker.notified])

    def test_red_verdict_move_failure_does_not_relaunch_dead_worker(self):
        self.worker.unique_launch_handles = True
        ref = self._spawned()
        rec_before = dispatcher._load_cards()[ref]
        old_handle = rec_before["handle"]
        launched_before = len(self.worker.launched)
        tasks_before = len(self.worker.tasks_written)
        notified_before = len(self.worker.notified)
        torn_down_before = len(self._rev_ws_torndown())
        self.worker.dead_handles.add(old_handle)
        self.board.move_fails = True

        ops.verdict(ref, "red", "блокер: нельзя будить воркера до move")
        dispatcher.tick()

        self.assertEqual(self._column(ref), "Validate")
        rec_after = dispatcher._load_cards()[ref]
        self.assertEqual(rec_after["handle"], old_handle)
        self.assertIn("review_baseline", rec_after)
        self.assertEqual(rec_after.get("review_returns", 0), rec_before.get("review_returns", 0))
        self.assertEqual(len(self.worker.launched), launched_before)
        self.assertEqual(len(self.worker.tasks_written), tasks_before)
        self.assertEqual(len(self.worker.notified), notified_before)
        self.assertEqual(len(self._rev_ws_torndown()), torn_down_before)
        self.assertFalse(any(r.get("reason") == "review-red" for r in self._runs()))

    def test_red_verdict_relaunch_failure_blocks_dead_worker_after_move(self):
        ref = self._spawned()
        rec_before = dispatcher._load_cards()[ref]
        old_handle = rec_before["handle"]
        launched_before = len(self.worker.launched)
        tasks_before = len(self.worker.tasks_written)
        notified_before = len(self.worker.notified)
        torn_down_before = len(self._rev_ws_torndown())
        self.worker.dead_handles.add(old_handle)

        ops.verdict(ref, "red", "блокер: launch_worker упал после возврата")
        with mock.patch.object(worker, "launch_worker",
                               side_effect=RuntimeError("orca create failed")):
            dispatcher.tick()

        self.assertEqual(self._column(ref), "Blocked")
        self.assertNotIn(ref, dispatcher._load_cards())
        self.assertEqual(len(self.worker.launched), launched_before)
        self.assertEqual(len(self.worker.tasks_written), tasks_before + 1)
        self.assertEqual(len(self.worker.notified), notified_before)
        self.assertIn(rec_before["workspace"], self.worker.stopped_terminals)
        self.assertEqual(len(self._rev_ws_torndown()), torn_down_before + 1)
        journal = self._markers(ref)
        self.assertIn("launch_worker упал после возврата", journal)
        self.assertIn("orca create failed", journal)
        self.assertTrue(any(r["event"] == "rework-worker" and r.get("to") == "Blocked"
                            and r.get("reason") == "review-red" for r in self._runs()))
        self.assertFalse(any(r["event"] == "review" and r.get("to") == model.IN_PROGRESS
                             and r.get("reason") == "review-red" for r in self._runs()))

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

    def test_reviewer_watchdog_uses_tracked_handle_not_active_shell_in_workspace(self):
        ref = self._spawned()
        rec = dispatcher._load_cards()[ref]
        tracked = rec["review_handle"]
        shell = "review-shell"
        self.worker.activity_by_handle[tracked] = None
        self.worker.activity_by_handle[shell] = time.time()
        dispatcher.WATCHDOG_SECONDS = -1

        dispatcher.tick()

        self.assertIn((tracked, rec["review_ws"]), self.worker.terminal_activity_polled)
        self.assertNotIn((shell, rec["review_ws"]), self.worker.terminal_activity_polled)
        self.assertEqual(self._column(ref), "Blocked")
        self.assertTrue(any(r.get("reason") == "review-watchdog" for r in self._runs()))

    def test_reviewer_dead_handle_blocks_without_waiting_for_silence(self):
        ref = self._spawned()
        rec = dispatcher._load_cards()[ref]
        tracked = rec["review_handle"]
        shell = "review-shell"
        dispatcher.WATCHDOG_SECONDS = 3600
        self.worker.dead_handles.add(tracked)
        self.worker.activity_by_handle[shell] = time.time()

        dispatcher.tick()

        self.assertIn((tracked, rec["review_ws"]), self.worker.terminal_status_polled)
        self.assertNotIn((shell, rec["review_ws"]), self.worker.terminal_status_polled)
        self.assertEqual(self._column(ref), "Blocked")
        self.assertIn("не живой", self._markers(ref))
        self.assertTrue(any(r.get("event") == "review"
                            and r.get("reason") == "review-watchdog"
                            and r.get("trigger") == validate.REVIEW_WATCHDOG_TRIGGER_DEAD_HANDLE
                            and r.get("handle_status") == "missing-terminal"
                            for r in self._runs()))

    def test_spawn_resolves_reviewer_head_via_fallback_when_resource_red(self):
        # secretary-355 (2026-07-10): the reviewer spawn must walk the same health fallback as the
        # worker claim — a red openai-sub sends codex-reviewer's card to its claude-opus fallback
        # instead of spawning a head that dies at the shell prompt on the usage limit.
        ref = self._to_validate()
        self.statuses["openai-sub"] = "red"
        self._ci_green()
        dispatcher.tick()
        self.assertEqual(self.worker.reviewer_heads, ["claude-opus"])
        self.assertEqual(dispatcher._load_cards()[ref]["review_head"], "claude-opus")
        self.assertEqual(self._column(ref), "Validate")

    def test_spawn_waits_when_reviewer_chain_all_red(self):
        ref = self._to_validate()
        self.statuses["openai-sub"] = "red"
        self.statuses["claude-sub"] = "red"
        self._ci_green()
        dispatcher.tick()
        self.assertEqual(self.worker.reviewer_spawns, [])
        self.assertNotIn("review_baseline", dispatcher._load_cards()[ref])
        self.assertEqual(self._column(ref), "Validate")
        self.assertTrue(any(r["event"] == "review" and r.get("result") == "skip-red"
                            for r in self._runs()))
        self.statuses["claude-sub"] = "green"
        dispatcher.tick()                                 # a resource recovered -> spawn resumes
        self.assertEqual(self.worker.reviewer_heads, ["claude-opus"])

    def test_dead_reviewer_handle_on_red_resource_respawns_not_blocks(self):
        # secretary-355 (2026-07-10), second half: a reviewer whose terminal died while its own
        # resource is red is the resource's outage, not a case for a human — clear the review
        # bookkeeping and let the next tick respawn through the health-aware path.
        ref = self._spawned()
        rec = dispatcher._load_cards()[ref]
        self.worker.dead_handles.add(rec["review_handle"])
        self.statuses["openai-sub"] = "red"
        dispatcher.tick()
        self.assertEqual(self._column(ref), "Validate")          # not Blocked
        self.assertEqual(len(self._rev_ws_torndown()), 1)        # dead reviewer's worktree cleaned
        self.assertNotIn("review_baseline", dispatcher._load_cards()[ref])
        self.assertTrue(any(r["event"] == "review" and r.get("result") == "dead-on-red"
                            for r in self._runs()))
        dispatcher.tick()                                        # respawn lands on the fallback
        self.assertEqual(self.worker.reviewer_heads[-1], "claude-opus")
        self.assertEqual(self._column(ref), "Validate")

    def test_dead_reviewer_handle_on_green_resource_still_blocks(self):
        ref = self._spawned()
        rec = dispatcher._load_cards()[ref]
        self.worker.dead_handles.add(rec["review_handle"])
        dispatcher.tick()
        self.assertEqual(self._column(ref), "Blocked")
        self.assertTrue(any(r.get("trigger") == validate.REVIEW_WATCHDOG_TRIGGER_DEAD_HANDLE
                            for r in self._runs()))

    def test_review_watchdog_frozen_while_reviewer_resource_is_red(self):
        # triggered-agents-241: the reviewer head has its own resource; a subscription-limit red
        # must freeze this clock too, not just dispatcher._advance's worker-side one (#31).
        ref = self._spawned()
        self.worker.activity_ts = None
        dispatcher.WATCHDOG_SECONDS = -1
        self.statuses["openai-sub"] = "red"
        dispatcher.tick()
        self.assertEqual(self._column(ref), "Validate")   # frozen, not Blocked
        self.assertEqual(self._rev_ws_torndown(), [])

    def test_review_watchdog_uses_selected_reviewer_head_resource(self):
        ref = self._spawned(review_head="claude-opus")
        self.worker.activity_ts = None
        dispatcher.WATCHDOG_SECONDS = -1
        self.statuses["claude-sub"] = "red"
        dispatcher.tick()
        self.assertEqual(self._column(ref), "Validate")   # frozen on claude-sub, not default reviewer
        self.statuses["claude-sub"] = "green"
        dispatcher.tick()
        self.assertEqual(self._column(ref), "Blocked")
        self.assertTrue(any(r.get("reason") == "review-watchdog" for r in self._runs()))

    def test_review_watchdog_resumes_once_reviewer_resource_goes_green_again(self):
        ref = self._spawned()
        self.worker.activity_ts = None
        dispatcher.WATCHDOG_SECONDS = -1
        self.statuses["openai-sub"] = "red"
        dispatcher.tick()
        self.assertEqual(self._column(ref), "Validate")   # still frozen
        self.statuses["openai-sub"] = "green"
        dispatcher.tick()   # clock resumes from the last frozen tick, still silent -> fires
        self.assertEqual(self._column(ref), "Blocked")
        self.assertTrue(any(r.get("reason") == "review-watchdog" for r in self._runs()))

    def test_review_watchdog_unfrozen_still_holds_when_reviewer_is_actually_active(self):
        ref = self._spawned()
        self.statuses["openai-sub"] = "red"
        dispatcher.WATCHDOG_SECONDS = 600
        dispatcher.tick()   # frozen tick
        self.statuses["openai-sub"] = "green"
        self.worker.activity_ts = time.time()   # fresh output once the resource recovers
        dispatcher.tick()
        self.assertEqual(self._column(ref), "Validate")

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

    def test_reviewer_inject_delivery_failure_is_explicit(self):
        ref = self._to_validate()
        self._ci_green()
        self.worker.reviewer_raises = worker.InjectDeliveryError("inject не доставлен: REVIEW.md")

        dispatcher.tick()

        self.assertEqual(self._column(ref), "Blocked")
        self.assertNotIn(ref, dispatcher._load_cards())
        posted = " ".join(c["comment"] for cl in self.board.comments.values() for c in cl)
        self.assertIn("inject не доставлен", posted)
        self.assertTrue(any(r.get("event") == "review"
                            and r.get("reason") == "inject-delivery"
                            and r.get("reference") == ref
                            for r in self._runs()))

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

        def claim_then_die(records, statuses):
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


class ValidateContribTest(_DispatcherBase):
    """Validate for a contrib (fork) card: it has no PR in this pipeline by definition (a human
    opens it against upstream from the pushed branch afterward) — layer 1 is the worker's own
    report (branch + head sha protocol lines, no CI polling), layer 3 spawns the reviewer straight
    off the reported branch, and green goes straight to Done (no PR to wait on for a merge)."""

    PROJECT = "agent-kanban"
    BRANCH_PREFIX = "pipeline"
    SHA = "abc1234"

    def setUp(self):
        super().setUp()
        self.worker.contrib_projects = {self.PROJECT}

    def _markers(self, ref):
        tid = next(t["id"] for t in self.board.tasks.values() if t["reference"] == ref)
        return " ".join(c["comment"] for c in self.board.comments.get(tid, []))

    def _claim(self, title="A"):
        return self._claim_one(title, project=self.PROJECT)

    def _branch(self, ref):
        return f"{self.BRANCH_PREFIX}/{ref}"

    @staticmethod
    def _full(sha):
        """Pad an abbreviated test sha into a realistic 40-char lowercase hex string — the shape
        `git ls-remote` (worker.remote_head_sha) always returns in production, never the bare
        7-char abbreviation a report may carry. A "match" test that fed the fake the exact reported
        string back would pass on plain string equality even with the pre-fix bug (a real
        full-vs-abbreviated/mixed-case comparison falsely mismatched — triggered-agents-240
        review); using this for the remote side forces the case-insensitive prefix comparison in
        validate._validate_contrib_card to actually run."""
        return (sha.lower() + "0" * 40)[:40]

    def _to_validate(self, sha=SHA):
        ref = self._claim()
        # remote_head_sha() mirrors this fork's real origin — a fresh push means the branch head
        # actually is the reported sha (padded to a realistic full sha), same as a real `git push`
        # would leave it.
        self.worker.remote_head_shas[self._branch(ref)] = self._full(sha)
        ops.report(ref, "done", f"готово\nbranch: {self._branch(ref)}\nhead: {sha}")
        dispatcher.tick()
        self.assertEqual(self._column(ref), "Validate")
        return ref

    def test_no_gh_poll_ever_for_a_contrib_card(self):
        ref = self._to_validate()
        dispatcher.tick()
        self.assertEqual(self.worker.polled, [])

    def test_missing_branch_stalls_with_own_reason_not_no_pr_ref(self):
        ref = self._claim()
        ops.report(ref, "done", "готово, но забыл протокольные строки")
        dispatcher.tick()
        self.assertEqual(self._column(ref), "Validate")
        self.assertEqual(self.worker.polled, [])
        runs = self._runs()
        self.assertTrue(any(r["event"] == "validate" and r.get("result") == "no-branch-ref"
                            and r.get("level") == "warn" for r in runs))
        self.assertFalse(any(r.get("result") == "no-pr-ref" for r in runs))

    def test_missing_head_alone_also_stalls(self):
        ref = self._claim()
        ops.report(ref, "done", f"готово\nbranch: {self._branch(ref)}")  # no head sha
        dispatcher.tick()
        self.assertEqual(self._column(ref), "Validate")
        self.assertTrue(any(r["event"] == "validate" and r.get("result") == "no-branch-ref"
                            for r in self._runs()))

    def test_missing_branch_escalates_after_stall_cap_with_own_reason(self):
        # Same cap/escalation machinery as the PR flow (_validate_stall), but a contrib card must
        # never surface as a no-pr-ref stall — it has no PR by definition.
        ref = self._claim()
        ops.report(ref, "done", "готово, но забыл протокольные строки")
        dispatcher.tick()
        for i in range(2, dispatcher.VALIDATE_STALL_ATTEMPTS):
            dispatcher.tick()
            self.assertEqual(self._column(ref), "Validate")
            self.assertEqual(dispatcher._load_cards()[ref]["validate_stall_fails"], i)
        dispatcher.tick()
        self.assertEqual(self._column(ref), "Blocked")
        self.assertNotIn(ref, dispatcher._load_cards())
        runs = self._runs()
        stalls = [r for r in runs if r["event"] == "validate" and r.get("reason") == "no-branch-ref-stall"]
        self.assertEqual(len(stalls), 1)
        self.assertFalse(any(r.get("reason") == "no-pr-ref-stall" for r in runs))

    # sha verification (triggered-agents-240): the report's claimed head must match the branch's
    # real head on origin before a reviewer is spawned — a worker whose session lives on could keep
    # pushing after report:done, so the claimed sha alone is not proof of what a review would land
    # on.
    def test_sha_mismatch_stalls_without_spawning_reviewer(self):
        ref = self._claim()
        branch = self._branch(ref)
        self.worker.remote_head_shas[branch] = "actualsha9"   # worker kept pushing past the report
        ops.report(ref, "done", f"готово\nbranch: {branch}\nhead: {self.SHA}")
        dispatcher.tick()
        self.assertEqual(self._column(ref), "Validate")       # not Blocked, not In progress — stall
        self.assertEqual(self.worker.reviewer_spawns, [])      # never reviewed the wrong state
        runs = self._runs()
        self.assertTrue(any(r["event"] == "validate" and r.get("result") == "sha-mismatch"
                            and r.get("level") == "warn" and r.get("branch") == branch
                            and r.get("reported") == self.SHA and r.get("actual") == "actualsha9"
                            for r in runs))

    def test_sha_mismatch_gives_a_chance_to_rereport_and_recovers(self):
        # The worker doesn't have to post a fresh report:done — once the branch on origin catches
        # up to what was already claimed, the very next tick's check passes and review proceeds.
        ref = self._claim()
        branch = self._branch(ref)
        self.worker.remote_head_shas[branch] = "actualsha9"
        ops.report(ref, "done", f"готово\nbranch: {branch}\nhead: {self.SHA}")
        dispatcher.tick()
        self.assertEqual(self.worker.reviewer_spawns, [])
        self.worker.remote_head_shas[branch] = self._full(self.SHA)   # origin now matches the report
        dispatcher.tick()
        self.assertEqual(self._column(ref), "Validate")
        self.assertEqual(len(self.worker.reviewer_spawns), 1)
        self.assertEqual(self.worker.reviewer_spawns[0][6], self.SHA)

    def test_branch_unreachable_on_origin_stalls_like_sha_mismatch(self):
        # remote_head_sha returns None (branch not found / remote unreachable) — treated the same
        # as a mismatch, not as "review the branch's current, unrelated tip".
        ref = self._claim()
        branch = self._branch(ref)
        ops.report(ref, "done", f"готово\nbranch: {branch}\nhead: {self.SHA}")   # no remote_head_shas entry
        dispatcher.tick()
        self.assertEqual(self._column(ref), "Validate")
        self.assertEqual(self.worker.reviewer_spawns, [])
        self.assertTrue(any(r["event"] == "validate" and r.get("result") == "branch-unavailable"
                            and r.get("branch") == branch for r in self._runs()))

    def test_sha_mismatch_escalates_after_stall_cap(self):
        ref = self._claim()
        branch = self._branch(ref)
        self.worker.remote_head_shas[branch] = "actualsha9"
        ops.report(ref, "done", f"готово\nbranch: {branch}\nhead: {self.SHA}")
        dispatcher.tick()
        for i in range(2, dispatcher.VALIDATE_STALL_ATTEMPTS):
            dispatcher.tick()
            self.assertEqual(self._column(ref), "Validate")
            self.assertEqual(dispatcher._load_cards()[ref]["validate_stall_fails"], i)
        dispatcher.tick()
        self.assertEqual(self._column(ref), "Blocked")
        self.assertNotIn(ref, dispatcher._load_cards())
        self.assertEqual(self.worker.reviewer_spawns, [])
        stalls = [r for r in self._runs() if r["event"] == "validate" and r.get("reason") == "sha-mismatch-stall"]
        self.assertEqual(len(stalls), 1)

    def test_abbreviated_and_mixed_case_reported_sha_still_matches(self):
        # triggered-agents-240 review finding: `git ls-remote` (worker.remote_head_sha) always
        # answers with the full 40-char lowercase object name, while a real worker's report may
        # legitimately carry an abbreviated and/or mixed-case sha (`git rev-parse --short`, `git
        # push`'s own summary line — `_CONTRIB_HEAD_RE` explicitly accepts both). A plain
        # string-equality check would flag this honestly-matching state as a mismatch and escalate
        # a good report to Blocked.
        ref = self._claim()
        branch = self._branch(ref)
        reported = "ABC1234"                                  # abbreviated, mixed-case
        self.worker.remote_head_shas[branch] = self._full(reported)   # full, lowercase — real shape
        ops.report(ref, "done", f"готово\nbranch: {branch}\nhead: {reported}")
        dispatcher.tick()
        self.assertEqual(self._column(ref), "Validate")
        self.assertEqual(len(self.worker.reviewer_spawns), 1)
        self.assertFalse(any(r.get("result") == "sha-mismatch" for r in self._runs()))

    def test_sha_check_skipped_once_reviewer_already_spawned(self):
        # The gate applies only before a fresh spawn — once the reviewer is up for this code state,
        # a later drift on origin (the worker still pushing) must not stall an in-flight review.
        ref = self._to_validate()
        self.assertEqual(len(self.worker.reviewer_spawns), 1)
        self.worker.remote_head_shas[self._branch(ref)] = "driftedsha"
        dispatcher.tick()                                     # watchdog path, not a re-check
        self.assertEqual(self._column(ref), "Validate")
        self.assertEqual(len(self.worker.reviewer_spawns), 1)  # no second spawn, no stall

    def test_report_with_branch_and_sha_greenlights_layer1_once(self):
        ref = self._to_validate()
        dispatcher.tick()                            # second green tick must not re-post
        journal = self._markers(ref)
        self.assertEqual(journal.count(f"[{model.MARKER_VALIDATE_GREEN}]"), 1)
        self.assertIn(self._branch(ref), journal)
        self.assertIn(self.SHA, journal)

    def test_spawns_reviewer_off_reported_branch_without_gh(self):
        ref = self._to_validate()
        self.assertEqual(len(self.worker.reviewer_spawns), 1)
        _, _, _, _, pr_branch, _, head_sha = self.worker.reviewer_spawns[0]
        self.assertEqual(pr_branch, self._branch(ref))     # naming.worker_branch(ref), no gh call
        self.assertEqual(head_sha, self.SHA)   # pinned to the reported sha, not the branch's tip
        self.assertEqual(self.worker.polled, [])

    def test_green_verdict_goes_straight_to_done_and_tears_down(self):
        ref = self._to_validate()
        ws = dispatcher._load_cards()[ref]["workspace"]
        ops.verdict(ref, "green", "каждый criterion реально выполнен")
        dispatcher.tick()
        self.assertEqual(self._column(ref), "Done")
        self.assertNotIn(ref, dispatcher._load_cards())
        self.assertIn(ws, self.worker.torn_down)
        self.assertEqual(self.worker.merged, [])            # no PR to merge, ever

    def test_red_verdict_returns_to_in_progress_same_cap_as_pr_flow(self):
        ref = self._to_validate()
        ops.verdict(ref, "red", "блокер: X")
        dispatcher.tick()
        self.assertEqual(self._column(ref), model.IN_PROGRESS)
        rec = dispatcher._load_cards()[ref]
        self.assertEqual(rec["review_returns"], 1)
        self.assertNotIn("review_baseline", rec)

    def test_return_cap_blocks_after_repeated_red_verdicts(self):
        ref = self._to_validate()
        for i in range(dispatcher.REVIEW_RETURN_CAP):
            ops.verdict(ref, "red", f"блокер {i}")
            dispatcher.tick()
            self.assertEqual(self._column(ref), model.IN_PROGRESS)
            sha = f"aaaaaa{i}"                                # valid hex, distinct each rework
            self.worker.remote_head_shas[self._branch(ref)] = self._full(sha)
            ops.report(ref, "done", f"fix\nbranch: {self._branch(ref)}\nhead: {sha}")
            dispatcher.tick()                                # -> Validate
            dispatcher.tick()                                # -> reviewer up again
        ops.verdict(ref, "red", "сверх капа")
        dispatcher.tick()
        self.assertEqual(self._column(ref), "Blocked")
        self.assertNotIn(ref, dispatcher._load_cards())

    def test_red_then_fresh_report_reruns_review_then_green_to_done(self):
        ref = self._to_validate()
        ops.verdict(ref, "red", "блокер: X")
        dispatcher.tick()
        self.assertEqual(self._column(ref), model.IN_PROGRESS)
        self.worker.remote_head_shas[self._branch(ref)] = self._full("def5678")
        ops.report(ref, "done", f"починил\nbranch: {self._branch(ref)}\nhead: def5678")
        dispatcher.tick()
        self.assertEqual(self._column(ref), "Validate")
        dispatcher.tick()                                    # fresh reviewer off the new head
        self.assertEqual(len(self.worker.reviewer_spawns), 2)
        self.assertEqual(self.worker.reviewer_spawns[1][6], "def5678")  # pinned to the reworked sha
        ops.verdict(ref, "green", "теперь всё реально")
        dispatcher.tick()
        self.assertEqual(self._column(ref), "Done")


class AutomergeTest(_DispatcherBase):
    """triggered-agents-221: for a project with a [stand] section, a green review verdict on top
    of already-green CI and stand triggers the dispatcher's own squash merge (worker.merge_pr)
    instead of waiting for a human — vladmesh's 2026-07-02 call that the live-stand e2e gate is
    enough assurance. triggered-agents-245 extends the same one-shot automerge to every project,
    stand or not, on a green review verdict — TA_AUTOMERGE=off is the kill switch back to a human
    merge, no redeploy needed."""

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

    def _to_review_green_no_stand(self, pr=PR, project="personal_site"):
        """Same as _to_review_green but for a project with no [stand] section: CI green is the
        last mechanical layer, straight to the reviewer."""
        self.worker.stand_config = None
        before = len(self.worker.reviewer_spawns)
        ref = self._claim_one(project=project)
        ops.report(ref, "done", f"готово\nPR: {pr}")
        self.worker.pr_status = None
        dispatcher.tick()
        self.assertEqual(self._column(ref), "Validate")
        self._ci_green()
        dispatcher.tick()                     # no stand -> spawns the reviewer directly
        self.assertEqual(len(self.worker.reviewer_spawns), before + 1)
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

    def test_no_stand_project_green_review_also_automerges(self):
        ref = self._to_review_green_no_stand()
        dispatcher.tick()
        self.assertEqual(self.worker.merged, [self.PR])
        journal = self._markers(ref)
        self.assertIn(f"[{model.MARKER_AUTOMERGE}]", journal)
        self.assertEqual(self._column(ref), "Validate")   # still waits on gh to report merged
        self.assertTrue(any(r["event"] == "review" and r.get("result") == "green-automerge"
                            for r in self._runs()))

    def test_no_stand_automerge_is_attempted_once_across_ticks(self):
        ref = self._to_review_green_no_stand()
        dispatcher.tick()
        self.assertEqual(self.worker.merged, [self.PR])
        dispatcher.tick()
        self.assertEqual(self.worker.merged, [self.PR])

    def test_no_stand_merge_failure_blocks_with_reason_and_does_not_retry(self):
        ref = self._to_review_green_no_stand()
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

    def test_base_branch_mismatch_blocks_without_merging(self):
        # triggered-agents-266: a sprint-shim card (base_branch override) whose worker opened the
        # PR against the wrong base (gh pr create defaulting to main) must not be silently
        # squash-merged there — the mismatch Blocks instead of reaching worker.merge_pr at all.
        self.worker.stand_config = None
        self.worker.remote_head_shas["sprint/007-dnd"] = "deadbeef"
        ref = self._claim_one(project="dnd-simulator",
                              meta={model.META_BASE_BRANCH: "sprint/007-dnd"})
        ops.report(ref, "done", f"готово\nPR: {self.PR}")
        self.worker.pr_status = None
        dispatcher.tick()
        self.assertEqual(self._column(ref), "Validate")
        self._ci_green()
        dispatcher.tick()                     # no stand -> spawns the reviewer directly
        ops.verdict(ref, "green", "все критерии реально выполнены")
        self.worker.pr_bases[self.PR] = "main"    # worker ignored TASK.md, PR opened against main
        dispatcher.tick()
        self.assertEqual(self._column(ref), "Blocked")
        self.assertNotIn(ref, dispatcher._load_cards())
        self.assertEqual(self.worker.merged, [])
        journal = self._markers(ref)
        self.assertIn("sprint/007-dnd", journal)
        self.assertIn("main", journal)
        self.assertTrue(any(r["event"] == "review" and r.get("reason") == "base-mismatch"
                            and r.get("expected") == "sprint/007-dnd" and r.get("actual") == "main"
                            for r in self._runs()))

    def test_base_branch_check_unavailable_retries_without_merging_or_blocking(self):
        ref = self._to_review_green_no_stand()
        self.worker.pr_bases[self.PR] = None      # gh can't answer baseRefName this tick
        dispatcher.tick()
        self.assertEqual(self.worker.merged, [])
        self.assertEqual(self._column(ref), "Validate")
        self.worker.pr_bases.pop(self.PR)          # gh answers "main" (the default) next tick
        dispatcher.tick()
        self.assertEqual(self.worker.merged, [self.PR])

    def test_automerge_off_switch_reverts_both_stand_and_no_stand_to_human_merge(self):
        os.environ["TA_AUTOMERGE"] = "off"
        self.addCleanup(lambda: os.environ.pop("TA_AUTOMERGE", None))
        stand_ref = self._to_review_green()
        dispatcher.tick()
        self.assertEqual(self.worker.merged, [])
        self.assertEqual(self._column(stand_ref), "Validate")
        no_stand_ref = self._to_review_green_no_stand(
            pr="https://github.com/vladmesh/other_project/pull/1", project="other_project")
        dispatcher.tick()
        self.assertEqual(self.worker.merged, [])
        self.assertEqual(self._column(no_stand_ref), "Validate")


class PipelinePauseTest(_DispatcherBase):
    """triggered-agents-281: the pause/resume API. Soft only turns off new claims — everything
    already claimed rides its normal cycle. Hard also stops every live worker/reviewer terminal and
    freezes the whole tick, so resume() has to relaunch each stopped head and reset its watchdog
    clock, never letting the paused stretch itself read as silence."""

    PR = "https://github.com/vladmesh/personal_site/pull/9"

    def _pause(self, mode="soft", reason="maintenance", actor="test"):
        return dispatcher.pause(mode, reason=reason, actor=actor)

    def _age_pause_flag(self, seconds: int) -> None:
        state = pause.load()
        state["since"] = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()
        pause.PAUSE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    def _assert_not_paused(self, status):
        self.assertFalse(status["paused"])
        self.assertEqual(status["live_state_path"], str(pause.PAUSE_FILE.resolve()))
        self.assertIn("warnings", status)

    def _markers(self, ref):
        tid = next(t["id"] for t in self.board.tasks.values() if t["reference"] == ref)
        return " ".join(c["comment"] for c in self.board.comments.get(tid, []))

    def _to_validate_with_reviewer(self):
        """A card In progress -> Validate (report:done) -> reviewer spawned (CI green, no stand)."""
        ref = self._claim_one()
        ops.report(ref, "done", f"готово\nPR: {self.PR}")
        self.worker.pr_status = None
        dispatcher.tick()
        self.worker.pr_status = {"merged": False, "state": "OPEN", "rollup": "SUCCESS",
                                 "failed_job": None, "failed_log": None}
        dispatcher.tick()
        self.assertEqual(self._column(ref), "Validate")
        self.assertEqual(len(self.worker.reviewer_spawns), 1)
        return ref

    # --- flag primitives / idempotency -----------------------------------------------------
    def test_pause_status_reports_running_when_no_flag(self):
        self._assert_not_paused(dispatcher.pause_status())

    def test_soft_pause_then_status_then_resume(self):
        result = self._pause("soft", reason="maintenance", actor="steward")
        self.assertEqual(result["paused"], True)
        self.assertEqual(result["mode"], "drain")
        self.assertEqual(result["internal_mode"], "soft")
        self.assertEqual(result["reason"], "maintenance")
        self.assertEqual(result["actor"], "steward")
        self.assertEqual(dispatcher.pause_status()["mode"], "drain")
        result = dispatcher.resume()
        self._assert_not_paused(result)
        self._assert_not_paused(dispatcher.pause_status())

    def test_pause_status_includes_reason_actor_paths_and_resume_behavior(self):
        ref = self._claim_one()
        result = self._pause("freeze", reason="deploy window", actor="vladmesh")
        self.assertEqual(result["mode"], "freeze")
        self.assertEqual(result["internal_mode"], "hard")
        self.assertEqual(result["reason"], "deploy window")
        self.assertEqual(result["actor"], "vladmesh")
        self.assertEqual(result["stopped_worker"], [ref])
        self.assertEqual(result["live_state_path"], str(pause.PAUSE_FILE.resolve()))
        self.assertIn("resume", result["on_resume"])
        self.assertIn("workspaces", result["on_resume"])

    def test_pause_status_warns_about_other_pause_json_files(self):
        shadow = Path(os.environ["TA_STATE"]) / "pipeline" / "pause.json"
        shadow.parent.mkdir(parents=True, exist_ok=True)
        shadow.write_text('{"mode": "hard"}', encoding="utf-8")

        status = dispatcher.pause_status()
        self.assertIn(str(shadow.resolve()), status["other_pause_files"])
        self.assertTrue(any(str(shadow.resolve()) in warning for warning in status["warnings"]))
        self.assertFalse(status["paused"])

    def test_drain_and_freeze_aliases_store_the_existing_internal_modes(self):
        result = self._pause("drain")
        self.assertEqual(result["mode"], "drain")
        self.assertEqual(pause.load()["mode"], "soft")
        dispatcher.resume()

        result = self._pause("freeze")
        self.assertEqual(result["mode"], "freeze")
        self.assertEqual(pause.load()["mode"], "hard")

    def test_repeated_pause_same_mode_is_idempotent_noop(self):
        self._pause("soft")
        self._pause("soft")   # must not raise, must not re-log a fresh "paused" transition
        actions = [r.get("action") for r in self._runs() if r["event"] == "pause"]
        self.assertEqual(actions, ["paused", "noop"])

    def test_pause_other_mode_while_already_paused_is_a_guard_error(self):
        self._pause("soft")
        with self.assertRaises(model.GuardError):
            self._pause("hard")
        self.assertEqual(dispatcher.pause_status()["mode"], "drain")   # unchanged

    def test_resume_when_not_paused_is_a_noop(self):
        result = dispatcher.resume()
        self._assert_not_paused(result)
        actions = [r.get("action") for r in self._runs() if r["event"] == "resume"]
        self.assertEqual(actions, ["noop"])

    def test_unknown_mode_is_a_guard_error(self):
        with self.assertRaises(model.GuardError):
            dispatcher.pause("nonsense")

    def test_pause_and_dispatcher_state_use_live_pipeline_state_dir(self):
        live = pipeline_state.STATE.dir
        local = runtime_state.STATE_ROOT
        self.assertEqual(pause.PAUSE_FILE, live / "pause.json")
        self.assertEqual(dispatcher.CARDS_FILE, live / "cards.json")
        self.assertFalse(str(pause.PAUSE_FILE).startswith(str(local)))

    def test_corrupt_pause_file_fails_open_but_logs_a_warn(self):
        pause.STATE.ensure_dir()
        pause.PAUSE_FILE.write_text("{not json", encoding="utf-8")
        self._assert_not_paused(dispatcher.pause_status())
        runs = [r for r in self._runs() if r["event"] == "pause-flag"]
        self.assertTrue(runs and runs[-1]["result"] == "corrupt" and runs[-1]["level"] == "warn")

    def test_resume_relaunch_failure_is_localized_not_a_crash(self):
        ref = self._claim_one(head="claude-opus")
        self._pause("hard")
        with mock.patch.object(worker, "launch_worker", side_effect=RuntimeError("orca timeout")):
            result = dispatcher.resume()   # must not raise despite the relaunch failing
        self._assert_not_paused(result)   # still cleared, not stuck paused forever
        runs = [r for r in self._runs() if r["event"] == "resume" and r.get("result") == "relaunch-failed"]
        self.assertTrue(any(r.get("reference") == ref for r in runs))

    # --- soft: claims off, everything claimed rides its cycle -------------------------------
    def test_soft_pause_blocks_new_claims(self):
        self._pause("soft")
        ref = self._ready_card("A")
        dispatcher.tick()
        self.assertEqual(self._column(ref), "Ready")
        self.assertEqual(self.worker.launched, [])
        runs = [r for r in self._runs() if r["event"] == "claim-skip" and r.get("mode") == "soft"]
        self.assertTrue(runs)

    def test_soft_pause_still_advances_a_claimed_card_to_validate(self):
        ref = self._claim_one()
        self._pause("soft")
        ops.report(ref, "done", f"готово\nPR: {self.PR}")
        dispatcher.tick()
        self.assertEqual(self._column(ref), "Validate")

    def test_soft_pause_still_runs_review_and_automerge(self):
        ref = self._to_validate_with_reviewer()
        self._pause("soft")
        ops.verdict(ref, "green", "каждый criterion выполнен")
        dispatcher.tick()
        self.assertEqual(self.worker.merged, [self.PR])

    def test_soft_pause_does_not_stop_any_live_terminal(self):
        ref = self._claim_one()
        self._pause("soft")
        self.assertEqual(self.worker.stopped_terminals, [])
        self.assertEqual(dispatcher._load_cards()[ref]["handle"], f"handle-{self.worker.launched[0]['worker']}")

    # --- hard: everything stops, tick freezes solid ------------------------------------------
    def test_hard_pause_stops_the_in_progress_worker_terminal(self):
        ref = self._claim_one()
        ws = dispatcher._load_cards()[ref]["workspace"]
        self._pause("hard")
        self.assertIn(ws, self.worker.stopped_terminals)
        self.assertEqual(dispatcher.pause_status()["stopped_worker"], [ref])

    def test_hard_pause_stops_the_live_reviewer_terminal_too(self):
        ref = self._to_validate_with_reviewer()
        review_ws = dispatcher._load_cards()[ref]["review_ws"]
        self._pause("hard")
        self.assertIn(review_ws, self.worker.stopped_terminals)
        self.assertEqual(dispatcher.pause_status()["stopped_reviewer"], [ref])

    def test_hard_pause_freezes_the_tick_entirely(self):
        ref = self._claim_one()
        self._pause("hard")
        ops.report(ref, "done", f"готово\nPR: {self.PR}")   # a report lands while hard-paused
        dispatcher.tick()
        self.assertEqual(self._column(ref), model.IN_PROGRESS)   # not advanced — tick never ran
        runs = [r for r in self._runs() if r["event"] == "tick" and r.get("result") == "paused"]
        self.assertTrue(runs)

    def test_hard_pause_leaves_ready_cards_unclaimed(self):
        self._pause("hard")
        ref = self._ready_card("A")
        dispatcher.tick()
        self.assertEqual(self._column(ref), "Ready")
        self.assertEqual(self.worker.launched, [])

    def test_precheck_hard_paused_skips_before_listing_the_board(self):
        self._pause("hard")
        rc = dispatcher.precheck()
        self.assertEqual(rc, PRECHECK_SKIP)
        runs = [r for r in self._runs() if r["event"] == "precheck"]
        self.assertEqual(runs[-1]["result"], "paused")
        self.assertEqual(runs[-1]["mode"], "hard")

    def test_stale_automation_hard_pause_auto_resumes_on_precheck(self):
        ref = self._claim_one(head="claude-opus")
        rec_before = dispatcher._load_cards()[ref]
        self._pause("hard", reason="secretary backup create", actor="secretary-backup")
        self._age_pause_flag(pause.HARD_PAUSE_AUTO_RESUME_TTL_SECONDS + 1)
        self.worker.launched.clear()

        rc = dispatcher.precheck()

        self.assertEqual(rc, 0)
        self._assert_not_paused(dispatcher.pause_status())
        self.assertEqual(len(self.worker.launched), 1)
        self.assertEqual(self.worker.launched[0]["ws"], rec_before["workspace"])
        self.assertEqual(self.worker.launched[0]["worker"], rec_before["worker"])
        rec_after = dispatcher._load_cards()[ref]
        self.assertGreaterEqual(rec_after["last_activity"], rec_before["last_activity"])
        ttl_runs = [r for r in self._runs() if r["event"] == "pause-ttl"]
        self.assertTrue(any(r.get("result") == "auto-resume"
                            and r.get("actor") == "secretary-backup" for r in ttl_runs))

    def test_stale_human_hard_pause_is_not_auto_resumed(self):
        self._claim_one()
        self._pause("hard", reason="long maintenance window", actor="vladmesh")
        self._age_pause_flag(pause.HARD_PAUSE_AUTO_RESUME_TTL_SECONDS + 1)
        self.worker.launched.clear()

        rc = dispatcher.precheck()

        self.assertEqual(rc, PRECHECK_SKIP)
        self.assertEqual(dispatcher.pause_status()["mode"], "freeze")
        self.assertEqual(self.worker.launched, [])
        runs = [r for r in self._runs() if r["event"] == "precheck"]
        self.assertEqual(runs[-1]["result"], "paused")

    def test_fresh_automation_hard_pause_waits_for_resume_or_ttl(self):
        self._claim_one()
        self._pause("hard", reason="secretary backup create", actor="secretary-backup")
        self.worker.launched.clear()

        rc = dispatcher.precheck()

        self.assertEqual(rc, PRECHECK_SKIP)
        self.assertEqual(dispatcher.pause_status()["mode"], "freeze")
        self.assertEqual(self.worker.launched, [])

    def test_precheck_soft_paused_with_inflight_card_still_dispatches(self):
        self._claim_one()
        self._pause("soft")
        rc = dispatcher.precheck()
        self.assertEqual(rc, 0)
        runs = [r for r in self._runs() if r["event"] == "precheck"]
        self.assertEqual(runs[-1]["result"], "dispatched")

    def test_precheck_soft_paused_with_nothing_inflight_skips_as_paused(self):
        self._pause("soft")
        rc = dispatcher.precheck()
        self.assertEqual(rc, PRECHECK_SKIP)
        runs = [r for r in self._runs() if r["event"] == "precheck"]
        self.assertEqual(runs[-1]["result"], "paused")

    def test_precheck_soft_paused_ignores_ready_cards_as_a_reason_to_dispatch(self):
        self._ready_card("A")   # a Ready card alone must not count as work while soft-paused
        self._pause("soft")
        rc = dispatcher.precheck()
        self.assertEqual(rc, PRECHECK_SKIP)

    # --- hard resume: relaunch + fresh watchdog clock ----------------------------------------
    def test_hard_resume_relaunches_the_worker_in_the_same_workspace(self):
        ref = self._claim_one(head="claude-opus")
        rec_before = dispatcher._load_cards()[ref]
        ws, head, worker_id = rec_before["workspace"], rec_before["head"], rec_before["worker"]
        self._pause("hard")
        self.worker.launched.clear()
        dispatcher.resume()
        self.assertEqual(len(self.worker.launched), 1)
        self.assertEqual(self.worker.launched[0]["ws"], ws)
        self.assertEqual(self.worker.launched[0]["head"], head)
        self.assertEqual(self.worker.launched[0]["worker"], worker_id)
        self._assert_not_paused(dispatcher.pause_status())

    def test_hard_resume_gives_the_worker_watchdog_a_fresh_window(self):
        ref = self._claim_one(head="claude-opus")
        records = dispatcher._load_cards()
        records[ref]["last_activity"] = time.time() - 10_000   # simulate the paused stretch
        dispatcher._save_cards(records)
        self._pause("hard")
        before_resume = time.time()
        dispatcher.resume()
        rec_after = dispatcher._load_cards()[ref]
        self.assertGreaterEqual(rec_after["last_activity"], before_resume)   # reset, not stale

    def test_hard_resume_relaunches_the_reviewer_in_the_same_workspace(self):
        ref = self._to_validate_with_reviewer()
        review_ws = dispatcher._load_cards()[ref]["review_ws"]
        self._pause("hard")
        launched_before = len(self.worker.launched)
        dispatcher.resume()
        self.assertEqual(len(self.worker.relaunched_reviewers), 1)
        self.assertEqual(self.worker.relaunched_reviewers[0]["ws"], review_ws)
        rec = dispatcher._load_cards()[ref]
        self.assertEqual(rec["review_handle"], self.worker.relaunched_reviewers[0]
                          and f"resumed-rev-handle-{rec['worker']}")
        # blocker B1 (review): the parked worker (kept only for a CI-red nudge) must NOT get a
        # fresh head while the reviewer is independently reviewing this exact branch.
        self.assertEqual(self.worker.launched[launched_before:], [])
        self.assertEqual(rec["handle"], "")

    def test_hard_resume_parks_rather_than_relaunches_a_validate_workers_terminal(self):
        ref = self._to_validate_with_reviewer()
        launched_before = len(self.worker.launched)
        self._pause("hard")
        dispatcher.resume()
        self.assertEqual(self.worker.launched[launched_before:], [])
        self.assertEqual(dispatcher._load_cards()[ref]["handle"], "")
        runs = [r for r in self._runs() if r["event"] == "resume"]
        self.assertIn(f"{ref}:worker", runs[-1]["parked"])

    def test_ci_red_after_hard_pause_relaunches_the_parked_worker_before_notifying(self):
        ref = self._to_validate_with_reviewer()
        self._pause("hard")
        dispatcher.resume()
        self.assertEqual(dispatcher._load_cards()[ref]["handle"], "")   # parked, not relaunched
        launched_before = len(self.worker.launched)
        self.worker.pr_status = {"merged": False, "state": "OPEN", "rollup": "FAILURE",
                                 "failed_job": "tests", "failed_log": "boom"}
        dispatcher.tick()
        self.assertEqual(self._column(ref), model.IN_PROGRESS)
        new_launches = self.worker.launched[launched_before:]
        self.assertEqual(len(new_launches), 1)                    # lazily relaunched right here
        rec = dispatcher._load_cards()[ref]
        expected_handle = f"handle-{new_launches[0]['worker']}"
        self.assertEqual(rec["handle"], expected_handle)
        self.assertEqual(self.worker.notified[-1][0], expected_handle)   # notified the fresh head

    def test_review_red_after_hard_pause_relaunches_the_parked_worker_before_notifying(self):
        ref = self._to_validate_with_reviewer()
        self._pause("hard")
        dispatcher.resume()
        self.assertEqual(dispatcher._load_cards()[ref]["handle"], "")   # parked, not relaunched
        launched_before = len(self.worker.launched)
        ops.verdict(ref, "red", "блокер: не так")
        dispatcher.tick()
        self.assertEqual(self._column(ref), model.IN_PROGRESS)
        new_launches = self.worker.launched[launched_before:]
        self.assertEqual(len(new_launches), 1)                    # lazily relaunched right here
        rec = dispatcher._load_cards()[ref]
        self.assertNotEqual(rec["handle"], "")

    def test_hard_resume_gives_the_reviewer_watchdog_a_fresh_window(self):
        ref = self._to_validate_with_reviewer()
        records = dispatcher._load_cards()
        records[ref]["review_activity"] = time.time() - 10_000   # simulate the paused stretch
        dispatcher._save_cards(records)
        self._pause("hard")
        before_resume = time.time()
        dispatcher.resume()
        rec_after = dispatcher._load_cards()[ref]
        self.assertGreaterEqual(rec_after["review_activity"], before_resume)   # reset, not stale

    def test_hard_resume_drops_stale_ci_pending_clock(self):
        ref = self._claim_one()
        ops.report(ref, "done", f"готово\nPR: {self.PR}")
        self.worker.pr_status = None
        dispatcher.tick()
        self.worker.pr_status = {"merged": False, "state": "OPEN", "rollup": "PENDING",
                                 "failed_job": None, "failed_log": None}
        dispatcher.tick()   # starts the ci_pending_since clock
        self.assertIn("ci_pending_since", dispatcher._load_cards()[ref])
        self._pause("hard")
        dispatcher.resume()
        self.assertNotIn("ci_pending_since", dispatcher._load_cards()[ref])
        validate.CI_PENDING_STALL_SECONDS = -1   # would escalate instantly on a stale clock
        dispatcher.tick()
        self.assertEqual(self._column(ref), "Validate")   # not Blocked by ci-pending-stall

    def test_hard_resume_skips_a_card_that_moved_on_while_paused(self):
        # A card pause() stopped whose column no longer matches once resume() runs (e.g. a human
        # moved it to Blocked by hand while paused) must not get a head relaunched into it.
        ref = self._claim_one()
        self._pause("hard")
        ops.move_card("steward", ref, "Blocked",
                      reason="test setup: escalation happens outside a dispatcher tick")
        self.worker.launched.clear()
        dispatcher.resume()
        self.assertEqual(self.worker.launched, [])
        runs = [r for r in self._runs() if r["event"] == "resume"]
        self.assertIn(f"{ref}:worker", runs[-1]["skipped"])

    # --- role guard ---------------------------------------------------------------------------
    def test_cli_pause_needs_po_or_steward_role(self):
        from triggered_agents.agents.pipeline import cli
        rc = cli.main(["--role", "worker", "pause", "--mode", "soft"])
        self.assertEqual(rc, 2)
        self._assert_not_paused(dispatcher.pause_status())
        rc = cli.main(["--role", "po", "pause", "drain", "--reason", "maintenance",
                       "--actor", "vladmesh"])
        self.assertEqual(rc, 0)
        status = dispatcher.pause_status()
        self.assertEqual(status["mode"], "drain")
        self.assertEqual(status["reason"], "maintenance")
        self.assertEqual(status["actor"], "vladmesh")

    def test_cli_legacy_mode_flag_still_works_but_reason_is_required(self):
        from triggered_agents.agents.pipeline import cli
        rc = cli.main(["--role", "po", "pause", "drain"])
        self.assertEqual(rc, 2)
        self._assert_not_paused(dispatcher.pause_status())

        rc = cli.main(["--role", "po", "pause", "--mode", "soft", "--reason", "legacy"])
        self.assertEqual(rc, 0)
        status = dispatcher.pause_status()
        self.assertEqual(status["mode"], "drain")
        self.assertEqual(status["actor"], "po")

    def test_cli_resume_needs_po_or_steward_role(self):
        self._pause("hard")
        from triggered_agents.agents.pipeline import cli
        rc = cli.main(["--role", "reviewer", "resume"])
        self.assertEqual(rc, 2)
        self.assertEqual(dispatcher.pause_status()["mode"], "freeze")
        rc = cli.main(["--role", "steward", "resume"])
        self.assertEqual(rc, 0)
        self._assert_not_paused(dispatcher.pause_status())

    def test_cli_pause_status_needs_no_role(self):
        from triggered_agents.agents.pipeline import cli
        rc = cli.main(["pause-status"])
        self.assertEqual(rc, 0)


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
        # Ассерты exec-дефолта не должны зависеть от env хоста: smoke гоняет юниты
        # в окружении диспетчера, где прод-флаг TA_CODEX_MODE=tui может быть включён.
        env_guard = mock.patch.dict("os.environ")
        env_guard.start()
        self.addCleanup(env_guard.stop)
        os.environ.pop("TA_CODEX_MODE", None)
        os.environ.pop("TA_CODEX_TUI", None)

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
        delay = mock.patch.object(worker, "TUI_DELIVERY_CHECK_DELAY_S", 0)
        delay.start()
        self.addCleanup(delay.stop)

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

    def test_launch_worker_codex_tui_waits_then_sends_initial_prompt(self):
        calls = []

        def fake_orca_json(args):
            calls.append(args)
            if args[:2] == ["terminal", "create"]:
                return {"terminal": {"handle": "term-tui"}}
            if args[:2] == ["terminal", "list"]:
                return {"terminals": [{"handle": "term-tui"}]}
            return {}

        with mock.patch.object(self.worker, "_orca_json", fake_orca_json):
            handle = self.worker.launch_worker("/ws/fresh", "codex-tui", "worker-1",
                                               "worker A-1: title")

        self.assertEqual(handle, "term-tui")
        create_i = next(i for i, c in enumerate(calls) if c[:2] == ["terminal", "create"])
        wait_i = next(i for i, c in enumerate(calls) if c[:2] == ["terminal", "wait"])
        send_i = next(i for i, c in enumerate(calls) if c[:2] == ["terminal", "send"])
        self.assertLess(create_i, wait_i)
        self.assertLess(wait_i, send_i)
        self.assertEqual(calls[wait_i], ["terminal", "wait", "--terminal", "term-tui",
                                         "--for", "tui-idle", "--timeout-ms",
                                         str(self.worker.TUI_IDLE_TIMEOUT_MS)])
        self.assertEqual(calls[send_i][calls[send_i].index("--terminal") + 1], "term-tui")
        self.assertIn("TASK.md", calls[send_i][calls[send_i].index("--text") + 1])
        self.assertNotIn("codex exec", calls[create_i][calls[create_i].index("--command") + 1])
        self.assertNotIn("--skip-git-repo-check",
                         calls[create_i][calls[create_i].index("--command") + 1])
        self.assertIn("'projects.\"/ws/fresh\".trust_level=\"trusted\"'",
                      calls[create_i][calls[create_i].index("--command") + 1])

    def test_launch_worker_codex_tui_resends_enter_when_prompt_stays_in_composer(self):
        calls = []
        reads = iter([
            {"terminal": {"tail": ["│ >_ OpenAI Codex │",
                                   "› Ты — воркер task-пайплайна. TASK.md gpt-5.5 · /ws/fresh"]}},
            {"terminal": {"tail": ["thinking"]}},
        ])

        def fake_orca_json(args):
            calls.append(args)
            if args[:2] == ["terminal", "create"]:
                return {"terminal": {"handle": "term-tui"}}
            if args[:2] == ["terminal", "read"]:
                return next(reads)
            if args[:2] == ["terminal", "list"]:
                return {"terminals": [{"handle": "term-tui"}]}
            return {}

        with mock.patch.object(self.worker, "_orca_json", fake_orca_json):
            handle = self.worker.launch_worker("/ws/fresh", "codex-tui", "worker-1",
                                               "worker A-1: title")

        self.assertEqual(handle, "term-tui")
        sends = [c for c in calls if c[:2] == ["terminal", "send"]]
        self.assertEqual(len(sends), 2)
        self.assertIn("TASK.md", sends[0][sends[0].index("--text") + 1])
        self.assertEqual(sends[1][sends[1].index("--text") + 1], "")

    def test_launch_worker_codex_tui_raises_when_prompt_stays_in_composer(self):
        calls = []

        def fake_orca_json(args):
            calls.append(args)
            if args[:2] == ["terminal", "create"]:
                return {"terminal": {"handle": "term-tui"}}
            if args[:2] == ["terminal", "read"]:
                return {"terminal": {"tail": [
                    "› Ты — воркер task-пайплайна. TASK.md gpt-5.5 · /ws/fresh",
                ]}}
            if args[:2] == ["terminal", "list"]:
                return {"terminals": [{"handle": "term-tui"}]}
            return {}

        with mock.patch.object(self.worker, "_orca_json", fake_orca_json), \
             self.assertRaises(self.worker.InjectDeliveryError):
            self.worker.launch_worker("/ws/fresh", "codex-tui", "worker-1",
                                      "worker A-1: title")

        sends = [c for c in calls if c[:2] == ["terminal", "send"]]
        self.assertEqual(len(sends), self.worker.TUI_DELIVERY_RETRIES + 1)
        self.assertEqual(sends[-1][sends[-1].index("--text") + 1], "")

    def test_launch_worker_codex_exec_does_not_send_post_start_prompt(self):
        self.worker.launch_worker("/ws/fresh", "codex", "worker-1", "worker A-1: title")
        self.assertFalse(any(c[:2] == ["terminal", "wait"] for c in self.calls))
        self.assertFalse(any(c[:2] == ["terminal", "send"] for c in self.calls))
        create = next(c for c in self.calls if c[:2] == ["terminal", "create"])
        self.assertIn("codex exec", create[create.index("--command") + 1])

    def test_relaunch_reviewer_codex_tui_uses_review_workspace_trust_override(self):
        calls = []

        def fake_orca_json(args):
            calls.append(args)
            if args[:2] == ["terminal", "create"]:
                return {"terminal": {"handle": "term-tui"}}
            if args[:2] == ["terminal", "list"]:
                return {"terminals": [{"handle": "term-tui"}]}
            return {}

        with mock.patch.object(self.worker, "_orca_json", fake_orca_json):
            handle = self.worker.relaunch_reviewer("/ws/rev", "rev-1", "review A-1: title",
                                                   "codex-reviewer-tui")

        self.assertEqual(handle, "term-tui")
        create = next(c for c in calls if c[:2] == ["terminal", "create"])
        command = create[create.index("--command") + 1]
        self.assertNotIn("codex exec", command)
        self.assertIn("'projects.\"/ws/rev\".trust_level=\"trusted\"'", command)
        send = next(c for c in calls if c[:2] == ["terminal", "send"])
        self.assertIn("REVIEW.md", send[send.index("--text") + 1])

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

    def test_spawn_reviewer_with_head_sha_pins_reset_to_it_not_fetch_head(self):
        # triggered-agents-240: a contrib card passes head_sha (the sha its report claimed) — the
        # reviewer must land on exactly that commit, not the branch's live tip.
        with mock.patch.object(self.worker, "_write_excluded", lambda *a: "/ws/fresh/REVIEW.md"):
            self.worker.spawn_reviewer("proj", "rev-1", "main", "REVIEW body", "review A-1: title",
                                       "pipeline/proj-1", "review/proj-1", head_sha="deadbeef")
        self.assertIn(["fetch", "origin", "pipeline/proj-1"], [a for _, a in self.git_calls])
        self.assertIn(["reset", "--hard", "deadbeef"], [a for _, a in self.git_calls])
        self.assertFalse(any(a == ["reset", "--hard", "FETCH_HEAD"] for _, a in self.git_calls))


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
