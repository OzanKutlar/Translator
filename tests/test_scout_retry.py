"""Tests for the term-query retry and grouped batch logs. Run from the project root: python -m unittest discover tests"""

import json
import os
import tempfile
import threading
import unittest
from unittest import mock

from booktl import storage
from booktl.batchlog import BatchLog
from booktl.cancel import ApiCancelled, CancelToken
from booktl.client import SHORT_ERROR_CHARS, ApiError, _short_error
from booktl.config import Config
from booktl.pipeline import Pipeline, _drift_text

PAGE = "某某书名\n第120章 血色\n" + "李耀走进大殿，苍穹宗的长老看着他，众人大惊。\n" * 6
SCOUT_REPLY = '{"candidateTerms": []}'
TRANSLATION = json.dumps({
    "chapters": [{"chapterNo": "Chapter 120", "content": "# Chapter 120: Blood\n\nLi Yao walked in.\n\n[[Chapter 121]]"}],
    "characterActions": [], "arcEnds": [], "termActions": [],
})


class ScriptedClient:
    """Returns (or raises) the scripted steps in order and records each call's label."""

    def __init__(self, steps):
        self._steps = list(steps)
        self._lock = threading.Lock()
        self.labels = []

    def chat(self, cfg, messages, max_tokens=None, cancel=None, label=None):
        with self._lock:
            self.labels.append(label or "")
            step = self._steps.pop(0) if self._steps else ApiError("script exhausted")
        if isinstance(step, Exception):
            raise step
        return step, {"total_tokens": 10}


class ScoutRetryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.book = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, steps, lookup=True):
        cfg = Config()
        cfg.model = "test-model"
        cfg.term_lookup = lookup
        cfg.set_book(self.book, "Test Book")
        client = ScriptedClient(steps)
        pipeline = Pipeline(cfg, client)
        entry = pipeline.pool.make_entry(120, "Chapter 120", PAGE)
        pipeline._run_batch(1, [entry], CancelToken())
        return pipeline, client

    def _saved(self) -> bool:
        return os.path.isfile(os.path.join(self.book, "Chapter 120.md"))

    def _quarantined(self) -> bool:
        return bool(storage.list_failed(self.book))

    def _assert_failed(self, pipeline):
        _missing, failed = pipeline.problems()
        self.assertIn("120", failed)
        self.assertFalse(self._saved())
        self.assertTrue(self._quarantined())
        self.assertEqual(pipeline.counters["failures"], 1)

    def test_failed_query_is_resent_once_then_translates(self):
        pipeline, client = self._run([ApiError("boom"), SCOUT_REPLY, TRANSLATION])
        self.assertEqual(len(client.labels), 3)
        self.assertTrue(client.labels[0].startswith("Batch #1: Term query"))
        self.assertTrue(client.labels[1].startswith("Batch #1: Term query"))
        self.assertTrue(client.labels[2].startswith("Batch #1: Translation"))
        self.assertTrue(self._saved())
        self.assertEqual(pipeline.problems()[1], {})

    def test_query_failing_twice_marks_chapters_failed(self):
        pipeline, client = self._run([ApiError("first"), ApiError("second")])
        self.assertEqual(len(client.labels), 2)
        self._assert_failed(pipeline)

    def test_unusable_reply_twice_marks_chapters_failed(self):
        pipeline, client = self._run(["not json", "still not json"])
        self.assertEqual(len(client.labels), 2)
        self._assert_failed(pipeline)

    def test_local_search_failure_is_not_resent(self):
        with mock.patch("booktl.termlookup.build_prior_usage", side_effect=OSError("disk")):
            pipeline, client = self._run([SCOUT_REPLY])
        self.assertEqual(len(client.labels), 1)
        self._assert_failed(pipeline)

    def test_lookup_off_sends_one_call(self):
        pipeline, client = self._run([TRANSLATION], lookup=False)
        self.assertEqual(client.labels, ["Batch #1: Translation"])
        self.assertTrue(self._saved())

    def test_cancel_during_query_marks_nothing_failed(self):
        pipeline, client = self._run([ApiCancelled("stop")])
        self.assertEqual(len(client.labels), 1)
        self.assertEqual(pipeline.counters["failures"], 0)
        self.assertFalse(self._quarantined())
        self.assertEqual(pipeline.problems()[1], {})


class ShortErrorTests(unittest.TestCase):
    def test_extracts_message(self):
        body = '{"error":{"code":503,"message":"The model is overloaded.","status":"UNAVAILABLE"}}'
        self.assertEqual(_short_error(503, body), "HTTP 503 · The model is overloaded.")

    def test_empty_and_long_bodies(self):
        self.assertEqual(_short_error(500, ""), "HTTP 500")
        text = _short_error(502, "x " * 500)
        self.assertLessEqual(len(text), len("HTTP 502 · ") + SHORT_ERROR_CHARS)
        self.assertTrue(text.endswith("…"))

    def test_api_error_summary_defaults_to_message(self):
        self.assertEqual(ApiError("short").summary, "short")


class GroupedLogTests(unittest.TestCase):
    def test_drift_lists_each_term_once(self):
        groups = [(("Seraphim", "炽天使"), [831, 832, 833]), (("Earth Law", "地法"), [851])]
        text = _drift_text(groups)
        self.assertEqual(text.count("Seraphim"), 1)
        self.assertIn("Chapters 831–833", text)
        self.assertIn("Chapter 851", text)

    def test_drift_caps_listed_terms(self):
        groups = [((f"Term {i}", f"词{i}"), [i]) for i in range(1, 6)]
        self.assertIn("2 more terms", _drift_text(groups))
        self.assertEqual(_drift_text([]), "")

    def test_batch_log_groups_details_under_headline(self):
        log = BatchLog("Batch #9")
        log.detail("Added 1 character")
        log.warn("Term drift: x")
        log.detail("   ")
        log.head("ok", "Saved 1 chapter")
        lines = log.lines()
        self.assertEqual(len(lines), 3)
        self.assertIn("Batch #9: Saved 1 chapter", lines[0])
        self.assertTrue(all(line.startswith("   ") for line in lines[1:]))

    def test_batch_log_rejects_bad_input(self):
        with self.assertRaises(ValueError):
            BatchLog("")
        with self.assertRaises(ValueError):
            BatchLog("Batch #1").head("loud", "x")


if __name__ == "__main__":
    unittest.main()
