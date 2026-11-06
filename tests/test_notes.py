"""Tests for Translator's Note checks and the note guidance in the prompt.
Run from the project root: python -m unittest discover tests
"""

import unittest

from booktl import prompts, qa, segments

RAW = "第8章 夜\n他说这简直是「对牛弹琴」。\n众人大笑。"
ZH = ["第8章 夜", "他说这简直是「对牛弹琴」。", "众人大笑。"]


def _note(**fields):
    item = {"afterId": 1, "kind": "culture", "zh": "", "correction": "", "text": "(TN: A note.)"}
    item.update(fields)
    note, reason = qa.normalise_note(item)
    if note is None:
        raise AssertionError(reason)
    return note


class NoteCheckTests(unittest.TestCase):
    def test_quoted_zh_is_cleaned_and_matched(self):
        note = _note(zh="「对牛弹琴」。")
        self.assertEqual(note.zh, "对牛弹琴")
        verdict = qa.check_note(note, ZH)
        self.assertIsNotNone(verdict.note)
        self.assertFalse(verdict.unmatched)
        self.assertFalse(verdict.moved)

    def test_note_moves_to_paragraph_with_its_chinese(self):
        verdict = qa.check_note(_note(afterId=2, zh="对牛弹琴"), ZH)
        self.assertEqual(verdict.note.after_id, 1)
        self.assertTrue(verdict.moved)

    def test_unmatched_culture_note_stays_at_after_id(self):
        verdict = qa.check_note(_note(zh="画蛇添足"), ZH)
        self.assertIsNotNone(verdict.note)
        self.assertEqual(verdict.note.after_id, 1)
        self.assertTrue(verdict.unmatched)

    def test_note_without_zh_is_placed_by_its_text(self):
        verdict = qa.check_note(_note(afterId=None, text="(TN: 对牛弹琴 means wasting words.)"), ZH)
        self.assertEqual(verdict.note.after_id, 1)

    def test_unmatched_source_note_is_removed(self):
        verdict = qa.check_note(_note(kind="source", zh="画蛇", correction="画虎"), ZH)
        self.assertIsNone(verdict.note)

    def test_unplaceable_note_is_removed(self):
        verdict = qa.check_note(_note(afterId=None, zh="画蛇添足"), ZH)
        self.assertIsNone(verdict.note)

    def test_report_counts_kinds(self):
        parts = segments.split_page(RAW)
        text = "⟦0⟧ # Chapter 8: Night\n\n⟦1⟧ He said it was a waste of breath.\n\n⟦2⟧ Everyone laughed."
        notes = [
            {"afterId": 1, "kind": "culture", "zh": "对牛弹琴", "correction": "", "text": "(TN: An idiom.)"},
            {"afterId": 2, "kind": "pun", "zh": "不存在", "correction": "", "text": "(TN: A pun.)"},
        ]
        work = qa.ChapterWork({"chapterNo": "Chapter 8", "content": text}, 8, text, parts, notes)
        work.refresh()
        report = qa.NoteReport()
        qa.place_notes(work, report)
        self.assertEqual(report.kinds, {"culture": 1, "pun": 1})
        self.assertEqual(len(report.unmatched), 1)
        self.assertEqual(report.removed, [])
        self.assertIn("(TN: An idiom.)", work.text)
        self.assertIn("(TN: A pun.)", work.text)


class PromptGuidanceTests(unittest.TestCase):
    def test_system_prompt_asks_for_culture_notes(self):
        self.assertIn("成语", prompts.SYSTEM_PROMPT)
        self.assertIn('"kind": "culture"', prompts.SYSTEM_PROMPT)
        self.assertNotIn("usually an empty list", prompts.SYSTEM_PROMPT)

    def test_followup_turns_repeat_the_reminder(self):
        self.assertIn("culture", prompts.TRANSLATE_NOW)
        self.assertIn("idiom", prompts.REPAIR_TURN)


if __name__ == "__main__":
    unittest.main()
