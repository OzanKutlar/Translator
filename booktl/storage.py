"""Chapter file persistence and spooling."""

import json
import os
import re
import time

RAW_DIRNAME = "_raw"
FAILED_DIRNAME = "_failed"


def sanitize_filename(name: str) -> str:
    """Strip illegal filesystem characters."""
    for ch in r'\/:*?"<>|':
        name = name.replace(ch, "_")
    return name.strip()


def save_chapters(chapters: list, book_dir: str) -> tuple[int, int, list[str]]:
    """Save translated chapters as markdown files. Returns (saved, skipped, written_filenames)."""
    os.makedirs(book_dir, exist_ok=True)
    saved = 0
    skipped = 0
    written = []

    for entry in chapters:
        if not isinstance(entry, dict):
            skipped += 1
            continue
        chapter_no = entry.get("chapterNo", "").strip()
        content = entry.get("content", "")
        if not chapter_no or not content:
            skipped += 1
            continue

        filename = sanitize_filename(f"{chapter_no}.md")
        filepath = os.path.join(book_dir, filename)
        try:
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(content)
            saved += 1
            written.append(filename)
        except IOError:
            skipped += 1

    return saved, skipped, written


def spool_pages(book_dir: str, batch_id: int, pages: list[str]) -> str:
    """Persist scraped batch pages to _raw/ before making the API request."""
    raw_dir = os.path.join(book_dir, RAW_DIRNAME)
    os.makedirs(raw_dir, exist_ok=True)
    filename = f"batch_{batch_id:04d}_pages.json"
    filepath = os.path.join(raw_dir, filename)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump({"batch_id": batch_id, "timestamp": time.time(), "pages": pages}, f, ensure_ascii=False, indent=2)
    return filepath


def save_raw_reply(book_dir: str, batch_id: int, reply_text: str) -> str:
    """Optionally save the raw text output from the model to _raw/."""
    raw_dir = os.path.join(book_dir, RAW_DIRNAME)
    os.makedirs(raw_dir, exist_ok=True)
    filename = f"batch_{batch_id:04d}_reply.txt"
    filepath = os.path.join(raw_dir, filename)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(reply_text)
    return filepath


def quarantine_failure(book_dir: str, batch_id: int, pages: list[str], reply_text: str, error: str) -> str:
    """Quarantine unparseable or failed batches to _failed/ for subsequent retries."""
    failed_dir = os.path.join(book_dir, FAILED_DIRNAME)
    os.makedirs(failed_dir, exist_ok=True)
    filename = f"failed_{int(time.time())}_batch_{batch_id:04d}.json"
    filepath = os.path.join(failed_dir, filename)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump({
            "batch_id": batch_id,
            "timestamp": time.time(),
            "error": error,
            "pages": pages,
            "reply": reply_text
        }, f, ensure_ascii=False, indent=2)
    return filepath


def list_failed(book_dir: str) -> list[dict]:
    """List all failed batch records currently in quarantine."""
    failed_dir = os.path.join(book_dir, FAILED_DIRNAME)
    if not os.path.exists(failed_dir):
        return []
    records = []
    for fname in sorted(os.listdir(failed_dir)):
        if fname.endswith(".json"):
            fpath = os.path.join(failed_dir, fname)
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    data = json.load(f)
                data["_filepath"] = fpath
                records.append(data)
            except Exception:
                pass
    return records


def remove_failed(filepath: str) -> None:
    """Remove a successfully reprocessed batch from the quarantine directory."""
    if os.path.exists(filepath):
        try:
            os.remove(filepath)
        except OSError:
            pass


def flush_buffer_to_raw(book_dir: str, pages: list[str]) -> str | None:
    """Dump any unsent pages in buffer when the application exits."""
    if not pages or not book_dir:
        return None
    raw_dir = os.path.join(book_dir, RAW_DIRNAME)
    os.makedirs(raw_dir, exist_ok=True)
    filename = f"unsent_{int(time.time())}.json"
    filepath = os.path.join(raw_dir, filename)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump({"timestamp": time.time(), "pages": pages}, f, ensure_ascii=False, indent=2)
    return filepath
