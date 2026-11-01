"""Tests for the reviewer and its edit tool. Run from the project root: python -m unittest discover tests"""

import json
import os
import tempfile
import unittest
from unittest import mock

from booktl import edits, qalog, review, segments
from booktl.client import ApiClient
from booktl.config import Config

MD = "# Chapter 5: Night\n\nZhang San walked in.\n\n(TN: A note.)\n\nNobody moved.\n\n[[Chapter 6]]"
ZH = ["第5章 夜", "张三走进来。", "李四抬头。", "没人动。"]
EN = {"0": "# Chapter 5: Night", "1": "Zhang San walked in.", "3": "Nobody moved."}
INSERT = {"search": "(TN: A note.)", "replace": "(TN: A note.)\n\n⟦2⟧ Li Si looked up."}


def _write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _edit(search, replace, kind="omission"):
    return edits.Edit(5, kind, "test", (edits.Pair(search, replace),))


class BookCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.book = self._tmp.name
        self.md_path = os.path.join(self.book, "Chapter 5.md")
        _write(self.md_path, MD)
        self._align(EN)
        chars = json.dumps({"张三": "Zhang San.md"}, ensure_ascii=False)
        _write(os.path.join(self.book, "Characters", "Characters.json"), chars)
        _write(os.path.join(self.book, "Characters", "Zhang San.md"), "# Zhang San\n\nA guard.\n")

    def tearDown(self):
        self._tmp.cleanup()

    def _align(self, english):
        record = {"version": segments.ALIGN_VERSION, "chapter": 5, "zh": ZH, "en": dict(english),
                  "skips": {}, "notes": [{"afterId": 1, "text": "(TN: A note.)"}]}
        segments.save_alignment(self.book, 5, record)

    def _source(self):
        source, err = edits.load_source(self.book, 5, self.md_path)
        self.assertEqual(err, "")
        return source


class EditTests(BookCase):
    def test_insert_missing_paragraph(self):
        source = self._source()
        self.assertEqual(source.missing, (2,))
        result = edits.apply_chapter(self.book, source, [_edit(**INSERT)])
        self.assertEqual(len(result.applied), 1)
        self.assertEqual(result.missing_after, ())
        text = _read(self.md_path)
        self.assertLess(text.index("(TN: A note.)"), text.index("Li Si looked up."))
        self.assertLess(text.index("Li Si looked up."), text.index("Nobody moved."))
        self.assertNotIn("⟦", text)
        _zh, en = segments.alignment_texts(segments.load_alignment(self.book, 5))
        self.assertEqual(en[2], "Li Si looked up.")
        self.assertTrue(os.path.isfile(result.backup))

    def test_undo_restores_file_and_alignment(self):
        edits.apply_chapter(self.book, self._source(), [_edit(**INSERT)])
        ok, _msg = edits.undo_review(self.book, 5, self.md_path)
        self.assertTrue(ok)
        self.assertEqual(_read(self.md_path), MD)
        self.assertEqual(self._source().missing, (2,))
        ok, _msg = edits.undo_review(self.book, 5, self.md_path)
        self.assertFalse(ok)

    def test_rejections_leave_the_file_alone(self):
        cases = (
            ("Not in the chapter.", "x"),
            ("Zhang San walked in.", "He walked in."),
            ("(TN: A note.)", ""),
            ("Nobody moved.", "Nobody moved.\n\n⟦1⟧ Again."),
            ("Nobody", "No ⟦2⟧ body"),
        )
        for search, replace in cases:
            result = edits.apply_chapter(self.book, self._source(), [_edit(search, replace, "grammar")])
            self.assertEqual(result.applied, [], (search, replace))
            self.assertEqual(len(result.rejected), 1)
        self.assertEqual(_read(self.md_path), MD)

    def test_normalise_edits(self):
        items = [
            {"chapter": 5, "kind": "omission", "reason": "r", "search_replace": [INSERT]},
            {"chapter": 9, "kind": "omission", "search_replace": [INSERT]},
            {"chapter": "Chapter 5", "kind": "weird", "search_replace": []},
        ]
        found, problems = edits.normalise_edits(items, {5})
        self.assertEqual(len(found), 1)
        self.assertEqual(len(problems), 2)


class FakeClient:
    def __init__(self, reply):
        self.reply = reply
        self.models = []

    def chat(self, cfg, messages, max_tokens=None, cancel=None, label=None, model=None):
        self.models.append(model)
        return self.reply, {"total_tokens": 3}


class ReviewRunTests(BookCase):
    def _cfg(self):
        cfg = Config()
        cfg.model = "translator"
        cfg.reviewer_model = "rev"
        cfg.set_book(self.book, "Test Book")
        return cfg

    def test_flagged_chapter_is_reviewed_and_fixed(self):
        reply = json.dumps({"phase": "REVIEW", "summary": "Restored ⟦2⟧.", "edits": [
            {"chapter": 5, "kind": "omission", "reason": "missing", "search_replace": [INSERT]}]}, ensure_ascii=False)
        client = FakeClient(reply)
        outcome = review.run(client, self._cfg(), [5], False, 1, None, "Test review")
        self.assertEqual(client.models, ["rev"])
        self.assertEqual(outcome.completed, [5])
        self.assertEqual(outcome.summaries, ["Restored ⟦2⟧."])
        records, err = qalog.read_records(self.book, qalog.REVIEW_FILENAME)
        self.assertIsNone(err)
        self.assertEqual(records[0]["status"], "applied")

    def test_clean_chapter_is_skipped_unless_forced(self):
        self._align({**EN, "2": "Li Si looked up."})
        client = FakeClient('{"phase": "REVIEW", "summary": "", "edits": []}')
        outcome = review.run(client, self._cfg(), [5], False, 1, None, "Test review")
        self.assertEqual((outcome.targets, outcome.clean, client.models), (0, 1, []))
        outcome = review.run(client, self._cfg(), [5], True, 1, None, "Test review")
        self.assertEqual(outcome.targets, 1)
        self.assertEqual(client.models, ["rev"])

    def test_unusable_reply_is_an_error(self):
        outcome = review.run(FakeClient("not json"), self._cfg(), [5], False, 1, None, "Test review")
        self.assertEqual(len(outcome.errors), 1)
        self.assertEqual(_read(self.md_path), MD)


class ClientAndConfigTests(unittest.TestCase):
    def test_model_override(self):
        cfg = Config()
        cfg.model = "translator"
        cfg.json_mode = "off"
        body = json.dumps({"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]})
        response = mock.Mock(status_code=200, text=body, headers={})
        response.json.return_value = json.loads(body)
        with mock.patch("booktl.client.requests.post", return_value=response) as post:
            ApiClient().chat(cfg, [{"role": "user", "content": "x"}], model="rev")
        self.assertEqual(post.call_args.kwargs["json"]["model"], "rev")

    def test_reviewer_off_values(self):
        cfg = Config()
        cfg.reviewer_model = "rev"
        self.assertEqual(cfg.reviewer_model, "rev")
        cfg.reviewer_model = "off"
        self.assertEqual(cfg.reviewer_model, "")


if __name__ == "__main__":
    unittest.main()
