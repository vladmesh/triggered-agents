"""Codex session rollout lookup: liveness signal parsing, scan policy, and the
worker.terminal_status integration for terminal_kind=codex-tui.

TA_STATE / TA_PIPELINE_STATE_DIR pinned to tempdirs before the triggered_agents import: this
module sorts before test_dispatcher, so when the suite runs without the tests package __init__
(e.g. `cd tests && python3 -m unittest ...`) it is the first to bind the pipeline STATE
singleton. Without the pin that binding lands on the live dispatcher state dir and
test_pipeline_health's setUp then wipes the real runs.jsonl (2026-07-10 incident, same class
as triggered-agents-330)."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ["TA_STATE"] = tempfile.mkdtemp(prefix="ta-codex-sessions-state-")
os.environ["TA_PIPELINE_STATE_DIR"] = tempfile.mkdtemp(prefix="ta-codex-sessions-pipeline-state-")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from triggered_agents.agents.pipeline import codex_sessions, worker  # noqa: E402


def _write_session(path: Path, cwd: str, mtime: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "type": "session_meta",
        "payload": {"cwd": cwd, "session_id": path.stem},
    }) + "\n", encoding="utf-8")
    os.utime(path, (mtime, mtime))


def _write_rows(path: Path, rows: list[dict], mtime: float = 1234.5) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    os.utime(path, (mtime, mtime))


class SessionCwdTest(unittest.TestCase):
    def test_reads_cwd_from_session_meta(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rollout.jsonl"
            _write_session(path, "/ws/fresh", 1234.5)
            self.assertEqual(codex_sessions.session_cwd(path), "/ws/fresh")

    def test_invalid_utf8_is_skipped_not_raised(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.jsonl"
            path.write_bytes(b"\xff\xfe\x00broken")
            self.assertIsNone(codex_sessions.session_cwd(path))

    def test_json_non_object_lines_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "list.jsonl"
            path.write_text("[]\n42\n\"str\"\n", encoding="utf-8")
            self.assertIsNone(codex_sessions.session_cwd(path))

    def test_non_dict_payload_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "payload.jsonl"
            path.write_text(json.dumps({"type": "session_meta", "payload": ["/ws"]}) + "\n",
                            encoding="utf-8")
            self.assertIsNone(codex_sessions.session_cwd(path))


class LatestActivityTest(unittest.TestCase):
    def test_matches_workspace_and_ignores_others(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "sessions"
            _write_session(root / "2026" / "07" / "10" / "rollout-match.jsonl", "/ws/fresh", 1234.5)
            _write_session(root / "2026" / "07" / "10" / "rollout-other.jsonl", "/ws/other", 9999.0)
            with mock.patch.object(codex_sessions, "SESSIONS_ROOT", root):
                self.assertEqual(codex_sessions.latest_activity_for("/ws/fresh"), 1234.5)


class LatestUserTurnTest(unittest.TestCase):
    def test_finds_user_turn_after_timestamp_for_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "sessions"
            _write_rows(root / "2026" / "07" / "10" / "rollout.jsonl", [
                {"type": "session_meta", "payload": {"cwd": "/ws/fresh"}},
                {"timestamp": "2026-07-10T07:59:07.142Z", "type": "response_item",
                 "payload": {"type": "message", "role": "user", "content": []}},
                {"timestamp": "2026-07-10T07:59:08.000Z", "type": "event_msg",
                 "payload": {"type": "user_message", "message": "read TASK.md"}},
            ])
            with mock.patch.object(codex_sessions, "SESSIONS_ROOT", root):
                self.assertEqual(
                    codex_sessions.latest_user_turn_for("/ws/fresh", 1783670347.5),
                    1783670348.0,
                )

    def test_ignores_other_workspace_and_old_turns(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "sessions"
            _write_rows(root / "2026" / "07" / "10" / "old.jsonl", [
                {"type": "session_meta", "payload": {"cwd": "/ws/fresh"}},
                {"timestamp": "2026-07-10T07:59:07.142Z", "type": "event_msg",
                 "payload": {"type": "user_message", "message": "old"}},
            ])
            _write_rows(root / "2026" / "07" / "10" / "other.jsonl", [
                {"type": "session_meta", "payload": {"cwd": "/ws/other"}},
                {"timestamp": "2026-07-10T07:59:08.000Z", "type": "event_msg",
                 "payload": {"type": "user_message", "message": "other"}},
            ])
            with mock.patch.object(codex_sessions, "SESSIONS_ROOT", root):
                self.assertIsNone(codex_sessions.latest_user_turn_for("/ws/fresh", 1783670347.5))

    def test_scan_limited_to_recent_day_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "sessions"
            _write_session(root / "2026" / "07" / "09" / "rollout-old.jsonl", "/ws/fresh", 1000.0)
            _write_session(root / "2026" / "07" / "10" / "rollout-new.jsonl", "/ws/fresh", 2000.0)
            _write_session(root / "2025" / "01" / "01" / "rollout-ancient.jsonl", "/ws/fresh", 3000.0)
            with mock.patch.object(codex_sessions, "SESSIONS_ROOT", root):
                # The two newest day dirs win; the ancient dir is never visited even
                # though its mtime is the largest.
                self.assertEqual(codex_sessions.latest_activity_for("/ws/fresh"), 2000.0)

    def test_flat_layout_falls_back_to_root_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "sessions"
            _write_session(root / "rollout.jsonl", "/ws/fresh", 1234.5)
            with mock.patch.object(codex_sessions, "SESSIONS_ROOT", root):
                self.assertEqual(codex_sessions.latest_activity_for("/ws/fresh"), 1234.5)

    def test_malformed_neighbor_does_not_break_lookup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "sessions"
            day = root / "2026" / "07" / "10"
            _write_session(day / "rollout-good.jsonl", "/ws/fresh", 1234.5)
            (day / "rollout-bad.jsonl").write_bytes(b"\xff\xfe\x00broken")
            (day / "rollout-list.jsonl").write_text("[]\n", encoding="utf-8")
            with mock.patch.object(codex_sessions, "SESSIONS_ROOT", root):
                self.assertEqual(codex_sessions.latest_activity_for("/ws/fresh"), 1234.5)


class TerminalStatusIntegrationTest(unittest.TestCase):
    def test_codex_tui_activity_uses_matching_session_mtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "sessions"
            _write_session(root / "2026" / "07" / "10" / "rollout-match.jsonl", "/ws/fresh", 1234.5)
            _write_session(root / "2026" / "07" / "10" / "rollout-other.jsonl", "/ws/other", 9999.0)

            def fake_orca_json(args):
                if args[:2] == ["terminal", "list"]:
                    return {"terminals": [{
                        "handle": "term-tui",
                        "connected": True,
                        "writable": True,
                        "preview": "›workgpt-5.5 xhigh · ~/ws/fresh",
                        "lastOutputAt": 1000,
                    }]}
                return {}

            with mock.patch.object(codex_sessions, "SESSIONS_ROOT", root), \
                    mock.patch.object(worker, "_orca_json", fake_orca_json):
                status = worker.terminal_status("term-tui", "/ws/fresh", "codex-tui")
            self.assertEqual(status["last_activity"], 1234.5)

    def test_codex_tui_activity_recovers_not_codex_tui_preview(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "sessions"
            _write_session(root / "2026" / "07" / "10" / "rollout-live.jsonl", "/ws/fresh", 1234.5)

            def fake_orca_json(args):
                if args[:2] == ["terminal", "list"]:
                    return {"terminals": [{
                        "handle": "term-tui",
                        "connected": True,
                        "writable": True,
                        "preview": "thinking",
                        "lastOutputAt": 1000,
                    }]}
                if args[:2] == ["terminal", "read"]:
                    return {"terminal": {"tail": ["thinking"]}}
                return {}

            with mock.patch.object(codex_sessions, "SESSIONS_ROOT", root), \
                    mock.patch.object(worker, "_orca_json", fake_orca_json):
                status = worker.terminal_status("term-tui", "/ws/fresh", "codex-tui")
                live = worker.terminal_live("term-tui", "/ws/fresh", "codex-tui")
            self.assertEqual(status, {
                "known": True,
                "live": True,
                "reason": "live",
                "last_activity": 1234.5,
            })
            self.assertTrue(live)

    def test_codex_tui_session_mtime_does_not_revive_dead_handle(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "sessions"
            _write_session(root / "rollout.jsonl", "/ws/fresh", 9999.0)

            with mock.patch.object(codex_sessions, "SESSIONS_ROOT", root), \
                    mock.patch.object(worker, "_orca_json", return_value={"terminals": []}):
                self.assertEqual(worker.terminal_status("term-tui", "/ws/fresh", "codex-tui"),
                                 {"known": True, "live": False, "reason": "missing-terminal"})

    def test_malformed_session_file_does_not_break_terminal_status(self):
        # Review 373 B2: one bad .jsonl aborted the whole dispatcher tick.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "sessions"
            day = root / "2026" / "07" / "10"
            day.mkdir(parents=True)
            (day / "rollout-bad.jsonl").write_bytes(b"\xff\xfe\x00broken")
            (day / "rollout-list.jsonl").write_text("[]\n", encoding="utf-8")

            with mock.patch.object(codex_sessions, "SESSIONS_ROOT", root), \
                    mock.patch.object(worker, "_orca_json", return_value={"terminals": []}):
                self.assertEqual(worker.terminal_status("term-tui", "/ws/fresh", "codex-tui"),
                                 {"known": True, "live": False, "reason": "missing-terminal"})


if __name__ == "__main__":
    unittest.main()
