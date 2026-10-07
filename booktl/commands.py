"""Interactive console command handlers. Commands are bare words; a leading '/' is optional."""

import time

from rich.markup import escape
from rich.table import Table

from . import console as con
from . import storage
from .client import ApiClient
from .config import Config, ConfigError
from .library import select_book
from .pipeline import Pipeline, HAS_CLIPBOARD
from .progressbar import render_bars

if HAS_CLIPBOARD:
    import pyperclip

try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import Completer, Completion
    from prompt_toolkit.patch_stdout import patch_stdout
    HAS_PROMPT_TOOLKIT = True
except ImportError:
    HAS_PROMPT_TOOLKIT = False

# The console runs until the user quits; this bound only guards against a runaway loop.
MAX_REPL_ITERATIONS = 10_000_000
ALIASES = {"exit": "quit", "q": "quit", "?": "help", "setpage": "batch"}

HELP_ROWS = (
    ("status", "Display live status, pipeline counters, and current settings."),
    ("model [id]", "Switch model directly, or open the arrow-key model picker."),
    ("endpoint <url>", "Change target API base URL."),
    ("key <token|none>", "Set or clear the API authorization bearer key."),
    ("batch <n>", "Set number of pages per translation batch (alias: setpage)."),
    ("concurrency <n>", "Adjust active parallel worker threads."),
    ("temp <0.0-2.0>", "Adjust model generation temperature."),
    ("maxtokens <n|none>", "Set or clear maximum response tokens."),
    ("jsonmode <auto|on|off>", "Configure JSON formatting parameter handling."),
    ("book", "Switch target book (arrow-key picker)."),
    ("goal <n|+n|none>", "Set or clear the stop chapter (e.g. goal 3000 or goal +50). Progress counts from this session's first scraped chapter."),
    ("progress", "Display current scraping and translation progress bars."),
    ("buffer", "List buffered pages with their chapter numbers."),
    ("flush", "Immediately dispatch all currently buffered pages."),
    ("auto <on|off>", "Toggle auto-dispatching (turn off to accumulate pages for manual)."),
    ("manual", "Copy buffered pages to clipboard for external translation."),
    ("paste", "Ingest translation JSON response from clipboard."),
    ("cancel", "Abort manual mode, queued and in-flight batches; pages return to buffer immediately."),
    ("retry", "Re-process quarantined failure batches from _failed/."),
    ("recover [batch_id|ch]", "List spooled batches in _raw/, or load one back by batch ID or chapter."),
    ("raw <on|off>", "Toggle saving raw model responses to _raw/."),
    ("clear", "Clear terminal screen."),
    ("help", "Show this list (alias: ?)."),
    ("quit", "Gracefully shut down workers and exit (aliases: exit, q)."),
)


class ReplContext:
    def __init__(self, cfg: Config, pipeline: Pipeline, client: ApiClient):
        self.cfg = cfg
        self.pipeline = pipeline
        self.client = client


# ----------------------------------------------------------------------
# Display helpers
# ----------------------------------------------------------------------

def print_help() -> None:
    table = Table(title="Available Commands", border_style="blue")
    table.add_column("Command", style="cyan", no_wrap=True)
    table.add_column("Description", style="white")
    for usage, description in HELP_ROWS:
        table.add_row(escape(usage), escape(description))
    con.console.print(table)
    con.dim("A leading '/' is optional, so '/goal 30' still works. Press Tab to complete command names.")


def print_spooled(ctx: ReplContext, limit: int = 15) -> None:
    """Show the most recent spooled batches in _raw/."""
    book_dir = ctx.cfg.book_dir
    if not book_dir:
        con.warn("No book directory selected.")
        return
    try:
        records = storage.list_spooled(book_dir)
    except OSError as e:
        con.fail(f"Could not read _raw/: {e}")
        return
    if not records:
        con.info("No spooled batches found in _raw/.")
        return

    shown = records[-limit:]
    table = Table(title=f"Spooled Batches (latest {len(shown)} of {len(records)})", border_style="magenta")
    table.add_column("Batch", style="cyan", justify="right")
    table.add_column("Chapters", style="bold green")
    table.add_column("Pages", justify="right")
    table.add_column("Spooled At", style="dim")
    for rec in shown:
        ts = rec.get("timestamp")
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if isinstance(ts, (int, float)) else "?"
        pages = str(rec["pages"]) if rec.get("pages") is not None else "[red]unreadable[/red]"
        ch_str = rec.get("chapters") or "[dim]?[/dim]"
        table.add_row(f"#{rec['batch_id']}", ch_str, pages, when)
    con.console.print(table)
    con.info("Load one into the buffer with [bold cyan]recover <batch_id>[/bold cyan] (e.g. recover 8 or recover ch71).")


def _current_chapter_text(tr: dict) -> str:
    curr = tr.get("current_chapter")
    if curr is None:
        return "(None yet)"
    return f"Chapter {'~' if tr.get('is_inferred') else ''}{curr}"


def _goal_status_text(tr: dict) -> str:
    """Rich-markup description of the goal, matching the progress bars."""
    state = tr.get("goal_state", "none")
    goal = tr.get("goal_chapter")
    if state == "pending":
        return f"[bold yellow]Next {tr.get('pending_goal_offset')} chapters (waiting for first chapter)[/bold yellow]"
    if state == "waiting":
        return f"[bold yellow]Chapter {goal} (waiting for first chapter)[/bold yellow]"
    if state == "behind":
        return f"[bold red]Chapter {goal} (before session start, goal passed)[/bold red]"
    if state != "active":
        return "(None set)"
    sc_done, total, _ = tr.get("scrape_progress", (0, None, None))
    tr_done = tr.get("translate_progress", (0, None, None))[0]
    if not total:
        return f"[bold green]Chapter {goal}[/bold green]"
    return (
        f"[bold green]Chapter {goal}[/bold green] "
        f"({sc_done}/{total} scraped, {tr_done}/{total} translated, {total - sc_done} left to scrape)"
    )


def _print_bars(tr: dict) -> None:
    con.console.print("\n" + render_bars(tr) + "\n", markup=False, highlight=False)


def print_status(ctx: ReplContext) -> None:
    s = ctx.pipeline.stats()
    c = s["counters"]
    tr = s.get("tracker", {})

    table = Table(title="Pipeline Status", border_style="cyan")
    table.add_column("Setting / Metric", style="bold white")
    table.add_column("Value", style="green")

    start = tr.get("session_start")
    table.add_row("Target Book", escape(s["book"]) if s["book"] else "(None)")
    table.add_row("Current Chapter", _current_chapter_text(tr))
    table.add_row("Session Start", f"Chapter {start}" if start is not None else "(waiting for first chapter)")
    table.add_row("Stop Goal", _goal_status_text(tr))
    table.add_row("Endpoint", escape(s["endpoint"]))
    table.add_row("Model", escape(s["model"]) if s["model"] else "(None)")
    table.add_row("Batch Size", str(s["batch_size"]))
    table.add_row("Concurrency", str(s["concurrency"]))
    table.add_row("Temperature", str(s["temperature"]))
    table.add_row("JSON Mode", s["json_mode"])
    table.add_row("Auto Dispatch", "[green]ON[/green]" if s["auto_dispatch"] else "[yellow]PAUSED[/yellow]")
    table.add_row("Manual Active", "[yellow]YES[/yellow]" if s["manual_active"] else "NO")
    table.add_row("Buffered Pages", f"{s['buffer_count']}/{s['batch_size']}")
    table.add_row("Queue Depth", str(s["queue_depth"]))
    table.add_row("In-Flight Batches", str(s["in_flight"]))
    table.add_row("Chapters Saved", f"[bold green]{c['chapters_saved']}[/bold green]")
    table.add_row("Pages Received", str(c["pages_seen"]))
    table.add_row("Failed Batches", f"[bold red]{c['failures']}[/bold red]" if c["failures"] else "0")
    table.add_row("Total Tokens Reported", f"{c['tokens']:,}")

    con.console.print(table)
    _print_bars(tr)


# ----------------------------------------------------------------------
# Command handlers: each takes (ctx, args)
# ----------------------------------------------------------------------

def cmd_help(ctx: ReplContext, args: list[str]) -> None:
    print_help()


def cmd_clear(ctx: ReplContext, args: list[str]) -> None:
    con.console.clear()


def cmd_status(ctx: ReplContext, args: list[str]) -> None:
    print_status(ctx)


def cmd_goal(ctx: ReplContext, args: list[str]) -> None:
    if args:
        con.ok(ctx.pipeline.tracker.set_goal(args[0]))
        return
    tr = ctx.pipeline.tracker.snapshot()
    con.info(f"Stop goal: {_goal_status_text(tr)}")
    if tr.get("goal_state") == "none":
        con.info("Usage: goal <chapter_number>, goal +<offset>, or goal none")


def cmd_progress(ctx: ReplContext, args: list[str]) -> None:
    _print_bars(ctx.pipeline.tracker.snapshot())


def cmd_model(ctx: ReplContext, args: list[str]) -> None:
    if not args:
        ctx.client.select_model(ctx.cfg)
        return
    new_model = " ".join(args).strip()
    try:
        ctx.cfg.model = new_model
    except ConfigError as e:
        con.fail(str(e))
        return
    con.ok(f"Active model set to [bold cyan]{escape(new_model)}[/bold cyan]")


def cmd_endpoint(ctx: ReplContext, args: list[str]) -> None:
    if not args:
        con.warn("Usage: endpoint <http://host:port/v1>")
        return
    try:
        ctx.cfg.base_url = args[0]
    except ConfigError as e:
        con.fail(str(e))
        return
    con.ok(f"Endpoint updated to [bold cyan]{escape(ctx.cfg.base_url)}[/bold cyan]")
    con.info("Re-querying model list for new endpoint...")
    ctx.client.select_model(ctx.cfg)


def cmd_key(ctx: ReplContext, args: list[str]) -> None:
    if not args:
        con.warn("Usage: key <token|none>")
        return
    ctx.cfg.api_key = args[0]
    con.ok("API key updated." if ctx.cfg.api_key else "API key cleared.")


def _set_setting(ctx: ReplContext, args: list[str], attr: str, label: str, usage: str) -> None:
    """Shared handler for simple Config properties validated by their setters."""
    if not args:
        con.warn(f"Current {label} is {getattr(ctx.cfg, attr)}. Usage: {usage}")
        return
    try:
        setattr(ctx.cfg, attr, args[0])
    except ConfigError as e:
        con.fail(str(e))
        return
    con.ok(f"{label[0].upper()}{label[1:]} set to [bold green]{getattr(ctx.cfg, attr)}[/bold green]")


def cmd_batch(ctx: ReplContext, args: list[str]) -> None:
    _set_setting(ctx, args, "batch_size", "batch size", "batch <number>")


def cmd_temp(ctx: ReplContext, args: list[str]) -> None:
    _set_setting(ctx, args, "temperature", "temperature", "temp <0.0-2.0>")


def cmd_maxtokens(ctx: ReplContext, args: list[str]) -> None:
    _set_setting(ctx, args, "max_tokens", "max tokens", "maxtokens <number|none>")


def cmd_jsonmode(ctx: ReplContext, args: list[str]) -> None:
    _set_setting(ctx, args, "json_mode", "JSON mode", "jsonmode <auto|on|off>")


def cmd_concurrency(ctx: ReplContext, args: list[str]) -> None:
    if not args:
        con.warn(f"Current concurrency is {ctx.cfg.concurrency}. Usage: concurrency <number>")
        return
    try:
        num = int(args[0])
    except ValueError:
        con.fail("Concurrency must be an integer.")
        return
    try:
        ctx.pipeline.set_concurrency(num)
    except ConfigError as e:
        con.fail(str(e))
        return
    con.ok(f"Concurrency adjusted to [bold green]{num}[/bold green] workers.")


def cmd_book(ctx: ReplContext, args: list[str]) -> None:
    res = select_book(ctx.cfg.library_dir)
    if not res:
        con.info("Book unchanged.")
        return
    bpath, bname = res
    ctx.cfg.set_book(bpath, bname)
    ctx.pipeline.tracker.reset()
    ctx.pipeline.sync_batch_seq()
    con.ok(f"Switched target novel to [bold green]{escape(bname)}[/bold green]")


def cmd_buffer(ctx: ReplContext, args: list[str]) -> None:
    with ctx.pipeline.lock:
        pages = list(ctx.pipeline.buffer)
    con.info(f"Buffer holds {len(pages)} page(s) (target: {ctx.cfg.batch_size}):")
    for i, page in enumerate(pages, start=1):
        label = ctx.pipeline.page_label(page)
        con.console.print(f"  Page {i}: [bold cyan]{label}[/bold cyan] ({len(page):,} characters)")


def cmd_flush(ctx: ReplContext, args: list[str]) -> None:
    con.ok(ctx.pipeline.flush())


def cmd_auto(ctx: ReplContext, args: list[str]) -> None:
    if not args or args[0].lower() not in ("on", "off"):
        con.warn("Usage: auto <on|off>")
        return
    enabled = args[0].lower() == "on"
    ctx.pipeline.auto_dispatch = enabled
    if enabled:
        con.ok("Auto-dispatch enabled. Batches will be sent to the LLM automatically.")
    else:
        con.warn("Auto-dispatch paused. Pages will accumulate in the buffer for manual or flush.")


def cmd_manual(ctx: ReplContext, args: list[str]) -> None:
    prompt, msg = ctx.pipeline.manual_begin()
    con.ok(msg)
    if prompt and not HAS_CLIPBOARD:
        con.rule("Manual Prompt")
        con.console.print(prompt, markup=False)


def cmd_paste(ctx: ReplContext, args: list[str]) -> None:
    if not HAS_CLIPBOARD:
        con.fail("pyperclip not available. Please install it to use paste from clipboard.")
        return
    try:
        clip_text = pyperclip.paste()
    except Exception as e:  # pyperclip raises platform-specific exception types
        con.fail(f"Could not read clipboard: {escape(str(e))}")
        return
    con.ok(ctx.pipeline.manual_paste(clip_text))


def cmd_cancel(ctx: ReplContext, args: list[str]) -> None:
    for msg in ctx.pipeline.cancel():
        con.warn(msg)


def cmd_retry(ctx: ReplContext, args: list[str]) -> None:
    con.ok(ctx.pipeline.retry_failed())


def _find_batch_by_chapter(records: list[dict], target_ch: int) -> dict | None:
    """Find the latest spooled batch containing target_ch."""
    matches = [r for r in records if target_ch in r.get("chapter_numbers", [])]
    return matches[-1] if matches else None


def cmd_recover(ctx: ReplContext, args: list[str]) -> None:
    if not args:
        print_spooled(ctx)
        return
    book_dir = ctx.cfg.book_dir
    if not book_dir:
        con.warn("No book directory selected.")
        return

    query = " ".join(args).strip()
    records = storage.list_spooled(book_dir)

    target_batch_id = None
    # Check if user typed a chapter prefix, e.g. 'ch71', 'ch 71', 'chapter 75'
    ch_match = None
    import re
    m = re.search(r'(?:ch(?:apter)?\.?\s*)(\d+)', query, re.IGNORECASE)
    if m:
        ch_match = int(m.group(1))

    if ch_match is not None:
        rec = _find_batch_by_chapter(records, ch_match)
        if not rec:
            con.warn(f"No spooled batch found containing Chapter {ch_match}.")
            return
        target_batch_id = rec["batch_id"]
        con.info(f"Located Chapter {ch_match} in Batch [bold cyan]#{target_batch_id}[/bold cyan] ({rec.get('chapters', '')}).")
    else:
        # Check if query is an integer
        clean_query = query.lstrip("#").strip()
        if clean_query.isdigit():
            val = int(clean_query)
            # If a batch with this batch_id exists, use it
            if any(r["batch_id"] == val for r in records):
                target_batch_id = val
            else:
                # Otherwise see if this integer corresponds to a chapter number in spooled batches
                rec = _find_batch_by_chapter(records, val)
                if rec:
                    target_batch_id = rec["batch_id"]
                    con.info(f"Batch #{val} not found, but located Chapter {val} in Batch [bold cyan]#{target_batch_id}[/bold cyan] ({rec.get('chapters', '')}).")
                else:
                    # Fall back to treating it as batch_id
                    target_batch_id = val
        else:
            con.warn("Usage: recover <batch_id|chapter>  (e.g. recover 8 or recover ch71)")
            return

    ok, msg = ctx.pipeline.recover_spooled(target_batch_id)
    if ok:
        con.ok(msg)
    else:
        con.warn(msg)


def cmd_raw(ctx: ReplContext, args: list[str]) -> None:
    if not args or args[0].lower() not in ("on", "off"):
        con.warn(f"Saving raw replies is currently {'on' if ctx.cfg.save_raw else 'off'}. Usage: raw <on|off>")
        return
    ctx.cfg.save_raw = args[0].lower() == "on"
    con.ok(f"Raw response storage set to {'on' if ctx.cfg.save_raw else 'off'}.")


HANDLERS = {
    "help": cmd_help,
    "clear": cmd_clear,
    "status": cmd_status,
    "goal": cmd_goal,
    "progress": cmd_progress,
    "model": cmd_model,
    "endpoint": cmd_endpoint,
    "key": cmd_key,
    "batch": cmd_batch,
    "concurrency": cmd_concurrency,
    "temp": cmd_temp,
    "maxtokens": cmd_maxtokens,
    "jsonmode": cmd_jsonmode,
    "book": cmd_book,
    "buffer": cmd_buffer,
    "flush": cmd_flush,
    "auto": cmd_auto,
    "manual": cmd_manual,
    "paste": cmd_paste,
    "cancel": cmd_cancel,
    "retry": cmd_retry,
    "recover": cmd_recover,
    "raw": cmd_raw,
}
COMMAND_WORDS = tuple(sorted(list(HANDLERS) + ["quit"]))


def _normalise_command(token: str) -> str:
    """Lower-case, strip an optional leading '/', and resolve aliases."""
    name = token.strip().lstrip("/").lower()
    return ALIASES.get(name, name)


def handle_command(ctx: ReplContext, line: str) -> bool:
    """Execute one console line. Returns False to terminate the console."""
    parts = line.strip().split()
    if not parts:
        return True
    cmd = _normalise_command(parts[0])
    if not cmd:
        return True
    if cmd == "quit":
        return False
    handler = HANDLERS.get(cmd)
    if handler is None:
        con.warn(f"Unknown command '{escape(cmd)}'. Type [bold cyan]help[/bold cyan] for available commands.")
        return True
    try:
        handler(ctx, parts[1:])
    except Exception as e:  # keep the console alive if a handler hits an unexpected error
        con.fail(f"Command '{cmd}' failed: {escape(str(e))}")
    return True


# ----------------------------------------------------------------------
# Console loop
# ----------------------------------------------------------------------

if HAS_PROMPT_TOOLKIT:
    class CommandCompleter(Completer):
        """Tab-completes command names in the first word only."""

        def __init__(self, words):
            self._words = tuple(words)

        def get_completions(self, document, complete_event):
            text = document.text_before_cursor.lstrip()
            if " " in text:
                return
            word = text.lstrip("/").lower()
            for name in self._words:
                if name.startswith(word):
                    yield Completion(name, start_position=-len(word))


def _make_prompt_reader(ctx: ReplContext):
    session = PromptSession(completer=CommandCompleter(COMMAND_WORDS), complete_while_typing=False)

    def get_prompt_message():
        bars = render_bars(ctx.pipeline.tracker.snapshot())
        return [
            ("class:bars", f"\n{bars}\n"),
            ("class:prompt", "booktl> "),
        ]

    def read_line() -> str | None:
        try:
            with patch_stdout(raw=True):
                return session.prompt(get_prompt_message, refresh_interval=0.5).strip()
        except (KeyboardInterrupt, EOFError):
            return None

    return read_line


def _read_plain_line() -> str | None:
    try:
        return con.console.input("[bold cyan]booktl> [/bold cyan]").strip()
    except (KeyboardInterrupt, EOFError):
        return None


def run_repl(ctx: ReplContext) -> None:
    con.rule("Translation Console Active")
    con.info("Type [bold cyan]help[/bold cyan] for commands, [bold cyan]quit[/bold cyan] to exit. Tab completes command names.\n")
    read_line = _make_prompt_reader(ctx) if HAS_PROMPT_TOOLKIT else _read_plain_line
    for _ in range(MAX_REPL_ITERATIONS):
        line = read_line()
        if line is None:
            break
        if line and not handle_command(ctx, line):
            break
