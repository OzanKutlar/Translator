"""Tests for the reopen address. Run from the project root: python -m unittest discover tests"""

import tempfile
import unittest
from unittest import mock

from booktl import console as con
from booktl import resume
from booktl.config import Config
from booktl.library import ask_reopen
from booktl.pipeline import Pipeline

URL = "https://www.69shuba.com/txt/12345/67890"
NEXT_URL = "https://www.69shuba.com/txt/12345/67891"
PAGE = "某某书名\n第120章 血色\n" + "李耀走进大殿，苍穹宗的长老看着他，众人大惊。\n" * 6
NEXT_PAGE = PAGE.replace("第120章", "第121章")


class CleanUrlTests(unittest.TestCase):
    def test_accepts_http_and_drops_fragment(self):
        self.assertEqual(resume.clean_url(URL + "#top"), URL)
        self.assertEqual(resume.clean_url("  " + URL + "  "), URL)
        self.assertEqual(resume.clean_url("HTTPS://WWW.69shuba.com/txt/1/2"), "https://www.69shuba.com/txt/1/2")

    def test_rejects_unusable(self):
        values = (
            None, 5, "", "javascript:alert(1)", "file:///etc/passwd", "https://",
            "https://a b.com/x", "https://x.com/\x07", "https://x.com/" + "a" * resume.MAX_URL_LENGTH,
        )
        for value in values:
            self.assertIsNone(resume.clean_url(value), repr(value))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.book = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_round_trip(self):
        resume.save_reopen(self.book, URL, 120)
        point = resume.load_reopen(self.book)
        self.assertEqual((point.url, point.chapter, point.label), (URL, 120, "Chapter 120"))

    def test_missing_and_corrupt_file(self):
        self.assertIsNone(resume.load_reopen(self.book))
        with open(resume.resume_path(self.book), "w", encoding="utf-8") as f:
            f.write("{not json")
        self.assertIsNone(resume.load_reopen(self.book))

    def test_save_rejects_bad_url(self):
        with self.assertRaises(ValueError):
            resume.save_reopen(self.book, "ftp://example.com/x", 1)


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.book = self._tmp.name
        cfg = Config()
        cfg.set_book(self.book, "Test Book")
        self.pipeline = Pipeline(cfg, client=None)
        patcher = mock.patch("booktl.pipeline.time.sleep")  # skip the scraper pacing delay
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self._tmp.cleanup()

    def _reopen_at(self, url: str, chapter: int) -> None:
        resume.save_reopen(self.book, url, chapter)
        self.pipeline.load_reopen_address()

    def test_scraped_page_sets_reopen_address(self):
        self.pipeline.offer_page(PAGE, True, url=URL)
        point = resume.load_reopen(self.book)
        self.assertEqual((point.url, point.chapter), (URL, 120))

    def test_accept_does_not_touch_reopen_address(self):
        self.pipeline.accept_text(PAGE)
        self.assertIsNone(resume.load_reopen(self.book))

    def test_first_page_from_reopen_address_is_ignored_once(self):
        self._reopen_at(URL, 120)
        directive = self.pipeline.offer_page(PAGE, True, url=URL + "#top")
        self.assertEqual(directive["action"], "next")
        self.assertEqual(len(self.pipeline.pool), 0)
        self.pipeline.offer_page(PAGE, True, url=URL)
        self.assertEqual(self.pipeline.pool.pending_keys(), {"120"})

    def test_other_pages_are_queued_and_become_reopen_address(self):
        self._reopen_at(URL, 120)
        self.pipeline.offer_page(NEXT_PAGE, True, url=NEXT_URL)
        self.assertEqual(self.pipeline.pool.pending_keys(), {"121"})
        self.assertEqual(resume.load_reopen(self.book).url, NEXT_URL)


class AskReopenTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.book = self._tmp.name
        resume.save_reopen(self.book, URL, 120)

    def tearDown(self):
        self._tmp.cleanup()

    def _ask(self, side_effect, book=None):
        with mock.patch.object(con.console, "input", side_effect=side_effect):
            return ask_reopen(book or self.book)

    def test_enter_means_yes(self):
        self.assertEqual(self._ask([""]), URL)

    def test_no(self):
        self.assertIsNone(self._ask(["n"]))

    def test_eof_means_no(self):
        self.assertIsNone(self._ask(EOFError()))

    def test_invalid_answer_is_asked_again(self):
        self.assertEqual(self._ask(["maybe", "Y"]), URL)

    def test_no_question_without_address(self):
        with tempfile.TemporaryDirectory() as empty:
            self.assertIsNone(self._ask(AssertionError("should not ask"), book=empty))


if __name__ == "__main__":
    unittest.main()
