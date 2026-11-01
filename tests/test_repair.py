"""Tests for paragraph repair and the Incomplete list. Run from the project root: python -m unittest discover tests"""

import json
import os
import tempfile
import threading
import unittest
from unittest import mock

from booktl import qa, repair, segments
from booktl.cancel import CancelToken
from booktl.config import Config
from booktl.pipeline import Pipeline
from booktl.workpool import PendingPool, parse_selection

PAGE = "某某书名\n第120章 血色\n" + "".join(f"李耀走进第{n}座大殿，众人大惊。\n" for n in range(1, 7))
PARTS = segments.split_page(PAGE)
EMPTY_REPAIR = '{"paragraphs": []}'


def _content(ids) -> str:
    paragraphs = ["⟦0⟧ # Chapter 120: Blood"] + [f"⟦{i}⟧ Li Yao entered hall {i}." for i in ids]
    return "\n\n".join(paragraphs + ["[[Chapter 121]]"])


def _reply(ids) -> str:
    return json.dumps({
        "chapters": [{"chapterNo": "Chapter 120", "content": _content(ids), "notes": []}],
        "characterActions": [], "arcEnds": [], "termActions": [],
    })


def _work(ids):
    entry = {"chapterNo": "Chapter 120", "content": _content(ids), "notes": []}
    work = qa.ChapterWork(entry, 120, _content(ids), PARTS, [])
    work.refresh()
    return work


class SplicingTests(unittest.TestCase):
    def test_insert_paragraph_in_middle(self):
        base = "⟦0⟧ # T\n\n⟦1⟧ A\n\n⟦3⟧ C\n\n[[Chapter 2]]"
        spliced = segments.insert_paragraph(base, 2, "B")
        self.assertEqual(spliced, "⟦0⟧ # T\n\n⟦1⟧ A\n\n⟦2⟧ B\n\n⟦3⟧ C\n\n[[Chapter 2]]")

    def test_insert_at_end_before_link(self):
        base = "⟦0⟧ # T\n\n⟦1⟧ A\n\n[[Chapter 2]]"
        spliced = segments.insert_paragraph(base, 2, "B")
        self.assertEqual(spliced, "⟦0⟧ # T\n\n⟦1⟧ A\n\n⟦2⟧ B\n\n[[Chapter 2]]")

    def test_insert_after_respects_notes(self):
        base = "⟦0⟧ # T\n\n⟦1⟧ A\n\n(TN: Note on A.)\n\n⟦3⟧ C"
        spliced = segments.insert_after(base, 1, "(TN: Second note.)")
        self.assertIn("(TN: Note on A.)\n\n(TN: Second note.)\n\n⟦3⟧", spliced)


class TargetTests(unittest.TestCase):
    def test_select_targets_and_too_many(self):
        clean = _work(range(1, 7))
        one_gap = _work([1, 2, 4, 5, 6])
        many_gaps = _work([1])
        with mock.patch.object(repair, "MAX_MISSING_PER_CHAPTER", 3):
            targets, too_many = repair.select_targets([clean, one_gap, many_gaps])
        self.assertEqual([t.work.chapter for t in targets], [120])
        self.assertEqual(targets[0].missing, (3,))
        self.assertEqual([w.chapter for w in too_many], [120])

    def test_build_request_and_context(self):
        target = repair.RepairTarget(_work([1, 2, 4, 5, 6]), (3,))
        request, used = repair.build_request([target])
        self.assertEqual(used, [target])
        self.assertIn("=== Chapter 120 ===", request)
        self.assertIn("⟦3⟧ ZH: 李耀走进第3座大殿", request)
        self.assertIn("EN: MISSING - translate this paragraph", request)
        self.assertIn("EN: Li Yao entered hall 2.", request)
        self.assertIn("李耀走进第3座大殿", repair.chinese_text(used))

    def test_apply_reply(self):
        target = repair.RepairTarget(_work([1, 2, 4, 5, 6]), (3,))
        reply = json.dumps({
            "paragraphs": [{"chapterNo": "Chapter 120", "id": 3, "text": "Li Yao entered hall 3."}],
            "notes": [],
        })
        outcome = repair.apply_reply([target], reply)
        self.assertEqual(outcome.filled, {120: [3]})
        self.assertEqual(target.work.missing_ids(), ())
        self.assertIn("⟦3⟧ Li Yao entered hall 3.", target.work.text)


class ScriptedClient:
    def __init__(self, steps):
        self._steps = list(steps)
        self._lock = threading.Lock()
        self.labels = []

    def chat(self, cfg, messages, max_tokens=None, cancel=None, label=None):
        with self._lock:
            self.labels.append(label or "")
            step = self._steps.pop(0) if self._steps else EMPTY_REPAIR
        if isinstance(step, Exception):
            raise step
        return step, {"total_tokens": 10}


class PipelineRepairTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.book = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, steps, repair_on=True):
        cfg = Config()
        cfg.model = "test-model"
        cfg.term_lookup = False
        cfg.repair = repair_on
        cfg.set_book(self.book, "Test Book")
        client = ScriptedClient(steps)
        pipeline = Pipeline(cfg, client)
        entry = pipeline.pool.make_entry(120, "Chapter 120", PAGE)
        pipeline._run_batch(1, [entry], CancelToken())
        return pipeline, client

    def _saved(self) -> str:
        with open(os.path.join(self.book, "Chapter 120.md"), "r", encoding="utf-8") as f:
            return f.read()

    def test_repair_fills_gap_and_clears_incomplete(self):
        repair_reply = json.dumps({
            "paragraphs": [{"chapterNo": "Chapter 120", "id": 3, "text": "Li Yao entered hall 3."}],
            "notes": [],
        })
        pipeline, client = self._run([_reply([1, 2, 4, 5, 6]), repair_reply])
        self.assertIn("Batch #1: Repair", client.labels[1])
        saved = self._saved()
        self.assertIn("Li Yao entered hall 3.", saved)
        self.assertNotIn("⟦", saved)
        self.assertEqual(pipeline.incomplete(), {})

    def test_unfilled_gap_marks_chapter_incomplete(self):
        pipeline, _client = self._run([_reply([1, 2, 4, 5, 6]), EMPTY_REPAIR])
        incomplete = pipeline.incomplete()
        self.assertIn("120", incomplete)
        self.assertIn("missing ⟦3⟧ after repair", incomplete["120"]["reason"])

    def test_repair_off_saves_incomplete_without_repair_call(self):
        pipeline, client = self._run([_reply([1, 2, 4, 5, 6])], repair_on=False)
        self.assertEqual(len(client.labels), 1)
        self.assertIn("120", pipeline.incomplete())
        self.assertIn("not repaired", pipeline.incomplete()["120"]["reason"])


class PoolIncompleteTests(unittest.TestCase):
    def test_requeue_incomplete_keyword(self):
        pool = PendingPool()
        pool.incomplete["120"] = {"chapter": 120, "label": "Chapter 120", "batch": 1, "reason": "gap", "time": 1.0}
        selection = parse_selection(["incomplete"])
        self.assertIn("incomplete", selection.keywords)
        pool.forget(["120"])
        self.assertEqual(pool.incomplete, {})


if __name__ == "__main__":
    unittest.main()
