"""Chapter file persistence and spooling."""

import json
import os
import re
import time

from .parsing import parse_chapter_number, format_chapter_ranges

RAW_DIRNAME = "_raw"
FAILED_DIRNAME = "_failed"
CHAPTER_PAGES_DIRNAME = "chapters"   # _raw/chapters/ch_000120.txt: latest raw page per chapter
QUEUE_STATE_FILENAME = "_queue_state.json"


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
        chapter_no = str(entry.get("chapterNo") or "").strip()
        content = entry.get("content") or ""
        content = content if isinstance(content, str) else str(content)
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


def quarantine_failure(book_dir: str, batch_id: int, pages: list[str], reply_text: str, error: str,
                       chapters: list | None = None) -> str:
    """Quarantine unparseable or failed batches to _failed/ for subsequent retries.

    Records written with ``chapters`` are marked "tracked": their chapters are listed as
    Failed in the queue state and 'retry' re-queues them per chapter. Records without
    the flag (older versions) are re-queued page by page.
    """
    failed_dir = os.path.join(book_dir, FAILED_DIRNAME)
    os.makedirs(failed_dir, exist_ok=True)
    filename = f"failed_{int(time.time())}_batch_{batch_id:04d}.json"
    filepath = os.path.join(failed_dir, filename)
    record = {
        "batch_id": batch_id,
        "timestamp": time.time(),
        "error": error,
        "pages": pages,
        "reply": reply_text,
    }
    if chapters is not None:
        record["tracked"] = True
        record["chapters"] = [c for c in chapters if isinstance(c, int)]
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False, indent=2)
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


_SPOOL_RE = re.compile(r"^batch_(\d+)_pages\.json$")


def _spooled_ids(book_dir: str) -> list[tuple[int, str]]:
    """Return sorted (batch_id, path) pairs for spool files in _raw/. Raises OSError if unreadable."""
    raw_dir = os.path.join(book_dir, RAW_DIRNAME)
    if not os.path.isdir(raw_dir):
        return []
    found = []
    for fname in os.listdir(raw_dir):
        m = _SPOOL_RE.match(fname)
        if m:
            found.append((int(m.group(1)), os.path.join(raw_dir, fname)))
    found.sort()
    return found


def _read_spool(path: str) -> tuple[dict | None, str | None]:
    """Read and validate a spool file. Returns (data, error)."""
    name = os.path.basename(path)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        return None, f"Could not read {name}: {e}"
    pages = data.get("pages") if isinstance(data, dict) else None
    if not isinstance(pages, list) or not all(isinstance(p, str) for p in pages):
        return None, f"{name} has no valid 'pages' list."
    return data, None


def max_spooled_batch_id(book_dir: str) -> int:
    """Highest batch id present in _raw/ (0 if none)."""
    ids = _spooled_ids(book_dir)
    return ids[-1][0] if ids else 0


def list_spooled(book_dir: str) -> list[dict]:
    """Summaries of spooled batches: batch_id, pages (count or None), timestamp, error, chapters."""
    summaries = []
    for batch_id, path in _spooled_ids(book_dir):
        data, err = _read_spool(path)
        pages = data.get("pages") if data and isinstance(data.get("pages"), list) else None
        ch_nums = []
        if pages:
            for p in pages:
                ch = parse_chapter_number(p)
                if ch is not None:
                    ch_nums.append(ch)
        ch_summary = format_chapter_ranges(ch_nums) if ch_nums else ("?" if pages else "")
        summaries.append({
            "batch_id": batch_id,
            "pages": len(pages) if pages is not None else None,
            "timestamp": data.get("timestamp") if data else None,
            "error": err,
            "chapter_numbers": ch_nums,
            "chapters": ch_summary,
        })
    return summaries


def load_spooled(book_dir: str, batch_id: int) -> tuple[list[str] | None, str | None]:
    """Load the pages of one spooled batch. Returns (pages, error)."""
    path = os.path.join(book_dir, RAW_DIRNAME, f"batch_{batch_id:04d}_pages.json")
    if not os.path.isfile(path):
        return None, f"No spooled pages found for batch #{batch_id} in _raw/."
    data, err = _read_spool(path)
    if err:
        return None, err
    if not data["pages"]:
        return None, f"Spooled batch #{batch_id} contains no pages."
    return list(data["pages"]), None


def remove_failed_matching(book_dir: str, batch_id: int, pages: list[str]) -> int:
    """Delete quarantine records with the same batch id AND identical pages. Returns count removed."""
    removed = 0
    for rec in list_failed(book_dir):
        if rec.get("batch_id") == batch_id and rec.get("pages") == pages:
            remove_failed(rec["_filepath"])
            removed += 1
    return removed


# ----------------------------------------------------------------------
# Per-chapter raw page store and source lookup
# ----------------------------------------------------------------------

def _write_text_atomic(path: str, text: str) -> None:
    """Write via a temp file and rename so a crash never leaves a half-written file."""
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def _require_chapter(chapter) -> None:
    if isinstance(chapter, bool) or not isinstance(chapter, int) or chapter <= 0:
        raise ValueError(f"Invalid chapter number: {chapter!r}")


def _chapter_page_path(book_dir: str, chapter: int) -> str:
    return os.path.join(book_dir, RAW_DIRNAME, CHAPTER_PAGES_DIRNAME, f"ch_{chapter:06d}.txt")


def save_scraped_page(book_dir: str, chapter: int, content: str) -> str:
    """Store the latest raw page for a chapter in _raw/chapters/. Raises OSError/ValueError."""
    if not book_dir:
        raise ValueError("No book directory selected")
    _require_chapter(chapter)
    if not isinstance(content, str) or not content.strip():
        raise ValueError("Page content is empty")
    path = _chapter_page_path(book_dir, chapter)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    _write_text_atomic(path, content)
    return path


def load_scraped_page(book_dir: str, chapter: int) -> str | None:
    """Return the stored raw page for a chapter, or None if none was saved. Raises OSError on read errors."""
    _require_chapter(chapter)
    path = _chapter_page_path(book_dir, chapter)
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _match_pages(pages, wanted: set[int], found: dict, source: str) -> None:
    for page in pages:
        if not isinstance(page, str):
            continue
        chapter = parse_chapter_number(page)
        if chapter in wanted and chapter not in found:
            found[chapter] = (page, source)


def _find_in_chapter_store(book_dir: str, wanted: set[int], found: dict, warnings: list[str]) -> None:
    for chapter in sorted(wanted):
        try:
            page = load_scraped_page(book_dir, chapter)
        except (OSError, ValueError) as e:
            warnings.append(f"Could not read stored page for Chapter {chapter}: {e}")
            continue
        if page:
            found[chapter] = (page, f"{RAW_DIRNAME}/{CHAPTER_PAGES_DIRNAME}")


def _find_in_spools(book_dir: str, wanted: set[int], found: dict, warnings: list[str]) -> None:
    try:
        spools = _spooled_ids(book_dir)
    except OSError as e:
        warnings.append(f"Could not list {RAW_DIRNAME}/: {e}")
        return
    for batch_id, path in reversed(spools):
        if not (wanted - found.keys()):
            return
        data, err = _read_spool(path)
        if err:
            warnings.append(err)
            continue
        _match_pages(data["pages"], wanted, found, f"{RAW_DIRNAME}/batch_{batch_id:04d}")


def _find_in_failed(book_dir: str, wanted: set[int], found: dict, warnings: list[str]) -> None:
    try:
        records = list_failed(book_dir)
    except OSError as e:
        warnings.append(f"Could not list {FAILED_DIRNAME}/: {e}")
        return
    for rec in reversed(records):
        if not (wanted - found.keys()):
            return
        pages = rec.get("pages") if isinstance(rec.get("pages"), list) else []
        _match_pages(pages, wanted, found, f"{FAILED_DIRNAME}/{os.path.basename(rec['_filepath'])}")


def find_chapter_pages(book_dir: str, chapters) -> tuple[dict[int, tuple[str, str]], list[str]]:
    """Locate raw pages for chapters.

    Search order: _raw/chapters/, spooled batches (newest first), then _failed/ records.
    Returns ({chapter: (content, source)}, warnings).
    """
    wanted = {c for c in (chapters or []) if isinstance(c, int) and not isinstance(c, bool) and c > 0}
    found: dict[int, tuple[str, str]] = {}
    warnings: list[str] = []
    if not book_dir or not wanted:
        return found, warnings
    _find_in_chapter_store(book_dir, wanted, found, warnings)
    if wanted - found.keys():
        _find_in_spools(book_dir, wanted, found, warnings)
    if wanted - found.keys():
        _find_in_failed(book_dir, wanted, found, warnings)
    return found, warnings


# ----------------------------------------------------------------------
# Missing/Failed queue state
# ----------------------------------------------------------------------

def _queue_state_path(book_dir: str) -> str:
    return os.path.join(book_dir, QUEUE_STATE_FILENAME)


def load_queue_state(book_dir: str) -> tuple[dict, str | None]:
    """Read <Book>/_queue_state.json. Returns (state, error_message); state is {} when absent."""
    if not book_dir:
        return {}, None
    path = _queue_state_path(book_dir)
    if not os.path.isfile(path):
        return {}, None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        return {}, f"Could not read {QUEUE_STATE_FILENAME}: {e}"
    if not isinstance(data, dict):
        return {}, f"{QUEUE_STATE_FILENAME} has an unexpected structure; ignoring it."
    return data, None


def save_queue_state(book_dir: str, state: dict) -> str:
    """Atomically write <Book>/_queue_state.json. Raises OSError/ValueError/TypeError."""
    if not book_dir:
        raise ValueError("No book directory selected")
    if not isinstance(state, dict):
        raise TypeError("Queue state must be a dict")
    os.makedirs(book_dir, exist_ok=True)
    path = _queue_state_path(book_dir)
    _write_text_atomic(path, json.dumps(state, ensure_ascii=False, indent=2))
    return path
