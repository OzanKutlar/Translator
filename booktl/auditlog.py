"""Audit progress for each book, stored in <Book>/_qa/audit/progress.json.

    {"version": 1, "auditedThrough": 1250,
     "history": [{"from": 1200, "to": 1250, "time": 1760270000.0, "findings": "audit_20261113-101500.md"}]}

auditedThrough is the highest chapter ever audited, so auditing an older range never moves it back.
audit.py records a part's chapters once combineCopy finishes that part, and a whole range when it had
nothing to send. 'audit.py --next N' continues with the chapters after auditedThrough.
"""

import json
import os
import threading
import time
from dataclasses import dataclass

from . import activity, qa

AUDIT_DIRNAME = "audit"
PROGRESS_FILENAME = "progress.json"
SCHEMA_VERSION = 1
MAX_HISTORY = 50

# Parts are recorded one after another, and on Windows a file that is open cannot be replaced,
# so every read and write of progress.json goes through this lock.
_file_lock = threading.Lock()


@dataclass(frozen=True)
class AuditRun:
    """One recorded audit: the chapters it covered."""

    low: int
    high: int
    time: float | None = None
    findings: str = ""

    @property
    def label(self) -> str:
        return f"Chapter {self.low}" if self.low == self.high else f"Chapters {self.low}–{self.high}"


@dataclass(frozen=True)
class Progress:
    """How far a book has been audited, plus the most recent runs (oldest first)."""

    audited_through: int
    history: tuple = ()

    @property
    def last_run(self) -> AuditRun | None:
        return self.history[-1] if self.history else None


def _chapter(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _stamp(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    return float(value)


def progress_path(book_dir: str) -> str:
    """Path of the book's progress file (it may not exist). Raises ValueError."""
    if not book_dir:
        raise ValueError("No book directory selected")
    return os.path.join(book_dir, qa.QA_DIRNAME, AUDIT_DIRNAME, PROGRESS_FILENAME)


def _display_name() -> str:
    return f"{qa.QA_DIRNAME}/{AUDIT_DIRNAME}/{PROGRESS_FILENAME}"


def _runs(raw) -> list[AuditRun]:
    """Valid history entries, the newest MAX_HISTORY of them."""
    if not isinstance(raw, list):
        return []
    runs = []
    for item in raw[-MAX_HISTORY:]:
        if not isinstance(item, dict):
            continue
        low, high = _chapter(item.get("from")), _chapter(item.get("to"))
        if low is None or high is None or high < low:
            continue
        runs.append(AuditRun(low, high, _stamp(item.get("time")), str(item.get("findings") or "")))
    return runs


def _run_record(run: AuditRun) -> dict:
    return {"from": run.low, "to": run.high, "time": run.time, "findings": run.findings}


def _read(path: str) -> dict:
    """Raises OSError/ValueError."""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{PROGRESS_FILENAME} does not have the expected structure")
    return data


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


def load_progress(book_dir: str) -> tuple[Progress | None, str | None]:
    """The book's audit progress; (None, None) when nothing is recorded, (None, error) when unreadable."""
    try:
        path = progress_path(book_dir)
    except ValueError:
        return None, None
    if not os.path.isfile(path):
        return None, None
    with _file_lock:
        try:
            data = _read(path)
        except (OSError, ValueError) as e:
            return None, f"Could not read {_display_name()} ({e}); fix or remove the file"
    through = _chapter(data.get("auditedThrough"))
    if through is None:
        return None, f"{_display_name()} has no valid auditedThrough; fix or remove the file"
    return Progress(through, tuple(_runs(data.get("history")))), None


def record_audit(book_dir: str, low, high, findings_path="") -> Progress:
    """Record that chapters low..high were audited. Raises OSError/ValueError; the caller reports it."""
    first, last = _chapter(low), _chapter(high)
    if first is None or last is None or last < first:
        raise ValueError(f"Invalid chapter span {low!r}-{high!r}")
    path = progress_path(book_dir)
    run = AuditRun(first, last, time.time(), os.path.basename(str(findings_path or "")))
    with _file_lock:
        previous = _read(path) if os.path.isfile(path) else {}
        through = max(_chapter(previous.get("auditedThrough")) or 0, last)
        runs = (_runs(previous.get("history")) + [run])[-MAX_HISTORY:]
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _write_atomic(path, {
            "version": SCHEMA_VERSION,
            "auditedThrough": through,
            "history": [_run_record(r) for r in runs],
        })
    return Progress(through, tuple(runs))


def next_span(audited_through, count, last) -> tuple[int, int]:
    """The count chapters after audited_through (from Chapter 1 when None), capped at last. Raises ValueError."""
    if _chapter(last) is None:
        raise ValueError("The book has no translated chapters.")
    if _chapter(count) is None:
        raise ValueError("The number of chapters must be a positive whole number.")
    start = (_chapter(audited_through) or 0) + 1
    if start > last:
        raise ValueError(f"Every translated chapter up to Chapter {last} is already audited.")
    return start, min(last, start + count - 1)


def describe(progress) -> str:
    """'Last audit: Chapters 1200–1250 (2d ago) · audited through Chapter 1250'; '' for no progress."""
    if not isinstance(progress, Progress):
        return ""
    through = f"audited through Chapter {progress.audited_through}"
    run = progress.last_run
    if run is None:
        return through[0].upper() + through[1:]
    age = activity.format_age(run.time) if run.time else ""
    when = f" ({age})" if age else ""
    return f"Last audit: {run.label}{when} · {through}"
