"""Unit tests for triggered_agents.agents.curator — discovery and watermark state for both
session transcripts and personal-memory files. No network, no real ~/.claude — TA_CLAUDE_PROJECTS_DIR
points discovery at a synthetic tree and TA_STATE gives the curator its own scratch watermark.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

_PROJECTS_DIR = tempfile.mkdtemp(prefix="ta-curator-projects-")
_STATE_DIR = tempfile.mkdtemp(prefix="ta-curator-state-")
os.environ["TA_CLAUDE_PROJECTS_DIR"] = _PROJECTS_DIR
os.environ["TA_STATE"] = _STATE_DIR

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from triggered_agents.agents.curator import discover, harvest  # noqa: E402
from triggered_agents.runtime.state import AgentState  # noqa: E402


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class DiscoverMemoryFilesTest(unittest.TestCase):
    """claude_memory_files(): finds new markdown facts, skips MEMORY.md, skips excluded cwds."""

    def setUp(self):
        self.projects = Path(tempfile.mkdtemp(prefix="ta-curator-projects-case-"))
        self._patch(discover, "CLAUDE_PROJECTS", self.projects)

    def _patch(self, target, attr, value):
        from unittest import mock
        p = mock.patch.object(target, attr, value)
        p.start()
        self.addCleanup(p.stop)

    def test_finds_memory_markdown_files(self):
        proj = self.projects / "-home-dev-some-project"
        _write(proj / "memory" / "MEMORY.md", "- [x](x.md) — index\n")
        _write(proj / "memory" / "fact-one.md", "---\nname: fact-one\n---\nbody\n")
        found = discover.claude_memory_files()
        names = {Path(m["path"]).name for m in found}
        self.assertEqual(names, {"fact-one.md"})
        self.assertEqual(found[0]["cwd"], "/home/dev/some/project")
        self.assertEqual(found[0]["head"], "claude")

    def test_skips_project_with_no_memory_dir(self):
        _write(self.projects / "-home-dev-bare" / "session.jsonl", "{}\n")
        self.assertEqual(discover.claude_memory_files(), [])

    def test_excludes_triggered_agents_self_checkout(self):
        proj = self.projects / "-home-dev-triggered-agents"
        _write(proj / "memory" / "fact.md", "body\n")
        self.assertEqual(discover.claude_memory_files(), [])

    def test_excludes_orca_worktree_of_triggered_agents(self):
        proj = self.projects / "-home-dev-orca-workspaces-triggered-agents-999-slug"
        _write(proj / "memory" / "fact.md", "body\n")
        self.assertEqual(discover.claude_memory_files(), [])

    def test_cwd_prefers_session_jsonl_over_dirname_fallback(self):
        # Real cwd carries dashes the lossy dirname->path fallback can't reconstruct.
        proj = self.projects / "-home-dev-some-project"
        _write(proj / "memory" / "fact.md", "body\n")
        _write(proj / "session.jsonl", json.dumps({"cwd": "/home/dev/some-project"}) + "\n")
        found = discover.claude_memory_files()
        self.assertEqual(found[0]["cwd"], "/home/dev/some-project")


class HarvestMemoryFilesTest(unittest.TestCase):
    """harvest_memory_files(): mtime/size watermark — new, changed, unchanged."""

    def setUp(self):
        self.projects = Path(tempfile.mkdtemp(prefix="ta-curator-projects-case-"))
        from unittest import mock
        p = mock.patch.object(discover, "CLAUDE_PROJECTS", self.projects)
        p.start()
        self.addCleanup(p.stop)
        self.path = self.projects / "-home-dev-proj" / "memory" / "fact.md"
        _write(self.path, "---\nname: fact\n---\nfirst version\n")

    def test_new_file_is_included_and_watermarked(self):
        entries, pending = harvest.harvest_memory_files({})
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["text"], "---\nname: fact\n---\nfirst version\n")
        self.assertIn(str(self.path), pending)
        self.assertIn("mtime", pending[str(self.path)])
        self.assertIn("size", pending[str(self.path)])

    def test_unchanged_file_is_skipped_on_second_pass(self):
        _, pending = harvest.harvest_memory_files({})
        entries_again, pending_again = harvest.harvest_memory_files(pending)
        self.assertEqual(entries_again, [])
        self.assertEqual(pending_again, {})

    def test_changed_file_is_picked_up_again(self):
        _, pending = harvest.harvest_memory_files({})
        # mtime resolution can be coarse on some filesystems; force a detectable change.
        import time
        time.sleep(0.01)
        os.utime(self.path, None)
        _write(self.path, "---\nname: fact\n---\nsecond version, longer body\n")
        entries, pending2 = harvest.harvest_memory_files(pending)
        self.assertEqual(len(entries), 1)
        self.assertIn("second version", entries[0]["text"])
        self.assertNotEqual(pending2[str(self.path)], pending[str(self.path)])

    def test_deleted_file_is_silently_dropped(self):
        _, pending = harvest.harvest_memory_files({})
        self.path.unlink()
        entries, pending2 = harvest.harvest_memory_files(pending)
        self.assertEqual(entries, [])
        self.assertEqual(pending2, {})


class HarvestCombinesSessionsAndMemoryTest(unittest.TestCase):
    """harvest(st)/advance(st, pending): one watermark dict, one advance call for both sources."""

    def setUp(self):
        self.projects = Path(tempfile.mkdtemp(prefix="ta-curator-projects-case-"))
        from unittest import mock
        p = mock.patch.object(discover, "CLAUDE_PROJECTS", self.projects)
        p.start()
        self.addCleanup(p.stop)
        self.state = AgentState("curator-test")
        self.mem_path = self.projects / "-home-dev-proj" / "memory" / "fact.md"
        _write(self.mem_path, "body\n")

    def test_precheck_shape_reports_no_new_turns_after_advance(self):
        batch = harvest.harvest(self.state)
        self.assertEqual(len(batch["memory"]), 1)
        self.assertEqual(batch["sessions"], [])
        harvest.advance(self.state, batch["pending"])

        batch2 = harvest.harvest(self.state)
        self.assertEqual(batch2["memory"], [])
        self.assertEqual(batch2["sessions"], [])

    def test_advance_persists_memory_watermark_in_same_state(self):
        batch = harvest.harvest(self.state)
        harvest.advance(self.state, batch["pending"])
        mark = self.state.load_watermark()
        self.assertIn(str(self.mem_path), mark)
        self.assertIn("size", mark[str(self.mem_path)])

    def test_render_markdown_reports_no_new_turns_when_batch_empty(self):
        empty = {"sessions": [], "memory": [], "pending": {}}
        self.assertIn("Нет новых ходов", harvest.render_markdown(empty))

    def test_render_markdown_includes_memory_section(self):
        batch = harvest.harvest(self.state)
        rendered = harvest.render_markdown(batch)
        self.assertIn("Личная память", rendered)
        self.assertIn("fact.md", rendered)


if __name__ == "__main__":
    unittest.main()
