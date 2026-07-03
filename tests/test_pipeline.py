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

    def test_reviewer_moves_nothing(self):
        with self.assertRaises(model.GuardError):
            model.check_move("reviewer", "Validate", "Done")
        with self.assertRaises(model.GuardError):
            model.check_move("reviewer", "In progress", "Validate")

    def test_po_cannot_touch_in_progress_side(self):
        with self.assertRaises(model.GuardError):
            model.check_move("po", "In progress", "Validate")

    def test_dispatcher_cannot_ready(self):
        with self.assertRaises(model.GuardError):
            model.check_move("dispatcher", "Идеи", "Ready")

    def test_steward_gets_every_po_transition_plus_the_override(self):
        steward_escalations = {
            ("Идеи", "Blocked"), ("Ready", "Blocked"),
            ("In progress", "Blocked"), ("Validate", "Blocked"),
        }
        self.assertEqual(model.TRANSITIONS["steward"],
                         model.TRANSITIONS["po"] | {model.STEWARD_OVERRIDE} | steward_escalations)

    def test_only_steward_may_move_blocked_to_done(self):
        model.check_move("steward", "Blocked", "Done")  # must not raise
        for role in ("po", "dispatcher", "worker", "reviewer"):
            with self.assertRaises(model.GuardError):
                model.check_move(role, "Blocked", "Done")

    def test_steward_may_escalate_any_active_card_straight_to_blocked(self):
        for column in ("Идеи", "Ready", "In progress", "Validate"):
            model.check_move("steward", column, "Blocked")  # must not raise
        with self.assertRaises(model.GuardError):
            model.check_move("steward", "Done", "Blocked")
        for role in ("po", "worker", "reviewer"):
            with self.assertRaises(model.GuardError):
                model.check_move(role, "Ready", "Blocked")

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

    def test_slug_is_stored_in_metadata(self):
        board = self.make_board()
        out = ops.create_card("personal_site", "code", "T", slug="teardown-done-workspaces")
        self.assertEqual(board.metadata[out["id"]][model.META_SLUG], "teardown-done-workspaces")

    def test_missing_slug_leaves_metadata_unset(self):
        board = self.make_board()
        out = ops.create_card("personal_site", "code", "T")
        self.assertNotIn(model.META_SLUG, board.metadata[out["id"]])

    def test_bad_slug_raises_and_creates_nothing(self):
        board = self.make_board()
        for bad in ("Has-Caps", "with_underscore", "a" * 31, "", "имя"):
            with self.assertRaises(model.GuardError):
                ops.create_card("personal_site", "code", "T", slug=bad)
        self.assertEqual(board.tasks, {})

    def test_head_is_stored_in_metadata(self):
        board = self.make_board()
        out = ops.create_card("personal_site", "code", "T", head="claude-opus")
        self.assertEqual(board.metadata[out["id"]][model.META_HEAD], "claude-opus")

    def test_unknown_head_raises_and_creates_nothing(self):
        board = self.make_board()
        with self.assertRaises(model.GuardError):
            ops.create_card("personal_site", "code", "T", head="codex-nope")
        self.assertEqual(board.tasks, {})

    def test_steward_role_scrubs_title_and_description(self):
        """2026-07-04 review, triggered-agents-244 blocker B1 (third round): SKILL.md sends
        steward through exactly this path (create in Идеи/Ready, then escalate to Blocked) to
        write up an anomaly it dug into via transcripts/journalctl/env — same reasoning as the
        scrub already applied to steward's add_comment."""
        board = self.make_board()
        ops.create_card(
            "triggered-agents", "code", "секрет API_TOKEN=supersecretvalue123 в заголовке",
            description="тело: KANBOARD_SECRET=anothersecretvalue999", role="steward")
        created = board.method_calls("createTask")
        self.assertNotIn("supersecretvalue123", created[0]["title"])
        self.assertNotIn("anothersecretvalue999", created[0]["description"])

    def test_po_role_is_not_scrubbed(self):
        board = self.make_board()
        ops.create_card("personal_site", "code", "token KANBOARD_API_TOKEN=supersecretvalue123",
                        role="po")
        created = board.method_calls("createTask")
        self.assertIn("supersecretvalue123", created[0]["title"])

    def test_no_role_is_not_scrubbed_backward_compatible(self):
        board = self.make_board()
        ops.create_card("personal_site", "code", "token KANBOARD_API_TOKEN=supersecretvalue123")
        created = board.method_calls("createTask")
        self.assertIn("supersecretvalue123", created[0]["title"])


class TestUpdate(PatchedBoardTest):
    def test_partial_update_touches_only_given_field(self):
        board = self.make_board()
        ref = board.add_task("A", "Ready", meta={model.META_TASK_TYPE: "code",
                                                 model.META_PROJECT: "personal_site",
                                                 model.META_SLUG: "old-slug",
                                                 model.META_HEAD: "claude-sonnet"})
        out = ops.update_card("po", ref, head="claude-opus")
        tid = next(t["id"] for t in board.tasks.values() if t["reference"] == ref)
        self.assertEqual(board.metadata[tid][model.META_SLUG], "old-slug")
        self.assertEqual(board.metadata[tid][model.META_HEAD], "claude-opus")
        self.assertEqual(out, {"action": "updated", "reference": ref,
                               "slug": "old-slug", "head": "claude-opus", "blocked_by": ""})

    def test_unknown_head_raises_and_writes_nothing(self):
        board = self.make_board()
        ref = board.add_task("A", "Ready", meta={model.META_TASK_TYPE: "code",
                                                 model.META_PROJECT: "personal_site"})
        with self.assertRaises(model.GuardError):
            ops.update_card("po", ref, head="nope-not-a-profile")
        self.assertEqual(board.method_calls("saveTaskMetadata"), [])

    def test_valid_blocked_by_is_stored(self):
        board = self.make_board()
        pred = board.add_task("A", "Done", meta={model.META_TASK_TYPE: "code",
                                                  model.META_PROJECT: "personal_site"})
        ref = board.add_task("B", "Ready", meta={model.META_TASK_TYPE: "code",
                                                 model.META_PROJECT: "personal_site"})
        out = ops.update_card("po", ref, blocked_by=pred)
        self.assertEqual(out["blocked_by"], pred)
        tid = next(t["id"] for t in board.tasks.values() if t["reference"] == ref)
        self.assertEqual(board.metadata[tid][model.META_BLOCKED_BY], pred)

    def test_bad_slug_raises_and_writes_nothing(self):
        board = self.make_board()
        ref = board.add_task("A", "Ready", meta={model.META_TASK_TYPE: "code",
                                                 model.META_PROJECT: "personal_site",
                                                 model.META_SLUG: "old-slug"})
        with self.assertRaises(model.GuardError):
            ops.update_card("po", ref, slug="Not Valid")
        tid = next(t["id"] for t in board.tasks.values() if t["reference"] == ref)
        self.assertEqual(board.metadata[tid][model.META_SLUG], "old-slug")
        self.assertEqual(board.method_calls("saveTaskMetadata"), [])

    def test_nonexistent_blocked_by_raises_and_writes_nothing(self):
        board = self.make_board()
        ref = board.add_task("B", "Ready", meta={model.META_TASK_TYPE: "code",
                                                 model.META_PROJECT: "personal_site"})
        with self.assertRaises(model.GuardError):
            ops.update_card("po", ref, blocked_by="ghost")
        self.assertEqual(board.method_calls("saveTaskMetadata"), [])

    def test_role_other_than_po_raises_and_writes_nothing(self):
        board = self.make_board()
        ref = board.add_task("A", "Ready", meta={model.META_TASK_TYPE: "code",
                                                 model.META_PROJECT: "personal_site"})
        for role in ("worker", "reviewer", "dispatcher"):
            with self.assertRaises(model.GuardError):
                ops.update_card(role, ref, slug="new-slug")
        self.assertEqual(board.method_calls("saveTaskMetadata"), [])

    def test_column_and_claim_untouched(self):
        board = self.make_board()
        ref = board.add_task("A", "In progress", meta={model.META_TASK_TYPE: "code",
                                                       model.META_PROJECT: "personal_site",
                                                       model.META_CLAIM: "w1"})
        ops.update_card("po", ref, head="claude-opus", slug="new-slug",
                        blocked_by=None)
        tid = next(t["id"] for t in board.tasks.values() if t["reference"] == ref)
        self.assertEqual(board._column_title_for(tid), "In progress")
        self.assertEqual(board.metadata[tid][model.META_CLAIM], "w1")
        self.assertEqual(board.method_calls("moveTaskPosition"), [])


class TestCliUpdate(PatchedBoardTest):
    """--ref/--slug/--head/--blocked-by reach ops through the cli seam; role is enforced in
    ops (GuardError -> exit 3), not by the cli's own role gate."""

    def setUp(self):
        from triggered_agents.agents.pipeline import cli
        self.cli = cli

    def test_po_update_ok(self):
        board = self.make_board()
        ref = board.add_task("A", "Ready", meta={model.META_TASK_TYPE: "code",
                                                 model.META_PROJECT: "personal_site"})
        rc = self.cli.main(["--role", "po", "update", "--ref", ref, "--slug", "new-slug"])
        self.assertEqual(rc, 0)
        tid = next(t["id"] for t in board.tasks.values() if t["reference"] == ref)
        self.assertEqual(board.metadata[tid][model.META_SLUG], "new-slug")

    def test_worker_update_exits_3(self):
        board = self.make_board()
        ref = board.add_task("A", "Ready", meta={model.META_TASK_TYPE: "code",
                                                 model.META_PROJECT: "personal_site"})
        rc = self.cli.main(["--role", "worker", "update", "--ref", ref, "--slug", "new-slug"])
        self.assertEqual(rc, 3)
        self.assertEqual(board.method_calls("saveTaskMetadata"), [])


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

    def test_known_head_claims_fine(self):
        board = self.make_board()
        ref = board.add_task("A", "Ready", meta={model.META_TASK_TYPE: "code",
                                                 model.META_PROJECT: "personal_site",
                                                 model.META_HEAD: "hermes-flash"})
        ops.claim_card(ref, "w1")  # must not raise

    def test_refuses_unknown_head_with_clear_message(self):
        board = self.make_board()
        ref = board.add_task("A", "Ready", meta={model.META_TASK_TYPE: "code",
                                                 model.META_PROJECT: "personal_site",
                                                 model.META_HEAD: "codex-nope"})
        with self.assertRaises(model.GuardError) as ctx:
            ops.claim_card(ref, "w1")
        self.assertIn("codex-nope", str(ctx.exception))
        tid = next(t["id"] for t in board.tasks.values() if t["reference"] == ref)
        self.assertEqual(board._column_title_for(tid), "Ready")  # claim never touched the card

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


class TestRetryState(PatchedBoardTest):
    """Watchdog retry bookkeeping (model.META_RETRY_*): reset on any arrival in Ready, restated by
    set_retry_state, readable back via get_metadata."""

    def test_ready_arrival_resets_retry_metadata_from_any_source_column(self):
        board = self.make_board()
        ref = board.add_task("A", "In progress",
                             meta={model.META_TASK_TYPE: "code", model.META_PROJECT: "personal_site",
                                   model.META_CLAIM: "w1", model.META_RETRY_SAME: "1",
                                   model.META_RETRY_SWITCH: "1", model.META_RETRY_HEADS: "a,b"})
        ops.move_card("dispatcher", ref, "Ready")
        meta = ops.get_metadata(ref)
        self.assertFalse(meta.get(model.META_CLAIM))
        self.assertFalse(meta.get(model.META_RETRY_SAME))
        self.assertFalse(meta.get(model.META_RETRY_SWITCH))
        self.assertFalse(meta.get(model.META_RETRY_HEADS))

    def test_set_retry_state_stamps_counters_and_history(self):
        board = self.make_board()
        ref = board.add_task("A", "Ready", meta={model.META_TASK_TYPE: "code",
                                                 model.META_PROJECT: "personal_site"})
        ops.set_retry_state(ref, retry_same=1, retry_switch=0, retry_heads="claude-sonnet")
        meta = ops.get_metadata(ref)
        self.assertEqual(meta[model.META_RETRY_SAME], "1")
        self.assertEqual(meta[model.META_RETRY_SWITCH], "0")
        self.assertEqual(meta[model.META_RETRY_HEADS], "claude-sonnet")
        self.assertNotIn(model.META_HEAD, meta)   # head untouched when not passed

    def test_set_retry_state_updates_head_on_switch(self):
        board = self.make_board()
        ref = board.add_task("A", "Ready", meta={model.META_TASK_TYPE: "code",
                                                 model.META_PROJECT: "personal_site",
                                                 model.META_HEAD: "claude-sonnet"})
        ops.set_retry_state(ref, retry_same=1, retry_switch=1,
                            retry_heads="claude-sonnet,hermes-flash", head="hermes-flash")
        meta = ops.get_metadata(ref)
        self.assertEqual(meta[model.META_HEAD], "hermes-flash")

    def test_set_retry_state_survives_being_written_right_after_a_ready_reset(self):
        # Exactly the sequence dispatcher._watchdog_retry runs: move_card(...,'Ready') resets the
        # fields to defaults, set_retry_state's write right after must be the value that sticks.
        board = self.make_board()
        ref = board.add_task("A", "In progress",
                             meta={model.META_TASK_TYPE: "code", model.META_PROJECT: "personal_site",
                                   model.META_CLAIM: "w1"})
        ops.move_card("dispatcher", ref, "Ready")
        ops.set_retry_state(ref, retry_same=1, retry_switch=0, retry_heads="claude-sonnet")
        meta = ops.get_metadata(ref)
        self.assertEqual(meta[model.META_RETRY_SAME], "1")


class TestStewardOverride(PatchedBoardTest):
    """steward's Blocked -> Done override (model.STEWARD_OVERRIDE): needs a non-empty
    justification, posted as a [steward:blocked-done] comment in the same ops.move_card call."""

    def test_moves_and_posts_justification_comment(self):
        board = self.make_board()
        ref = board.add_task("A", "Blocked", meta={model.META_TASK_TYPE: "code",
                                                   model.META_PROJECT: "personal_site"})
        out = ops.move_card("steward", ref, "Done", reason="vladmesh approved skipping review")
        self.assertEqual(out, {"action": "moved", "reference": ref, "from": "Blocked", "to": "Done"})
        tid = next(t["id"] for t in board.tasks.values() if t["reference"] == ref)
        self.assertEqual(board._column_title_for(tid), "Done")
        posted = board.method_calls("createComment")
        self.assertEqual(len(posted), 1)
        self.assertEqual(posted[0]["content"],
                         f"[{model.MARKER_STEWARD_OVERRIDE}]\nvladmesh approved skipping review")

    def test_empty_reason_raises_and_moves_nothing(self):
        board = self.make_board()
        ref = board.add_task("A", "Blocked", meta={model.META_TASK_TYPE: "code",
                                                   model.META_PROJECT: "personal_site"})
        with self.assertRaises(model.GuardError):
            ops.move_card("steward", ref, "Done")
        tid = next(t["id"] for t in board.tasks.values() if t["reference"] == ref)
        self.assertEqual(board._column_title_for(tid), "Blocked")
        self.assertEqual(board.method_calls("createComment"), [])

    def test_whitespace_only_reason_raises(self):
        board = self.make_board()
        ref = board.add_task("A", "Blocked", meta={model.META_TASK_TYPE: "code",
                                                   model.META_PROJECT: "personal_site"})
        with self.assertRaises(model.GuardError):
            ops.move_card("steward", ref, "Done", reason="   ")

    def test_other_roles_still_forbidden_even_with_a_reason(self):
        board = self.make_board()
        ref = board.add_task("A", "Blocked", meta={model.META_TASK_TYPE: "code",
                                                   model.META_PROJECT: "personal_site"})
        for role in ("po", "dispatcher", "worker", "reviewer"):
            with self.assertRaises(model.GuardError):
                ops.move_card(role, ref, "Done", reason="whatever")

    def test_reason_ignored_for_ordinary_transitions(self):
        board = self.make_board()
        ref = board.add_task("A", "Blocked", meta={model.META_TASK_TYPE: "code",
                                                   model.META_PROJECT: "personal_site"})
        ops.move_card("steward", ref, "Ready")  # no reason needed, not the override pair
        tid = next(t["id"] for t in board.tasks.values() if t["reference"] == ref)
        self.assertEqual(board._column_title_for(tid), "Ready")
        self.assertEqual(board.method_calls("createComment"), [])


class TestAddComment(PatchedBoardTest):
    """2026-07-04 review, triggered-agents-244 remark Z1: steward reads more raw system surface
    than any other role (transcripts, journalctl, env files) and could quote a secret by
    accident — its comments get the same scrub_secrets backstop as the reviewer's verdict."""

    def test_steward_comment_is_scrubbed(self):
        board = self.make_board()
        ref = board.add_task("A", "Blocked")
        ops.add_comment("steward", ref, "нашёл в логе KANBOARD_API_TOKEN=supersecretvalue123")
        posted = board.method_calls("createComment")
        self.assertNotIn("supersecretvalue123", posted[0]["content"])

    def test_other_roles_are_not_scrubbed(self):
        board = self.make_board()
        ref = board.add_task("A", "Blocked")
        ops.add_comment("dispatcher", ref, "token KANBOARD_API_TOKEN=supersecretvalue123")
        posted = board.method_calls("createComment")
        self.assertIn("supersecretvalue123", posted[0]["content"])

    def test_ordinary_steward_comment_text_survives_unchanged(self):
        board = self.make_board()
        ref = board.add_task("A", "Blocked")
        ops.add_comment("steward", ref, "разобрался, ложное срабатывание")
        posted = board.method_calls("createComment")
        self.assertEqual(posted[0]["content"], "[steward]\nразобрался, ложное срабатывание")


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


class TestVerdict(PatchedBoardTest):
    def test_red_without_body_raises(self):
        board = self.make_board()
        ref = board.add_task("A", "Validate")
        with self.assertRaises(model.GuardError):
            ops.verdict(ref, "red", "")

    def test_bad_kind_raises(self):
        board = self.make_board()
        ref = board.add_task("A", "Validate")
        with self.assertRaises(model.GuardError):
            ops.verdict(ref, "maybe", "body")

    def test_green_posts_marker_comment(self):
        board = self.make_board()
        ref = board.add_task("A", "Validate")
        ops.verdict(ref, "green", "все criteria реально выполнены")
        posted = board.method_calls("createComment")
        self.assertEqual(len(posted), 1)
        self.assertTrue(posted[0]["content"].startswith(f"[{model.MARKER_REVIEW_GREEN}]"))

    def test_red_posts_marker_comment(self):
        board = self.make_board()
        ref = board.add_task("A", "Validate")
        ops.verdict(ref, "red", "блокер: foo.py")
        posted = board.method_calls("createComment")
        self.assertTrue(posted[0]["content"].startswith(f"[{model.MARKER_REVIEW_RED}]"))

    def test_verdict_body_is_scrubbed(self):
        board = self.make_board()
        ref = board.add_task("A", "Validate")
        ops.verdict(ref, "red", "нашёл в логе KANBOARD_API_TOKEN=supersecretvalue123 — утечка")
        posted = board.method_calls("createComment")
        self.assertNotIn("supersecretvalue123", posted[0]["content"])


class TestReviewerIdea(PatchedBoardTest):
    def test_creates_card_in_ideas(self):
        board = self.make_board()
        out = ops.reviewer_idea("personal_site", "pre-existing долг в X", "детали")
        self.assertEqual(out["column"], "Идеи")

    def test_title_and_description_scrubbed(self):
        board = self.make_board()
        ops.reviewer_idea("personal_site", "секрет API_TOKEN=supersecretvalue123 в конфиге",
                          "тело: KANBOARD_SECRET=anothersecretvalue999")
        created = board.method_calls("createTask")
        blob = created[0]["title"] + created[0].get("description", "")
        self.assertNotIn("supersecretvalue123", blob)
        self.assertNotIn("anothersecretvalue999", blob)

    def test_slug_passed_through_to_metadata(self):
        board = self.make_board()
        out = ops.reviewer_idea("personal_site", "T", slug="found-a-bug")
        self.assertEqual(board.metadata[out["id"]][model.META_SLUG], "found-a-bug")


class TestCliSlug(PatchedBoardTest):
    """--slug reaches ops through the cli seam (create/idea), same exit-code contract as
    task_type/column: 0 on a valid slug, 3 (GuardError) on a bad one."""

    def setUp(self):
        from triggered_agents.agents.pipeline import cli
        self.cli = cli

    def test_create_with_valid_slug_ok(self):
        board = self.make_board()
        rc = self.cli.main(["--role", "po", "create", "--project", "personal_site",
                            "--type", "code", "--title", "T", "--slug", "teardown-slug"])
        self.assertEqual(rc, 0)
        (task_id, meta), = [(tid, m) for tid, m in board.metadata.items()]
        self.assertEqual(meta[model.META_SLUG], "teardown-slug")

    def test_create_with_bad_slug_exits_3(self):
        self.make_board()
        rc = self.cli.main(["--role", "po", "create", "--project", "personal_site",
                            "--type", "code", "--title", "T", "--slug", "Not Valid"])
        self.assertEqual(rc, 3)

    def test_idea_with_valid_slug_ok(self):
        board = self.make_board()
        rc = self.cli.main(["--role", "reviewer", "idea", "--project", "personal_site",
                            "--title", "T", "--slug", "found-issue"])
        self.assertEqual(rc, 0)
        (task_id, meta), = [(tid, m) for tid, m in board.metadata.items()]
        self.assertEqual(meta[model.META_SLUG], "found-issue")


class TestCliStewardRole(PatchedBoardTest):
    """Role guards for steward at the CLI seam: same create right as po, plus move --reason for
    the Blocked->Done override."""

    def setUp(self):
        from triggered_agents.agents.pipeline import cli
        self.cli = cli

    def test_steward_create_ok(self):
        board = self.make_board()
        rc = self.cli.main(["--role", "steward", "create", "--project", "personal_site",
                            "--type", "code", "--title", "T"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(board.tasks), 1)

    def test_steward_create_via_cli_is_scrubbed(self):
        board = self.make_board()
        rc = self.cli.main(["--role", "steward", "create", "--project", "triggered-agents",
                            "--type", "code",
                            "--title", "нашёл KANBOARD_API_TOKEN=supersecretvalue123",
                            "--description", "AWS_SECRET=anothersecretvalue999"])
        self.assertEqual(rc, 0)
        created = board.method_calls("createTask")
        self.assertNotIn("supersecretvalue123", created[0]["title"])
        self.assertNotIn("anothersecretvalue999", created[0]["description"])

    def test_steward_move_blocked_to_done_with_reason_ok(self):
        board = self.make_board()
        ref = board.add_task("A", "Blocked", meta={model.META_TASK_TYPE: "code",
                                                   model.META_PROJECT: "personal_site"})
        rc = self.cli.main(["--role", "steward", "move", "--ref", ref, "--to", "Done",
                            "--reason", "vladmesh approved skipping review"])
        self.assertEqual(rc, 0)
        tid = next(t["id"] for t in board.tasks.values() if t["reference"] == ref)
        self.assertEqual(board._column_title_for(tid), "Done")

    def test_steward_move_blocked_to_done_without_reason_exits_3(self):
        board = self.make_board()
        ref = board.add_task("A", "Blocked", meta={model.META_TASK_TYPE: "code",
                                                   model.META_PROJECT: "personal_site"})
        rc = self.cli.main(["--role", "steward", "move", "--ref", ref, "--to", "Done"])
        self.assertEqual(rc, 3)
        tid = next(t["id"] for t in board.tasks.values() if t["reference"] == ref)
        self.assertEqual(board._column_title_for(tid), "Blocked")

    def test_worker_move_blocked_to_done_exits_3(self):
        board = self.make_board()
        ref = board.add_task("A", "Blocked", meta={model.META_TASK_TYPE: "code",
                                                   model.META_PROJECT: "personal_site"})
        rc = self.cli.main(["--role", "worker", "move", "--ref", ref, "--to", "Done",
                            "--reason", "whatever"])
        self.assertEqual(rc, 3)


class TestReviewerRole(unittest.TestCase):
    """Role guards for the layer-3 reviewer at the CLI seam: it may post a verdict and file Идеи
    cards, but never move a card, claim, report, or create a Ready card."""

    def setUp(self):
        from triggered_agents.agents.pipeline import cli
        self.cli = cli
        self.calls = []
        for name in ("verdict", "reviewer_idea", "claim_card", "report"):
            p = mock.patch(f"triggered_agents.agents.pipeline.ops.{name}",
                           lambda *a, n=name, **k: self.calls.append((n, a, k)) or {"ok": n})
            p.start()
            self.addCleanup(p.stop)

    def _run(self, argv):
        return self.cli.main(argv)

    def test_reviewer_verdict_allowed(self):
        rc = self._run(["--role", "reviewer", "verdict", "--ref", "R", "--kind", "red",
                        "--body", "блокер"])
        self.assertEqual(rc, 0)
        self.assertEqual(self.calls[0][0], "verdict")

    def test_reviewer_idea_routes_to_reviewer_idea_op(self):
        rc = self._run(["--role", "reviewer", "idea", "--project", "p", "--title", "t",
                        "--description", "d"])
        self.assertEqual(rc, 0)
        self.assertEqual(self.calls[0][0], "reviewer_idea")
        self.assertEqual(self.calls[0][2]["project"], "p")

    def test_worker_cannot_verdict(self):
        rc = self._run(["--role", "worker", "verdict", "--ref", "R", "--kind", "green"])
        self.assertEqual(rc, 2)
        self.assertEqual(self.calls, [])

    def test_reviewer_cannot_claim_or_report(self):
        # claim is dispatcher-only, report worker-only — both rejected before any ops call.
        self.assertEqual(self._run(["--role", "reviewer", "claim", "--ref", "R", "--worker", "w"]), 2)
        self.assertEqual(self._run(["--role", "reviewer", "report", "--ref", "R", "--kind", "done"]), 2)
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
