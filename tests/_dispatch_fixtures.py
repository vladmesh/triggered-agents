"""Shared fixture for runtime/dispatch.py unit tests — no network, no Orca.

Not a `test_*.py` module itself (nothing here runs under `unittest discover`); it exists purely
so `test_runtime_dispatch.py` and `test_dispatch_lifecycle.py` don't each define their own copy
of `_DispatchBase`.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from triggered_agents.runtime import dispatch


class _DispatchBase(unittest.TestCase):
    """Fakes every Orca touch dispatch.run() makes; records the ones a test cares about."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._state_patch = mock.patch("triggered_agents.runtime.state.STATE_ROOT", Path(self.tmp.name))
        self._state_patch.start()
        self.addCleanup(self._state_patch.stop)

        self.terminals = []          # terminal list dispatch sees for the workspace
        self.orca_json_calls = []
        self.orca_calls = []
        self.ready_calls = []        # (workspace,) each time _ensure_claude_ready runs

        p = mock.patch.object(dispatch, "_launch_cmd",
                              lambda agent, variant=None: ("/skill", "claude ... /skill", "fake-profile"))
        p.start()
        self.addCleanup(p.stop)

        p = mock.patch.object(dispatch, "_workspace", lambda agent: "/ws/agent")
        p.start()
        self.addCleanup(p.stop)

        p = mock.patch.object(dispatch, "_reap_ghosts", lambda ws: 0)
        p.start()
        self.addCleanup(p.stop)

        def fake_orca_json(args):
            self.orca_json_calls.append(args)
            if args[:2] == ["terminal", "list"]:
                return {"terminals": self.terminals}
            if args[:2] == ["terminal", "wait"]:
                return {"wait": {"satisfied": self.idle}}
            if args[:2] == ["terminal", "create"]:
                return {"terminal": {"handle": "new-terminal"}}
            return {}

        p = mock.patch.object(dispatch, "_orca_json", fake_orca_json)
        p.start()
        self.addCleanup(p.stop)

        def fake_orca(args):
            self.orca_calls.append(args)
            if args[:2] == ["terminal", "stop"]:
                # Mirrors real Orca: `terminal stop --worktree` kills every live terminal in the
                # workspace, so `_stop_and_confirm`'s re-list (via `_agent_terminals`) comes back
                # empty. Every test class here models exactly one workspace, so a stop always
                # clears the whole fixture list.
                self.terminals = []

        p = mock.patch.object(dispatch, "_orca", fake_orca)
        p.start()
        self.addCleanup(p.stop)

        p = mock.patch.object(dispatch, "_ensure_claude_ready",
                              lambda ws: self.ready_calls.append(ws))
        p.start()
        self.addCleanup(p.stop)

        p = mock.patch("time.sleep", lambda s: None)
        p.start()
        self.addCleanup(p.stop)

        self.idle = True

    def _logged_actions(self):
        import json

        runs = dispatch.AgentState("agent").dir / "runs.jsonl"
        if not runs.is_file():
            return []
        return [json.loads(l).get("action") for l in runs.read_text(encoding="utf-8").splitlines()]
