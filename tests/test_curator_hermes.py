"""Unit tests for the Hermes source of triggered_agents.agents.curator.

Hermes (0.17.0, this host) stores sessions in a shared SQLite DB rather than the
per-session JSON files the Orca ai-vault scanner (our format reference) assumes --
verified live against ~/.hermes/state.db (hermes_state.py: "replacing the per-session
JSONL file approach"). The schema below is a real subset of that live schema (columns,
types, NOT NULL flags, session-id shape and role values all match a real inspected DB),
not an invented format.

No network, no real ~/.hermes or Codex runtime home: every test builds its own tiny
state.db / memories dir and patches discover.HERMES_STATE_DB / discover.HERMES_MEMORY_DIR /
discover.EXCLUDE_CWDS, the same way test_curator.py patches discover.CLAUDE_PROJECTS.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_STATE_DIR = tempfile.mkdtemp(prefix="ta-curator-hermes-state-")
_CODEX_SESSIONS_DIR = tempfile.mkdtemp(prefix="ta-curator-hermes-codex-sessions-")
os.environ.setdefault("TA_STATE", _STATE_DIR)
os.environ.setdefault("TA_CODEX_SESSIONS_DIR", _CODEX_SESSIONS_DIR)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from triggered_agents.agents.curator import discover, harvest  # noqa: E402
from triggered_agents.runtime.state import AgentState  # noqa: E402

_SESSIONS_SCHEMA = """
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    cwd TEXT,
    started_at REAL NOT NULL,
    archived INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT,
    timestamp REAL NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);
"""


def _make_state_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    con.executescript(_SESSIONS_SCHEMA)
    con.commit()
    return con


def _add_session(con, session_id, cwd, *, source="cli", started_at=1783117223.8, archived=0):
    con.execute(
        "INSERT INTO sessions (id, source, cwd, started_at, archived) VALUES (?, ?, ?, ?, ?)",
        (session_id, source, cwd, started_at, archived),
    )
    con.commit()


def _add_message(con, session_id, role, content, *, timestamp=1783117223.8, active=1):
    con.execute(
        "INSERT INTO messages (session_id, role, content, timestamp, active) VALUES (?, ?, ?, ?, ?)",
        (session_id, role, content, timestamp, active),
    )
    con.commit()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class _HermesFixtureCase(unittest.TestCase):
    """Common patch/teardown plumbing for a synthetic ~/.hermes tree."""

    def setUp(self):
        self.hermes_home = Path(tempfile.mkdtemp(prefix="ta-curator-hermes-home-"))
        self.db_path = self.hermes_home / "state.db"
        self.mem_dir = self.hermes_home / "memories"
        self._patch(discover, "HERMES_STATE_DB", self.db_path)
        self._patch(discover, "HERMES_MEMORY_DIR", self.mem_dir)
        self._patch(discover, "CODEX_SESSIONS", Path(tempfile.mkdtemp(prefix="ta-curator-hermes-nocodex-")))
        exclude = ["/home/dev/triggered-agents", "/home/dev/orca/workspaces/triggered-agents"]
        self._patch(discover, "EXCLUDE_CWDS", exclude)
        # harvest.harvest()/harvest_memory_files() also walk the Claude side
        # (discover.all_sessions()/all_memory_files()) -- point it at an empty tree so a
        # real ~/.claude/projects on the box running these tests can't leak in and
        # contaminate the Hermes-only assertions below.
        self._patch(discover, "CLAUDE_PROJECTS", Path(tempfile.mkdtemp(prefix="ta-curator-hermes-noclaude-")))

    def _patch(self, target, attr, value):
        p = mock.patch.object(target, attr, value)
        p.start()
        self.addCleanup(p.stop)


class DiscoverHermesSessionsTest(_HermesFixtureCase):
    def test_missing_db_returns_empty(self):
        self.assertEqual(discover.hermes_sessions(), [])

    def test_finds_session_with_head_and_cwd(self):
        con = _make_state_db(self.db_path)
        _add_session(con, "20260704_012020_868db3", "/home/dev/some/project")
        con.close()
        found = discover.hermes_sessions()
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["head"], "hermes")
        self.assertEqual(found[0]["session_id"], "20260704_012020_868db3")
        self.assertEqual(found[0]["cwd"], "/home/dev/some/project")
        self.assertEqual(found[0]["path"], str(self.db_path))

    def test_excludes_triggered_agents_worktree_cwd(self):
        con = _make_state_db(self.db_path)
        _add_session(con, "sess-excluded", "/home/dev/orca/workspaces/triggered-agents/9-x")
        _add_session(con, "sess-kept", "/home/dev/some/project")
        con.close()
        found = {s["session_id"] for s in discover.hermes_sessions()}
        self.assertEqual(found, {"sess-kept"})

    def test_excludes_archived_sessions(self):
        con = _make_state_db(self.db_path)
        _add_session(con, "sess-archived", "/home/dev/some/project", archived=1)
        con.close()
        self.assertEqual(discover.hermes_sessions(), [])

    def test_corrupted_db_degrades_to_empty_instead_of_raising(self):
        # A state.db that fails to open/query (mid-write corruption, foreign-format file,
        # transient lock) must not take down the whole harvest tick over an unrelated
        # Hermes hiccup -- degrade to "no Hermes sessions this run", same as a missing file.
        self.db_path.write_bytes(b"not a sqlite file")
        self.assertEqual(discover.hermes_sessions(), [])
        self.assertEqual(discover.hermes_messages("whatever"), [])


class HermesMessagesAndParsingTest(_HermesFixtureCase):
    def setUp(self):
        super().setUp()
        self.con = _make_state_db(self.db_path)
        _add_session(self.con, "sess-1", "/home/dev/some/project")

    def tearDown(self):
        self.con.close()

    def test_hermes_messages_skips_inactive_rows(self):
        _add_message(self.con, "sess-1", "user", "rolled back question", active=0)
        _add_message(self.con, "sess-1", "user", "kept question", active=1)
        rows = discover.hermes_messages("sess-1")
        self.assertEqual([r["content"] for r in rows], ["kept question"])

    def test_hermes_messages_respects_since_id(self):
        _add_message(self.con, "sess-1", "user", "first")
        rows = discover.hermes_messages("sess-1")
        since = rows[0]["id"]
        _add_message(self.con, "sess-1", "assistant", "second")
        rows2 = discover.hermes_messages("sess-1", since_id=since)
        self.assertEqual([r["content"] for r in rows2], ["second"])

    def test_parse_hermes_rows_keeps_only_user_assistant_text(self):
        _add_message(self.con, "sess-1", "user", "Запомни маркер X")
        _add_message(self.con, "sess-1", "assistant", "")  # tool call in flight
        _add_message(self.con, "sess-1", "tool", '{"success": false}')
        _add_message(self.con, "sess-1", "assistant", "Готово, запомнил.")
        rows = discover.hermes_messages("sess-1")
        turns = harvest.parse_hermes_rows(rows)
        self.assertEqual(
            [(t["role"], t["text"]) for t in turns],
            [("user", "Запомни маркер X"), ("assistant", "Готово, запомнил.")],
        )
        self.assertIsNotNone(turns[0]["ts"])  # epoch converted to ISO8601


class HarvestHermesSessionsWatermarkTest(_HermesFixtureCase):
    """harvest()/advance(): Hermes is watermarked by session_id -> last message id, since
    every session shares one file (state.db) whose mtime changes on every write."""

    def setUp(self):
        super().setUp()
        self.con = _make_state_db(self.db_path)
        _add_session(self.con, "sess-1", "/home/dev/some/project")
        # Unique agent name per test method: AgentState's watermark lives on real disk
        # under STATE_ROOT/<agent>/, shared across every test in this process -- a name
        # reused across test methods would leak one test's watermark into the next.
        self.state = AgentState(f"curator-hermes-test-{self._testMethodName}")

    def tearDown(self):
        self.con.close()

    def test_new_turns_are_harvested_and_watermarked(self):
        _add_message(self.con, "sess-1", "user", "HARVEST-MARKER-alpha")
        _add_message(self.con, "sess-1", "assistant", "ack alpha")
        batch = harvest.harvest(self.state)
        self.assertEqual(len(batch["sessions"]), 1)
        sess = batch["sessions"][0]
        self.assertEqual(sess["head"], "hermes")
        self.assertEqual([t["text"] for t in sess["turns"]], ["HARVEST-MARKER-alpha", "ack alpha"])
        self.assertIn("hermes:sess-1", batch["pending"])
        harvest.advance(self.state, batch["pending"])

    def test_second_run_only_returns_new_turns_after_advance(self):
        _add_message(self.con, "sess-1", "user", "first turn")
        batch = harvest.harvest(self.state)
        harvest.advance(self.state, batch["pending"])

        _add_message(self.con, "sess-1", "assistant", "second turn")
        batch2 = harvest.harvest(self.state)
        self.assertEqual(len(batch2["sessions"]), 1)
        self.assertEqual([t["text"] for t in batch2["sessions"][0]["turns"]], ["second turn"])

    def test_unchanged_session_yields_nothing_on_second_run(self):
        _add_message(self.con, "sess-1", "user", "only turn")
        batch = harvest.harvest(self.state)
        harvest.advance(self.state, batch["pending"])

        batch2 = harvest.harvest(self.state)
        self.assertEqual(batch2["sessions"], [])


class HermesMemoryFilesTest(_HermesFixtureCase):
    """Hermes's global MEMORY.md/USER.md ride the same whole-file mtime/size watermark
    as Claude's per-project memory files (discover.all_memory_files() -> harvest.harvest_memory_files())."""

    def test_discovers_memory_and_user_files_with_global_cwd(self):
        _write(self.mem_dir / "MEMORY.md", "curator harvest note\n\xa7\nsecond entry")
        _write(self.mem_dir / "USER.md", "user likes terse replies")
        found = {m["path"]: m for m in discover.hermes_memory_files()}
        names = {Path(p).name for p in found}
        self.assertEqual(names, {"MEMORY.md", "USER.md"})
        self.assertTrue(all(m["head"] == "hermes" and m["cwd"] == "" for m in found.values()))

    def test_harvest_memory_files_reads_and_redacts_hermes_memory(self):
        secret = "sk-ant-" + "a" * 30
        _write(self.mem_dir / "MEMORY.md", f"leaked key {secret}")
        entries, pending = harvest.harvest_memory_files({})
        self.assertEqual(len(entries), 1)
        self.assertNotIn(secret, entries[0]["text"])
        self.assertIn("REDACTED", entries[0]["text"])
        self.assertIn(str(self.mem_dir / "MEMORY.md"), pending)

    def test_unchanged_hermes_memory_file_is_skipped_on_second_pass(self):
        _write(self.mem_dir / "MEMORY.md", "stable note")
        _, pending = harvest.harvest_memory_files({})
        entries_again, pending_again = harvest.harvest_memory_files(pending)
        self.assertEqual(entries_again, [])
        self.assertEqual(pending_again, {})


class RedactionAppliesToHermesTurnsTest(_HermesFixtureCase):
    def test_secret_in_hermes_turn_is_redacted(self):
        con = _make_state_db(self.db_path)
        _add_session(con, "sess-1", "/home/dev/some/project")
        secret = "sk-ant-" + "b" * 30
        _add_message(con, "sess-1", "user", f"my key is {secret}")
        con.close()

        state = AgentState("curator-hermes-redact-test")
        batch = harvest.harvest(state)
        text = batch["sessions"][0]["turns"][0]["text"]
        self.assertNotIn(secret, text)
        self.assertIn("REDACTED", text)


class AllSourcesCombineBothHeadsTest(_HermesFixtureCase):
    def setUp(self):
        super().setUp()
        self.projects = Path(tempfile.mkdtemp(prefix="ta-curator-projects-case-"))
        self._patch(discover, "CLAUDE_PROJECTS", self.projects)

    def test_all_sessions_includes_both_heads(self):
        con = _make_state_db(self.db_path)
        _add_session(con, "sess-1", "/home/dev/some/project")
        con.close()
        _write(
            self.projects / "-home-dev-other" / "abc.jsonl",
            '{"type":"user","message":{"role":"user","content":"hi"},"cwd":"/home/dev/other"}\n',
        )
        heads = {s["head"] for s in discover.all_sessions()}
        self.assertEqual(heads, {"claude", "hermes"})

    def test_all_memory_files_includes_both_heads(self):
        _write(self.mem_dir / "MEMORY.md", "hermes note")
        _write(self.projects / "-home-dev-other" / "memory" / "fact.md", "claude note")
        heads = {m["head"] for m in discover.all_memory_files()}
        self.assertEqual(heads, {"claude", "hermes"})


if __name__ == "__main__":
    unittest.main()
