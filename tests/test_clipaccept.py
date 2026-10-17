"""Tests for manual chapter accept (clipboard intake). Run from the project root: python -m unittest discover tests"""

import os
import tempfile
import threading
import time
import unittest

from booktl import clipaccept, storage
from booktl.config import Config
from booktl.pipeline import Pipeline
from booktl.tracker import ChapterTracker

PAGE = "某某书名\n第120章 血色\n" + "李耀走进大殿，苍穹宗的长老看着他，众人大惊。\n" * 6
UNNUMBERED = "这是一段没有编号的文字。\n" * 10


class CheckTests(unittest.TestCase):
    def test_accepts_chinese_page(self):
        self.assertTrue(clipaccept.check_clipboard_text(PAGE).ok)

    def test_empty_is_quiet(self):
        check = clipaccept.check_clipboard_text("   ")
        self.assertFalse(check.ok)
        self.assertTrue(check.quiet)

    def test_too_little_chinese_unless_forced(self):
        text = "Chapter 5 is here. 你好"
        check = clipaccept.check_clipboard_text(text)
        self.assertFalse(check.ok)
        self.assertFalse(check.quiet)
        self.assertTrue(clipaccept.check_clipboard_text(text, force=True).ok)

    def test_manual_prompt_rejected_even_when_forced(self):
        text = "You are a professional literary translator...\n=== BEGIN PAGES ===\n" + PAGE
        self.assertFalse(clipaccept.check_clipboard_text(text, force=True).ok)

    def test_reply_json_rejected(self):
        reply = '{"chapters": [{"chapterNo": "Chapter 1", "content": "x"}]}'
        fence = "`" * 3
        for text in (reply, fence + "json\n" + reply + "\n" + fence):
            check = clipaccept.check_clipboard_text(text, force=True)
            self.assertFalse(check.ok)
            self.assertTrue(check.quiet)

    def test_repeat_and_ignored_text(self):
        fp = clipaccept.fingerprint(PAGE)
        self.assertEqual(fp, clipaccept.fingerprint(PAGE + "  \n\n"))
        self.assertFalse(clipaccept.check_clipboard_text(PAGE, last_fp=fp).ok)
        self.assertFalse(clipaccept.check_clipboard_text(PAGE, ignored_fps=[fp]).ok)

    def test_length_cap(self):
        text = "字" * (clipaccept.MAX_TEXT_CHARS + 1)
        self.assertFalse(clipaccept.check_clipboard_text(text, force=True).ok)


class TrackerTests(unittest.TestCase):
    def test_explicit_chapter_wins(self):
        self.assertEqual(ChapterTracker().observe_page(None, PAGE, chapter=500), (500, "parsed"))

    def test_invalid_explicit_chapter_is_ignored(self):
        self.assertEqual(ChapterTracker().observe_page(None, PAGE, chapter=0), (120, "parsed"))
        self.assertEqual(ChapterTracker().observe_page(None, PAGE, chapter=True), (120, "parsed"))


class AcceptPipelineTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        cfg = Config()
        cfg.set_book(self._tmp.name, "Test Book")
        self.pipeline = Pipeline(cfg, client=None)

    def tearDown(self):
        self._tmp.cleanup()

    def test_accept_queues_and_stores_page(self):
        report = self.pipeline.accept_text(PAGE)
        self.assertEqual(report[0][0], "ok")
        self.assertEqual(self.pipeline.pool.pending_keys(), {"120"})
        self.assertTrue(os.path.isfile(storage.chapter_page_path(self._tmp.name, 120)))

    def test_same_text_twice_is_ignored(self):
        self.pipeline.accept_text(PAGE)
        report = self.pipeline.accept_text(PAGE)
        self.assertEqual(report[0][0], "dim")
        self.assertEqual(len(self.pipeline.pool), 1)

    def test_explicit_chapter_replaces_unnumbered_copy(self):
        first = self.pipeline.accept_text(UNNUMBERED)
        self.assertTrue(any(level == "warn" for level, _ in first))
        self.pipeline.accept_text(UNNUMBERED, chapter=7)
        self.assertEqual(self.pipeline.pool.pending_keys(), {"7"})

    def test_watch_rejections_are_dim(self):
        report = self.pipeline.accept_text("just an English sentence", watch=True)
        self.assertEqual(report[0][0], "dim")
        self.assertEqual(len(self.pipeline.pool), 0)


class WatcherTests(unittest.TestCase):
    def test_new_copies_are_passed_on_and_baseline_skipped(self):
        clipboard = {"text": "baseline"}
        received = []
        got = threading.Event()

        def on_text(text):
            received.append(text)
            got.set()

        watcher = clipaccept.ClipboardWatcher(lambda: clipboard["text"], on_text, poll_seconds=0.01)
        self.assertTrue(watcher.start())
        try:
            clipboard["text"] = "new copy"
            self.assertTrue(got.wait(2))
        finally:
            self.assertTrue(watcher.stop())
        self.assertEqual(received, ["new copy"])

    def test_stops_after_repeated_read_failures(self):
        calls = {"n": 0}

        def read():
            calls["n"] += 1
            if calls["n"] > 1:
                raise RuntimeError("no clipboard")
            return ""

        watcher = clipaccept.ClipboardWatcher(read, lambda text: None, poll_seconds=0.01)
        watcher.start()
        for _ in range(300):
            if not watcher.is_running:
                break
            time.sleep(0.01)
        self.assertFalse(watcher.is_running)
        self.assertGreaterEqual(calls["n"], clipaccept.MAX_READ_FAILURES + 1)
        watcher.stop()


if __name__ == "__main__":
    unittest.main()
