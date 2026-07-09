"""Unit tests for the Codex source of triggered_agents.agents.curator.

Codex sessions in Orca live as JSONL files under the managed runtime home. Tests use a
synthetic sessions tree so they never read the live host transcripts.
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

_STATE_DIR = tempfile.mkdtemp(prefix="ta-curator-codex-state-")
_CODEX_SESSIONS_DIR = tempfile.mkdtemp(prefix="ta-curator-codex-sessions-")
_CLAUDE_PROJECTS_DIR = tempfile.mkdtemp(prefix="ta-curator-codex-claude-")
_HERMES_HOME_DIR = tempfile.mkdtemp(prefix="ta-curator-codex-hermes-")
os.environ.setdefault("TA_STATE", _STATE_DIR)
os.environ.setdefault("TA_CODEX_SESSIONS_DIR", _CODEX_SESSIONS_DIR)
os.environ.setdefault("TA_CLAUDE_PROJECTS_DIR", _CLAUDE_PROJECTS_DIR)
os.environ.setdefault("TA_HERMES_HOME_DIR", _HERMES_HOME_DIR)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from triggered_agents.agents.curator import discover, harvest  # noqa: E402
from triggered_agents.runtime.state import AgentState  # noqa: E402


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n"
    path.write_text(text, encoding="utf-8")


def _session_meta(session_id="codex-1", cwd="/home/dev/some-project") -> dict:
    return {
        "timestamp": "2026-07-08T19:13:45.496Z",
        "type": "session_meta",
        "payload": {
            "session_id": session_id,
            "id": session_id,
            "cwd": cwd,
            "originator": "codex-tui",
        },
    }


def _message(role: str, text: str, *, ts="2026-07-08T19:13:45.577Z") -> dict:
    block_type = "input_text" if role == "user" else "output_text"
    return {
        "timestamp": ts,
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": role,
            "content": [{"type": block_type, "text": text}],
        },
    }


def _event_message(text: str) -> dict:
    return {
        "timestamp": "2026-07-08T19:13:45.578Z",
        "type": "event_msg",
        "payload": {"type": "user_message", "message": text},
    }


class _CodexFixtureCase(unittest.TestCase):
    def setUp(self):
        self.codex_sessions = Path(tempfile.mkdtemp(prefix="ta-curator-codex-case-"))
        self._patch(discover, "CODEX_SESSIONS", self.codex_sessions)
        self._patch(discover, "CLAUDE_PROJECTS", Path(tempfile.mkdtemp(prefix="ta-curator-codex-noclaude-")))
        hermes_home = Path(tempfile.mkdtemp(prefix="ta-curator-codex-nohermes-"))
        self._patch(discover, "HERMES_STATE_DB", hermes_home / "state.db")
        self._patch(discover, "HERMES_MEMORY_DIR", hermes_home / "memories")
        self._patch(discover, "EXCLUDE_CWDS", [
            "/home/dev/triggered-agents",
            "/home/dev/orca/workspaces/triggered-agents",
        ])

    def _patch(self, target, attr, value):
        p = mock.patch.object(target, attr, value)
        p.start()
        self.addCleanup(p.stop)

    def _state(self):
        state_dir = Path(tempfile.mkdtemp(prefix=f"ta-curator-codex-state-{self._testMethodName}-"))
        return AgentState(f"curator-codex-test-{self._testMethodName}", state_dir=state_dir)


class DiscoverCodexSessionsTest(_CodexFixtureCase):
    def test_missing_sessions_dir_returns_empty(self):
        missing = self.codex_sessions / "missing"
        self._patch(discover, "CODEX_SESSIONS", missing)
        self.assertEqual(discover.codex_sessions(), [])

    def test_finds_session_with_head_cwd_and_jsonl_path(self):
        path = self.codex_sessions / "2026" / "07" / "08" / "rollout.jsonl"
        _write_jsonl(path, [_session_meta("codex-session-1", "/home/dev/project")])

        found = discover.codex_sessions()

        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["head"], "codex")
        self.assertEqual(found[0]["session_id"], "codex-session-1")
        self.assertEqual(found[0]["cwd"], "/home/dev/project")
        self.assertEqual(found[0]["path"], str(path))

    def test_excludes_triggered_agents_worktree_cwd(self):
        _write_jsonl(
            self.codex_sessions / "excluded.jsonl",
            [_session_meta("excluded", "/home/dev/orca/workspaces/triggered-agents/296-card")],
        )
        _write_jsonl(
            self.codex_sessions / "kept.jsonl",
            [_session_meta("kept", "/home/dev/project")],
        )

        found = {s["session_id"] for s in discover.codex_sessions()}

        self.assertEqual(found, {"kept"})


class HarvestCodexSessionsTest(_CodexFixtureCase):
    def setUp(self):
        super().setUp()
        self.path = self.codex_sessions / "2026" / "07" / "08" / "rollout.jsonl"

    def _write_session(self, *rows: dict) -> None:
        _write_jsonl(self.path, [_session_meta("codex-session-1", "/home/dev/project"), *rows])

    def test_new_turns_are_harvested_and_watermarked(self):
        self._write_session(
            _message("user", "# AGENTS.md instructions for /home/dev/project\n<INSTRUCTIONS>noise</INSTRUCTIONS>"),
            _event_message("duplicated event payload"),
            _message("developer", "developer message is not a user turn"),
            _message("user", "remember codex marker"),
            _message("assistant", "stored codex marker"),
        )

        batch = harvest.harvest(self._state())

        self.assertEqual(len(batch["sessions"]), 1)
        sess = batch["sessions"][0]
        self.assertEqual(sess["head"], "codex")
        self.assertEqual([t["role"] for t in sess["turns"]], ["user", "assistant"])
        self.assertEqual([t["text"] for t in sess["turns"]], ["remember codex marker", "stored codex marker"])
        self.assertIn(str(self.path), batch["pending"])
        self.assertIn("lines", batch["pending"][str(self.path)])

    def test_repeat_harvest_before_advance_keeps_same_codex_turns(self):
        self._write_session(_message("user", "first codex turn"))
        state = self._state()

        first = harvest.harvest(state)
        second = harvest.harvest(state)

        self.assertEqual(first["sessions"][0]["turns"], second["sessions"][0]["turns"])

    def test_after_advance_processed_codex_lines_are_not_returned(self):
        self._write_session(_message("user", "first codex turn"))
        state = self._state()
        batch = harvest.harvest(state)
        harvest.advance(state, batch["pending"])

        again = harvest.harvest(state)

        self.assertEqual(again["sessions"], [])

    def test_after_advance_only_appended_codex_lines_are_returned(self):
        self._write_session(_message("user", "first codex turn"))
        state = self._state()
        batch = harvest.harvest(state)
        harvest.advance(state, batch["pending"])

        time.sleep(0.01)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(_message("assistant", "second codex turn"), ensure_ascii=False) + "\n")
        os.utime(self.path, None)

        next_batch = harvest.harvest(state)

        self.assertEqual([t["text"] for t in next_batch["sessions"][0]["turns"]], ["second codex turn"])

    def test_after_advance_incomplete_tail_is_returned_when_completed(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        prefix = "\n".join(
            json.dumps(r, ensure_ascii=False)
            for r in [
                _session_meta("codex-session-1", "/home/dev/project"),
                _message("user", "first complete codex turn"),
            ]
        ) + "\n"
        tail = json.dumps(_message("assistant", "tail completed later"), ensure_ascii=False)
        self.path.write_text(prefix + tail[:len(tail) // 2], encoding="utf-8")
        state = self._state()

        batch = harvest.harvest(state)
        harvest.advance(state, batch["pending"])

        self.assertEqual([t["text"] for t in batch["sessions"][0]["turns"]], ["first complete codex turn"])
        self.assertEqual(batch["pending"][str(self.path)]["lines"], 2)

        time.sleep(0.01)
        self.path.write_text(prefix + tail + "\n", encoding="utf-8")
        os.utime(self.path, None)

        next_batch = harvest.harvest(state)

        self.assertEqual([t["text"] for t in next_batch["sessions"][0]["turns"]], ["tail completed later"])

    def test_codex_turns_are_redacted_before_markdown_render(self):
        secret = "sk-ant-" + "c" * 30
        self._write_session(_message("user", f"secret is {secret}"))

        batch = harvest.harvest(self._state())
        rendered = harvest.render_markdown(batch)

        self.assertNotIn(secret, batch["sessions"][0]["turns"][0]["text"])
        self.assertNotIn(secret, rendered)
        self.assertIn("REDACTED", rendered)


if __name__ == "__main__":
    unittest.main()
