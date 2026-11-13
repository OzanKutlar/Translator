"""Tests for Translator's Note checks and the note guidance in the prompt.
Run from the project root: python -m unittest discover tests
"""

import unittest

from booktl import auditor, prompts, qa, segments

RAW = "第8章 夜\n他说这简直是「对牛弹琴」。\n众人大笑。"
ZH = ["第8章 夜", "他说这简直是「对牛弹琴」。", "众人大笑。"]
LINE_1031 = "老大道：“放心吧，这小子鬼精鬼精的，实力又没得说，他如果不行，那就没有人行了。”"
TN_1031 = ("(TN: The original uses the phrase 鬼精鬼精 (gui jing gui jing), which literally means 'ghost smart', "
           "describing someone who is very cunning, crafty, or clever.)")
BASE_TEXT = "⟦0⟧ # Chapter 8: Night\n\n⟦1⟧ He said it was a waste of breath.\n\n⟦2⟧ Everyone laughed."


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


class StarTests(unittest.TestCase):
    def _placed(self, text, notes):
        work = qa.ChapterWork({"chapterNo": "Chapter 8", "content": text}, 8, text, segments.split_page(RAW), notes)
        work.refresh()
        qa.place_notes(work, qa.NoteReport())
        return work.text

    def _culture(self, kind="culture"):
        return [{"afterId": 1, "kind": kind, "zh": "对牛弹琴", "correction": "", "text": "(TN: An idiom.)"}]

    def test_culture_note_gets_a_star(self):
        result = self._placed(BASE_TEXT, self._culture())
        self.assertIn("waste of breath.*\n\n(TN: An idiom.)", result)
        self.assertNotIn("laughed.*", result)

    def test_star_from_the_model_is_kept(self):
        text = BASE_TEXT.replace("waste of breath.", "waste of breath*, he said.")
        result = self._placed(text, self._culture())
        self.assertEqual(result.count("*"), 1)
        self.assertIn("breath*, he said.", result)

    def test_term_note_gets_no_star(self):
        self.assertNotIn("*", self._placed(BASE_TEXT, self._culture("term")))

    def test_skip_and_absent_paragraph_are_left_alone(self):
        self.assertEqual(segments.ensure_star("⟦1⟧ [SKIP: author's note]", 1), "⟦1⟧ [SKIP: author's note]")
        self.assertEqual(segments.ensure_star("⟦1⟧ A", 2), "⟦1⟧ A")
        self.assertEqual(segments.ensure_star("⟦1⟧ A\nsecond line\n\n⟦2⟧ B", 1), "⟦1⟧ A\nsecond line*\n\n⟦2⟧ B")


class ExampleTests(unittest.TestCase):
    def test_1031_example_passes_the_note_check(self):
        note = _note(zh="鬼精鬼精", text=TN_1031)
        verdict = qa.check_note(note, ["第1031章 永恒深渊与末日之灾的交锋", LINE_1031])
        self.assertIsNotNone(verdict.note)
        self.assertFalse(verdict.unmatched)
        self.assertFalse(verdict.moved)

    def test_example_reaches_translator_and_audit(self):
        self.assertIn("鬼精鬼精", prompts.SYSTEM_PROMPT)
        self.assertIn("ghost smart", auditor.TN_EXAMPLES)
        self.assertIn(auditor.TN_EXAMPLES, auditor.TN_PASS_INSTRUCTIONS)
        self.assertIn("Every note", auditor.TN_PASS_INSTRUCTIONS)


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
