"""Tests for curator's secretary memory protocol adapter."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from triggered_agents.agents.curator import memory_protocol  # noqa: E402
from triggered_agents.runtime.state import AgentState  # noqa: E402


def _completed(payload: dict, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=[],
        returncode=returncode,
        stdout=json.dumps(payload),
        stderr="",
    )


class CuratorMemoryProtocolTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ta-curator-memory-protocol-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = AgentState("curator-test", state_dir=self.root / "state")
        self.fact = self.root / "fact.md"
        self.fact.write_text("durable fact\n", encoding="utf-8")
        self.request = memory_protocol.MemoryWriteRequest(
            instance=self.root / "instance",
            actor="curator:codex/session123",
            scope="project:triggered-agents",
            slug="protocol-fact",
            fact_file=self.fact,
            source="curator:codex/session123",
            tags="curator,memory",
            secretary_repo=self.root / "secretary",
        )

    def test_successful_write_proposes_and_commits_with_provenance(self):
        calls = []

        def runner(args, **kwargs):
            calls.append((args, kwargs))
            if "propose" in args:
                return _completed({
                    "ok": True,
                    "op": "propose",
                    "propose_id": "proposal-1",
                    "fact": "triggered-agents/protocol-fact",
                    "actor": self.request.actor,
                    "source": self.request.source,
                })
            return _completed({
                "ok": True,
                "op": "commit",
                "commit": "abc123",
                "fact": "triggered-agents/protocol-fact",
                "actor": self.request.actor,
                "source": self.request.source,
                "changed_facts": ["triggered-agents/protocol-fact"],
                "propose_id": "proposal-1",
            })

        result = memory_protocol.write_fact(self.state, self.request, runner=runner)

        self.assertEqual(result["commit"], "abc123")
        propose_args = calls[0][0]
        commit_args = calls[1][0]
        self.assertIn("propose", propose_args)
        self.assertIn("commit", commit_args)
        self.assertIn("--actor", propose_args)
        self.assertIn("curator:codex/session123", propose_args)
        self.assertIn("--source", propose_args)
        self.assertIn("curator:codex/session123", propose_args)
        self.assertEqual(list((self.state.dir / "memory_protocol").glob("*.json")), [])
        env = calls[0][1]["env"]
        self.assertIn(str(self.request.secretary_repo), env["PYTHONPATH"])
        self.assertFalse(any(arg == "git" or "panelmem-kb" in arg for args, _kwargs in calls for arg in args))

    def test_protocol_failure_keeps_proposal_for_retry(self):
        calls = []

        def failing_runner(args, **kwargs):
            calls.append(args)
            if "propose" in args:
                return _completed({"ok": True, "op": "propose", "propose_id": "proposal-2"})
            return _completed({"ok": False, "error": "locked", "message": "busy"}, returncode=75)

        with self.assertRaises(memory_protocol.MemoryProtocolError):
            memory_protocol.write_fact(self.state, self.request, runner=failing_runner)

        saved = list((self.state.dir / "memory_protocol").glob("*.json"))
        self.assertEqual(len(saved), 1)
        self.assertIn("proposal-2", saved[0].read_text(encoding="utf-8"))

        retry_calls = []

        def retry_runner(args, **kwargs):
            retry_calls.append(args)
            return _completed({
                "ok": True,
                "op": "commit",
                "commit": "def456",
                "fact": "triggered-agents/protocol-fact",
                "changed_facts": ["triggered-agents/protocol-fact"],
                "propose_id": "proposal-2",
            })

        result = memory_protocol.write_fact(self.state, self.request, runner=retry_runner)
        self.assertEqual(result["commit"], "def456")
        self.assertEqual(len(retry_calls), 1)
        self.assertIn("commit", retry_calls[0])
        self.assertNotIn("propose", retry_calls[0])

    def test_export_failure_after_commit_retries_same_proposal(self):
        attempts = []

        def first_runner(args, **kwargs):
            attempts.append(args)
            if "propose" in args:
                return _completed({"ok": True, "op": "propose", "propose_id": "proposal-3"})
            return _completed({
                "ok": False,
                "op": "commit",
                "error": "export",
                "commit": "committed-head",
                "fact": "triggered-agents/protocol-fact",
                "propose_id": "proposal-3",
            }, returncode=1)

        with self.assertRaises(memory_protocol.MemoryProtocolError) as raised:
            memory_protocol.write_fact(self.state, self.request, runner=first_runner)

        self.assertEqual(raised.exception.payload["commit"], "committed-head")

        retry_calls = []

        def retry_runner(args, **kwargs):
            retry_calls.append(args)
            return _completed({
                "ok": True,
                "op": "commit",
                "commit": "committed-head",
                "fact": "triggered-agents/protocol-fact",
                "changed_facts": ["triggered-agents/protocol-fact"],
                "propose_id": "proposal-3",
            })

        result = memory_protocol.write_fact(self.state, self.request, runner=retry_runner)
        self.assertEqual(result["commit"], "committed-head")
        self.assertEqual(len(retry_calls), 1)
        self.assertIn("commit", retry_calls[0])

    def test_runtime_config_does_not_expose_old_panelmem_write_path(self):
        repo = Path(__file__).resolve().parents[1]
        skill = (repo / ".claude" / "skills" / "curate" / "SKILL.md").read_text(encoding="utf-8")
        settings = (repo / ".claude" / "settings.json").read_text(encoding="utf-8")
        role_env = (repo / "triggered_agents" / "runtime" / "role_env.py").read_text(encoding="utf-8")

        self.assertNotIn("PANELMEM_KB_PAT", role_env)
        self.assertNotIn("git -C ~/panelmem-kb", skill)
        self.assertNotIn("Bash(git push:*)", settings)


if __name__ == "__main__":
    unittest.main()
