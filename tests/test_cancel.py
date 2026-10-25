"""Tests for cancelling in-flight batches. Run from the project root: python -m unittest discover tests"""

import json
import tempfile
import threading
import time
import unittest
from unittest import mock

from booktl.cancel import ApiCancelled, CancelToken
from booktl.client import ApiClient
from booktl.config import Config
from booktl.pipeline import Pipeline

PAGE = "某某书名\n第{n}章 血色\n" + "李耀走进大殿，苍穹宗的长老看着他，众人大惊。\n" * 6
OK_BODY = json.dumps({"choices": [{"message": {"content": "ok"}}], "usage": None})
ABORT_BODY = '{"error":{"message":"Cancelled via console command","type":"human_abort","code":"aborted"}}'


class FakeResponse:
    def __init__(self, status: int, text: str):
        self.status_code = status
        self.text = text
        self.headers = {}

    def json(self):
        return json.loads(self.text)


def _cfg() -> Config:
    cfg = Config()
    cfg.model = "test-model"
    cfg.retries = 2
    cfg.json_mode = "off"
    return cfg


class BlockingClient:
    """chat() blocks until its batch is cancelled, then raises ApiCancelled like ApiClient."""

    def __init__(self):
        self.started = threading.Event()
        self.calls = 0

    def chat(self, cfg, messages, max_tokens=None, cancel=None, label=None):
        self.calls += 1
        self.started.set()
        if cancel is None or not cancel.wait(5):
            raise AssertionError("chat was not cancelled")
        raise ApiCancelled("cancelled")


class PipelineCancelTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        cfg = _cfg()
        cfg.batch_size = 2
        cfg.term_lookup = False
        cfg.set_book(self._tmp.name, "Test Book")
        self.client = BlockingClient()
        self.pipeline = Pipeline(cfg, self.client)
        self.pipeline.start()

    def tearDown(self):
        self.pipeline.shutdown(drain=False)
        self._tmp.cleanup()

    def _wait_for(self, predicate) -> bool:
        for _ in range(200):
            if predicate():
                return True
            time.sleep(0.02)
        return predicate()

    def test_cancel_removes_batch_from_flight_at_once(self):
        self.pipeline.accept_text(PAGE.format(n=120))
        self.pipeline.accept_text(PAGE.format(n=121))
        self.assertTrue(self.client.started.wait(3))
        self.assertEqual(self.pipeline.queue_counts()["in_flight_batches"], 1)

        self.pipeline.set_auto_dispatch(False)
        messages = self.pipeline.cancel()
        self.assertTrue(any("aborted" in m for m in messages))

        counts = self.pipeline.queue_counts()
        self.assertEqual(counts["in_flight"], 0)
        self.assertEqual(counts["in_flight_batches"], 0)
        self.assertEqual(counts["pending"], 2)

        self.assertTrue(self._wait_for(lambda: self.pipeline.counters["batches_done"] == 1))
        self.assertEqual(self.pipeline.counters["failures"], 0)
        self.assertEqual(self.pipeline.queue_counts()["failed"], 0)
        self.assertEqual(self.client.calls, 1)


class ClientCancelTests(unittest.TestCase):
    def test_reply_after_cancel_is_dropped(self):
        token = CancelToken()
        calls = []

        def fake_post(*args, **kwargs):
            calls.append(1)
            token.cancel()
            return FakeResponse(400, ABORT_BODY)

        with mock.patch("booktl.client.requests.post", side_effect=fake_post):
            with self.assertRaises(ApiCancelled):
                ApiClient().chat(_cfg(), [{"role": "user", "content": "x"}], cancel=token)
        self.assertEqual(len(calls), 1)

    def test_cancel_during_backoff_stops_retries(self):
        token = CancelToken()
        calls = []

        def fake_post(*args, **kwargs):
            calls.append(1)
            return FakeResponse(500, "server error")

        timer = threading.Timer(0.2, token.cancel)
        started = time.monotonic()
        with mock.patch("booktl.client.requests.post", side_effect=fake_post):
            timer.start()
            try:
                with self.assertRaises(ApiCancelled):
                    ApiClient().chat(_cfg(), [{"role": "user", "content": "x"}], cancel=token)
            finally:
                timer.cancel()
        self.assertLess(time.monotonic() - started, 0.9)  # the 1s backoff was cut short
        self.assertEqual(len(calls), 1)

    def test_blocked_request_is_abandoned(self):
        token = CancelToken()
        release = threading.Event()

        def fake_post(*args, **kwargs):
            release.wait(5)
            return FakeResponse(200, OK_BODY)

        timer = threading.Timer(0.2, token.cancel)
        started = time.monotonic()
        with mock.patch("booktl.client.requests.post", side_effect=fake_post):
            timer.start()
            try:
                with self.assertRaises(ApiCancelled):
                    ApiClient().chat(_cfg(), [{"role": "user", "content": "x"}], cancel=token)
            finally:
                timer.cancel()
                release.set()
        self.assertLess(time.monotonic() - started, 2.0)

    def test_400_is_still_retried_without_cancel(self):
        replies = [FakeResponse(400, ABORT_BODY), FakeResponse(200, OK_BODY)]
        with mock.patch("booktl.client.requests.post", side_effect=replies) as post, \
                mock.patch("booktl.client.time.sleep"):
            content, _usage = ApiClient().chat(_cfg(), [{"role": "user", "content": "x"}])
        self.assertEqual(content, "ok")
        self.assertEqual(post.call_count, 2)

    def test_rejects_non_token(self):
        with self.assertRaises(TypeError):
            ApiClient().chat(_cfg(), [], cancel=True)


class TokenTests(unittest.TestCase):
    def test_wait_returns_early_once_cancelled(self):
        token = CancelToken()
        self.assertFalse(token.wait(0))
        token.cancel()
        self.assertTrue(token.is_cancelled)
        self.assertTrue(token.wait(5))

    def test_wait_rejects_bad_values(self):
        for value in (-1, True, "1"):
            with self.assertRaises(ValueError):
                CancelToken().wait(value)


if __name__ == "__main__":
    unittest.main()
