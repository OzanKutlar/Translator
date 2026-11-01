"""Tests for paragraph anchors. Run from the project root: python -m unittest discover tests"""

import json
import os
import tempfile
import unittest
from unittest import mock

from booktl import prompts, segments
from booktl.batchlog import BatchLog
from booktl.cancel import CancelToken
from booktl.config import Config
from booktl.pipeline import Pipeline

RAW = "\n".join([
    "某某书名",
    "2025-01-25 作者：某人",
    "第12章 风起",
    "张三推开门。",
    "李四抬头看他。",
    "访问 www.example.com 阅读更多",
    "两人都没有说话。",
    "(本章完)",
    "（第一卷完）",
])
REPLY = "\n\n".join([
    "⟦0⟧ # Chapter 12: The Wind Rises",
    "⟦1⟧ Zhang San pushed the door open.",
    "(TN: A note.)",
    "⟦2⟧ Li Si looked up at him.",
    "⟦3⟧ Neither of them spoke.",
    "[[Chapter 13]]",
])
PAGE = "某某书名\n第120章 血色\n" + "".join(f"李耀走进第{n}座大殿，众人大惊。\n" for n in range(1, 7))


class SplitTests(unittest.TestCase):
    def test_split_drops_site_text(self):
        parts = segments.split_page(RAW)
        self.assertEqual(parts.title, "第12章 风起")
        self.assertEqual(parts.body, ("张三推开门。", "李四抬头看他。", "两人都没有说话。"))
        self.assertEqual(parts.trailer, ("（第一卷完）",))
        self.assertEqual(parts.dropped, 4)

    def test_mark_page(self):
        parts = segments.split_page(RAW)
        marked = segments.mark_page(parts)
        self.assertEqual(segments.expected_ids(parts), (0, 1, 2, 3))
        self.assertIn("⟦0⟧ 第12章 风起", marked)
        self.assertIn("⟦3⟧ 两人都没有说话。", marked)
        self.assertNotIn("某某书名", marked)
        self.assertNotIn("www.example.com", marked)
        self.assertIn(segments.TRAILER_LABEL + "\n（第一卷完）", marked)

    def test_page_without_title_numbers_from_one(self):
        parts = segments.split_page("张三推开门。\n李四抬头看他。")
        self.assertEqual(parts.title, "")
        self.assertEqual(segments.expected_ids(parts), (1, 2))

    def test_clean_raw_keeps_title_and_body(self):
        lines = segments.clean_raw(RAW)
        self.assertEqual(lines[0], "第12章 风起")
        self.assertEqual(len(lines), 4)

    def test_prompt_mentions_trailer_label(self):
        self.assertIn(segments.TRAILER_LABEL, prompts.SYSTEM_PROMPT)


class ParseTests(unittest.TestCase):
    def _check(self, reply):
        return segments.check_anchors((0, 1, 2, 3), segments.parse_marked(reply))

    def test_complete_reply(self):
        out = segments.parse_marked(REPLY)
        self.assertEqual(sorted(out.paragraphs), [0, 1, 2, 3])
        self.assertEqual(out.notes, [(1, "(TN: A note.)")])
        self.assertEqual(self._check(REPLY), [])

    def test_missing_paragraph(self):
        findings = self._check(REPLY.replace("⟦2⟧ Li Si looked up at him.", ""))
        self.assertEqual(findings, [segments.Finding("missing", (2,))])
        self.assertEqual(segments.describe_findings(findings), "missing ⟦2⟧")

    def test_declared_skip(self):
        reply = REPLY.replace("⟦2⟧ Li Si looked up at him.", "⟦2⟧ [SKIP: author's note]")
        out = segments.parse_marked(reply)
        self.assertEqual(out.skips, {2: "author's note"})
        self.assertEqual(self._check(reply), [])
        stripped = segments.strip_markers(reply)
        self.assertNotIn("SKIP", stripped)
        self.assertNotIn("\n\n\n", stripped)

    def test_marker_variants(self):
        reply = "# ⟦0⟧ Chapter 12: X\n\n⟦１⟧ One.\n\n【2】 Two.\n\n[[3]] Three."
        out = segments.parse_marked(reply)
        self.assertEqual(out.paragraphs[0], "# Chapter 12: X")
        self.assertEqual(out.paragraphs[1], "One.")
        self.assertEqual(out.paragraphs[2], "Two.")
        self.assertEqual(out.paragraphs[3], "Three.")
        self.assertEqual(self._check(reply), [])

    def test_unmarked_heading_counts_as_title(self):
        self.assertEqual(self._check(REPLY.replace("⟦0⟧ # Chapter", "# Chapter")), [])

    def test_continuation_lines_join_their_paragraph(self):
        out = segments.parse_marked("⟦1⟧ First line\nsecond line\n\n⟦2⟧ Next")
        self.assertEqual(out.paragraphs[1], "First line\nsecond line")

    def test_problems_are_reported(self):
        reply = "⟦0⟧ # T\n\n⟦2⟧ B\n\n⟦1⟧ A\n\n⟦1⟧ A again\n\n⟦9⟧ Z\n\nStray paragraph."
        kinds = {f.kind: f for f in self._check(reply)}
        self.assertEqual(kinds["missing"].ids, (3,))
        self.assertEqual(kinds["unknown"].ids, (9,))
        self.assertEqual(kinds["duplicate"].ids, (1,))
        self.assertEqual(kinds["out_of_order"].ids, (1,))
        self.assertEqual(kinds["unmarked"].count, 1)

    def test_reply_without_markers(self):
        self.assertEqual(self._check("# Chapter 12\n\nJust text."), [segments.Finding("no_markers")])

    def test_strip_markers(self):
        stripped = segments.strip_markers(REPLY)
        self.assertTrue(stripped.startswith("# Chapter 12: The Wind Rises"))
        self.assertNotIn("⟦", stripped)
        self.assertIn("(TN: A note.)", stripped)
        self.assertTrue(stripped.endswith("[[Chapter 13]]"))

    def test_format_ids(self):
        self.assertEqual(segments.format_ids([20, 14, 15, 16, 17]), "⟦14–17⟧, ⟦20⟧")
        self.assertEqual(segments.format_ids([]), "")


class AlignmentTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.book = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_round_trip_and_remove(self):
        parts = segments.split_page(RAW)
        data = segments.build_alignment(12, parts, segments.parse_marked(REPLY))
        segments.save_alignment(self.book, 12, data)
        zh, en = segments.alignment_texts(segments.load_alignment(self.book, 12))
        self.assertEqual(zh[2], "李四抬头看他。")
        self.assertEqual(en[2], "Li Si looked up at him.")
        self.assertTrue(segments.remove_alignment(self.book, 12))
        self.assertIsNone(segments.load_alignment(self.book, 12))
        self.assertFalse(segments.remove_alignment(self.book, 12))

    def test_corrupt_file_and_bad_input(self):
        path = segments.alignment_path(self.book, 5)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("{broken")
        self.assertIsNone(segments.load_alignment(self.book, 5))
        self.assertIsNone(segments.load_alignment(self.book, 0))
        with self.assertRaises(ValueError):
            segments.alignment_path(self.book, True)


def _reply(ids) -> str:
    paragraphs = ["⟦0⟧ # Chapter 120: Blood"] + [f"⟦{i}⟧ Li Yao entered hall {i}." for i in ids]
    content = "\n\n".join(paragraphs + ["[[Chapter 121]]"])
    return json.dumps({
        "chapters": [{"chapterNo": "Chapter 120", "content": content}],
        "characterActions": [], "arcEnds": [], "termActions": [],
    })


class FakeClient:
    def __init__(self, reply):
        self.reply = reply
        self.sent = []

    def chat(self, cfg, messages, max_tokens=None, cancel=None, label=None):
        self.sent.append(messages)
        return self.reply, {"total_tokens": 1}


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.book = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, ids):
        cfg = Config()
        cfg.model = "test-model"
        cfg.term_lookup = False
        cfg.set_book(self.book, "Test Book")
        client = FakeClient(_reply(ids))
        pipeline = Pipeline(cfg, client)
        entry = pipeline.pool.make_entry(120, "Chapter 120", PAGE)
        lines = []
        with mock.patch.object(BatchLog, "emit", autospec=True, side_effect=lambda log: lines.extend(log.lines())):
            pipeline._run_batch(1, [entry], CancelToken())
        return client, "\n".join(lines)

    def _saved(self) -> str:
        with open(os.path.join(self.book, "Chapter 120.md"), "r", encoding="utf-8") as f:
            return f.read()

    def _aligned_english(self) -> dict:
        return segments.alignment_texts(segments.load_alignment(self.book, 120))[1]

    def test_marked_pages_go_out_and_markers_are_removed(self):
        client, log = self._run(range(1, 7))
        sent = client.sent[0][-1]["content"]
        self.assertIn("⟦1⟧ 李耀走进第1座大殿", sent)
        self.assertNotIn("某某书名", sent)
        self.assertNotIn("⟦", self._saved())
        self.assertEqual(sorted(self._aligned_english()), list(range(7)))
        self.assertNotIn("Anchors", log)

    def test_missing_paragraph_is_reported(self):
        _client, log = self._run([1, 2, 4, 5, 6])
        self.assertIn("Anchors: Chapter 120 missing ⟦3⟧", log)
        self.assertIn("Li Yao entered hall 4.", self._saved())
        self.assertNotIn(3, self._aligned_english())


if __name__ == "__main__":
    unittest.main()
