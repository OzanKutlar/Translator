"""Pending-page pool: pages waiting for translation, plus Missing/Failed chapter tracking.

The pool holds individual pages, not pre-formed batches. A worker cuts a batch from the
front only when it is free to send one, so batch-size changes apply to the next batch.
Missing and Failed chapters are only recorded here; nothing is re-queued automatically.
"""

import re
import threading
import time
from dataclasses import dataclass, field

KNOWN_PAGE_LIMIT = 2000   # unnumbered pages remembered so 'retry page:<id>' can find them
MAX_SELECTION = 5000      # most chapters one retry/recover command may select
STATE_VERSION = 1
SELECTION_KEYWORDS = ("missing", "failed", "all")
MAX_REASON_CHARS = 300


@dataclass(frozen=True)
class PageEntry:
    """One raw page waiting for, or undergoing, translation."""

    id: int
    chapter: int | None
    label: str
    content: str

    @property
    def key(self) -> str:
        """Identity used for de-duplication and Missing/Failed records."""
        return str(self.chapter) if self.chapter is not None else f"page:{self.id}"


def entry_sort_key(entry: PageEntry) -> tuple[int, int]:
    """Chapter order first; unnumbered pages follow in arrival order."""
    if entry.chapter is None:
        return (1, entry.id)
    return (0, entry.chapter)


def _dedupe_last(entries: list[PageEntry]) -> list[PageEntry]:
    """Keep the newest entry per key, at the position of the key's first appearance."""
    latest = {entry.key: entry for entry in entries}
    seen = set()
    ordered = []
    for entry in entries:
        if entry.key not in seen:
            seen.add(entry.key)
            ordered.append(latest[entry.key])
    return ordered


def _clean_records(raw) -> dict[str, dict]:
    """Validate Missing/Failed records loaded from disk."""
    if not isinstance(raw, dict):
        return {}
    clean = {}
    for key, info in raw.items():
        if not isinstance(key, str) or not isinstance(info, dict):
            continue
        chapter = info.get("chapter")
        stamp = info.get("time")
        clean[key] = {
            "chapter": chapter if isinstance(chapter, int) and chapter > 0 else None,
            "label": str(info.get("label") or key),
            "batch": info.get("batch"),
            "reason": str(info.get("reason") or "")[:MAX_REASON_CHARS],
            "time": stamp if isinstance(stamp, (int, float)) else None,
        }
    return clean


class PendingPool:
    """Thread-safe ordered pool of pending pages.

    Pass the pipeline's RLock so pool and pipeline state change atomically together.
    """

    def __init__(self, lock=None):
        self._cond = threading.Condition(lock if lock is not None else threading.RLock())
        self._pending: list[PageEntry] = []
        self._drain = False
        self._next_id = 0
        self._known: dict[int, PageEntry] = {}
        self.missing: dict[str, dict] = {}
        self.failed: dict[str, dict] = {}

    # ------------------------------------------------------------------
    # Entries
    # ------------------------------------------------------------------

    def make_entry(self, chapter, label: str, content: str) -> PageEntry:
        """Create a PageEntry with a fresh id. Invalid chapter numbers become None."""
        if not isinstance(content, str):
            raise TypeError("Page content must be a string")
        if isinstance(chapter, bool) or not isinstance(chapter, int) or chapter <= 0:
            chapter = None
        with self._cond:
            self._next_id += 1
            entry = PageEntry(self._next_id, chapter, label or "Chapter ?", content)
            if chapter is None:
                self._remember(entry)
            return entry

    def _remember(self, entry: PageEntry) -> None:
        self._known[entry.id] = entry
        if len(self._known) > KNOWN_PAGE_LIMIT:
            self._known.pop(next(iter(self._known)))

    def known_page(self, page_id: int) -> PageEntry | None:
        with self._cond:
            return self._known.get(page_id)

    def __len__(self) -> int:
        with self._cond:
            return len(self._pending)

    @property
    def drain(self) -> bool:
        with self._cond:
            return self._drain

    def entries(self) -> list[PageEntry]:
        with self._cond:
            return list(self._pending)

    def pending_keys(self) -> set[str]:
        with self._cond:
            return {entry.key for entry in self._pending}

    def add(self, entries: list[PageEntry], front: bool = False) -> int:
        """Add pages; a page whose key is already pending replaces it. Returns replacements."""
        clean = _dedupe_last([e for e in entries if isinstance(e, PageEntry)])
        if not clean:
            return 0
        with self._cond:
            replaced = self._add_front(clean) if front else self._add_back(clean)
            self._cond.notify_all()
            return replaced

    def _add_front(self, entries: list[PageEntry]) -> int:
        keys = {entry.key for entry in entries}
        rest = [entry for entry in self._pending if entry.key not in keys]
        replaced = len(self._pending) - len(rest)
        self._pending = entries + rest
        return replaced

    def _add_back(self, entries: list[PageEntry]) -> int:
        index = {entry.key: pos for pos, entry in enumerate(self._pending)}
        replaced = 0
        for entry in entries:
            pos = index.get(entry.key)
            if pos is None:
                index[entry.key] = len(self._pending)
                self._pending.append(entry)
            else:
                self._pending[pos] = entry
                replaced += 1
        return replaced

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    def set_drain(self) -> None:
        """Allow partial batches until the pool is empty (goal reached, flush, retry, shutdown)."""
        with self._cond:
            if self._pending:
                self._drain = True
            self._cond.notify_all()

    def notify(self) -> None:
        with self._cond:
            self._cond.notify_all()

    def wait(self, timeout: float) -> None:
        with self._cond:
            self._cond.wait(timeout)

    def _ready(self, max_pages: int) -> bool:
        return bool(self._pending) and (len(self._pending) >= max_pages or self._drain)

    def take(self, max_pages: int, timeout: float, allowed=None) -> list[PageEntry] | None:
        """Cut the next batch: a full one, or a partial one in drain mode. None if not ready."""
        if isinstance(max_pages, bool) or not isinstance(max_pages, int) or max_pages < 1:
            raise ValueError("max_pages must be a positive integer")
        with self._cond:
            if not self._ready(max_pages):
                self._cond.wait(timeout)
            if allowed is not None and not allowed():
                return None
            if not self._ready(max_pages):
                return None
            return self._pop_front(max_pages)

    def pop_front(self, count: int) -> list[PageEntry]:
        """Remove up to count pages from the front regardless of drain mode (manual mode)."""
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise ValueError("count must be a positive integer")
        with self._cond:
            return self._pop_front(count)

    def _pop_front(self, count: int) -> list[PageEntry]:
        batch = self._pending[:count]
        del self._pending[:count]
        if not self._pending:
            self._drain = False
        return batch

    def drain_all(self) -> list[PageEntry]:
        """Remove and return every pending page (used at shutdown)."""
        with self._cond:
            batch, self._pending = self._pending, []
            self._drain = False
            return batch

    # ------------------------------------------------------------------
    # Missing / Failed
    # ------------------------------------------------------------------

    @staticmethod
    def _record(entry: PageEntry, batch, reason) -> dict:
        return {
            "chapter": entry.chapter,
            "label": entry.label,
            "batch": batch,
            "reason": str(reason)[:MAX_REASON_CHARS],
            "time": time.time(),
        }

    def mark_missing(self, entries: list[PageEntry], batch, reason) -> None:
        with self._cond:
            for entry in entries:
                self.failed.pop(entry.key, None)
                self.missing[entry.key] = self._record(entry, batch, reason)

    def mark_failed(self, entries: list[PageEntry], batch, reason) -> None:
        with self._cond:
            for entry in entries:
                self.missing.pop(entry.key, None)
                self.failed[entry.key] = self._record(entry, batch, reason)

    def forget(self, keys) -> int:
        """Drop keys from both lists (translated again, or re-queued). Returns records removed."""
        removed = 0
        with self._cond:
            for key in keys:
                text = str(key)
                removed += int(self.missing.pop(text, None) is not None)
                removed += int(self.failed.pop(text, None) is not None)
        return removed

    def problems(self) -> tuple[dict, dict]:
        """Copies of the (missing, failed) records."""
        with self._cond:
            return (
                {k: dict(v) for k, v in self.missing.items()},
                {k: dict(v) for k, v in self.failed.items()},
            )

    def export_state(self) -> dict:
        missing, failed = self.problems()
        return {"version": STATE_VERSION, "missing": missing, "failed": failed}

    def import_state(self, state) -> int:
        """Replace Missing/Failed with records from disk. Returns how many were loaded."""
        state = state if isinstance(state, dict) else {}
        missing = _clean_records(state.get("missing"))
        failed = _clean_records(state.get("failed"))
        with self._cond:
            self.missing = missing
            self.failed = failed
        return len(missing) + len(failed)


# ----------------------------------------------------------------------
# Chapter selection parsing (retry / recover)
# ----------------------------------------------------------------------

@dataclass
class Selection:
    """Parsed chapter selection for retry/recover."""

    chapters: list[int] = field(default_factory=list)
    page_ids: list[int] = field(default_factory=list)
    keywords: set[str] = field(default_factory=set)
    errors: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.chapters or self.page_ids or self.keywords)


_CH_PREFIX_RE = re.compile(r"\bch(?:apter)?\.?\s+(?=\d)", re.IGNORECASE)
_DASH_RE = re.compile(r"\s*[-\u2013\u2014~]\s*")
_RANGE_RE = re.compile(r"^(?:ch(?:apter)?\.?)?(\d{1,7})(?:-(?:ch(?:apter)?\.?)?(\d{1,7}))?$", re.IGNORECASE)
_PAGE_RE = re.compile(r"^page:(\d{1,9})$", re.IGNORECASE)
_SPLIT_RE = re.compile(r"[,;\s]+")


def parse_selection(args) -> Selection:
    """Parse '112-115' (inclusive), 'ch120', '120 130', 'missing', 'failed', 'all' or 'page:<id>'."""
    selection = Selection()
    text = " ".join(str(arg) for arg in (args or []))
    text = _DASH_RE.sub("-", _CH_PREFIX_RE.sub("ch", text))
    chapters: set[int] = set()
    for token in (t for t in _SPLIT_RE.split(text) if t):
        _parse_token(token, selection, chapters)
        if len(chapters) > MAX_SELECTION:
            selection.errors.append(f"Selection is larger than {MAX_SELECTION} chapters; narrow it down.")
            break
    selection.chapters = sorted(chapters)
    return selection


def _parse_token(token: str, selection: Selection, chapters: set[int]) -> None:
    low = token.lower()
    if low in SELECTION_KEYWORDS:
        selection.keywords.add(low)
        return
    page = _PAGE_RE.match(token)
    if page:
        selection.page_ids.append(int(page.group(1)))
        return
    match = _RANGE_RE.match(token)
    if not match:
        selection.errors.append(
            f"Could not read '{token}'. Use e.g. 120, 112-115, ch120, missing, failed or page:<id>."
        )
        return
    start = int(match.group(1))
    end = int(match.group(2) or match.group(1))
    if start <= 0 or end <= 0:
        selection.errors.append(f"Chapter numbers must be positive ('{token}').")
    elif end < start:
        selection.errors.append(f"Range '{token}' runs backwards; write it as {end}-{start}.")
    elif end - start + 1 > MAX_SELECTION:
        selection.errors.append(f"Range '{token}' covers more than {MAX_SELECTION} chapters.")
    else:
        chapters.update(range(start, end + 1))
