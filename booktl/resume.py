"""Reopen address for each book, stored in <Book>/_resume.json.

    {"version": 1, "url": "https://www.69shuba.com/txt/12345/67890", "chapter": 1240, "updated": 1760270000.0}

The scraper sends the address of every page it scrapes and the latest one becomes the book's
reopen address. When the book is opened again the user is offered that page; the first page
that then arrives from it is ignored by the pipeline, because it was already scraped.
"""

import json
import os
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

RESUME_FILENAME = "_resume.json"
SCHEMA_VERSION = 1
MAX_URL_LENGTH = 2048
ALLOWED_SCHEMES = ("http", "https")

# Scraper requests are handled on several threads, and on Windows a file that is open cannot
# be replaced, so every read and write of _resume.json goes through this lock.
_file_lock = threading.Lock()


@dataclass(frozen=True)
class ReopenPoint:
    """The page to offer when a book is opened again."""

    url: str
    chapter: int | None

    @property
    def label(self) -> str:
        return f"Chapter {self.chapter}" if self.chapter else "the last scraped page"


def resume_path(book_dir: str) -> str:
    return os.path.join(book_dir, RESUME_FILENAME)


def _valid_chapter(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def clean_url(value) -> str | None:
    """Normalised http(s) URL without its #fragment, or None when it is not safe to store or open."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > MAX_URL_LENGTH:
        return None
    if any(ord(ch) <= 32 or ord(ch) == 127 for ch in text):
        return None  # whitespace and control characters never appear in a real page address
    try:
        parts = urlsplit(text)
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES or not parts.netloc:
        return None
    return urlunsplit((scheme, parts.netloc.lower(), parts.path, parts.query, ""))


# ----------------------------------------------------------------------
# File access
# ----------------------------------------------------------------------

def _discard_temp(tmp_path: str) -> None:
    """Best-effort cleanup after a failed save; the caller re-raises the original error."""
    try:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
    except OSError:
        pass  # the save error being re-raised is the one that matters


def _write_atomic(path: str, data: dict) -> None:
    """Write via a temp file and rename, so a crash never leaves half a file."""
    tmp_path = path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp_path, path)
    except OSError:
        _discard_temp(tmp_path)
        raise


def _read_file(path: str) -> dict | None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None  # deliberately non-fatal: a broken file only means nothing is offered
    return data if isinstance(data, dict) else None


def save_reopen(book_dir: str, url, chapter=None) -> None:
    """Make url the book's reopen address. Raises OSError/ValueError; the caller logs it."""
    if not book_dir or not os.path.isdir(book_dir):
        raise ValueError(f"Book directory does not exist: {book_dir!r}")
    clean = clean_url(url)
    if clean is None:
        raise ValueError(f"Not a usable page address: {url!r}")
    data = {
        "version": SCHEMA_VERSION,
        "url": clean,
        "chapter": _valid_chapter(chapter),
        "updated": time.time(),
    }
    with _file_lock:
        _write_atomic(resume_path(book_dir), data)


def load_reopen(book_dir: str) -> ReopenPoint | None:
    """The book's reopen address, or None when there is none or the file is unusable. Never raises."""
    if not book_dir or not os.path.isdir(book_dir):
        return None
    path = resume_path(book_dir)
    if not os.path.isfile(path):
        return None
    with _file_lock:
        data = _read_file(path)
    url = clean_url(data.get("url")) if data else None
    if url is None:
        return None
    return ReopenPoint(url, _valid_chapter(data.get("chapter")))
