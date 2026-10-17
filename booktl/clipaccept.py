"""Clipboard intake for manual chapter accept mode.

'accept' reads the clipboard once; 'accept on' starts a ClipboardWatcher that hands every
newly copied text to a callback. check_clipboard_text() decides whether a text can be queued
as a raw chapter page. It always rejects the translator's own clipboard output (the prompt
copied by 'manual' and translation JSON meant for 'paste'), so watch mode never queues it.
"""

import hashlib
import json
import re
import threading
from dataclasses import dataclass

from rich.markup import escape

from . import console as con

MIN_CJK_CHARS = 50
MAX_TEXT_CHARS = 200_000
MAX_IGNORED_FINGERPRINTS = 20
WATCH_POLL_SECONDS = 1.0
MAX_WATCH_POLLS = 50_000_000   # bounded-loop guard (~1.5 years of polling once a second)
MAX_READ_FAILURES = 5          # consecutive failed clipboard reads before watching stops
STOP_JOIN_SECONDS = 2.0
PROMPT_MARKERS = (
    "=== BEGIN PAGES ===",
    "=== END PAGES ===",
    "You are a professional literary translator",
)
REPLY_KEYS = ('"chapters"', '"chapterNo"', '"candidateTerms"', '"characterActions"')
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_WS_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class ClipCheck:
    """Outcome of check_clipboard_text. quiet marks expected rejections (own output, repeats)."""

    ok: bool
    reason: str = ""
    quiet: bool = False


def fingerprint(text) -> str:
    """Hash of the whitespace-normalised text; '' for anything that is not a non-empty string."""
    if not isinstance(text, str):
        return ""
    normalised = _WS_RE.sub(" ", text).strip()
    if not normalised:
        return ""
    return hashlib.sha1(normalised.encode("utf-8")).hexdigest()


def count_cjk(text) -> int:
    """Number of Chinese characters in text."""
    if not isinstance(text, str):
        return 0
    return len(_CJK_RE.findall(text))


def _strip_fence(text: str) -> str:
    head = text.lstrip()
    if not head.startswith("```"):
        return head
    head = head[3:]
    if head[:4].lower() == "json":
        head = head[4:]
    return head.lstrip()


def looks_like_reply_json(text) -> bool:
    """True for JSON such as a translation reply; raw pages never start with '{' or '['."""
    if not isinstance(text, str):
        return False
    head = _strip_fence(text)
    if not head.startswith(("{", "[")):
        return False
    if any(key in head for key in REPLY_KEYS):
        return True
    try:
        json.loads(head)
    except ValueError:
        return False
    return True


def check_clipboard_text(text, last_fp: str = "", ignored_fps=(), force: bool = False) -> ClipCheck:
    """Decide whether copied text can be queued as a raw chapter page.

    last_fp is the fingerprint of the text accepted last; ignored_fps are fingerprints of text
    the translator copied itself. force skips only the Chinese-character minimum.
    """
    if not isinstance(text, str) or not text.strip():
        return ClipCheck(False, "The clipboard is empty.", quiet=True)
    if len(text) > MAX_TEXT_CHARS:
        return ClipCheck(False, f"The copied text has {len(text):,} characters; the limit is {MAX_TEXT_CHARS:,}.")
    if any(marker in text for marker in PROMPT_MARKERS):
        return ClipCheck(False, "The clipboard holds a translation prompt (from 'manual'), not a raw page.", quiet=True)
    if looks_like_reply_json(text):
        return ClipCheck(False, "The clipboard holds JSON, not a raw page. Use 'paste' for translation replies.", quiet=True)
    fp = fingerprint(text)
    if fp in (ignored_fps or ()):
        return ClipCheck(False, "The clipboard holds text the translator copied itself.", quiet=True)
    if last_fp and fp == last_fp:
        return ClipCheck(
            False, "This text was already accepted. Use 'accept <n>' to queue it under another chapter number.", quiet=True
        )
    if not force:
        cjk = count_cjk(text)
        if cjk < MIN_CJK_CHARS:
            return ClipCheck(
                False,
                f"The copied text has only {cjk} Chinese character(s) (at least {MIN_CJK_CHARS} needed). "
                "Use 'accept force' to take it anyway.",
            )
    return ClipCheck(True)


class ClipboardWatcher:
    """Polls the clipboard on a daemon thread and passes every newly copied text to on_text.

    read_clipboard and on_text are injected, so the watcher knows nothing about the pipeline.
    """

    def __init__(self, read_clipboard, on_text, poll_seconds: float = WATCH_POLL_SECONDS):
        if not callable(read_clipboard) or not callable(on_text):
            raise TypeError("read_clipboard and on_text must be callable")
        if isinstance(poll_seconds, bool) or not isinstance(poll_seconds, (int, float)) or poll_seconds <= 0:
            raise ValueError("poll_seconds must be a positive number")
        self._read = read_clipboard
        self._on_text = on_text
        self._poll = float(poll_seconds)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_text = ""

    def _alive_locked(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and not self._stop.is_set()

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._alive_locked()

    def start(self) -> bool:
        """Start watching; what is on the clipboard now counts as seen. False if already running.

        Raises whatever read_clipboard raises, so the caller can report why watching could not start.
        """
        baseline = self._read()
        with self._lock:
            if self._alive_locked():
                return False
            self._last_text = baseline if isinstance(baseline, str) else ""
            self._stop = threading.Event()
            self._thread = threading.Thread(
                target=self._run, args=(self._stop,), daemon=True, name="booktl-clipboard-watch"
            )
            self._thread.start()
        return True

    def stop(self) -> bool:
        """Stop watching. Returns True when the watcher was running."""
        with self._lock:
            was_running = self._alive_locked()
            thread = self._thread
            self._stop.set()
            self._thread = None
        if thread is not None and thread is not threading.current_thread():
            thread.join(STOP_JOIN_SECONDS)
        return was_running

    def _run(self, stop: threading.Event) -> None:
        failures = 0
        for _ in range(MAX_WATCH_POLLS):
            if stop.wait(self._poll):
                return
            try:
                text = self._read()
            except Exception as e:  # pyperclip raises platform-specific exception types
                failures += 1
                if failures >= MAX_READ_FAILURES:
                    con.fail(f"Clipboard watch stopped after {failures} failed reads: {escape(str(e))}")
                    stop.set()
                    return
                continue
            failures = 0
            self._handle(text, stop)
        con.warn("Clipboard watch reached its poll limit and stopped.")
        stop.set()

    def _handle(self, text, stop: threading.Event) -> None:
        """Pass text on once per change; blank and unchanged clipboards are ignored."""
        if not isinstance(text, str) or not text.strip():
            return
        with self._lock:
            if text == self._last_text:
                return
            self._last_text = text
        if stop.is_set():
            return
        try:
            self._on_text(text)
        except Exception as e:  # one bad copy must never kill the watcher
            con.fail(f"Clipboard watch could not process the copied text: {escape(str(e))}")
