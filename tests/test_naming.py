"""Unit tests for triggered_agents.agents.pipeline.naming — pure functions, no I/O."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from triggered_agents.agents.pipeline import naming  # noqa: E402


class SlugValidationTest(unittest.TestCase):
    def test_accepts_lowercase_alnum_and_dash(self):
        for slug in ("a", "teardown-done-workspaces", "a" * 30, "123-abc"):
            self.assertRegex(slug, naming.SLUG_RE)

    def test_rejects_empty(self):
        self.assertNotRegex("", naming.SLUG_RE)

    def test_rejects_too_long(self):
        self.assertNotRegex("a" * 31, naming.SLUG_RE)

    def test_rejects_uppercase(self):
        self.assertNotRegex("Teardown", naming.SLUG_RE)

    def test_rejects_underscore_and_spaces(self):
        self.assertNotRegex("teardown_done", naming.SLUG_RE)
        self.assertNotRegex("teardown done", naming.SLUG_RE)

    def test_rejects_cyrillic(self):
        self.assertNotRegex("слаг", naming.SLUG_RE)


class FallbackSlugTest(unittest.TestCase):
    def test_transliterates_cyrillic_title(self):
        self.assertEqual(naming.fallback_slug("Teardown воркспейсов"), "teardown-vorkspeisov")

    def test_ascii_title_lowercased_and_dashed(self):
        self.assertEqual(naming.fallback_slug("Fix Login Bug"), "fix-login-bug")

    def test_caps_at_30_chars_no_trailing_dash(self):
        slug = naming.fallback_slug("a" * 40)
        self.assertEqual(len(slug), 30)
        self.assertFalse(slug.endswith("-"))

    def test_collapses_punctuation_runs(self):
        self.assertEqual(naming.fallback_slug("fix: login... bug!!"), "fix-login-bug")

    def test_empty_title_falls_back_to_task(self):
        self.assertEqual(naming.fallback_slug(""), "task")
        self.assertEqual(naming.fallback_slug("!!!"), "task")

    def test_never_matches_invalid_slug_charset(self):
        # whatever the input, the output must itself be a valid slug (claim-time re-use safety)
        for title in ("Слаг Задачи 218", "", "!!!", "a" * 100, "MiXeD-Кейс_123"):
            self.assertRegex(naming.fallback_slug(title), naming.SLUG_RE)


class CardSlugTest(unittest.TestCase):
    def test_explicit_slug_wins(self):
        self.assertEqual(naming.card_slug({"slug": "my-slug", "title": "T"}), "my-slug")

    def test_falls_back_to_title_transliteration_when_unset(self):
        self.assertEqual(naming.card_slug({"title": "Fix Login Bug"}), "fix-login-bug")

    def test_blank_slug_falls_back_too(self):
        self.assertEqual(naming.card_slug({"slug": "  ", "title": "Fix Login Bug"}), "fix-login-bug")

    def test_falls_back_to_reference_when_title_missing(self):
        self.assertEqual(naming.card_slug({"reference": "personal_site-9"}), "personal-site-9")


class CardIdTest(unittest.TestCase):
    def test_extracts_numeric_tail_of_reference(self):
        self.assertEqual(naming.card_id("triggered-agents-218"), "218")

    def test_project_name_with_dashes(self):
        self.assertEqual(naming.card_id("personal-site-42"), "42")

    def test_single_segment_reference_returns_itself(self):
        self.assertEqual(naming.card_id("218"), "218")


class WorkspaceNameBuildTest(unittest.TestCase):
    def test_worker_base_is_id_dash_slug(self):
        self.assertEqual(naming.worker_workspace_base("218", "rename-slug"),
                         "218-rename-slug")

    def test_reviewer_base_is_prefixed(self):
        self.assertEqual(naming.reviewer_workspace_base("218", "rename-slug"),
                         "review-218-rename-slug")

    def test_titles_are_human_readable(self):
        self.assertEqual(naming.worker_title("218", "Слаги воркспейсов"),
                         "worker 218: Слаги воркспейсов")
        self.assertEqual(naming.reviewer_title("218", "Слаги воркспейсов"),
                         "review 218: Слаги воркспейсов")


class BranchNameTest(unittest.TestCase):
    """One ref per actor (git hygiene): worker/reviewer/stand each get a distinct, deterministic
    branch name so no two actors ever contend for the same local ref."""

    def test_worker_branch_is_pipeline_prefixed(self):
        self.assertEqual(naming.worker_branch("triggered-agents-220"),
                         "pipeline/triggered-agents-220")

    def test_reviewer_branch_is_review_prefixed(self):
        self.assertEqual(naming.reviewer_branch("triggered-agents-220"),
                         "review/triggered-agents-220")

    def test_stand_branch_is_per_project(self):
        self.assertEqual(naming.stand_branch("personal_site"), "stand/personal_site")

    def test_worker_and_reviewer_branches_never_collide(self):
        ref = "triggered-agents-220"
        self.assertNotEqual(naming.worker_branch(ref), naming.reviewer_branch(ref))


class DedupeTest(unittest.TestCase):
    def test_returns_base_when_free(self):
        self.assertEqual(naming.dedupe("A-x", lambda n: False), "A-x")

    def test_suffixes_2_on_first_collision(self):
        taken = {"A-x"}
        self.assertEqual(naming.dedupe("A-x", lambda n: n in taken), "A-x-2")

    def test_keeps_incrementing_past_multiple_collisions(self):
        taken = {"A-x", "A-x-2", "A-x-3"}
        self.assertEqual(naming.dedupe("A-x", lambda n: n in taken), "A-x-4")

    def test_never_raises_and_does_not_hang_on_pathological_exists(self):
        calls = []

        def exists(n):
            calls.append(n)
            return len(calls) < 5   # frees up after a handful of tries

        self.assertEqual(naming.dedupe("A-x", exists), "A-x-5")


if __name__ == "__main__":
    unittest.main()
