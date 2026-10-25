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
from dataclasses import dataclass, field

from rich.markup import escape

from . import console as con
from . import activity, arcs, clipaccept, prompts, parsing, resume, storage, characters, glossary, termlookup
from .batchlog import BatchLog
from .tracker import ChapterTracker
from .client import ApiClient, ApiError
from .cancel import ApiCancelled, CancelToken
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
SCOUT_MAX_TOKENS = 4096        # the scout only returns a short term list
SCOUT_ATTEMPTS = 2             # a failed term query is re-sent once, then the batch is marked Failed
MAX_LISTED_NAMES = 5           # names listed in one log line before "…"
MAX_LISTED_TERMS = 3           # terms listed in one warning line before "N more"
ONE_LINE_CHARS = 160           # longest error text shown on the console


def _is_saveable(entry) -> bool:
    """Mirror storage.save_chapters: an entry needs a chapterNo and non-empty content."""
    if not isinstance(entry, dict):
        return False
    return bool(str(entry.get("chapterNo") or "").strip()) and bool(entry.get("content"))


_format_chapter_ranges = parsing.format_chapter_ranges


def _one_line(text, limit: int = ONE_LINE_CHARS) -> str:
    """Text collapsed to one line and cut to limit characters, for console display."""
    flat = " ".join(str(text or "").split())
    if limit < 2 or len(flat) <= limit:
        return flat
    return flat[:limit - 1].rstrip() + "…"


def _error_summary(error) -> str:
    """Short form of an API error; ApiError carries one, anything else is collapsed to one line."""
    return _one_line(getattr(error, "summary", "") or str(error))


def _tokens_text(usage) -> str:
    total = usage.get("total_tokens") if isinstance(usage, dict) else None
    if isinstance(total, bool) or not isinstance(total, (int, float)):
        return ""
    return f" · {int(total):,} tokens"


def _plural(count: int, singular: str, plural: str) -> str:
    return f"{count} {singular if count == 1 else plural}"


def _more_text(total: int) -> str:
    extra = total - MAX_LISTED_TERMS
    return f" · {extra} more" if extra > 0 else ""


def _reused_text(recovered) -> str:
    """'reused 2 earlier renderings: 地法 → Earth Law (Ch 477), …' or ''."""
    items = [ev for ev in recovered if isinstance(ev, dict)] if isinstance(recovered, list) else []
    if not items:
        return ""
    shown = [
        f"{escape(str(ev.get('chinese') or ''))} → {escape(str(ev.get('english') or ''))} (Ch {ev.get('chapter')})"
        for ev in items[:MAX_LISTED_NAMES]
    ]
    more = ", …" if len(items) > MAX_LISTED_NAMES else ""
    return f"reused {_plural(len(items), 'earlier rendering', 'earlier renderings')}: {', '.join(shown)}{more}"


def _conflicts_text(conflicts) -> str:
    """One warning naming locked terms the model tried to render differently, or ''."""
    items = [ev for ev in conflicts if isinstance(ev, dict)] if isinstance(conflicts, list) else []
    if not items:
        return ""
    shown = [
        f"{escape(str(ev.get('chinese') or ''))} stays “{escape(str(ev.get('current') or ''))}” "
        f"(model suggested “{escape(str(ev.get('suggested') or ''))}”)"
        for ev in items[:MAX_LISTED_TERMS]
    ]
    return "Kept locked terms: " + "; ".join(shown) + _more_text(len(items))


def _unconfirmed_text(missing: list) -> str:
    """One warning naming reused renderings that their first chapter does not contain."""
    shown = [
        f"“{escape(str(ev.get('english') or ''))}” ({escape(str(ev.get('chinese') or ''))}, Ch {ev.get('chapter')})"
        for ev in missing[:MAX_LISTED_TERMS]
    ]
    return (
        f"Reused but not in its first chapter: {', '.join(shown)}{_more_text(len(missing))} "
        "· check with 'terms audit <chinese>'"
    )


def _drift_text(groups) -> str:
    """One warning for a batch's term drift: each term once, with the chapters it is missing from."""
    if not groups:
        return ""
    shown = [
        f"“{escape(english)}” ({escape(forms)}) missing from {_format_chapter_ranges(chapters)}"
        for (english, forms), chapters in groups[:MAX_LISTED_TERMS]
    ]
    extra = len(groups) - MAX_LISTED_TERMS
    tail = f" · {_plural(extra, 'more term', 'more terms')}" if extra > 0 else ""
    return "Term drift: " + " · ".join(shown) + tail


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


@dataclass
class ScoutOutcome:
    """What a batch's term scout produced.

    messages are the exact scout messages sent and reply is the scout's raw answer; the translation
    call continues that conversation. Both are empty when the scout was skipped or failed, which
    means the batch goes out as a single translation call.
    """

    lookup: termlookup.LookupResult = field(default_factory=termlookup.LookupResult)
    messages: list = field(default_factory=list)
    reply: str = ""
    ran: bool = False   # True when a term query was actually sent and answered

    @property
    def usable(self) -> bool:
        return bool(self.messages) and isinstance(self.reply, str) and bool(self.reply.strip())


class ScoutFailed(Exception):
    """The term query did not produce usable results; the batch must not be translated without them.

    retryable is False when re-sending the query cannot help (e.g. the local search failed).
    """

    def __init__(self, reason: str, reply: str = "", retryable: bool = True):
        super().__init__(reason)
        self.reason = str(reason)
        self.reply = reply if isinstance(reply, str) else ""
        self.retryable = bool(retryable)


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
        self._active: dict[int, dict] = {}  # batch_id -> {'entries': [PageEntry], 'token': CancelToken}
        self._cancelled_ids: set[int] = set()  # cancelled batches whose worker has not finished yet
        self._state_io_lock = threading.Lock()  # always taken BEFORE self.lock, never while holding it
        self._backlog_full = False
        self.tracker = ChapterTracker()
        self.corpus = termlookup.TermCorpus()
        self.auto_dispatch = True
        self.manual_active = False
        self.manual_entries: list[PageEntry] | None = None
        self._last_accepted_fp = ""          # clipboard text accepted last (see accept_text)
        self._last_accepted_key: str | None = None
        self._ignored_fps: list[str] = []    # text the translator copied itself ('manual' prompts)
        self._clip_watcher: clipaccept.ClipboardWatcher | None = None
        self._skip_url: str | None = None    # last session's reopen address; its first page is ignored

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
        self.load_reopen_address()
        with self.lock:
            self.stop_event.clear()
            self._worker_target = self.cfg.concurrency
            self._ensure_workers_locked()

    def on_book_changed(self) -> None:
        """Reset session tracking and load the new book's Missing/Failed lists."""
        self.tracker.reset()
        self.corpus.clear()
        with self.lock:
            self._last_accepted_fp = ""
            self._last_accepted_key = None
        self.sync_batch_seq()
        self.load_queue_state()
        self.load_reopen_address()
        if self.clipboard_watch_active():
            con.info("Clipboard watch is still on; newly copied pages now go to this book.")

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

    def load_reopen_address(self) -> None:
        """Remember the book's reopen address; the first page that arrives from it is ignored."""
        point = resume.load_reopen(self.cfg.book_dir)
        with self.lock:
            self._skip_url = point.url if point else None

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

    def offer_page(self, content: str, is_active: bool, title: str | None = None,
                   url: str | None = None) -> dict:
        """Ingest a page from Tampermonkey and return the scraper directive.

        url is the page's own address and becomes the book's reopen address. The first page
        that arrives from the previous session's reopen address is ignored: it was already scraped.
        """
        if not isinstance(content, str) or not content.strip():
            return {"action": "wait", "retryAfter": 3, "reason": "Empty content received"}
        if not self.cfg.book_dir:
            return {"action": "wait", "retryAfter": 5, "reason": "No book directory selected"}
        page_url = resume.clean_url(url)
        if self._take_skip(page_url):
            self._log_skipped(content, title)
            directive = self.poll_status(is_active)
        else:
            directive = self._queue_scraped(content, title, page_url)
        if directive.get("action") in ("wait", "stop"):
            return directive

        # Pacing delay to avoid hammering the source novel website
        delay = 1.0 if is_active else random.uniform(1.0, 3.5)
        time.sleep(delay)
        return directive

    def _queue_scraped(self, content: str, title: str | None, page_url: str | None) -> dict:
        """Queue a scraped page, store its URL as the reopen address, return the directive."""
        with self.lock:
            entry, confidence, replaced, forgot = self._ingest_locked(content, title)
            pending = len(self.pool)
            note = " · replaced pending copy" if replaced else ""
            con.info(
                f"Queued page ({len(content):,} chars) · [bold cyan]{escape(entry.label)}[/bold cyan] "
                f"· pending [bold]{pending}[/bold]{note}"
            )
            directive = self._scraper_directive_locked()
        self._after_ingest(entry, confidence, forgot)
        self._save_reopen(page_url, entry)
        return directive

    def _take_skip(self, page_url: str | None) -> bool:
        """True exactly once for the previous session's reopen address."""
        if not page_url:
            return False
        with self.lock:
            if page_url != self._skip_url:
                return False
            self._skip_url = None
            return True

    def _log_skipped(self, content: str, title: str | None) -> None:
        """Explain why a page from the reopen address was not queued."""
        chapter = parsing.parse_chapter_number(title if isinstance(title, str) else "")
        if chapter is None:
            chapter = parsing.parse_chapter_number(content)
        label = f"Chapter {chapter}" if chapter else "The page"
        con.info(
            f"Ignored {escape(label)} from the reopen address (already scraped last session); "
            "the scraper moves on to the next chapter."
        )
        if chapter and not self._has_translation(chapter):
            con.dim(f"{escape(label)} has no translated file yet. If it still needs translating, run 'retry {chapter}'.")

    def _has_translation(self, chapter: int) -> bool:
        filename = storage.sanitize_filename(f"Chapter {chapter}.md")
        return os.path.isfile(os.path.join(self.cfg.book_dir, filename))

    def _save_reopen(self, page_url: str | None, entry: PageEntry) -> None:
        """Make the page's URL the book's reopen address. Must not be called while holding self.lock."""
        if not page_url:
            return
        try:
            resume.save_reopen(self.cfg.book_dir, page_url, entry.chapter)
        except (OSError, ValueError) as e:
            con.warn(f"Could not save the reopen address to {resume.RESUME_FILENAME}: {escape(str(e))}")

    def _after_ingest(self, entry: PageEntry, confidence: str, forgot: int) -> None:
        """Disk work after a page joined the pool. Must not be called while holding self.lock."""
        if confidence == "parsed":
            self._store_raw_page(entry)
        if forgot:
            self._persist_queue_state()

    def _ingest_locked(self, content: str, title: str | None,
                       chapter: int | None = None) -> tuple[PageEntry, str, int, int]:
        self.counters["pages_seen"] += 1
        ch_num, confidence = self.tracker.observe_page(title, content, chapter)
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
    # Manual chapter accept (clipboard)
    # ------------------------------------------------------------------

    def accept_text(self, text, chapter: int | None = None, force: bool = False,
                    source: str = "clipboard", watch: bool = False) -> list[tuple[str, str]]:
        """Queue copied text as a raw page, exactly like a scraped page. Returns (level, message) pairs.

        chapter overrides the number found in the text. Copying the same text again is ignored,
        unless a chapter is given: then the earlier pending copy is replaced under that number.
        In watch mode every rejection is reported dimly, since most copies are not chapters.
        """
        if not self.cfg.book_dir:
            return [("dim" if watch else "warn", "No book directory selected; the copied text was not queued.")]
        if chapter is not None and (isinstance(chapter, bool) or not isinstance(chapter, int) or chapter <= 0):
            return [("fail", f"Chapter must be a positive whole number, not {escape(repr(chapter))}.")]
        with self.lock:
            fingerprint = clipaccept.fingerprint(text)
            relabel = chapter is not None and bool(fingerprint) and fingerprint == self._last_accepted_fp
            check = clipaccept.check_clipboard_text(
                text, "" if relabel else self._last_accepted_fp, self._ignored_fps, force
            )
            if not check.ok:
                return [("dim" if check.quiet or watch else "warn", escape(check.reason))]
            dropped = self.pool.remove([self._last_accepted_key]) if relabel and self._last_accepted_key else 0
            entry, confidence, replaced, forgot = self._ingest_locked(text, None, chapter)
            self._last_accepted_fp, self._last_accepted_key = fingerprint, entry.key
            report = self._accept_report_locked(entry, confidence, replaced, source)
        self._after_ingest(entry, confidence, forgot)
        if dropped:
            report.insert(1, ("info", "Removed the earlier pending copy of this text that had the wrong chapter number."))
        return report

    def _accept_report_locked(self, entry: PageEntry, confidence: str, replaced: int,
                              source: str) -> list[tuple[str, str]]:
        pending = len(self.pool)
        note = " · replaced pending copy" if replaced else ""
        report = [("ok", (
            f"Accepted [bold cyan]{escape(entry.label)}[/bold cyan] from {escape(str(source))} "
            f"({len(entry.content):,} chars) · pending [bold]{pending}[/bold]{note}"
        ))]
        if confidence != "parsed":
            report.append(("warn", (
                f"No chapter number found in the copied text, so it was queued as {escape(entry.label)}. "
                "With the text still copied, run 'accept <n>' to set the number."
            )))
        if self.tracker.goal_reached():
            self._goal_directive_locked()
        elif self.tracker.goal_state() == "none" and pending > self.backlog_limit():
            report.append(("info", (
                f"{pending} pages pending, above the scraper's limit of {self.backlog_limit()}; "
                "copied pages are accepted anyway."
            )))
        report.append(self._accept_dispatch_note_locked(pending))
        return report

    def _accept_dispatch_note_locked(self, pending: int) -> tuple[str, str]:
        if not self._dispatch_enabled_locked():
            return ("info", "Auto-dispatch is paused or a manual session is open; use 'manual' or 'flush' when ready.")
        if self.pool.drain or pending >= self.cfg.batch_size:
            return ("dim", "It goes out with the next batch.")
        return ("dim", f"Sent once {self.cfg.batch_size} pages are pending; use 'flush' to send now.")

    def _ignore_text_locked(self, text: str) -> None:
        """Remember text the translator put on the clipboard so accept never queues it."""
        fingerprint = clipaccept.fingerprint(text)
        if fingerprint and fingerprint not in self._ignored_fps:
            self._ignored_fps.append(fingerprint)
            del self._ignored_fps[:-clipaccept.MAX_IGNORED_FINGERPRINTS]

    def start_clipboard_watch(self, report) -> tuple[bool, str]:
        """Queue every newly copied chapter page; report receives each accept_text result."""
        if not callable(report):
            raise TypeError("report must be callable")
        if not HAS_CLIPBOARD:
            return False, "pyperclip not available. Install it to watch the clipboard."
        if self.clipboard_watch_active():
            return False, "Clipboard watch is already on."
        watcher = clipaccept.ClipboardWatcher(
            pyperclip.paste,
            lambda text: report(self.accept_text(text, source="clipboard watch", watch=True)),
        )
        try:
            watcher.start()
        except Exception as e:  # pyperclip raises platform-specific exception types
            return False, f"Could not read the clipboard: {escape(str(e))}"
        with self.lock:
            previous, self._clip_watcher = self._clip_watcher, watcher
        if previous is not None:
            previous.stop()
        book = escape(self.cfg.book_name or "(no book)")
        return True, (
            f"Clipboard watch on: every newly copied chapter page is queued for [bold green]{book}[/bold green]. "
            "What is on the clipboard right now is skipped; use 'accept' to take it. 'accept off' stops watching."
        )

    def stop_clipboard_watch(self) -> bool:
        """Stop the clipboard watcher. Returns True when it was running."""
        with self.lock:
            watcher, self._clip_watcher = self._clip_watcher, None
        return watcher.stop() if watcher is not None else False

    def clipboard_watch_active(self) -> bool:
        with self.lock:
            watcher = self._clip_watcher
        return watcher is not None and watcher.is_running

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
            self._ignore_text_locked(prompt)
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

        reply = parsing.parse_reply(text)
        if reply.error:
            return f"Parse error: {escape(reply.error)}. Please copy the entire JSON object and run paste again."
        if not self.cfg.book_dir:
            return "No target book folder selected."

        chapters = reply.chapters or []
        log = BatchLog("Manual paste")
        try:
            numbers = self._save_results(log, chapters, reply)
            with self.lock:
                entries = self.manual_entries if self.manual_active else None
                self.manual_active = False
                self.manual_entries = None
                self.pool.notify()
            if entries:
                self._reconcile(log, "manual", entries, chapters, numbers)
                self._check_term_drift(log, entries, chapters)
                return "Manual session closed."
            self._resolve_returned(numbers)
            return "Pasted translation processed."
        finally:
            log.emit()

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
        """Abort in-flight batches, drop them from the in-flight list and return their pages."""
        recovered = []
        for bid in sorted(self._active):
            info = self._active.pop(bid)
            token = info.get("token")
            if isinstance(token, CancelToken):
                token.cancel()
            self._cancelled_ids.add(bid)
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
                f"Cancelled in-flight batch(es) {ids}: request aborted and {count} page(s) returned to the front of the pending pool. "
                f"A reply that still arrives is discarded."
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
            if batch_id in self._active:
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
            batch_id, entries, token = claimed
            try:
                self._process_batch(batch_id, entries, token)
            except Exception as e:  # one bad batch must never kill the worker
                con.fail(f"Unexpected worker error on batch #{batch_id}: {escape(str(e))}")
        con.warn(f"Worker {worker_id} reached its iteration limit and stopped.")

    def _claim_batch(self) -> tuple[int, list[PageEntry], CancelToken] | None:
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
            token = CancelToken()
            self._active[batch_id] = {"entries": list(entries), "token": token}
            self.counters["batches_sent"] += 1
            return batch_id, entries, token

    def _process_batch(self, batch_id: int, entries: list[PageEntry], token: CancelToken) -> None:
        try:
            self._run_batch(batch_id, entries, token)
        except Exception as e:
            self._handle_batch_failure(batch_id, entries, "", f"Unexpected {type(e).__name__}: {e}")
        finally:
            with self.lock:
                self._active.pop(batch_id, None)
                self._cancelled_ids.discard(batch_id)
                self.counters["batches_done"] += 1
                self.pool.notify()

    def _is_cancelled(self, batch_id: int) -> bool:
        with self.lock:
            return batch_id in self._cancelled_ids

    def _run_batch(self, batch_id: int, entries: list[PageEntry], token: CancelToken) -> None:
        if not isinstance(token, CancelToken):
            raise TypeError("token must be a CancelToken")
        label = f"Batch #{batch_id}"
        pages = [e.content for e in entries]
        self._spool_batch(batch_id, pages)
        pages_text = PAGE_SEPARATOR.join(pages)
        cancelled_note = f"{label}: cancelled during the term query; nothing was translated (pages are back in the pending pool)."
        try:
            scout = self._scout_terms(label, entries, pages_text, token)
            messages = self._translation_messages(pages_text, scout)
        except ApiCancelled:
            con.dim(cancelled_note)
            return
        except ScoutFailed as e:
            self._handle_batch_failure(batch_id, entries, e.reply, f"Term query failed: {e.reason}")
            return
        if self._is_cancelled(batch_id):
            con.dim(cancelled_note)
            return
        if self.cfg.dry_run:
            self._log_dry_run(label, entries, messages)
            return
        self._send_translation(batch_id, entries, messages, scout.lookup.first_seen, token)

    def _spool_batch(self, batch_id: int, pages: list[str]) -> None:
        """Back up the batch's pages to _raw/ before any API call."""
        try:
            storage.spool_pages(self.cfg.book_dir, batch_id, pages)
        except OSError as e:
            con.warn(f"Batch #{batch_id}: could not back up pages to _raw/ ({escape(str(e))}); continuing without backup.")

    def _log_dry_run(self, label: str, entries: list[PageEntry], messages: list[dict]) -> None:
        model = escape(self.cfg.model or "(no model)")
        con.dim(
            f"{label}: dry run · built {len(messages)} message(s) for {model} · "
            f"{len(entries)} pages · {escape(describe_entries(entries))}"
        )

    def _send_translation(self, batch_id: int, entries: list[PageEntry], messages: list[dict],
                          first_seen: dict, token: CancelToken) -> None:
        """Send the translation call and process its reply."""
        label = f"Batch #{batch_id}"
        con.sent(f"{label}: Translation sent to [bold]{escape(self.cfg.model)}[/bold]")
        started = time.monotonic()
        try:
            raw_reply, usage = self.client.chat(self.cfg, messages, cancel=token, label=f"{label}: Translation")
        except ApiCancelled:
            con.dim(f"{label}: aborted; its pages are already back in the pending pool.")
            return
        except ApiError as e:
            self._handle_batch_failure(batch_id, entries, "", f"API error: {e}", summary=f"API error: {_error_summary(e)}")
            return
        self._record_usage(usage)
        self._save_raw_reply(batch_id, raw_reply)
        if self._is_cancelled(batch_id):
            con.dim(f"{label}: reply arrived after cancellation and was discarded (pages are already back in the pending pool).")
            return
        timing = f"in {time.monotonic() - started:.0f}s{_tokens_text(usage)}"
        self._process_reply(batch_id, entries, raw_reply, first_seen, timing)

    def _process_reply(self, batch_id: int, entries: list[PageEntry], raw_reply: str,
                       first_seen: dict, timing: str) -> None:
        """Parse, save and check a translation reply, then print it as one block."""
        reply = parsing.parse_reply(raw_reply)
        if reply.error:
            self._handle_batch_failure(batch_id, entries, raw_reply, f"JSON error: {reply.error}")
            return
        if not reply.chapters:
            self._handle_batch_failure(batch_id, entries, raw_reply, "Reply contained no chapters")
            return
        log = BatchLog(f"Batch #{batch_id}")
        try:
            numbers = self._save_results(log, reply.chapters, reply, first_seen, timing)
            self._reconcile(log, batch_id, entries, reply.chapters, numbers)
            self._check_term_drift(log, entries, reply.chapters)
        finally:
            log.emit()

    # ------------------------------------------------------------------
    # Prior-usage term lookup
    # ------------------------------------------------------------------

    def _scout_terms(self, label: str, entries: list[PageEntry], pages_text: str,
                     token: CancelToken | None = None) -> ScoutOutcome:
        """Run the term query, re-sending it once if it fails.

        Returns an empty outcome when the lookup is off or in a dry run. Raises ScoutFailed when no
        attempt produced usable results, and ApiCancelled when the batch was cancelled.
        """
        if not self.cfg.term_lookup:
            return ScoutOutcome()
        if self.cfg.dry_run:
            con.dim(f"{label}: dry run · term query skipped")
            return ScoutOutcome()
        span = escape(describe_entries(entries))
        last: ScoutFailed | None = None
        for attempt in range(1, SCOUT_ATTEMPTS + 1):
            again = f" [dim](attempt {attempt}/{SCOUT_ATTEMPTS})[/dim]" if attempt > 1 else ""
            con.sent(f"{label}: Term query sent ({len(entries)} pages · {span}){again}")
            try:
                return self._scout_once(label, entries, pages_text, token)
            except ScoutFailed as e:
                if not e.retryable:
                    raise
                last = e
            if attempt < SCOUT_ATTEMPTS:
                con.warn(f"{label}: Term query failed ({escape(_one_line(last.reason))}); sending it once more")
        reason = last.reason if last is not None else "no attempt was made"
        reply = last.reply if last is not None else ""
        raise ScoutFailed(f"sent {SCOUT_ATTEMPTS} times, last error: {reason}", reply, retryable=False)

    def _scout_once(self, label: str, entries: list[PageEntry], pages_text: str,
                    token: CancelToken | None) -> ScoutOutcome:
        """One term-query call plus the prior-usage search. Raises ScoutFailed or ApiCancelled."""
        messages = prompts.build_scout_conversation(self.cfg.book_dir, pages_text)
        try:
            raw_reply, usage = self.client.chat(
                self.cfg, messages, max_tokens=SCOUT_MAX_TOKENS, cancel=token, label=f"{label}: Term query"
            )
        except ApiError as e:
            raise ScoutFailed(f"API error: {_error_summary(e)}") from e
        self._record_usage(usage)
        reply_text = raw_reply if isinstance(raw_reply, str) else ""
        proposed, err = parsing.parse_scout_reply(reply_text)
        if err:
            raise ScoutFailed(f"unusable reply: {err}", reply_text)
        try:
            result = self._lookup_prior_usage(entries, pages_text, proposed)
        except Exception as e:  # a local search problem; re-sending the query cannot fix it
            raise ScoutFailed(f"prior-usage search failed: {type(e).__name__}: {e}", reply_text, retryable=False) from e
        self._log_lookup(label, len(proposed), result)
        return ScoutOutcome(result, messages, reply_text, ran=True)

    def _lookup_prior_usage(self, entries: list[PageEntry], pages_text: str,
                            proposed: list) -> termlookup.LookupResult:
        book_dir = self.cfg.book_dir
        candidates = termlookup.filter_candidates(book_dir, proposed, pages_text)
        batch_chapters = {e.chapter for e in entries if e.chapter is not None}
        return termlookup.build_prior_usage(self.corpus, book_dir, candidates, batch_chapters)

    def _translation_messages(self, pages_text: str, scout: ScoutOutcome) -> list[dict]:
        """Continue the term-query conversation, or build one plain call when no term query ran.

        Raises ScoutFailed when a term query ran but its conversation cannot be continued.
        """
        if not scout.ran:
            return prompts.build_messages(self.cfg.book_dir, pages_text)
        if not scout.usable:
            raise ScoutFailed("the term query left no conversation to continue", scout.reply, retryable=False)
        try:
            return prompts.build_translation_followup(scout.messages, scout.reply, scout.lookup.context)
        except ValueError as e:
            raise ScoutFailed(f"could not continue the term-query conversation: {e}", scout.reply, retryable=False) from e

    @staticmethod
    def _log_lookup(label: str, proposed: int, result: termlookup.LookupResult) -> None:
        """One line summing up the term query's answer and the prior-usage search."""
        if not result.candidates:
            text = f"{label}: Term query returned {proposed} term(s), none new"
        else:
            text = f"{label}: Term query returned {result.candidates} new term(s), {len(result.usages)} used before"
            found = [f"{u.candidate.chinese} ← Ch {u.chapters[0]}" for u in result.usages]
            if found:
                more = ", …" if len(found) > MAX_LISTED_NAMES else ""
                text += f" ({escape(', '.join(found[:MAX_LISTED_NAMES]))}{more})"
        notes = []
        if result.truncated:
            notes.append(f"{result.truncated} left out to keep the prompt small")
        if result.warnings:
            notes.append(f"{len(result.warnings)} lookup warning(s), e.g. {_one_line(result.warnings[0], 80)}")
        if notes:
            text += f" [dim]· {escape(' · '.join(notes))}[/dim]"
        con.received(text)

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

    def _handle_batch_failure(self, batch_id: int, entries: list[PageEntry], reply: str, error: str,
                              summary: str | None = None) -> None:
        """Quarantine a failed batch and list its chapters as Failed. Never re-queued automatically.

        error is stored in full in _failed/; summary (default: error) is what the console shows.
        """
        label = f"Batch #{batch_id}"
        shown = escape(_one_line(summary or error))
        if self._is_cancelled(batch_id):
            con.dim(f"{label}: ended after cancellation ({shown}); its pages are already back in the pending pool.")
            return
        with self.lock:
            self.counters["failures"] += 1
        pages = [e.content for e in entries]
        chapters = [e.chapter for e in entries if e.chapter is not None]
        log = BatchLog(label)
        log.head("fail", f"Failed: {shown}")
        try:
            qpath = storage.quarantine_failure(self.cfg.book_dir, batch_id, pages, reply, error, chapters=chapters)
        except OSError as qe:
            with self.lock:
                self.pool.add(entries, front=True)
            log.fail(f"Could not quarantine it ({escape(str(qe))}); {len(pages)} page(s) returned to the pending pool")
            log.emit()
            return
        with self.lock:
            self.pool.mark_failed(entries, batch_id, error)
        self._persist_queue_state()
        log.detail(
            f"{escape(describe_entries(entries))} marked Failed · saved to [cyan]{escape(os.path.basename(qpath))}[/cyan] "
            "· not re-queued, use 'retry failed'"
        )
        log.emit()

    # ------------------------------------------------------------------
    # Results
    # ------------------------------------------------------------------

    def _save_results(self, log: BatchLog, chapters: list, reply: parsing.ParsedReply,
                      first_seen: dict | None = None, timing: str = "") -> list[int]:
        """Store chapters, apply character, term and arc updates. Returns translated chapter numbers.

        first_seen (from the prior-usage lookup) maps Chinese terms to their earliest earlier chapter.
        Every outcome is added to log; the caller prints it.
        """
        numbers = self._store_chapters(log, chapters, timing)
        self._apply_character_actions(log, reply.character_actions)
        self._apply_term_actions(log, reply.term_actions, numbers, first_seen)
        self._apply_arc_ends(log, reply.arc_ends, chapters)
        return numbers

    def _reconcile(self, log: BatchLog, batch_ref, entries: list[PageEntry], chapters: list,
                   numbers: list[int]) -> None:
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
            log.warn(
                f"Missing {escape(describe_entries(missing))}: left out of the reply "
                "(not re-queued; use 'retry missing')"
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

    def _store_chapters(self, log: BatchLog, chapters: list, timing: str = "") -> list[int]:
        """Write chapters to disk, update counters and tracker, and set the block's headline."""
        saved, skipped, _written = storage.save_chapters(chapters, self.cfg.book_dir)
        numbers = self._record_translated(chapters, saved, skipped)
        noun = "chapter" if saved == 1 else "chapters"
        msg = f"Saved [bold green]{saved}[/bold green] {noun}"
        span = _format_chapter_ranges(numbers)
        if span:
            msg += f" ({span})"
        msg += f" to '{escape(self.cfg.book_name)}'"
        if skipped:
            msg += f", [yellow]{skipped} skipped[/yellow]"
        if timing:
            msg += f" [dim]{escape(timing)}[/dim]"
        log.head("ok" if saved else "warn", msg)
        if saved:
            self._record_activity(log, numbers)
        return numbers

    def _record_activity(self, log: BatchLog, numbers: list[int]) -> None:
        """Stamp the book as just translated so the book picker lists it first."""
        try:
            activity.record_translation(self.cfg.book_dir, numbers)
        except (OSError, ValueError, TypeError) as e:
            log.warn(f"Could not update {activity.ACTIVITY_FILENAME}: {escape(str(e))}")

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

    def _apply_arc_ends(self, log: BatchLog, arc_ends: list, chapters: list) -> None:
        """Record author-declared arc/volume endings in Arcs.json and note every change."""
        if not arc_ends:
            return
        valid = arcs.chapter_numbers([entry for entry in chapters if _is_saveable(entry)])
        try:
            summary = arcs.record_arc_ends(self.cfg.book_dir, arc_ends, valid)
        except (OSError, ValueError, TypeError) as e:
            log.fail(f"Arc/volume update failed: {escape(str(e))}")
            return
        self._log_arc_summary(log, summary)

    @staticmethod
    def _log_arc_summary(log: BatchLog, summary: dict) -> None:
        """One detail line per recorded change; skipped entries and failures are never silent."""
        for ev in summary.get("recorded", []):
            kind = "Volume" if ev["type"] == "volume" else "Arc"
            log.detail(f"{kind} end recorded at Chapter {ev['chapter']}: “{escape(str(ev['name']))}”")
        for ev in summary.get("extended", []):
            log.detail(f"Afterword Chapter {ev['to']} added to “{escape(str(ev['name']))}” (was ending at {ev['from']})")
        for reason in summary.get("skipped", [])[:MAX_REPORTED_WARNINGS]:
            log.warn(f"Arc/volume entry skipped: {escape(str(reason))}")
        for err in summary.get("failed", []):
            log.fail(f"Arc/volume update failed: {escape(str(err))}")

    def _apply_character_actions(self, log: BatchLog, char_actions: list) -> None:
        """Apply character mutations and add one combined summary line."""
        if not char_actions:
            return
        try:
            summary = characters.process_character_actions(self.cfg.book_dir, char_actions)
        except (OSError, ValueError) as e:
            log.fail(f"Character updates failed: {escape(str(e))}")
            return
        for err in summary.get("failed", []):
            log.fail(f"Character update failed: {escape(str(err))}")
        line = characters.format_summary(summary)
        if line:
            log.detail(escape(line))

    def _apply_term_actions(self, log: BatchLog, term_actions: list, numbers: list[int],
                            first_seen: dict | None = None) -> None:
        """Register new glossary terms; a different rendering for a locked term is only reported."""
        if not term_actions:
            return
        first = min(numbers) if numbers else None
        try:
            summary = glossary.record_term_actions(self.cfg.book_dir, term_actions, first, first_seen)
        except (OSError, ValueError, TypeError) as e:
            log.fail(f"Glossary update failed: {escape(str(e))}")
            return
        self._log_term_summary(log, summary)
        self._verify_recovered(log, summary.get("recovered", []))

    def _verify_recovered(self, log: BatchLog, recovered) -> None:
        """One warning naming terms registered from prior usage that their first chapter does not contain."""
        if not isinstance(recovered, list):
            return
        missing, unchecked = [], 0
        for ev in recovered[:glossary.MAX_ACTIONS_PER_REPLY]:
            if not isinstance(ev, dict):
                continue
            english = str(ev.get("english") or "")
            found = termlookup.rendering_in_chapter(self.cfg.book_dir, ev.get("chapter"), english)
            if found is None:
                unchecked += 1
            elif not found:
                missing.append(ev)
        if missing:
            log.warn(_unconfirmed_text(missing))
        if unchecked:
            log.detail(f"[dim]{_plural(unchecked, 'reused rendering', 'reused renderings')} could not be checked (no readable translation)[/dim]")

    @staticmethod
    def _log_term_summary(log: BatchLog, summary: dict) -> None:
        """Failures one line each; additions and reused renderings one line; conflicts one warning."""
        for err in summary.get("failed", []):
            log.fail(f"Glossary update failed: {escape(str(err))}")
        parts = []
        line = glossary.format_summary(summary)
        if line:
            parts.append(escape(line))
        reused = _reused_text(summary.get("recovered", []))
        if reused:
            parts.append(reused)
        if parts:
            log.detail(" · ".join(parts))
        conflicts = _conflicts_text(summary.get("conflicts", []))
        if conflicts:
            log.warn(conflicts)

    def _check_term_drift(self, log: BatchLog, entries: list[PageEntry], chapters: list) -> None:
        """One warning listing locked terms whose English is missing from the translation, grouped by term."""
        try:
            groups = self._find_term_drift(entries, chapters)
        except Exception as e:  # a checker bug must never fail a batch that is already saved
            log.warn(f"Term drift check failed: {escape(str(e))}")
            return
        text = _drift_text(groups)
        if text:
            log.warn(text)

    def _find_term_drift(self, entries: list[PageEntry], chapters: list) -> list:
        """[((english, forms), [chapters])] for drifted terms, the term missing from most chapters first."""
        translated = self._translated_by_number(chapters)
        if not translated:
            return []
        terms = glossary.load_terms(self.cfg.book_dir)
        if not terms:
            return []
        groups: dict[tuple[str, str], list[int]] = {}
        for entry in entries:
            if entry.chapter not in translated:
                continue
            pair = (entry.label, entry.content, translated[entry.chapter])
            for ev in glossary.check_term_usage(terms, [pair]):
                groups.setdefault((ev["english"], ev["forms"]), []).append(entry.chapter)
        return sorted(groups.items(), key=lambda item: (-len(item[1]), item[0][0].casefold()))

    @staticmethod
    def _translated_by_number(chapters: list) -> dict[int, str]:
        """Translated chapter content keyed by chapter number."""
        result = {}
        for entry in chapters or []:
            if not _is_saveable(entry):
                continue
            num = parsing.parse_chapter_number(str(entry["chapterNo"]))
            content = entry.get("content")
            if num is not None and isinstance(content, str):
                result[num] = content
        return result

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
                    (bid, list(info["entries"]))
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
                "term_lookup": self.cfg.term_lookup,
                "clipboard_watch": self.clipboard_watch_active(),
                "counters": dict(self.counters),
                "tracker": self.tracker.snapshot(),
                **counts,
            }

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def shutdown(self, drain: bool = True, timeout: int = 600) -> None:
        """Send pending pages (if dispatch is enabled), wait for in-flight batches, then stop."""
        self.stop_clipboard_watch()
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
