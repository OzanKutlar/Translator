"""Tests for the scout/translation conversation. Run from the project root: python -m unittest discover tests"""

import tempfile
import unittest

from booktl import prompts

PAGES = "第120章 血色\n李耀拿出血魔印，众人大惊。"
REPLY = '{"candidateTerms": [{"Chinese": "血魔印", "ProposedEnglish": "Blood Fiend Mark", "Type": "item"}]}'
USAGE = "## 血魔印\n- Chapter 300 (earliest)\n  EN: The Blood Demon Seal lit up."


class ConversationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.book = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def _scout(self):
        return prompts.build_scout_conversation(self.book, PAGES)

    def test_scout_conversation_ends_with_pages_once(self):
        messages = self._scout()
        self.assertEqual(messages[0], {"role": "system", "content": prompts.SYSTEM_PROMPT})
        self.assertEqual(messages[-1]["role"], "user")
        self.assertIn("candidateTerms", messages[-1]["content"])
        joined = "".join(m["content"] for m in messages)
        self.assertEqual(joined.count(PAGES), 1)

    def test_followup_extends_without_mutating(self):
        scout = self._scout()
        before = [dict(m) for m in scout]
        messages = prompts.build_translation_followup(scout, REPLY + "\n", USAGE)
        self.assertEqual(scout, before)
        self.assertEqual(messages[:len(scout)], scout)
        self.assertEqual(messages[len(scout)], {"role": "assistant", "content": REPLY})
        last = messages[-1]
        self.assertEqual(last["role"], "user")
        self.assertIn(USAGE, last["content"])
        self.assertIn("EARLIEST", last["content"])
        self.assertIn(prompts.TRANSLATE_NOW, last["content"])
        self.assertNotIn(PAGES, last["content"])

    def test_followup_without_prior_usage(self):
        last = prompts.build_translation_followup(self._scout(), REPLY, "")[-1]
        self.assertIn(prompts.NO_PRIOR_USAGE_FOLLOWUP, last["content"])
        self.assertNotIn("=== PRIOR USAGE ===", last["content"])

    def test_followup_rejects_bad_input(self):
        scout = self._scout()
        bad = (
            (scout, "  "),
            ([], REPLY),
            ("not a list", REPLY),
            (scout[:-1] + [{"role": "assistant", "content": "x"}], REPLY),
            ([{"role": "user"}], REPLY),
        )
        for args in bad:
            with self.assertRaises(ValueError):
                prompts.build_translation_followup(*args)

    def test_scout_messages_alias(self):
        self.assertIs(prompts.build_scout_messages, prompts.build_scout_conversation)


class ChapterNoteTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.book = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_note_lists_pages_in_order(self):
        note = prompts.chapter_note([496, 497, None, 499])
        self.assertIn("Chapters 496–497, 499", note)
        self.assertIn("page 1 = Chapter 496", note)
        self.assertIn("page 3 = number unknown", note)
        self.assertIn("page 4 = Chapter 499", note)

    def test_no_known_numbers_gives_no_note(self):
        self.assertEqual(prompts.chapter_note([None, None]), "")
        self.assertEqual(prompts.chapter_note([]), "")
        self.assertEqual(prompts.chapter_note("496"), "")

    def test_note_ends_every_translation_request(self):
        note = prompts.chapter_note([120])
        single = prompts.build_messages(self.book, PAGES, note=note)[-1]["content"]
        self.assertTrue(single.endswith(note))
        scout = prompts.build_scout_conversation(self.book, PAGES)
        self.assertNotIn(note, scout[-1]["content"])
        for usage in ("", USAGE):
            last = prompts.build_translation_followup(scout, REPLY, usage, note)[-1]["content"]
            self.assertTrue(last.endswith(note))
        self.assertTrue(prompts.build_manual_prompt(self.book, PAGES, note).endswith(note))


if __name__ == "__main__":
    unittest.main()
