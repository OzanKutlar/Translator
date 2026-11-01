"""Tests for the style guide and style check. Run from the project root: python -m unittest discover tests"""

import os
import tempfile
import unittest

from booktl import prompts, review, segments, style


def _write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _kinds(text, caps=None):
    return [f.kind for f in style.check_text(text, caps)]


class CheckTests(unittest.TestCase):
    def test_articles(self):
        self.assertEqual(_kinds("He met an foreign man."), ["article"])
        self.assertEqual(_kinds("She ate a apple."), ["article"])
        for fine in ("It took an hour.", "He went to a university.", "A apple fell.", "He saw an FBI agent."):
            self.assertEqual(_kinds(fine), [], fine)

    def test_doubled_words(self):
        self.assertEqual(_kinds("He saw the the door."), ["doubled"])
        self.assertEqual(_kinds("No no, he said."), [])
        self.assertEqual(_kinds("He said go\n\ngo away."), [])

    def test_leftover_chinese_outside_notes(self):
        self.assertEqual(_kinds("The 血魔 glowed."), ["chinese"])
        self.assertEqual(_kinds("The seal glowed.\n\n(TN: 血魔 means blood demon.)"), [])

    def test_phrase_caps(self):
        text = "It actually worked. He actually left. She actually came. They actually won."
        findings = style.check_text(text)
        self.assertEqual([f.kind for f in findings], ["phrase"])
        self.assertFalse(findings[0].grammar)
        self.assertEqual(style.check_text(text, {"actually": 1000.0}), [])
        self.assertEqual(style.check_text("It actually worked."), [])

    def test_heading_and_link_ignored(self):
        self.assertEqual(_kinds("# Chapter 1: 血\n\nHe left.\n\n[[Chapter 2]]"), [])


class GuideTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.book = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_template_sends_nothing(self):
        ok, _msg = style.create_template(self.book)
        self.assertTrue(ok)
        guide = style.load_style(self.book)
        self.assertTrue(guide.exists)
        self.assertEqual(guide.prompt_text(), "")
        self.assertEqual(guide.caps, style.DEFAULT_CAPS)
        self.assertFalse(style.create_template(self.book)[0])

    def test_rules_and_caps(self):
        text = (
            "# Style guide\n<!-- hidden -->\n\n## Translation rules\n- Keep profanity as strong as the source.\n\n"
            "## Capped phrases\n- actually: off\n- suddenly: 1\n- nonsense line\n"
        )
        _write(style.style_path(self.book), text)
        guide = style.load_style(self.book)
        self.assertNotIn("actually", guide.caps)
        self.assertEqual(guide.caps["suddenly"], 1.0)
        self.assertEqual(len(guide.errors), 1)
        prompt = guide.prompt_text()
        self.assertIn("Keep profanity as strong as the source.", prompt)
        self.assertIn("suddenly (at most 1 per 1,000 words)", prompt)
        self.assertNotIn("hidden", prompt)
        self.assertNotIn("actually", prompt)

    def test_style_guide_reaches_the_prompt(self):
        _write(style.style_path(self.book), "## Translation rules\n- Transliterate names.\n")
        messages = prompts.build_messages(self.book, "第1章 夜\n他走了。")
        joined = "\n".join(m["content"] for m in messages)
        self.assertIn("=== STYLE GUIDE ===", joined)
        self.assertIn("Transliterate names.", joined)


class ReviewHintTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.book = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def _chapter(self, english: str) -> None:
        _write(os.path.join(self.book, "Chapter 1.md"), f"# Chapter 1: Night\n\n{english}\n\n[[Chapter 2]]")
        record = {"version": segments.ALIGN_VERSION, "chapter": 1, "zh": ["第1章 夜", "甲乙。"],
                  "en": {"0": "# Chapter 1: Night", "1": english}, "skips": {}, "notes": []}
        segments.save_alignment(self.book, 1, record)

    def _targets(self):
        outcome = review.ReviewOutcome()
        return review.collect_targets(self.book, [1], False, outcome), outcome

    def test_grammar_finding_triggers_review(self):
        self._chapter("He met an foreign man.")
        targets, _outcome = self._targets()
        self.assertEqual(len(targets), 1)
        self.assertTrue(any("wrong article" in hint for hint in targets[0].hints))

    def test_phrase_finding_alone_does_not(self):
        self._chapter("It actually worked. He actually left. She actually came. They actually won.")
        targets, outcome = self._targets()
        self.assertEqual(targets, [])
        self.assertEqual(outcome.clean, 1)


if __name__ == "__main__":
    unittest.main()
