"""Translation pipeline and worker thread management."""

import os
import queue
import random
import threading
import time

from rich.markup import escape

from . import console as con
from . import arcs, prompts, parsing, storage, characters
from .tracker import ChapterTracker
from .client import ApiClient, ApiError

try:
    import pyperclip
    HAS_CLIPBOARD = True
except ImportError:
    HAS_CLIPBOARD = False

PAGE_SEPARATOR = "\n\n--- PAGE BREAK ---\n\n"
LABEL_CACHE_LIMIT = 1000  # remembered "Chapter N" labels for buffered pages


def _is_saveable(entry) -> bool:
    """Mirror storage.save_chapters: an entry needs a chapterNo and non-empty content."""
    if not isinstance(entry, dict):
        return False
    return bool(str(entry.get("chapterNo") or "").strip()) and bool(entry.get("content"))


_format_chapter_ranges = parsing.format_chapter_ranges


class Pipeline:
    def __init__(self, cfg, client: ApiClient):
        self.cfg = cfg
        self.client = client
        self.lock = threading.RLock()
        self.buffer = []
        self.work_q = queue.Queue()
        self.stop_event = threading.Event()
        self.workers = []
        self._worker_target = cfg.concurrency
        self._batch_seq = 0
        self._active = {}  # batch_id -> {'pages': list, 'cancel': bool}
        self._cancel_gen = 0  # bumped by /cancel; queue items tagged with an older value are recovered
        self.tracker = ChapterTracker()
        self.auto_dispatch = True
        self.manual_active = False
        self.manual_pages = None
        self._page_labels: dict[str, str] = {}

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

    def start(self) -> None:
        """Spawn worker daemon threads matching target concurrency."""
        self.sync_batch_seq()
        with self.lock:
            self.stop_event.clear()
            for i in range(self.cfg.concurrency):
                self._spawn_worker(i + 1)

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

    def _spawn_worker(self, worker_id: int) -> None:
        t = threading.Thread(target=self._worker_loop, args=(worker_id,), daemon=True)
        self.workers.append(t)
        t.start()

    def set_concurrency(self, new_val: int) -> None:
        with self.lock:
            self.cfg.concurrency = new_val
            self._worker_target = new_val
            alive_count = len([w for w in self.workers if w.is_alive()])
            if new_val > alive_count:
                for i in range(alive_count, new_val):
                    self._spawn_worker(i + 1)

    def offer_page(self, content: str, is_active: bool, title: str | None = None) -> dict:
        """Ingest page from Tampermonkey and return scraper directive."""
        with self.lock:
            self.counters["pages_seen"] += 1
            ch_num, conf = self.tracker.observe_page(title, content)
            self.buffer.append(content)
            count = len(self.buffer)
            needed = self.cfg.batch_size

            ch_label = self._chapter_label(ch_num, conf)
            self._remember_label_locked(content, ch_label)
            con.info(f"Buffered page [bold]{count}/{needed}[/bold] ({len(content):,} chars) · [bold cyan]{ch_label}[/bold cyan]")

            # Check if scraper reached stop goal
            if self.tracker.goal_reached():
                directive = self._goal_directive_locked()
                return directive

            if count < needed:
                directive = None
            else:
                directive = self._try_dispatch_locked()

        if directive is not None and directive.get("action") in ("wait", "stop"):
            return directive

        # Pacing delay to avoid hammering the source novel website
        delay = 1.0 if is_active else random.uniform(1.0, 3.5)
        time.sleep(delay)
        return directive if directive else {"action": "next", "buffered": count, "needed": needed, "chapter": ch_num}

    @staticmethod
    def _chapter_label(ch_num: int | None, confidence: str) -> str:
        """Human label for a detected chapter: 'Chapter 12', 'Chapter ~13' or 'Chapter ?'."""
        if ch_num is None or confidence == "unknown":
            return "Chapter ?"
        prefix = "~" if confidence == "inferred" else ""
        return f"Chapter {prefix}{ch_num}"

    def _remember_label_locked(self, content: str, label: str) -> None:
        """Remember the label shown at ingest so the buffer command repeats it (bounded cache)."""
        self._page_labels[content] = label
        if len(self._page_labels) > LABEL_CACHE_LIMIT:
            self._page_labels.pop(next(iter(self._page_labels)))

    def page_label(self, content: str) -> str:
        """Chapter label for a buffered page; falls back to parsing the page text."""
        with self.lock:
            label = self._page_labels.get(content)
        if label:
            return label
        ch_num = parsing.parse_chapter_number(content)
        return self._chapter_label(ch_num, "parsed" if ch_num is not None else "unknown")

    def poll_status(self, is_active: bool) -> dict:
        """Handle polling requests from scraper during wait states."""
        with self.lock:
            if self.tracker.goal_reached():
                return self._goal_directive_locked()
            if len(self.buffer) < self.cfg.batch_size:
                return {"action": "next", "buffered": len(self.buffer), "chapter": self.tracker.current_chapter}
            return self._try_dispatch_locked()

    def _goal_directive_locked(self) -> dict:
        """Flush any buffered pages and halt scraper upon reaching goal."""
        if not self.tracker.goal_reached_flag:
            self.tracker.goal_reached_flag = True
            goal = self.tracker.goal_chapter
            curr = self.tracker.current_chapter
            con.panel(
                f"Goal of Chapter [bold green]{goal}[/bold green] reached (current: {curr})!\n"
                f"Flushing remaining buffered pages to active worker queue...",
                title="Goal Reached",
                style="green"
            )
            if self.buffer and self.auto_dispatch:
                pages = list(self.buffer)
                self.buffer = []
                bid = self._enqueue_locked(pages)
                con.ok(f"Dispatched final batch [bold cyan]#{bid}[/bold cyan] ({len(pages)} pages)")

        return {
            "action": "stop",
            "reason": f"Goal chapter {self.tracker.goal_chapter} reached",
            "chapter": self.tracker.current_chapter,
            "goal": self.tracker.goal_chapter
        }

    def _try_dispatch_locked(self) -> dict:
        if not self.cfg.book_dir:
            return {"action": "wait", "retryAfter": 5, "reason": "No book directory selected"}
        if not self.auto_dispatch:
            return {"action": "wait", "retryAfter": 5, "reason": "Auto-dispatch paused for manual mode"}

        # Backstop: stop queueing new work if translated chapters hit or exceed goal
        if self.tracker.translated_goal_reached():
            return {"action": "stop", "reason": f"Translation goal of Chapter {self.tracker.goal_chapter} reached"}

        depth = self.work_q.qsize()
        if depth >= max(2, self.cfg.concurrency * 2):
            con.warn(f"Work queue full ({depth} batches waiting), asking scraper to hold...")
            return {"action": "wait", "retryAfter": 5, "reason": "Translation queue busy"}

        pages = list(self.buffer)
        self.buffer = []
        batch_id = self._enqueue_locked(pages)
        con.ok(f"Enqueued batch [bold cyan]#{batch_id}[/bold cyan] ({len(pages)} pages)")
        return {"action": "next", "status": "dispatched", "batchId": batch_id}

    def _enqueue_locked(self, pages: list[str]) -> int:
        self._batch_seq += 1
        bid = self._batch_seq
        self.counters["batches_sent"] += 1
        self.work_q.put((bid, pages, self._cancel_gen))
        return bid

    def flush(self) -> str:
        """Manually dispatch whatever pages are currently sitting in buffer."""
        with self.lock:
            if not self.buffer:
                return "Buffer is empty. Nothing to flush."
            pages = list(self.buffer)
            self.buffer = []
            bid = self._enqueue_locked(pages)
            return f"Flushed {len(pages)} page(s) as batch #{bid}."

    def manual_begin(self) -> tuple[str | None, str]:
        """Export current buffer to clipboard for manual out-of-band translation."""
        with self.lock:
            if self.manual_active:
                return None, "Manual mode is already active."
            if not self.buffer:
                return None, "Buffer is empty. No pages to translate."
            pages = list(self.buffer)
            self.buffer = []
            self.manual_pages = pages
            self.manual_active = True
            pages_text = PAGE_SEPARATOR.join(pages)
            prompt = prompts.build_manual_prompt(self.cfg.book_dir, pages_text)

        if HAS_CLIPBOARD:
            try:
                pyperclip.copy(prompt)
                return prompt, f"Copied prompt for {len(pages)} page(s) to clipboard. Bring the reply back with the paste command."
            except Exception as e:
                return prompt, f"Clipboard write failed ({e}). Prompt shown below. Use paste when ready."
        else:
            return prompt, "pyperclip missing. Prompt printed below. Copy it, translate, and use paste."

    def manual_paste(self, text: str) -> str:
        """Parse and ingest JSON pasted by the user."""
        if not text or not text.strip():
            return "Provided payload is empty."

        chapters, char_actions, arc_ends, err = parsing.parse_reply(text)
        if err:
            return f"Parse error: {err}. Please copy the entire JSON object and run paste again."

        book_dir = self.cfg.book_dir
        if not book_dir:
            return "No target book folder selected."

        self._store_chapters("Manual paste", chapters)
        with self.lock:
            was_manual = self.manual_active
            self.manual_active = False
            self.manual_pages = None

        self._apply_character_actions("Manual paste", char_actions)
        self._apply_arc_ends("Manual paste", arc_ends, chapters)

        return "Manual session closed." if was_manual else "Pasted translation processed."

    # ------------------------------------------------------------------
    # Cancellation & recovery
    # ------------------------------------------------------------------

    def cancel(self) -> list[str]:
        """Cancel manual mode, queued and in-flight batches; pages return to the buffer immediately."""
        with self.lock:
            self._cancel_gen += 1
            active = self._cancel_active_locked()
            queued = self._drain_queue_locked()
            manual_pages = self._cancel_manual_locked()
            restored = [p for _, pages in active + queued for p in pages]
            if manual_pages:
                restored.extend(manual_pages)
            self.buffer = restored + self.buffer
            auto_on = self.auto_dispatch
        return self._describe_cancel(active, queued, manual_pages, auto_on)

    def _cancel_active_locked(self) -> list[tuple[int, list[str]]]:
        """Flag in-flight batches as cancelled and return their pages. Late replies get discarded."""
        recovered = []
        for bid in sorted(self._active):
            info = self._active[bid]
            if info.get("cancel"):
                continue
            info["cancel"] = True
            recovered.append((bid, list(info.get("pages", []))))
        return recovered

    def _drain_queue_locked(self) -> list[tuple[int, list[str]]]:
        """Pull batches that are queued but not yet started back out of the work queue."""
        drained = []
        for _ in range(self.work_q.qsize()):
            try:
                bid, pages, _gen = self.work_q.get_nowait()
            except queue.Empty:
                break
            self.work_q.task_done()
            drained.append((bid, list(pages)))
        return drained

    def _cancel_manual_locked(self) -> list[str] | None:
        """End an active manual session and return its pages (None if no session)."""
        if not self.manual_active:
            return None
        pages = list(self.manual_pages or [])
        self.manual_active = False
        self.manual_pages = None
        return pages

    @staticmethod
    def _describe_cancel(active: list, queued: list, manual_pages: list | None, auto_on: bool) -> list[str]:
        messages = []
        if manual_pages is not None:
            messages.append(f"Manual translation aborted. {len(manual_pages)} page(s) returned to buffer.")
        if active:
            ids = ", ".join(f"#{bid}" for bid, _ in active)
            count = sum(len(p) for _, p in active)
            messages.append(
                f"Cancelled in-flight batch(es) {ids}: {count} page(s) returned to buffer. "
                f"Any late reply will be discarded."
            )
        if queued:
            ids = ", ".join(f"#{bid}" for bid, _ in queued)
            count = sum(len(p) for _, p in queued)
            messages.append(f"Pulled queued batch(es) {ids} back: {count} page(s) returned to buffer.")
        if not messages:
            return ["No active manual session, queued, or in-flight batches to cancel."]
        if (active or queued) and auto_on:
            messages.append("Auto-dispatch is ON, so these pages may be re-sent with the next batch. Use 'auto off' to keep them for manual.")
        return messages

    def recover_spooled(self, batch_id: int) -> tuple[bool, str]:
        """Load a spooled batch from _raw/ back into the buffer (skipping pages already buffered)."""
        book_dir = self.cfg.book_dir
        if not book_dir:
            return False, "No book directory selected."
        if batch_id <= 0:
            return False, "Batch id must be a positive integer."
        with self.lock:
            info = self._active.get(batch_id)
            if info is not None and not info.get("cancel"):
                return False, f"Batch #{batch_id} is still in flight. Use cancel to recover it instead."

        pages, err = storage.load_spooled(book_dir, batch_id)
        if err:
            return False, err

        with self.lock:
            new_pages = [p for p in pages if p not in self.buffer]
            self.buffer = new_pages + self.buffer
            total = len(self.buffer)
            auto_on = self.auto_dispatch
        if not new_pages:
            return False, f"All {len(pages)} page(s) from batch #{batch_id} are already in the buffer."

        msg = f"Recovered {len(new_pages)} page(s) from batch #{batch_id} ({total} now buffered). Use manual or flush."
        msg += self._drop_matching_quarantine(batch_id, pages)
        if auto_on:
            msg += " Auto-dispatch is ON; use 'auto off' to keep them for manual."
        return True, msg

    def _drop_matching_quarantine(self, batch_id: int, pages: list[str]) -> str:
        """Remove identical _failed/ records so /retry cannot resend recovered pages."""
        try:
            removed = storage.remove_failed_matching(self.cfg.book_dir, batch_id, pages)
        except OSError as e:
            return f" (Could not check _failed/: {e})"
        if removed:
            return f" Removed {removed} matching _failed/ record(s) so retry won't resend them."
        return ""

    def retry_failed(self) -> str:
        """Load and re-queue all batches quarantined in _failed/."""
        book_dir = self.cfg.book_dir
        if not book_dir:
            return "No book directory selected."
        failed_records = storage.list_failed(book_dir)
        if not failed_records:
            return "No quarantined batches found in _failed/."

        requeued = 0
        with self.lock:
            for rec in failed_records:
                pages = rec.get("pages", [])
                if pages:
                    storage.remove_failed(rec["_filepath"])
                    self._enqueue_locked(pages)
                    requeued += 1
        return f"Re-queued {requeued} failed batch(es) for processing."

    # ------------------------------------------------------------------
    # Workers
    # ------------------------------------------------------------------

    def _worker_loop(self, worker_id: int) -> None:
        while not self.stop_event.is_set():
            try:
                item = self.work_q.get(timeout=0.5)
            except queue.Empty:
                # Auto-retire excess workers if concurrency was lowered
                with self.lock:
                    if worker_id > self._worker_target:
                        return
                continue

            batch_id, pages, gen = item
            try:
                self._process_batch(batch_id, pages, gen)
            except Exception as e:
                con.fail(f"Unexpected worker error on batch #{batch_id}: {e}")
            finally:
                self.work_q.task_done()

    def _process_batch(self, batch_id: int, pages: list[str], gen: int) -> None:
        if not self._register_batch(batch_id, pages, gen):
            return
        try:
            self._run_batch(batch_id, pages)
        except Exception as e:
            self._handle_batch_failure(batch_id, pages, "", f"Unexpected {type(e).__name__}: {e}")
        finally:
            with self.lock:
                self._active.pop(batch_id, None)
                self.counters["batches_done"] += 1

    def _register_batch(self, batch_id: int, pages: list[str], gen: int) -> bool:
        """Mark batch active, unless /cancel ran while it was being dequeued (then recover it)."""
        with self.lock:
            if gen < self._cancel_gen:
                self.buffer = list(pages) + self.buffer
                con.warn(f"Batch #{batch_id} was cancelled before starting — {len(pages)} page(s) returned to buffer.")
                return False
            self._active[batch_id] = {"pages": list(pages), "cancel": False}
            return True

    def _is_cancelled(self, batch_id: int) -> bool:
        with self.lock:
            return bool(self._active.get(batch_id, {}).get("cancel", False))

    def _run_batch(self, batch_id: int, pages: list[str]) -> None:
        cfg = self.cfg
        book_dir = cfg.book_dir
        try:
            storage.spool_pages(book_dir, batch_id, pages)
        except OSError as e:
            con.warn(f"Could not spool batch #{batch_id} to _raw/ ({e}); continuing without backup.")

        pages_text = PAGE_SEPARATOR.join(pages)
        messages = prompts.build_messages(book_dir, pages_text)

        if cfg.dry_run:
            con.rule(f"Dry Run: Batch #{batch_id}")
            con.console.print(f"[dim]Constructed {len(messages)} messages for {cfg.model or '(no model)'}[/dim]")
            return

        con.info(f"Dispatching batch [bold cyan]#{batch_id}[/bold cyan] ({len(pages)} pages) to [bold green]{cfg.model}[/bold green]...")
        try:
            raw_reply, usage = self.client.chat(cfg, messages)
        except ApiError as e:
            self._handle_batch_failure(batch_id, pages, "", f"API error: {e}")
            return

        self._record_usage(usage)
        if cfg.save_raw:
            try:
                storage.save_raw_reply(book_dir, batch_id, raw_reply)
            except OSError as e:
                con.warn(f"Could not save raw reply for batch #{batch_id}: {e}")

        if self._is_cancelled(batch_id):
            con.warn(f"Batch #{batch_id} finished after cancellation — reply discarded (pages are already in the buffer).")
            return

        chapters, char_actions, arc_ends, err = parsing.parse_reply(raw_reply)
        if err:
            self._handle_batch_failure(batch_id, pages, raw_reply, f"JSON error: {err}")
            return
        if not chapters:
            self._handle_batch_failure(batch_id, pages, raw_reply, "Reply contained no chapters")
            return
        self._save_batch_results(batch_id, chapters, char_actions, arc_ends)

    def _record_usage(self, usage) -> None:
        if not isinstance(usage, dict):
            return
        toks = usage.get("total_tokens")
        if isinstance(toks, (int, float)):
            with self.lock:
                self.counters["tokens"] += int(toks)

    def _handle_batch_failure(self, batch_id: int, pages: list[str], reply: str, error: str) -> None:
        """Quarantine a failed batch so it is never silently dropped."""
        if self._is_cancelled(batch_id):
            con.warn(f"Batch #{batch_id} ended after cancellation ({error}). Pages are already in the buffer.")
            return
        with self.lock:
            self.counters["failures"] += 1
        try:
            qpath = storage.quarantine_failure(self.cfg.book_dir, batch_id, pages, reply, error)
        except OSError as qe:
            with self.lock:
                self.buffer = list(pages) + self.buffer
            con.fail(f"Batch #{batch_id} failed ({error}) and could not be quarantined ({qe}). {len(pages)} page(s) returned to buffer.")
            return
        con.fail(f"Batch #{batch_id} failed: {error}")
        con.info(f"Quarantined {len(pages)} page(s) to [cyan]{os.path.basename(qpath)}[/cyan] (use retry, or recover {batch_id} for manual)")

    def _save_batch_results(self, batch_id: int, chapters: list, char_actions: list, arc_ends: list) -> None:
        label = f"Batch #{batch_id}"
        self._store_chapters(label, chapters)
        self._apply_character_actions(label, char_actions)
        self._apply_arc_ends(label, arc_ends, chapters)

    def _store_chapters(self, label: str, chapters: list) -> int:
        """Write chapters to disk, update counters and tracker, and log one summary line."""
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
        return saved

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

        

        

    def stats(self) -> dict:
        with self.lock:
            return {
                "model": self.cfg.model,
                "endpoint": self.cfg.base_url,
                "book": self.cfg.book_name,
                "batch_size": self.cfg.batch_size,
                "concurrency": self.cfg.concurrency,
                "buffer_count": len(self.buffer),
                "queue_depth": self.work_q.qsize(),
                "in_flight": len(self._active),
                "auto_dispatch": self.auto_dispatch,
                "manual_active": self.manual_active,
                "json_mode": self.cfg.json_mode,
                "temperature": self.cfg.temperature,
                "counters": dict(self.counters),
                "tracker": self.tracker.snapshot(),
            }

    def shutdown(self, drain: bool = True, timeout: int = 600) -> None:
        """Gracefully drain in-flight batches and shut down workers."""
        if drain:
            with self.lock:
                q_depth = self.work_q.qsize()
                in_flight = len(self._active)
            if q_depth > 0 or in_flight > 0:
                con.info(f"Draining {q_depth} queued and {in_flight} active batch(es)... (Press Ctrl+C to force exit)")
                start = time.time()
                try:
                    while (self.work_q.qsize() > 0 or len(self._active) > 0) and (time.time() - start < timeout):
                        time.sleep(0.5)
                except KeyboardInterrupt:
                    con.warn("Drain interrupted by user. Forcing immediate shutdown...")

        self.stop_event.set()
        with self.lock:
            if self.buffer and self.cfg.book_dir:
                fpath = storage.flush_buffer_to_raw(self.cfg.book_dir, self.buffer)
                if fpath:
                    con.warn(f"Saved {len(self.buffer)} unsent buffered page(s) to [cyan]{os.path.basename(fpath)}[/cyan]")
                self.buffer = []
