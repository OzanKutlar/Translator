"""Translation pipeline: pending-page pool, worker threads and result reconciliation.

Scraped pages go into one pending pool. A worker cuts a batch from the front of the pool
only when it is free to send one, so 'batch <n>' applies to the very next batch.
Chapters a reply leaves out are listed as Missing and failed batches as Failed; neither
is re-queued automatically (use 'retry').
"""

import os
import random
import threading
import time

from rich.markup import escape

from . import console as con
from . import arcs, prompts, parsing, storage, characters
from .tracker import ChapterTracker
from .client import ApiClient, ApiError
from .workpool import PageEntry, PendingPool, entry_sort_key, parse_selection

try:
    import pyperclip
    HAS_CLIPBOARD = True
except ImportError:
    HAS_CLIPBOARD = False

PAGE_SEPARATOR = "\n\n--- PAGE BREAK ---\n\n"
BACKLOG_MULTIPLIER = 4          # without a goal the scraper waits at 4 x batch size pending pages
WORKER_POLL_SECONDS = 0.5
MAX_WORKER_POLLS = 50_000_000   # bounded-loop guard (~290 days of idle polling)
IDLE_POLL_SECONDS = 0.5
MAX_REPORTED_WARNINGS = 5
MISSING_REASON = "Not in model reply"


def _is_saveable(entry) -> bool:
    """Mirror storage.save_chapters: an entry needs a chapterNo and non-empty content."""
    if not isinstance(entry, dict):
        return False
    return bool(str(entry.get("chapterNo") or "").strip()) and bool(entry.get("content"))


_format_chapter_ranges = parsing.format_chapter_ranges


def describe_entries(entries) -> str:
    """Summarise pages as 'Chapters 10–14' plus a count of unnumbered pages."""
    numbers = [e.chapter for e in entries if e.chapter is not None]
    text = _format_chapter_ranges(numbers)
    unnumbered = sum(1 for e in entries if e.chapter is None)
    if unnumbered:
        extra = f"{unnumbered} unnumbered page{'' if unnumbered == 1 else 's'}"
        text = f"{text} + {extra}" if text else extra
    return text or "no pages"


def _describe_keys(keys) -> str:
    """Summarise a mix of chapter numbers and 'page:<id>' keys."""
    numbers = [k for k in keys if isinstance(k, int)]
    others = [str(k) for k in keys if not isinstance(k, int)]
    parts = [p for p in (_format_chapter_ranges(numbers), ", ".join(others)) if p]
    return "; ".join(parts)


class Pipeline:
    def __init__(self, cfg, client: ApiClient):
        self.cfg = cfg
        self.client = client
        self.lock = threading.RLock()
        self.pool = PendingPool(self.lock)
        self.stop_event = threading.Event()
        self._workers: dict[int, threading.Thread] = {}
        self._worker_target = cfg.concurrency
        self._batch_seq = 0
        self._active: dict[int, dict] = {}  # batch_id -> {'entries': [PageEntry], 'cancel': bool}
        self._state_io_lock = threading.Lock()  # always taken BEFORE self.lock, never while holding it
        self._backlog_full = False
        self.tracker = ChapterTracker()
        self.auto_dispatch = True
        self.manual_active = False
        self.manual_entries: list[PageEntry] | None = None

        self.counters = {
            "pages_seen": 0,
            "batches_sent": 0,
            "batches_done": 0,
            "chapters_saved": 0,
            "skipped": 0,
            "failures": 0,
            "chars_added": 0,
            "tokens": 0,
        }

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Load queue state and spawn worker threads matching target concurrency."""
        self.sync_batch_seq()
        self.load_queue_state()
        with self.lock:
            self.stop_event.clear()
            self._worker_target = self.cfg.concurrency
            self._ensure_workers_locked()

    def on_book_changed(self) -> None:
        """Reset session tracking and load the new book's Missing/Failed lists."""
        self.tracker.reset()
        self.sync_batch_seq()
        self.load_queue_state()

    def sync_batch_seq(self) -> None:
        """Advance batch numbering past existing spool files so _raw/ entries are never overwritten."""
        book_dir = self.cfg.book_dir
        if not book_dir:
            return
        try:
            highest = storage.max_spooled_batch_id(book_dir)
        except OSError as e:
            con.warn(f"Could not scan spooled batches in _raw/: {e}")
            return
        with self.lock:
            if highest > self._batch_seq:
                self._batch_seq = highest

    def load_queue_state(self) -> None:
        state, err = storage.load_queue_state(self.cfg.book_dir)
        if err:
            con.warn(escape(err))
        count = self.pool.import_state(state)
        if count:
            con.info(
                f"Loaded {count} missing/failed chapter record(s) from {storage.QUEUE_STATE_FILENAME}. "
                f"Type [bold cyan]missing[/bold cyan] to view them."
            )

    def _persist_queue_state(self) -> None:
        """Write Missing/Failed to disk. Must not be called while holding self.lock."""
        book_dir = self.cfg.book_dir
        if not book_dir:
            return
        with self._state_io_lock:
            state = self.pool.export_state()
            try:
                storage.save_queue_state(book_dir, state)
            except (OSError, ValueError, TypeError) as e:
                con.warn(f"Could not save {storage.QUEUE_STATE_FILENAME}: {escape(str(e))}")

    def _ensure_workers_locked(self) -> None:
        for worker_id in range(1, self._worker_target + 1):
            thread = self._workers.get(worker_id)
            if thread is None or not thread.is_alive():
                self._spawn_worker(worker_id)

    def _spawn_worker(self, worker_id: int) -> None:
        t = threading.Thread(
            target=self._worker_loop, args=(worker_id,), daemon=True, name=f"booktl-worker-{worker_id}"
        )
        self._workers[worker_id] = t
        t.start()

    def set_concurrency(self, new_val: int) -> None:
        with self.lock:
            self.cfg.concurrency = new_val  # validates; raises ConfigError
            self._worker_target = self.cfg.concurrency
            self._ensure_workers_locked()
            self.pool.notify()

    def set_auto_dispatch(self, enabled: bool) -> None:
        with self.lock:
            self.auto_dispatch = bool(enabled)
            self.pool.notify()

    def wake_workers(self) -> None:
        """Re-evaluate batch readiness now (e.g. after 'batch <n>')."""
        self.pool.notify()

    def backlog_limit(self) -> int:
        return BACKLOG_MULTIPLIER * self.cfg.batch_size

    # ------------------------------------------------------------------
    # Scraper ingest
    # ------------------------------------------------------------------

    def offer_page(self, content: str, is_active: bool, title: str | None = None) -> dict:
        """Ingest a page from Tampermonkey and return the scraper directive."""
        if not isinstance(content, str) or not content.strip():
            return {"action": "wait", "retryAfter": 3, "reason": "Empty content received"}
        if not self.cfg.book_dir:
            return {"action": "wait", "retryAfter": 5, "reason": "No book directory selected"}
        with self.lock:
            entry, confidence, replaced, forgot = self._ingest_locked(content, title)
            pending = len(self.pool)
            note = " · replaced pending copy" if replaced else ""
            con.info(
                f"Queued page ({len(content):,} chars) · [bold cyan]{escape(entry.label)}[/bold cyan] "
                f"· pending [bold]{pending}[/bold]{note}"
            )
            directive = self._scraper_directive_locked()
        if confidence == "parsed":
            self._store_raw_page(entry)
        if forgot:
            self._persist_queue_state()
        if directive.get("action") in ("wait", "stop"):
            return directive

        # Pacing delay to avoid hammering the source novel website
        delay = 1.0 if is_active else random.uniform(1.0, 3.5)
        time.sleep(delay)
        return directive

    def _ingest_locked(self, content: str, title: str | None) -> tuple[PageEntry, str, int, int]:
        self.counters["pages_seen"] += 1
        ch_num, confidence = self.tracker.observe_page(title, content)
        chapter = ch_num if confidence != "unknown" else None
        entry = self.pool.make_entry(chapter, self._chapter_label(ch_num, confidence), content)
        replaced = self.pool.add([entry])
        forgot = self.pool.forget([entry.key]) if entry.chapter is not None else 0
        return entry, confidence, replaced, forgot

    def _store_raw_page(self, entry: PageEntry) -> None:
        """Keep the raw page on disk so 'retry <chapter>' works in later sessions."""
        if entry.chapter is None:
            return
        try:
            storage.save_scraped_page(self.cfg.book_dir, entry.chapter, entry.content)
        except (OSError, ValueError) as e:
            con.warn(f"Could not store raw page for {escape(entry.label)}: {escape(str(e))}")

    @staticmethod
    def _chapter_label(ch_num: int | None, confidence: str) -> str:
        """Human label for a detected chapter: 'Chapter 12', 'Chapter ~13' or 'Chapter ?'."""
        if ch_num is None or confidence == "unknown":
            return "Chapter ?"
        prefix = "~" if confidence == "inferred" else ""
        return f"Chapter {prefix}{ch_num}"

    def poll_status(self, is_active: bool) -> dict:
        """Handle polling requests from the scraper during wait states."""
        with self.lock:
            return self._scraper_directive_locked()

    def _scraper_directive_locked(self) -> dict:
        """With a goal: scrape until it is reached. Without one: wait at 4 x batch size pending."""
        if not self.cfg.book_dir:
            return {"action": "wait", "retryAfter": 5, "reason": "No book directory selected"}
        if self.tracker.goal_reached():
            return self._goal_directive_locked()
        pending = len(self.pool)
        base = {
            "pending": pending,
            "buffered": pending,
            "needed": self.cfg.batch_size,
            "chapter": self.tracker.current_chapter,
        }
        if self.tracker.goal_state() != "none":
            self._backlog_full = False
            return {"action": "next", "backlogLimit": None, **base}
        return self._backlog_directive_locked(pending, base)

    def _backlog_directive_locked(self, pending: int, base: dict) -> dict:
        limit = self.backlog_limit()
        if pending >= limit:
            if not self._backlog_full:
                con.warn(f"Backlog full ({pending}/{limit} pages pending, no goal set). Asking scraper to wait...")
            self._backlog_full = True
            return {
                "action": "wait",
                "retryAfter": 5,
                "reason": f"Backlog full ({pending}/{limit} pages pending)",
                "backlogLimit": limit,
                **base,
            }
        if self._backlog_full:
            con.info(f"Backlog below {limit} pages again; scraping resumes.")
            self._backlog_full = False
        return {"action": "next", "backlogLimit": limit, **base}

    def _goal_directive_locked(self) -> dict:
        """Halt the scraper at the goal and let the remaining pages go out as partial batches."""
        if not self.tracker.goal_reached_flag:
            self.tracker.goal_reached_flag = True
            goal = self.tracker.goal_chapter
            curr = self.tracker.current_chapter
            pending = len(self.pool)
            con.panel(
                f"Goal of Chapter [bold green]{goal}[/bold green] reached (current: {curr})!\n"
                f"Scraping stops. {pending} pending page(s) will be sent, including a final partial batch.",
                title="Goal Reached",
                style="green"
            )
            self.pool.set_drain()
        return {
            "action": "stop",
            "reason": f"Goal chapter {self.tracker.goal_chapter} reached",
            "chapter": self.tracker.current_chapter,
            "goal": self.tracker.goal_chapter,
            "pending": len(self.pool),
        }

    # ------------------------------------------------------------------
    # Flush / manual mode
    # ------------------------------------------------------------------

    def flush(self) -> str:
        """Send everything pending now, including a final partial batch."""
        with self.lock:
            pending = len(self.pool)
            if not pending:
                return "Nothing is pending. Nothing to flush."
            self.pool.set_drain()
            enabled = self._dispatch_enabled_locked()
        msg = f"Drain mode on: {pending} pending page(s) will be sent now, including a final partial batch."
        if not enabled:
            msg += " Auto-dispatch is paused or a manual session is open, so nothing is sent until that changes."
        return msg

    def manual_begin(self) -> tuple[str | None, str]:
        """Take the next batch of pending pages and copy a prompt for out-of-band translation."""
        with self.lock:
            if self.manual_active:
                return None, "Manual mode is already active."
            entries = self.pool.pop_front(self.cfg.batch_size)
            if not entries:
                return None, "Nothing is pending. No pages to translate."
            try:
                prompt = prompts.build_manual_prompt(
                    self.cfg.book_dir, PAGE_SEPARATOR.join(e.content for e in entries)
                )
            except Exception:
                self.pool.add(entries, front=True)  # never lose pages if prompt building fails
                raise
            self.manual_entries = entries
            self.manual_active = True
        span = describe_entries(entries)

        if not HAS_CLIPBOARD:
            return prompt, f"pyperclip missing. Prompt for {span} printed below. Copy it, translate, and use paste."
        try:
            pyperclip.copy(prompt)
        except Exception as e:  # pyperclip raises platform-specific exception types
            return prompt, f"Clipboard write failed ({escape(str(e))}). Prompt for {span} shown below. Use paste when ready."
        return prompt, f"Copied prompt for {len(entries)} page(s) ({span}) to clipboard. Bring the reply back with the paste command."

    def manual_paste(self, text: str) -> str:
        """Parse and ingest JSON pasted by the user."""
        if not text or not text.strip():
            return "Provided payload is empty."

        chapters, char_actions, arc_ends, err = parsing.parse_reply(text)
        if err:
            return f"Parse error: {escape(err)}. Please copy the entire JSON object and run paste again."
        if not self.cfg.book_dir:
            return "No target book folder selected."

        chapters = chapters or []
        numbers = self._save_results("Manual paste", chapters, char_actions, arc_ends)
        with self.lock:
            entries = self.manual_entries if self.manual_active else None
            self.manual_active = False
            self.manual_entries = None
            self.pool.notify()
        if entries:
            self._reconcile("Manual paste", "manual", entries, chapters, numbers)
            return "Manual session closed."
        self._resolve_returned(numbers)
        return "Pasted translation processed."

    # ------------------------------------------------------------------
    # Cancellation
    # ------------------------------------------------------------------

    def cancel(self) -> list[str]:
        """Cancel manual mode and in-flight batches; pages return to the front of the pool."""
        with self.lock:
            active = self._cancel_active_locked()
            manual = self._cancel_manual_locked()
            restored = [e for _, entries in active for e in entries] + (manual or [])
            self.pool.add(sorted(restored, key=entry_sort_key), front=True)
            auto_on = self.auto_dispatch
        return self._describe_cancel(active, manual, auto_on)

    def _cancel_active_locked(self) -> list[tuple[int, list[PageEntry]]]:
        """Flag in-flight batches as cancelled and return their pages. Late replies get discarded."""
        recovered = []
        for bid in sorted(self._active):
            info = self._active[bid]
            if info.get("cancel"):
                continue
            info["cancel"] = True
            recovered.append((bid, list(info.get("entries", []))))
        return recovered

    def _cancel_manual_locked(self) -> list[PageEntry] | None:
        """End an active manual session and return its pages (None if no session)."""
        if not self.manual_active:
            return None
        entries = list(self.manual_entries or [])
        self.manual_active = False
        self.manual_entries = None
        self.pool.notify()
        return entries

    @staticmethod
    def _describe_cancel(active: list, manual: list | None, auto_on: bool) -> list[str]:
        messages = []
        if manual is not None:
            messages.append(f"Manual translation aborted. {len(manual)} page(s) returned to the front of the pending pool.")
        if active:
            ids = ", ".join(f"#{bid}" for bid, _ in active)
            count = sum(len(entries) for _, entries in active)
            messages.append(
                f"Cancelled in-flight batch(es) {ids}: {count} page(s) returned to the front of the pending pool. "
                f"Any late reply will be discarded."
            )
        if not messages:
            return ["No active manual session or in-flight batches to cancel."]
        if active and auto_on:
            messages.append("Auto-dispatch is ON, so these pages will be sent again with the next batch. Use 'auto off' first to keep them for manual.")
        return messages

    # ------------------------------------------------------------------
    # Retry / recover
    # ------------------------------------------------------------------

    def requeue(self, args, drain: bool = True, verb: str = "Re-queued") -> list[tuple[str, str]]:
        """Put selected chapters back at the front of the pool.

        args accepts chapter numbers and inclusive ranges (112-115), 'missing', 'failed',
        'all' and 'page:<id>'. Returns (level, message) pairs for display.
        """
        if not self.cfg.book_dir:
            return [("warn", "No book directory selected.")]
        selection = parse_selection(args)
        if selection.errors:
            return [("fail", escape(err)) for err in selection.errors]
        if selection.empty:
            return [("warn", "Nothing selected. Use e.g. 'retry 112-115', 'retry missing' or 'retry failed'.")]
        chapters, page_ids, report = self._expand_selection(selection)
        if not chapters and not page_ids:
            return report or [("info", "Nothing to re-queue.")]
        entries, already, absent, warnings = self._collect_sources(chapters, page_ids)
        report += self._queue_entries(entries, drain, verb)
        report += self._describe_requeue_gaps(already, absent, warnings)
        return report

    def _expand_selection(self, selection) -> tuple[set[int], set[int], list[tuple[str, str]]]:
        chapters = set(selection.chapters)
        page_ids = set(selection.page_ids)
        notes: list[tuple[str, str]] = []
        missing, failed = self.pool.problems()
        wanted = []
        if selection.keywords & {"missing", "all"}:
            wanted.append(("missing", missing))
        if selection.keywords & {"failed", "all"}:
            wanted.append(("failed", failed))
        for name, records in wanted:
            if not records:
                notes.append(("info", f"No {name} chapters recorded."))
                continue
            for key in records:
                self._add_key(key, chapters, page_ids)
        return chapters, page_ids, notes

    @staticmethod
    def _add_key(key: str, chapters: set[int], page_ids: set[int]) -> None:
        if key.startswith("page:") and key[5:].isdigit():
            page_ids.add(int(key[5:]))
        elif key.isdigit():
            chapters.add(int(key))

    def _collect_sources(self, chapters: set[int], page_ids: set[int]):
        """Find source pages: in-flight batches, then disk. Returns (entries, already, absent, warnings)."""
        with self.lock:
            pending = self.pool.pending_keys()
            in_flight = {
                e.chapter: e.content
                for info in self._active.values() for e in info["entries"] if e.chapter is not None
            }
        entries, already, absent = [], [], []
        on_disk = set()
        for chapter in sorted(chapters):
            if str(chapter) in pending:
                already.append(chapter)
            elif chapter in in_flight:
                entries.append(self.pool.make_entry(chapter, f"Chapter {chapter}", in_flight[chapter]))
            else:
                on_disk.add(chapter)
        found, warnings = storage.find_chapter_pages(self.cfg.book_dir, on_disk)
        for chapter in sorted(on_disk):
            if chapter in found:
                entries.append(self.pool.make_entry(chapter, f"Chapter {chapter}", found[chapter][0]))
            else:
                absent.append(chapter)
        self._collect_unnumbered(page_ids, pending, entries, already, absent)
        return entries, already, absent, warnings

    def _collect_unnumbered(self, page_ids, pending, entries, already, absent) -> None:
        for page_id in sorted(page_ids):
            key = f"page:{page_id}"
            known = self.pool.known_page(page_id)
            if key in pending:
                already.append(key)
            elif known is not None:
                entries.append(known)
            else:
                absent.append(key)

    def _queue_entries(self, entries: list[PageEntry], drain: bool, verb: str) -> list[tuple[str, str]]:
        if not entries:
            return []
        ordered = sorted(entries, key=entry_sort_key)
        with self.lock:
            self.pool.add(ordered, front=True)
            self.pool.forget([e.key for e in ordered])
            if drain:
                self.pool.set_drain()
            enabled = self._dispatch_enabled_locked()
            pending = len(self.pool)
        self._persist_queue_state()
        report = [("ok", f"{verb} {escape(describe_entries(ordered))} ({len(ordered)} page(s)) at the front of the pending pool ({pending} pending).")]
        if not enabled:
            report.append(("info", "Auto-dispatch is paused or a manual session is open; use 'auto on', 'manual' or 'flush' when ready."))
        elif not drain:
            report.append(("info", "They go out with the next full batch; use 'flush' to send them now."))
        return report

    @staticmethod
    def _describe_requeue_gaps(already: list, absent: list, warnings: list[str]) -> list[tuple[str, str]]:
        report = []
        if already:
            report.append(("info", f"Already pending (left in place): {escape(_describe_keys(already))}."))
        if absent:
            report.append((
                "warn",
                f"No source page found for {escape(_describe_keys(absent))} "
                f"(looked in memory, _raw/chapters, _raw spools and _failed/).",
            ))
        for warning in warnings[:MAX_REPORTED_WARNINGS]:
            report.append(("warn", escape(warning)))
        if len(warnings) > MAX_REPORTED_WARNINGS:
            report.append(("warn", f"... and {len(warnings) - MAX_REPORTED_WARNINGS} more read warning(s)."))
        return report

    def retry_failed(self) -> list[tuple[str, str]]:
        """Re-queue every Failed chapter plus legacy _failed/ batches; prune resolved records."""
        if not self.cfg.book_dir:
            return [("warn", "No book directory selected.")]
        report: list[tuple[str, str]] = []
        _missing, failed = self.pool.problems()
        if failed:
            report += self.requeue(["failed"], drain=True, verb="Re-queued failed")
        report += self._sweep_failed_records()
        if not report:
            report.append(("info", "No failed chapters or quarantined batches found."))
        return report

    def _sweep_failed_records(self) -> list[tuple[str, str]]:
        """Re-queue untracked (legacy) _failed/ records and delete tracked ones that are resolved."""
        try:
            records = storage.list_failed(self.cfg.book_dir)
        except OSError as e:
            return [("fail", f"Could not read _failed/: {escape(str(e))}")]
        _missing, failed = self.pool.problems()
        referenced = {info.get("batch") for info in failed.values()}
        legacy: list[PageEntry] = []
        cleared = 0
        for rec in records:
            if rec.get("tracked"):
                if rec.get("batch_id") not in referenced:
                    storage.remove_failed(rec["_filepath"])
                    cleared += 1
                continue
            pages = rec.get("pages") if isinstance(rec.get("pages"), list) else []
            legacy.extend(self._entry_from_page(p) for p in pages if isinstance(p, str) and p.strip())
            storage.remove_failed(rec["_filepath"])
        report = self._queue_entries(legacy, True, "Re-queued older _failed/ pages:")
        if cleared:
            report.append(("info", f"Removed {cleared} resolved _failed/ record(s)."))
        return report

    def _entry_from_page(self, content: str) -> PageEntry:
        chapter = parsing.parse_chapter_number(content)
        label = self._chapter_label(chapter, "parsed" if chapter is not None else "unknown")
        return self.pool.make_entry(chapter, label, content)

    def recover_spooled(self, batch_id: int) -> tuple[bool, str]:
        """Load a spooled batch from _raw/ to the front of the pool (skipping pages already pending)."""
        book_dir = self.cfg.book_dir
        if not book_dir:
            return False, "No book directory selected."
        if not isinstance(batch_id, int) or batch_id <= 0:
            return False, "Batch id must be a positive integer."
        with self.lock:
            info = self._active.get(batch_id)
            if info is not None and not info.get("cancel"):
                return False, f"Batch #{batch_id} is still in flight. Use cancel to recover it instead."

        pages, err = storage.load_spooled(book_dir, batch_id)
        if err:
            return False, escape(err)

        with self.lock:
            pending_keys = self.pool.pending_keys()
            entries = [self._entry_from_page(p) for p in pages]
            new = [e for e in entries if e.key not in pending_keys]
            self.pool.add(new, front=True)
            forgot = self.pool.forget([e.key for e in new])
            total = len(self.pool)
            auto_on = self.auto_dispatch
        if forgot:
            self._persist_queue_state()
        if not new:
            return False, f"All {len(pages)} page(s) from batch #{batch_id} are already pending."

        msg = (
            f"Recovered {len(new)} page(s) from batch #{batch_id} ({escape(describe_entries(new))}) "
            f"to the front of the pending pool ({total} pending)."
        )
        msg += self._drop_matching_quarantine(batch_id, pages)
        if auto_on:
            msg += " Auto-dispatch is ON, so they go out with the next batch; use 'auto off' first to keep them for manual."
        else:
            msg += " Use manual or flush."
        return True, msg

    def _drop_matching_quarantine(self, batch_id: int, pages: list[str]) -> str:
        """Remove identical _failed/ records so retry cannot resend recovered pages."""
        try:
            removed = storage.remove_failed_matching(self.cfg.book_dir, batch_id, pages)
        except OSError as e:
            return f" (Could not check _failed/: {escape(str(e))})"
        if removed:
            return f" Removed {removed} matching _failed/ record(s) so retry won't resend them."
        return ""

    # ------------------------------------------------------------------
    # Workers
    # ------------------------------------------------------------------

    def _dispatch_enabled_locked(self) -> bool:
        return bool(self.cfg.book_dir) and self.auto_dispatch and not self.manual_active

    def _should_retire(self, worker_id: int) -> bool:
        with self.lock:
            return worker_id > self._worker_target

    def _worker_loop(self, worker_id: int) -> None:
        for _ in range(MAX_WORKER_POLLS):
            if self.stop_event.is_set() or self._should_retire(worker_id):
                return
            claimed = self._claim_batch()
            if claimed is None:
                continue
            batch_id, entries = claimed
            try:
                self._process_batch(batch_id, entries)
            except Exception as e:  # one bad batch must never kill the worker
                con.fail(f"Unexpected worker error on batch #{batch_id}: {escape(str(e))}")
        con.warn(f"Worker {worker_id} reached its iteration limit and stopped.")

    def _claim_batch(self) -> tuple[int, list[PageEntry]] | None:
        """Cut a batch at the CURRENT batch size and register it as in flight, atomically."""
        with self.lock:
            if self.stop_event.is_set():
                return None
            if not self._dispatch_enabled_locked():
                self.pool.wait(WORKER_POLL_SECONDS)
                return None
            entries = self.pool.take(
                self.cfg.batch_size,
                WORKER_POLL_SECONDS,
                allowed=lambda: self._dispatch_enabled_locked() and not self.stop_event.is_set(),
            )
            if not entries:
                return None
            self._batch_seq += 1
            batch_id = self._batch_seq
            self._active[batch_id] = {"entries": list(entries), "cancel": False}
            self.counters["batches_sent"] += 1
            return batch_id, entries

    def _process_batch(self, batch_id: int, entries: list[PageEntry]) -> None:
        try:
            self._run_batch(batch_id, entries)
        except Exception as e:
            self._handle_batch_failure(batch_id, entries, "", f"Unexpected {type(e).__name__}: {e}")
        finally:
            with self.lock:
                self._active.pop(batch_id, None)
                self.counters["batches_done"] += 1
                self.pool.notify()

    def _is_cancelled(self, batch_id: int) -> bool:
        with self.lock:
            return bool(self._active.get(batch_id, {}).get("cancel", False))

    def _run_batch(self, batch_id: int, entries: list[PageEntry]) -> None:
        cfg = self.cfg
        book_dir = cfg.book_dir
        pages = [e.content for e in entries]
        span = escape(describe_entries(entries))
        try:
            storage.spool_pages(book_dir, batch_id, pages)
        except OSError as e:
            con.warn(f"Could not spool batch #{batch_id} to _raw/ ({escape(str(e))}); continuing without backup.")

        messages = prompts.build_messages(book_dir, PAGE_SEPARATOR.join(pages))
        if cfg.dry_run:
            con.rule(f"Dry Run: Batch #{batch_id}")
            con.console.print(f"[dim]Constructed {len(messages)} messages for {escape(cfg.model or '(no model)')} · {len(pages)} pages · {span}[/dim]")
            return

        con.info(f"Dispatching batch [bold cyan]#{batch_id}[/bold cyan] ({len(pages)} pages · {span}) to [bold green]{escape(cfg.model)}[/bold green]...")
        try:
            raw_reply, usage = self.client.chat(cfg, messages)
        except ApiError as e:
            self._handle_batch_failure(batch_id, entries, "", f"API error: {e}")
            return

        self._record_usage(usage)
        self._save_raw_reply(batch_id, raw_reply)
        if self._is_cancelled(batch_id):
            con.warn(f"Batch #{batch_id} finished after cancellation — reply discarded (pages are already back in the pending pool).")
            return

        chapters, char_actions, arc_ends, err = parsing.parse_reply(raw_reply)
        if err:
            self._handle_batch_failure(batch_id, entries, raw_reply, f"JSON error: {err}")
            return
        if not chapters:
            self._handle_batch_failure(batch_id, entries, raw_reply, "Reply contained no chapters")
            return
        label = f"Batch #{batch_id}"
        numbers = self._save_results(label, chapters, char_actions, arc_ends)
        self._reconcile(label, batch_id, entries, chapters, numbers)

    def _save_raw_reply(self, batch_id: int, raw_reply: str) -> None:
        if not self.cfg.save_raw:
            return
        try:
            storage.save_raw_reply(self.cfg.book_dir, batch_id, raw_reply)
        except OSError as e:
            con.warn(f"Could not save raw reply for batch #{batch_id}: {escape(str(e))}")

    def _record_usage(self, usage) -> None:
        if not isinstance(usage, dict):
            return
        toks = usage.get("total_tokens")
        if isinstance(toks, (int, float)):
            with self.lock:
                self.counters["tokens"] += int(toks)

    def _handle_batch_failure(self, batch_id: int, entries: list[PageEntry], reply: str, error: str) -> None:
        """Quarantine a failed batch and list its chapters as Failed. Never re-queued automatically."""
        if self._is_cancelled(batch_id):
            con.warn(f"Batch #{batch_id} ended after cancellation ({escape(error)}). Pages are already back in the pending pool.")
            return
        with self.lock:
            self.counters["failures"] += 1
        pages = [e.content for e in entries]
        chapters = [e.chapter for e in entries if e.chapter is not None]
        try:
            qpath = storage.quarantine_failure(self.cfg.book_dir, batch_id, pages, reply, error, chapters=chapters)
        except OSError as qe:
            with self.lock:
                self.pool.add(entries, front=True)
            con.fail(
                f"Batch #{batch_id} failed ({escape(error)}) and could not be quarantined ({escape(str(qe))}). "
                f"{len(pages)} page(s) returned to the pending pool."
            )
            return
        with self.lock:
            self.pool.mark_failed(entries, batch_id, error)
        self._persist_queue_state()
        con.fail(f"Batch #{batch_id} failed: {escape(error)}")
        con.info(
            f"Quarantined {len(pages)} page(s) to [cyan]{escape(os.path.basename(qpath))}[/cyan]. "
            f"{escape(describe_entries(entries))} marked Failed (not re-queued — use 'retry' or 'retry <chapters>')."
        )

    # ------------------------------------------------------------------
    # Results
    # ------------------------------------------------------------------

    def _save_results(self, label: str, chapters: list, char_actions: list, arc_ends: list) -> list[int]:
        """Store chapters, apply character and arc updates. Returns translated chapter numbers."""
        numbers = self._store_chapters(label, chapters)
        self._apply_character_actions(label, char_actions)
        self._apply_arc_ends(label, arc_ends, chapters)
        return numbers

    def _reconcile(self, label: str, batch_ref, entries: list[PageEntry], chapters: list, numbers: list[int]) -> None:
        """Compare sent chapters with the reply; list anything left out as Missing (not re-queued)."""
        returned = set(numbers)
        missing, matched_unnumbered = self._find_missing(entries, chapters, returned)
        resolved = [str(n) for n in returned] + [e.key for e in matched_unnumbered]
        with self.lock:
            changed = self.pool.forget(resolved)
            if missing:
                self.pool.mark_missing(missing, batch_ref, MISSING_REASON)
        if changed or missing:
            self._persist_queue_state()
        if missing:
            con.warn(
                f"{label}: ⚠ Missing {escape(describe_entries(missing))} "
                f"(not re-queued — use 'retry missing' or 'retry <chapters>')"
            )

    @staticmethod
    def _find_missing(entries: list[PageEntry], chapters: list, returned: set[int]):
        """Numbered pages match by chapter; unnumbered pages can only be matched by count."""
        sent_numbers = {e.chapter for e in entries if e.chapter is not None}
        missing = [e for e in entries if e.chapter is not None and e.chapter not in returned]
        unnumbered = [e for e in entries if e.chapter is None]
        if not unnumbered:
            return missing, []
        saveable = sum(1 for c in chapters if _is_saveable(c))
        extra = max(0, saveable - len(returned & sent_numbers))
        matched = unnumbered[:extra]
        missing += unnumbered[len(matched):]
        return missing, matched

    def _resolve_returned(self, numbers: list[int]) -> None:
        with self.lock:
            changed = self.pool.forget([str(n) for n in numbers])
        if changed:
            self._persist_queue_state()

    def _store_chapters(self, label: str, chapters: list) -> list[int]:
        """Write chapters to disk, update counters and tracker, log one summary line."""
        saved, skipped, _written = storage.save_chapters(chapters, self.cfg.book_dir)
        numbers = self._record_translated(chapters, saved, skipped)
        noun = "chapter" if saved == 1 else "chapters"
        msg = f"{label}: Saved [bold green]{saved}[/bold green] {noun}"
        span = _format_chapter_ranges(numbers)
        if span:
            msg += f" ({span})"
        msg += f" to '{escape(self.cfg.book_name)}'"
        if skipped:
            msg += f", [yellow]{skipped} skipped[/yellow]"
        if saved:
            con.ok(msg)
        else:
            con.warn(msg)
        return numbers

    def _record_translated(self, chapters: list, saved: int, skipped: int) -> list[int]:
        """Update counters and the tracker; returns the chapter numbers that were read."""
        numbers = []
        with self.lock:
            self.counters["chapters_saved"] += saved
            self.counters["skipped"] += skipped
            for entry in chapters:
                if not _is_saveable(entry):
                    continue
                num = self.tracker.observe_translated(str(entry["chapterNo"]))
                if num is not None:
                    numbers.append(num)
        return numbers

    def _apply_arc_ends(self, label: str, arc_ends: list, chapters: list) -> None:
        """Record author-declared arc/volume endings in Arcs.json and log every change."""
        if not arc_ends:
            return
        valid = arcs.chapter_numbers([entry for entry in chapters if _is_saveable(entry)])
        try:
            summary = arcs.record_arc_ends(self.cfg.book_dir, arc_ends, valid)
        except (OSError, ValueError, TypeError) as e:
            con.fail(f"{label}: arc/volume update failed: {escape(str(e))}")
            return
        self._log_arc_summary(label, summary)

    @staticmethod
    def _log_arc_summary(label: str, summary: dict) -> None:
        """One log line per arc event; failures are never silent."""
        for ev in summary.get("recorded", []):
            kind = "Volume" if ev["type"] == "volume" else "Arc"
            con.ok(f"{label}: 📕 {kind} end recorded at Chapter {ev['chapter']} — “{escape(ev['name'])}”")
        for ev in summary.get("extended", []):
            con.ok(f"{label}: 📕 Afterword Chapter {ev['to']} added to “{escape(ev['name'])}” (was ending at {ev['from']})")
        for ev in summary.get("unchanged", []):
            con.dim(f"{label}: ending at Chapter {ev['chapter']} already recorded as “{escape(ev['name'])}”")
        for reason in summary.get("skipped", []):
            con.warn(f"{label}: arc/volume entry skipped: {escape(reason)}")
        for err in summary.get("failed", []):
            con.fail(f"{label}: arc/volume update failed: {escape(err)}")

    def _apply_character_actions(self, label: str, char_actions: list) -> None:
        """Apply character mutations and log one combined summary line."""
        if not char_actions:
            return
        try:
            summary = characters.process_character_actions(self.cfg.book_dir, char_actions)
        except (OSError, ValueError) as e:
            con.fail(f"{label}: character updates failed: {escape(str(e))}")
            return
        for err in summary.get("failed", []):
            con.fail(f"{label}: character update failed: {escape(err)}")
        line = characters.format_summary(summary)
        if line:
            con.ok(f"{label}: {escape(line)}")

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def queue_counts(self) -> dict:
        with self.lock:
            return {
                "pending": len(self.pool),
                "in_flight": sum(len(info["entries"]) for info in self._active.values()),
                "in_flight_batches": len(self._active),
                "missing": len(self.pool.missing),
                "failed": len(self.pool.failed),
                "manual": len(self.manual_entries or []),
            }

    def pending_view(self) -> dict:
        with self.lock:
            return {
                "pending": self.pool.entries(),
                "in_flight": [
                    (bid, list(info["entries"]), bool(info.get("cancel")))
                    for bid, info in sorted(self._active.items())
                ],
                "manual": list(self.manual_entries or []),
                "drain": self.pool.drain,
                "batch_size": self.cfg.batch_size,
                "backlog_limit": self.backlog_limit() if self.tracker.goal_state() == "none" else None,
            }

    def problems(self) -> tuple[dict, dict]:
        """Copies of the (missing, failed) chapter records."""
        return self.pool.problems()

    def is_idle(self) -> bool:
        """True when nothing is in flight and nothing pending could still be sent."""
        with self.lock:
            if self._active:
                return False
            return len(self.pool) == 0 or not self._dispatch_enabled_locked()

    def wait_until_idle(self, timeout: float) -> bool:
        """Send everything pending (drain mode) and wait, bounded by timeout. Returns True if idle."""
        self.pool.set_drain()
        polls = max(1, int(max(0.0, float(timeout)) / IDLE_POLL_SECONDS))
        for _ in range(polls):
            if self.is_idle():
                return True
            time.sleep(IDLE_POLL_SECONDS)
        return self.is_idle()

    def stats(self) -> dict:
        with self.lock:
            counts = self.queue_counts()
            return {
                "model": self.cfg.model,
                "endpoint": self.cfg.base_url,
                "book": self.cfg.book_name,
                "batch_size": self.cfg.batch_size,
                "concurrency": self.cfg.concurrency,
                "backlog_limit": self.backlog_limit() if self.tracker.goal_state() == "none" else None,
                "drain": self.pool.drain,
                "auto_dispatch": self.auto_dispatch,
                "manual_active": self.manual_active,
                "json_mode": self.cfg.json_mode,
                "temperature": self.cfg.temperature,
                "counters": dict(self.counters),
                "tracker": self.tracker.snapshot(),
                **counts,
            }

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def shutdown(self, drain: bool = True, timeout: int = 600) -> None:
        """Send pending pages (if dispatch is enabled), wait for in-flight batches, then stop."""
        if drain:
            self._drain_before_shutdown(timeout)
        self.stop_event.set()
        with self.lock:
            self.pool.notify()
            leftover = self.pool.drain_all()
            manual = self._cancel_manual_locked() or []
        self._save_unsent(leftover + manual)
        self._persist_queue_state()

    def _drain_before_shutdown(self, timeout: int) -> None:
        counts = self.queue_counts()
        with self.lock:
            sendable = self._dispatch_enabled_locked()
        pending = counts["pending"] if sendable else 0
        if not pending and not counts["in_flight_batches"]:
            return
        con.info(
            f"Draining {pending} pending page(s) and {counts['in_flight_batches']} in-flight batch(es)... "
            f"(Press Ctrl+C to force exit)"
        )
        try:
            if not self.wait_until_idle(timeout):
                con.warn("Drain timed out; remaining pending pages will be saved as unsent.")
        except KeyboardInterrupt:
            con.warn("Drain interrupted by user. Forcing immediate shutdown...")

    def _save_unsent(self, entries: list[PageEntry]) -> None:
        if not entries or not self.cfg.book_dir:
            return
        try:
            fpath = storage.flush_buffer_to_raw(self.cfg.book_dir, [e.content for e in entries])
        except OSError as e:
            con.fail(f"Could not save {len(entries)} unsent page(s): {escape(str(e))}")
            return
        if fpath:
            con.warn(
                f"Saved {len(entries)} unsent pending page(s) ({escape(describe_entries(entries))}) "
                f"to [cyan]{escape(os.path.basename(fpath))}[/cyan]"
            )
