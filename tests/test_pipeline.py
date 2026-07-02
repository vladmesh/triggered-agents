"""Unit tests for the pipeline agent — stdlib unittest, no network.

TA_STATE is pointed at a tempdir BEFORE importing any triggered_agents module, because
runtime/state.py reads it at import time and claim uses a real host lock. The Kanboard
transport is replaced per test by FakeBoard.call, so nothing leaves the process.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

_STATE_DIR = tempfile.mkdtemp(prefix="ta-pipeline-test-")
os.environ["TA_STATE"] = _STATE_DIR
os.environ.pop("KANBOARD_ADMIN_USER", None)

from triggered_agents.agents.board.kanboard import KanboardError  # noqa: E402
from triggered_agents.agents.pipeline import model, ops  # noqa: E402


class FakeBoard:
    """In-memory Kanboard stand-in dispatched by RPC method name; records every call."""

    def __init__(self, columns=None):
        self.name = model.BOARD_NAME
        self.pid = 1
        titles = columns if columns is not None else model.COLUMNS
        self.columns = [{"id": 101 + i, "title": t, "position": i + 1} for i, t in enumerate(titles)]
        self.swimlanes = [{"id": 1, "name": "Default swimlane"}]
        self.tasks: dict[int, dict] = {}
        self.metadata: dict[int, dict] = {}
        self.comments: dict[int, list] = {}
        self._next_task = 1
        self._next_sw = 2
        self._next_col = 200
        self.calls: list[tuple[str, dict]] = []
        self.move_fails = False  # make moveTaskPosition return False (Kanboard-style failure)

    # test helpers -----------------------------------------------------------
    def add_task(self, title, column, swimlane="personal_site", reference=None,
                 meta=None, is_active=1):
        tid = self._next_task
        self._next_task += 1
        col_id = next(c["id"] for c in self.columns if c["title"] == column)
        sw_id = self._swimlane_id(swimlane)
        self.tasks[tid] = {
            "id": tid, "title": title, "project_id": self.pid, "column_id": col_id,
            "swimlane_id": sw_id, "description": "",
            "reference": reference if reference is not None else f"{swimlane}-{tid}",
            "is_active": is_active, "position": 1,
        }
        self.metadata[tid] = dict(meta or {})
        return self.tasks[tid]["reference"]

    def _swimlane_id(self, name):
        for s in self.swimlanes:
            if s["name"] == name:
                return s["id"]
        sid = self._next_sw
        self._next_sw += 1
        self.swimlanes.append({"id": sid, "name": name})
        return sid

    def method_calls(self, method):
        return [p for m, p in self.calls if m == method]

    def _column_title_for(self, tid):
        col_id = self.tasks[tid]["column_id"]
        return next(c["title"] for c in self.columns if c["id"] == col_id)

    def call_index(self, method):
        return next(i for i, (m, _) in enumerate(self.calls) if m == method)

    # RPC dispatch -----------------------------------------------------------
    def call(self, method, **p):
        self.calls.append((method, p))
        return getattr(self, "m_" + method)(**p)

    def m_getAllProjects(self):
        return [{"id": self.pid, "name": self.name}]

    def m_createProject(self, name):
        self.name = name
        return self.pid

    def m_getColumns(self, project_id):
        return list(self.columns)

    def m_getActiveSwimlanes(self, project_id):
        return list(self.swimlanes)

    def m_addSwimlane(self, project_id, name):
        return self._swimlane_id(name)

    def m_createTask(self, title, project_id, column_id, swimlane_id, description="",
                     reference=None):
        tid = self._next_task
        self._next_task += 1
        self.tasks[tid] = {
            "id": tid, "title": title, "project_id": project_id, "column_id": column_id,
            "swimlane_id": swimlane_id, "description": description,
            "reference": reference or "", "is_active": 1, "position": 1,
        }
        self.metadata.setdefault(tid, {})
        return tid

    def m_updateTask(self, id, **fields):
        self.tasks[int(id)].update(fields)
        return True

    def m_saveTaskMetadata(self, task_id, values):
        self.metadata.setdefault(int(task_id), {}).update({k: str(v) for k, v in values.items()})
        return True

    def m_getTaskMetadata(self, task_id):
        return dict(self.metadata.get(int(task_id), {}))

    def m_getTaskByReference(self, project_id, reference):
        for t in self.tasks.values():
            if t.get("reference") == reference:
                return dict(t)
        return None

    def m_moveTaskPosition(self, project_id, task_id, column_id, position, swimlane_id):
        if self.move_fails:
            return False
        self.tasks[int(task_id)]["column_id"] = column_id
        self.tasks[int(task_id)]["swimlane_id"] = swimlane_id
        self.tasks[int(task_id)]["position"] = position
        return True

    def m_getAllTasks(self, project_id, status_id=1):
        return [dict(t) for t in self.tasks.values() if t["is_active"] == status_id]

    def m_createComment(self, task_id, user_id, content):
        self.comments.setdefault(int(task_id), []).append(
            {"id": len(self.comments.get(int(task_id), [])) + 1,
             "date_creation": 111, "user_id": user_id, "comment": content})
        return True

    def m_getAllComments(self, task_id):
        return list(self.comments.get(int(task_id), []))

    def m_closeTask(self, task_id):
        self.tasks[int(task_id)]["is_active"] = 0
        return True


class PatchedBoardTest(unittest.TestCase):
    """Base: install a FakeBoard as the transport for the ops module."""

    def make_board(self, **kw):
        board = FakeBoard(**kw)
        patcher = mock.patch("triggered_agents.agents.pipeline.ops.call", board.call)
        patcher.start()
        self.addCleanup(patcher.stop)
        return board


class TestMatrix(unittest.TestCase):
    def test_all_allowed_transitions_pass(self):
        for role, pairs in model.TRANSITIONS.items():
            for frm, to in pairs:
                model.check_move(role, frm, to)  # must not raise

    def test_worker_moves_nothing(self):
        with self.assertRaises(model.GuardError):
            model.check_move("worker", "Идеи", "Ready")
        with self.assertRaises(model.GuardError):
            model.check_move("worker", "Validate", "Done")

    def test_po_cannot_touch_in_progress_side(self):
        with self.assertRaises(model.GuardError):
            model.check_move("po", "In progress", "Validate")

    def test_dispatcher_cannot_ready(self):
        with self.assertRaises(model.GuardError):
            model.check_move("dispatcher", "Идеи", "Ready")

    def test_unknown_role_and_column(self):
        with self.assertRaises(model.GuardError):
            model.check_move("nobody", "Идеи", "Ready")
        with self.assertRaises(model.GuardError):
            model.check_move("po", "Идеи", "Nope")

    def test_ready_to_in_progress_via_move_hints_claim(self):
        with self.assertRaises(model.GuardError) as ctx:
            model.check_move("dispatcher", "Ready", "In progress")
        self.assertIn("claim", str(ctx.exception))


class TestCreate(PatchedBoardTest):
    def test_bad_type_raises(self):
        self.make_board()
        with self.assertRaises(model.GuardError):
            ops.create_card("personal_site", "nope", "T")

    def test_bad_column_raises(self):
        self.make_board()
        with self.assertRaises(model.GuardError):
            ops.create_card("personal_site", "code", "T", column="Done")

    def test_auto_ref_derived_and_updated(self):
        board = self.make_board()
        out = ops.create_card("personal_site", "code", "Build the thing")
        self.assertEqual(out["reference"], f"personal_site-{out['id']}")
        upd = board.method_calls("updateTask")
        self.assertEqual(len(upd), 1)
        self.assertEqual(upd[0]["reference"], out["reference"])
        meta = board.metadata[out["id"]]
        self.assertEqual(meta[model.META_TASK_TYPE], "code")
        self.assertEqual(meta[model.META_PROJECT], "personal_site")

    def test_explicit_ref_skips_update(self):
        board = self.make_board()
        out = ops.create_card("personal_site", "code", "T", ref="my-ref", column="Ready")
        self.assertEqual(out["reference"], "my-ref")
        self.assertEqual(board.method_calls("updateTask"), [])


class TestClaim(PatchedBoardTest):
    def test_happy_path_saves_then_moves(self):
        board = self.make_board()
        ref = board.add_task("A", "Ready", meta={model.META_TASK_TYPE: "code",
                                                 model.META_PROJECT: "personal_site"})
        out = ops.claim_card(ref, "w1")
        self.assertEqual(out, {"action": "claimed", "reference": ref, "worker": "w1"})
        tid = next(t["id"] for t in board.tasks.values() if t["reference"] == ref)
        self.assertEqual(board.metadata[tid][model.META_CLAIM], "w1")
        # In progress now, and metadata was saved before the move.
        self.assertEqual(board._column_title_for(tid), model.IN_PROGRESS)
        self.assertLess(board.call_index("saveTaskMetadata"), board.call_index("moveTaskPosition"))

    def test_refuses_when_not_ready(self):
        board = self.make_board()
        ref = board.add_task("A", "Идеи", meta={model.META_TASK_TYPE: "code",
                                                model.META_PROJECT: "personal_site"})
        with self.assertRaises(model.GuardError):
            ops.claim_card(ref, "w1")

    def test_refuses_when_already_claimed(self):
        board = self.make_board()
        ref = board.add_task("A", "Ready", meta={model.META_TASK_TYPE: "code",
                                                 model.META_PROJECT: "personal_site",
                                                 model.META_CLAIM: "w0"})
        with self.assertRaises(model.GuardError):
            ops.claim_card(ref, "w1")

    def test_refuses_when_blocked_by_not_done(self):
        board = self.make_board()
        pred = board.add_task("A", "Ready", meta={model.META_TASK_TYPE: "code",
                                                  model.META_PROJECT: "personal_site"})
        ref = board.add_task("B", "Ready", meta={model.META_TASK_TYPE: "code",
                                                 model.META_PROJECT: "personal_site",
                                                 model.META_BLOCKED_BY: pred})
        with self.assertRaises(model.GuardError):
            ops.claim_card(ref, "w1")

    def test_allows_when_blocked_by_done_column(self):
        board = self.make_board()
        pred = board.add_task("A", "Done", meta={model.META_TASK_TYPE: "code",
                                                 model.META_PROJECT: "personal_site"})
        ref = board.add_task("B", "Ready", meta={model.META_TASK_TYPE: "code",
                                                 model.META_PROJECT: "personal_site",
                                                 model.META_BLOCKED_BY: pred})
        ops.claim_card(ref, "w1")  # must not raise

    def test_allows_when_blocked_by_closed(self):
        board = self.make_board()
        pred = board.add_task("A", "Validate", is_active=0,
                              meta={model.META_TASK_TYPE: "code", model.META_PROJECT: "personal_site"})
        ref = board.add_task("B", "Ready", meta={model.META_TASK_TYPE: "code",
                                                 model.META_PROJECT: "personal_site",
                                                 model.META_BLOCKED_BY: pred})
        ops.claim_card(ref, "w1")  # must not raise

    def test_refuses_nonexistent_blocked_by(self):
        board = self.make_board()
        ref = board.add_task("B", "Ready", meta={model.META_TASK_TYPE: "code",
                                                 model.META_PROJECT: "personal_site",
                                                 model.META_BLOCKED_BY: "ghost"})
        with self.assertRaises(model.GuardError):
            ops.claim_card(ref, "w1")

    def test_refuses_second_code_card_in_progress(self):
        board = self.make_board()
        board.add_task("A", "In progress", meta={model.META_TASK_TYPE: "code",
                                                 model.META_PROJECT: "personal_site",
                                                 model.META_CLAIM: "w0"})
        ref = board.add_task("B", "Ready", meta={model.META_TASK_TYPE: "code",
                                                 model.META_PROJECT: "personal_site"})
        with self.assertRaises(model.GuardError):
            ops.claim_card(ref, "w1")

    def test_refuses_second_code_card_in_validate(self):
        board = self.make_board()
        board.add_task("A", "Validate", meta={model.META_TASK_TYPE: "code",
                                              model.META_PROJECT: "personal_site",
                                              model.META_CLAIM: "w0"})
        ref = board.add_task("B", "Ready", meta={model.META_TASK_TYPE: "code",
                                                 model.META_PROJECT: "personal_site"})
        with self.assertRaises(model.GuardError):
            ops.claim_card(ref, "w1")

    def test_allows_research_parallel_with_code(self):
        board = self.make_board()
        board.add_task("A", "In progress", meta={model.META_TASK_TYPE: "code",
                                                 model.META_PROJECT: "personal_site",
                                                 model.META_CLAIM: "w0"})
        ref = board.add_task("R", "Ready", meta={model.META_TASK_TYPE: "research",
                                                 model.META_PROJECT: "personal_site"})
        ops.claim_card(ref, "w1", cap=5)  # not serialized, must not raise

    def test_refuses_at_cap(self):
        board = self.make_board()
        board.add_task("A", "In progress", meta={model.META_TASK_TYPE: "research",
                                                 model.META_PROJECT: "other",
                                                 model.META_CLAIM: "w0"})
        ref = board.add_task("B", "Ready", meta={model.META_TASK_TYPE: "research",
                                                 model.META_PROJECT: "personal_site"})
        with self.assertRaises(model.GuardError):
            ops.claim_card(ref, "w1", cap=1)

    def test_refuses_at_cap_counting_validate(self):
        # A Validate card still owns its worker session, so it occupies a cap slot.
        board = self.make_board()
        board.add_task("A", "Validate", meta={model.META_TASK_TYPE: "research",
                                              model.META_PROJECT: "other",
                                              model.META_CLAIM: "w0"})
        ref = board.add_task("B", "Ready", meta={model.META_TASK_TYPE: "research",
                                                 model.META_PROJECT: "personal_site"})
        with self.assertRaises(model.GuardError) as ctx:
            ops.claim_card(ref, "w1", cap=1)
        self.assertIn("cap reached", str(ctx.exception))

    def test_blocked_to_ready_clears_claim_and_reclaim_succeeds(self):
        board = self.make_board()
        ref = board.add_task("A", "In progress", meta={model.META_TASK_TYPE: "code",
                                                       model.META_PROJECT: "personal_site",
                                                       model.META_CLAIM: "w1"})
        ops.move_card("dispatcher", ref, "Blocked")
        ops.move_card("po", ref, "Ready")
        tid = next(t["id"] for t in board.tasks.values() if t["reference"] == ref)
        self.assertFalse(board.metadata[tid].get(model.META_CLAIM))
        out = ops.claim_card(ref, "w2")
        self.assertEqual(out["worker"], "w2")
        self.assertEqual(board.metadata[tid][model.META_CLAIM], "w2")

    def test_failed_move_raises_kanboard_error(self):
        board = self.make_board()
        ref = board.add_task("A", "Ready", meta={model.META_TASK_TYPE: "code",
                                                 model.META_PROJECT: "personal_site"})
        board.move_fails = True
        with self.assertRaises(KanboardError):
            ops.claim_card(ref, "w1")


class TestReport(PatchedBoardTest):
    def test_blocked_without_body_raises(self):
        board = self.make_board()
        ref = board.add_task("A", "In progress")
        with self.assertRaises(model.GuardError):
            ops.report(ref, "blocked", "")

    def test_done_posts_marker_comment(self):
        board = self.make_board()
        ref = board.add_task("A", "In progress")
        ops.report(ref, "done", "shipped")
        posted = board.method_calls("createComment")
        self.assertEqual(len(posted), 1)
        self.assertEqual(posted[0]["user_id"], 0)
        self.assertTrue(posted[0]["content"].startswith(f"[{model.MARKER_REPORT_DONE}]"))


if __name__ == "__main__":
    unittest.main()
