"""Source-error reports: typos the model corrected and author mistakes it kept as written.

Nothing about the Chinese is corrected or kept silently. The model lists every typo it fixed in
"sourceFixes" and every author mistake it kept in "continuity". Each entry is checked against the
page, shown under the batch and appended to a log, one JSON object per line:

    <Book>/_qa/source_fixes.jsonl   {"chapter": 812, "afterId": 4, "zh": "沉有", "correction": "没有",
                                     "status": "verified", "reason": "", "batch": 78, "time": 1760270000.0}
    <Book>/_qa/continuity.jsonl     {"chapter": 812, "afterId": 7, "zh": "…", "issue": "…",
                                     "status": "on page", "reason": "", "batch": 78, "time": 1760270000.0}

A fix is "verified" when its text is on the page and the correction changes one or two characters;
otherwise it is "unverified" and the translation needs a look.
"""

import json
import os
import re
import threading
import time

from . import parsing, qa

SOURCE_FIXES_FILENAME = "source_fixes.jsonl"
CONTINUITY_FILENAME = "continuity.jsonl"
REVIEW_FILENAME = "review.jsonl"     # reviewer edits: applied, rejected and undone
MAX_REPORTS_PER_REPLY = 100
MAX_FIELD_CHARS = 300
MAX_READ_RECORDS = 20_000
MAX_TYPO_CHARS = 40
MAX_TYPO_EDITS = 2
_SPACE_RE = re.compile(r"\s+")

# Workers can append concurrently, so every read and write of the logs goes through this lock.
_file_lock = threading.Lock()


def _clean(value, limit: int = MAX_FIELD_CHARS) -> str:
    if value is None or isinstance(value, (dict, list)):
        return ""
    return _SPACE_RE.sub(" ", str(value)).strip()[:limit]


def _marker(value) -> int | None:
    if isinstance(value, str) and value.strip().isdecimal():
        value = int(value.strip())
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def typo_problem(zh: str, correction: str) -> str:
    """Why zh -> correction is not a plausible typo fix, or '' when it is."""
    if not zh or not correction:
        return "a fix needs the text as written (zh) and the correction"
    if max(len(zh), len(correction)) > MAX_TYPO_CHARS:
        return "too long to be a typo"
    edits = qa.edit_distance(zh, correction)
    if edits == 0:
        return "the correction is the same as the text"
    if edits > MAX_TYPO_EDITS:
        return f"a typo changes one or two characters; this correction changes {edits}"
    return ""


def _locate(paragraphs: list, zh: str, preferred) -> int | None:
    """Marker of the paragraph containing zh, preferring the one the model named."""
    if not zh:
        return None
    if preferred is not None and 0 <= preferred < len(paragraphs) and zh in paragraphs[preferred]:
        return preferred
    for index, paragraph in enumerate(paragraphs):
        if isinstance(paragraph, str) and zh in paragraph:
            return index
    return None


def _chapter_of(item, saved: set, kind: str, skipped: list) -> int | None:
    """Chapter of a report, or None (with a reason in skipped) when it is not usable."""
    if not isinstance(item, dict):
        skipped.append(f"a {kind} entry is not an object")
        return None
    chapter = parsing.chapter_value(item.get("chapterNo"))
    if chapter is None or chapter not in saved:
        name = _clean(item.get("chapterNo"), 40) or "?"
        skipped.append(f"{kind} entry for {name}: not a chapter saved in this reply")
        return None
    return chapter


def _base(item: dict, chapter: int, batch) -> dict:
    return {
        "chapter": chapter,
        "afterId": _marker(item.get("afterId")),
        "zh": _clean(item.get("zh")),
        "batch": batch,
        "time": time.time(),
    }


def _bounded(items, kind: str, skipped: list) -> list:
    if not isinstance(items, list):
        return []
    if len(items) > MAX_REPORTS_PER_REPLY:
        skipped.append(f"{len(items)} {kind} entries in one reply; only the first {MAX_REPORTS_PER_REPLY} were read")
    return items[:MAX_REPORTS_PER_REPLY]


def check_fixes(items, zh_by_chapter: dict, saved, batch) -> tuple[list[dict], list[str]]:
    """Records for the model's typo fixes, each verified or unverified, plus reasons for skipped entries."""
    records, skipped = [], []
    saved_set = set(saved or ())
    for item in _bounded(items, "sourceFixes", skipped):
        chapter = _chapter_of(item, saved_set, "sourceFixes", skipped)
        if chapter is None:
            continue
        record = _base(item, chapter, batch)
        record["correction"] = _clean(item.get("correction"))
        problem = typo_problem(record["zh"], record["correction"])
        paragraphs = (zh_by_chapter or {}).get(chapter) or []
        if not problem and paragraphs:
            place = _locate(paragraphs, record["zh"], record["afterId"])
            if place is None:
                problem = "the text is not on the page"
            else:
                record["afterId"] = place
        record["status"] = "unverified" if problem else "verified"
        record["reason"] = problem
        records.append(record)
    return records, skipped


def check_continuity(items, zh_by_chapter: dict, saved, batch) -> tuple[list[dict], list[str]]:
    """Records for author mistakes the model kept as written, plus reasons for skipped entries."""
    records, skipped = [], []
    saved_set = set(saved or ())
    for item in _bounded(items, "continuity", skipped):
        chapter = _chapter_of(item, saved_set, "continuity", skipped)
        if chapter is None:
            continue
        record = _base(item, chapter, batch)
        record["issue"] = _clean(item.get("issue"))
        if not record["issue"]:
            skipped.append(f"continuity entry for Chapter {chapter} names no issue")
            continue
        paragraphs = (zh_by_chapter or {}).get(chapter) or []
        place = _locate(paragraphs, record["zh"], record["afterId"]) if paragraphs else record["afterId"]
        found = place is not None or not paragraphs
        record["afterId"] = place
        record["status"] = "on page" if found else "not on page"
        record["reason"] = "" if found else "the quoted text is not on the page"
        records.append(record)
    return records, skipped


def log_path(book_dir: str, filename: str) -> str:
    """Path of one of the two logs. Raises ValueError."""
    if not book_dir:
        raise ValueError("No book directory selected")
    if filename not in (SOURCE_FIXES_FILENAME, CONTINUITY_FILENAME, REVIEW_FILENAME):
        raise ValueError(f"Unknown log file: {filename!r}")
    return os.path.join(book_dir, qa.QA_DIRNAME, filename)


def append_records(book_dir: str, filename: str, records) -> int:
    """Append records to a log. Returns how many were written. Raises OSError/ValueError."""
    path = log_path(book_dir, filename)
    lines = [json.dumps(r, ensure_ascii=False) for r in (records or []) if isinstance(r, dict)]
    if not lines:
        return 0
    with _file_lock:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    return len(lines)


def _parse_lines(lines: list[str]) -> tuple[list[dict], int]:
    records, bad = [], 0
    for line in lines:
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except ValueError:
            bad += 1
            continue
        if isinstance(data, dict):
            records.append(data)
        else:
            bad += 1
    return records, bad


def read_records(book_dir: str, filename: str, limit: int = MAX_READ_RECORDS) -> tuple[list[dict], str | None]:
    """The newest records of a log, oldest first, and an error message when lines were unreadable."""
    path = log_path(book_dir, filename)
    if not os.path.isfile(path):
        return [], None
    try:
        with _file_lock:
            with open(path, "r", encoding="utf-8") as f:
                lines = f.read().splitlines()
    except (OSError, ValueError) as e:
        return [], f"Could not read {qa.QA_DIRNAME}/{filename}: {e}"
    records, bad = _parse_lines(lines[-max(1, int(limit)):])
    error = f"{bad} unreadable line(s) in {qa.QA_DIRNAME}/{filename} were skipped" if bad else None
    return records, error


def _where(record: dict) -> str:
    after = record.get("afterId")
    marker = f" ⟦{after}⟧" if isinstance(after, int) and not isinstance(after, bool) else ""
    return f"Chapter {record.get('chapter')}{marker}"


def describe_fix(record: dict) -> str:
    """'Chapter 812 ⟦4⟧ 沉有 → 没有'."""
    return f"{_where(record)} {record.get('zh', '')} → {record.get('correction', '')}"


def describe_continuity(record: dict) -> str:
    """'Chapter 812 ⟦7⟧ “张三和老张”: the same person is listed twice'."""
    return f"{_where(record)} “{record.get('zh', '')}”: {record.get('issue', '')}"
