"""Translation pipeline and worker thread management."""

import os
import queue
import random
import threading
import time

from . import console as con
from . import prompts, parsing, storage, characters
from .client import ApiClient, ApiError

try:
    import pyperclip
    HAS_CLIPBOARD = True
except ImportError:
    HAS_CLIPBOARD = False


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

        self.auto_dispatch = True
        self.manual_active = False
        self.manual_pages = None

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
        with self.lock:
            self.stop_event.clear()
            for i in range(self.cfg.concurrency):
                self._spawn_worker(i + 1)

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

    def offer_page(self, content: str, is_active: bool) -> dict:
        """Ingest page from Tampermonkey and return scraper directive."""
        with self.lock:
            self.counters["pages_seen"] += 1
            self.buffer.append(content)
            count = len(self.buffer)
            needed = self.cfg.batch_size
            con.info(f"Buffered page [bold]{count}/{needed}[/bold] ({len(content)} chars)")

            if count < needed:
                directive = None
            else:
                directive = self._try_dispatch_locked()

        if directive is not None and directive.get("action") == "wait":
            return directive

        # Pacing delay to avoid hammering the source novel website
        delay = 1.0 if is_active else random.uniform(1.0, 3.5)
        time.sleep(delay)
        return directive if directive else {"action": "next", "buffered": count, "needed": needed}

    def poll_status(self, is_active: bool) -> dict:
        """Handle polling requests from scraper during wait states."""
        with self.lock:
            if len(self.buffer) < self.cfg.batch_size:
                return {"action": "next", "buffered": len(self.buffer)}
            return self._try_dispatch_locked()

    def _try_dispatch_locked(self) -> dict:
        if not self.cfg.book_dir:
            return {"action": "wait", "retryAfter": 5, "reason": "No book directory selected"}
        if not self.auto_dispatch:
            return {"action": "wait", "retryAfter": 5, "reason": "Auto-dispatch paused for manual mode"}

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
        self.work_q.put((bid, pages))
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
            pages_text = "\n\n--- PAGE BREAK ---\n\n".join(pages)
            prompt = prompts.build_manual_prompt(self.cfg.book_dir, pages_text)

        if HAS_CLIPBOARD:
            try:
                pyperclip.copy(prompt)
                return prompt, f"Copied prompt for {len(pages)} page(s) to clipboard. Paste reply with /paste."
            except Exception as e:
                return prompt, f"Clipboard write failed ({e}). Prompt shown below. Use /paste when ready."
        else:
            return prompt, "pyperclip missing. Prompt printed below. Copy it, translate, and use /paste."

    def manual_paste(self, text: str) -> str:
        """Parse and ingest JSON pasted by the user."""
        if not text or not text.strip():
            return "Provided payload is empty."

        chapters, char_actions, err = parsing.parse_response(text)
        if err:
            return f"Parse error: {err}. Please copy the entire JSON object and run /paste again."

        book_dir = self.cfg.book_dir
        if not book_dir:
            return "No target book folder selected."

        saved, skipped, files = storage.save_chapters(chapters, book_dir)
        with self.lock:
            self.counters["chapters_saved"] += saved
            self.counters["skipped"] += skipped
            if self.manual_active:
                self.manual_active = False
                self.manual_pages = None

        action_msgs = []
        if char_actions:
            action_msgs = characters.process_character_actions(book_dir, char_actions)
            for msg in action_msgs:
                con.console.print(f"  {msg}")

        res = f"Saved {saved} chapter(s) to '{self.cfg.book_name}'."
        if skipped:
            res += f" ({skipped} skipped)"
        return res

    def cancel(self) -> list[str]:
        """Cancel manual mode or in-flight translation and recover pages to buffer."""
        messages = []
        with self.lock:
            if self.manual_active:
                pages = self.manual_pages or []
                self.buffer = pages + self.buffer
                self.manual_active = False
                self.manual_pages = None
                messages.append(f"Manual translation aborted. {len(pages)} page(s) returned to buffer.")

            cancelled_batches = 0
            for bid, info in self._active.items():
                if not info.get("cancel"):
                    info["cancel"] = True
                    cancelled_batches += 1

            if cancelled_batches > 0:
                messages.append(f"Flagged {cancelled_batches} in-flight batch(es) for cancellation. Pages will revert to buffer.")

            if not messages:
                messages.append("No active manual session or in-flight batches to cancel.")

        return messages

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

            batch_id, pages = item
            try:
                self._process_batch(batch_id, pages)
            except Exception as e:
                con.fail(f"Unexpected worker error on batch #{batch_id}: {e}")
            finally:
                self.work_q.task_done()

    def _process_batch(self, batch_id: int, pages: list[str]) -> None:
        cfg = self.cfg
        book_dir = cfg.book_dir

        with self.lock:
            self._active[batch_id] = {"pages": list(pages), "cancel": False}

        try:
            storage.spool_pages(book_dir, batch_id, pages)
            pages_text = "\n\n--- PAGE BREAK ---\n\n".join(pages)
            messages = prompts.build_messages(book_dir, pages_text)

            if cfg.dry_run:
                con.rule(f"Dry Run: Batch #{batch_id}")
                con.console.print(f"[dim]Constructed {len(messages)} messages for {cfg.model or '(no model)'}[/dim]")
                return

            con.info(f"Dispatching batch [bold cyan]#{batch_id}[/bold cyan] ({len(pages)} pages) to [bold green]{cfg.model}[/bold green]...")
            raw_reply, usage = self.client.chat(cfg, messages)

            if usage and isinstance(usage, dict):
                toks = usage.get("total_tokens", 0)
                with self.lock:
                    self.counters["tokens"] += toks

            if cfg.save_raw:
                storage.save_raw_reply(book_dir, batch_id, raw_reply)

            with self.lock:
                was_cancelled = self._active.get(batch_id, {}).get("cancel", False)

            if was_cancelled:
                with self.lock:
                    self.buffer = pages + self.buffer
                con.warn(f"Batch #{batch_id} cancelled by user — {len(pages)} pages recovered to buffer.")
                return

            chapters, char_actions, err = parsing.parse_response(raw_reply)
            if err:
                with self.lock:
                    self.counters["failures"] += 1
                qpath = storage.quarantine_failure(book_dir, batch_id, pages, raw_reply, err)
                con.fail(f"Batch #{batch_id} JSON error: {err}")
                con.info(f"Quarantined failed payload to [cyan]{os.path.basename(qpath)}[/cyan] (use /retry later)")
                return

            saved, skipped, filenames = storage.save_chapters(chapters, book_dir)
            with self.lock:
                self.counters["chapters_saved"] += saved
                self.counters["skipped"] += skipped

            con.ok(f"Batch #{batch_id} complete: saved [bold green]{saved}[/bold green] chapter(s) to '{cfg.book_name}'")
            for fn in filenames:
                con.dim(f"  ↳ wrote {fn}")

            if char_actions:
                action_results = characters.process_character_actions(book_dir, char_actions)
                for res in action_results:
                    con.console.print(f"  {res}")

        finally:
            with self.lock:
                self._active.pop(batch_id, None)
                self.counters["batches_done"] += 1

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
            }

    def shutdown(self) -> None:
        self.stop_event.set()
        with self.lock:
            if self.buffer and self.cfg.book_dir:
                fpath = storage.flush_buffer_to_raw(self.cfg.book_dir, self.buffer)
                if fpath:
                    con.warn(f"Saved {len(self.buffer)} unsent buffered page(s) to [cyan]{os.path.basename(fpath)}[/cyan]")
                self.buffer = []
