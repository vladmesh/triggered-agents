"""Unit tests for triggered_agents.__main__'s `dispatch` argv parsing (triggered-agents-445):
`--cleanup-only` must reach `dispatch.run` as a flag, never as the variant positional, and must
compose correctly with a real variant like the steward's "deep-sweep".
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root

from triggered_agents import __main__ as ta_main  # noqa: E402
from triggered_agents.runtime import dispatch  # noqa: E402


class DispatchArgvParsingTest(unittest.TestCase):
    def setUp(self):
        self.calls = []
        p = mock.patch.object(dispatch, "run", lambda *a, **kw: self.calls.append((a, kw)) or 0)
        p.start()
        self.addCleanup(p.stop)

    def test_plain_dispatch_has_no_variant_and_is_not_cleanup_only(self):
        ta_main.main(["curator", "dispatch"])
        self.assertEqual(self.calls, [(("curator", None), {"cleanup_only": False})])

    def test_variant_is_passed_through(self):
        ta_main.main(["steward", "dispatch", "deep-sweep"])
        self.assertEqual(self.calls, [(("steward", "deep-sweep"), {"cleanup_only": False})])

    def test_cleanup_only_flag_is_not_read_as_a_variant(self):
        ta_main.main(["curator", "dispatch", "--cleanup-only"])
        self.assertEqual(self.calls, [(("curator", None), {"cleanup_only": True})])

    def test_cleanup_only_flag_composes_with_a_variant_in_either_order(self):
        ta_main.main(["steward", "dispatch", "--cleanup-only", "deep-sweep"])
        self.assertEqual(self.calls, [(("steward", "deep-sweep"), {"cleanup_only": True})])
        self.calls.clear()
        ta_main.main(["steward", "dispatch", "deep-sweep", "--cleanup-only"])
        self.assertEqual(self.calls, [(("steward", "deep-sweep"), {"cleanup_only": True})])


if __name__ == "__main__":
    unittest.main()
