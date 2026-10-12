"""When each book was last translated, stored in <Book>/_activity.json.

    {"version": 1, "lastTranslated": 1760270000.0, "lastChapter": 1240}

The book picker lists recently translated books first. A book without this file falls back
to the modification time of its newest chapter file (e.g. "Chapter 1240.md"), so books
translated before the file existed still sort sensibly.
"""

import json
import os
import threading
import time

from .parsing import parse_chapter_number

ACTIVITY_FILENAME = "_activity.json"
SCHEMA_VERSION = 1
MAX_SCAN_ENTRIES = 20_000   # bounds the chapter-file fallback scan per book
SECONDS_PER_DAY = 86_400

# Books translated within this many days count as recent and are listed first.
# None means every book with a recorded translation counts as recent.
RECENT_WINDOW_DAYS: float | None = None

# Workers can finish batches concurrently, and on Windows a file that is open cannot be
# replaced, so every read and write of _activity.json goes through this lock.
_file_lock = threading.Lock()


def activity_path(book_dir: str) -> str:
    return os.path.join(book_dir, ACTIVITY_FILENAME)


def _valid_chapter(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _valid_stamp(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    return float(value)


# ----------------------------------------------------------------------
# File access
# ----------------------------------------------------------------------

def _read_file(book_dir: str) -> dict | None:
    """Parsed _activity.json, or None when it is absent or unusable (the fallback applies)."""
    path = activity_path(book_dir)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None  # deliberately non-fatal: a broken file must never block the book picker
    return data if isinstance(data, dict) else None


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


def record_translation(book_dir: str, chapter_numbers=None) -> None:
    """Stamp the book as translated now. Raises OSError/ValueError; the caller logs it.

    lastChapter is the highest chapter in chapter_numbers, or the previous value when
    the batch had no readable chapter numbers.
    """
    if not book_dir or not os.path.isdir(book_dir):
        raise ValueError(f"Book directory does not exist: {book_dir!r}")
    numbers = [n for n in (chapter_numbers or []) if _valid_chapter(n) is not None]
    with _file_lock:
        previous = _read_file(book_dir) or {}
        last_chapter = max(numbers) if numbers else _valid_chapter(previous.get("lastChapter"))
        _write_atomic(activity_path(book_dir), {
            "version": SCHEMA_VERSION,
            "lastTranslated": time.time(),
            "lastChapter": last_chapter,
        })


# ----------------------------------------------------------------------
# Lookup
# ----------------------------------------------------------------------

def _chapter_file_info(entry) -> tuple[float, int] | None:
    """(mtime, chapter) for a translated chapter file such as 'Chapter 12.md', else None."""
    name = entry.name
    if not name.lower().endswith(".md"):
        return None
    chapter = parse_chapter_number(name[:-3])
    if chapter is None:
        return None
    try:
        if not entry.is_file():
            return None
        return entry.stat().st_mtime, chapter
    except OSError:
        return None  # a file removed mid-scan simply drops out of the fallback


def _newest_chapter_file(book_dir: str) -> tuple[float | None, int | None]:
    """Modification time and chapter of the newest chapter file, scanning at most MAX_SCAN_ENTRIES."""
    best = None
    try:
        with os.scandir(book_dir) as entries:
            for count, entry in enumerate(entries):
                if count >= MAX_SCAN_ENTRIES:
                    break
                info = _chapter_file_info(entry)
                if info is not None and (best is None or info[0] > best[0]):
                    best = info
    except OSError:
        return None, None  # unreadable folder: it sorts with the books that have no activity
    return best if best is not None else (None, None)


def last_translated(book_dir: str) -> tuple[float | None, int | None]:
    """(timestamp, last chapter) of the latest translation, or (None, None). Never raises."""
    if not book_dir or not os.path.isdir(book_dir):
        return None, None
    with _file_lock:
        data = _read_file(book_dir)
    stamp = _valid_stamp(data.get("lastTranslated")) if data else None
    if stamp is not None:
        return stamp, _valid_chapter(data.get("lastChapter"))
    return _newest_chapter_file(book_dir)


def is_recent(stamp, now: float | None = None) -> bool:
    """True when stamp falls inside RECENT_WINDOW_DAYS (any valid stamp when the window is None)."""
    valid = _valid_stamp(stamp)
    if valid is None:
        return False
    if RECENT_WINDOW_DAYS is None:
        return True
    current = time.time() if now is None else now
    return current - valid <= RECENT_WINDOW_DAYS * SECONDS_PER_DAY


def format_age(stamp, now: float | None = None) -> str:
    """'just now', '5m ago', '2h ago', '3d ago', or a date for anything older than 30 days."""
    valid = _valid_stamp(stamp)
    if valid is None:
        return ""
    current = time.time() if now is None else now
    seconds = max(0.0, current - valid)
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < SECONDS_PER_DAY:
        return f"{int(seconds // 3600)}h ago"
    if seconds < 30 * SECONDS_PER_DAY:
        return f"{int(seconds // SECONDS_PER_DAY)}d ago"
    return time.strftime("%Y-%m-%d", time.localtime(valid))
