"""Tests for shared state path resolution."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from triggered_agents.runtime import shared_state


class PipelineStatePathTest(unittest.TestCase):
    def test_default_pipeline_state_dir_uses_pipeline_worktree(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("TA_PIPELINE_STATE_DIR", None)
                resolved = shared_state.resolve_pipeline_state_dir(root)
        self.assertEqual(resolved, root / "triggered-agents" / "pipeline" / "state" / "pipeline")

    def test_pipeline_state_dir_override_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            override = Path(tmp) / "live-pipeline-state"
            with mock.patch.dict(os.environ, {"TA_PIPELINE_STATE_DIR": str(override)}):
                self.assertEqual(shared_state.resolve_pipeline_state_dir(Path("/ignored")), override)


if __name__ == "__main__":
    unittest.main()
