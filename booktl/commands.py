"""Interactive console command handlers. Commands are bare words; a leading '/' is optional."""

import re
import time

from rich.markup import escape
from rich.table import Table

from . import console as con
from . import glossary
from . import storage
from .client import ApiClient
from .config import Config, ConfigError
from .library import create_book, select_book
from .parsing import format_chapter_ranges
from .pipeline import Pipeline, HAS_CLIPBOARD, describe_entries
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
MAX_PROBLEM_ROWS = 200
MAX_TERM_ROWS = 300
TERM_USAGE = (
    "Usage: term add <chinese> = <english> [type:<type>] · "
    "term alias <chinese> = <term> · term rm <chinese>"
)
ALIASES = {
    "exit": "quit", "q": "quit", "?": "help", "setpage": "batch",
    "buffer": "pending", "queue": "pending", "retranslate": "retry", "rt": "retry",
    "glossary": "terms",
}
_BATCH_REF_RE = re.compile(r"^(?:#|b(?:atch)?\s*#?)\s*(\d+)$", re.IGNORECASE)

HELP_ROWS = (
    ("status", "Display live status, pipeline counters, and current settings."),
    ("model [id]", "Switch model directly, or open the arrow-key model picker."),
    ("endpoint <url>", "Change target API base URL."),
    ("key <token|none>", "Set or clear the API authorization bearer key."),
    ("batch <n>", "Set pages per translation batch (alias: setpage). Applies to the very next batch sent."),
    ("concurrency <n>", "Adjust active parallel worker threads."),
    ("temp <0.0-2.0>", "Adjust model generation temperature."),
    ("maxtokens <n|none>", "Set or clear maximum response tokens."),
    ("jsonmode <auto|on|off>", "Configure JSON formatting parameter handling."),
    ("book", "Switch target book. Recently translated books are listed first; the last row creates a new book."),
    ("book new <name>", "Create a new book folder in the library and switch to it."),
    ("goal <n|+n|none>", "Set or clear the stop chapter. With a goal the scraper runs until the goal; without one it waits at 4 x batch size pending pages."),
    ("progress", "Display current scraping and translation progress bars."),
    ("pending [all]", "List pending chapters and in-flight batches; 'all' lists every page (aliases: buffer, queue)."),
    ("missing", "List chapters left out of replies and chapters from failed batches. They are never re-queued automatically."),
    ("retry [chapters]", "Re-queue and send chapters now: retry 112-115, retry 120 130, retry missing, retry failed, retry all. No argument re-queues all failed chapters (aliases: retranslate, rt)."),
    ("recover [#batch|chapters]", "No argument lists spooled batches. 'recover #8' loads batch 8; 'recover 112-115' loads chapters to the front of the pool without forcing a send."),
    ("terms [search]", "Show the glossary of locked terms (titles, skills, debuffs, items, places...), optionally filtered (alias: glossary)."),
    ("term add <chinese> = <english> [type:<type>]", "Add or override a term and lock it as yours; the model never changes your terms."),
    ("term alias <chinese> = <term>", "Make another Chinese spelling render like an existing term (named by its Chinese or English)."),
    ("term rm <chinese>", "Remove a term together with its aliases, or remove a single alias."),
    ("flush", "Send all pending pages now, including a final partial batch."),
    ("auto <on|off>", "Toggle auto-dispatching (turn off to keep pages pending for manual)."),
    ("manual", "Copy the next batch of pending pages to the clipboard for external translation."),
    ("paste", "Ingest translation JSON response from clipboard."),
    ("cancel", "Abort manual mode and in-flight batches; their pages return to the front of the pending pool."),
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

_REPORT_PRINTERS = {"ok": con.ok, "info": con.info, "warn": con.warn, "fail": con.fail}


def print_report(report) -> None:
    """Print (level, message) pairs returned by pipeline operations."""
    for level, message in report or []:
        _REPORT_PRINTERS.get(level, con.info)(message)


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
        con.fail(f"Could not read _raw/: {escape(str(e))}")
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
    con.info(
        "Load a batch with [bold cyan]recover #<batch_id>[/bold cyan] (e.g. recover #8), "
        "or chapters with [bold cyan]recover 112-115[/bold cyan]."
    )


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


def _print_bars(tr: dict, queue: dict | None = None) -> None:
    con.console.print("\n" + render_bars(tr, queue=queue) + "\n", markup=False, highlight=False)


def _pending_text(s: dict) -> str:
    limit = s.get("backlog_limit")
    cap = f" (scraper waits at {limit})" if limit else " (no cap while a goal is set)"
    drain = " · [yellow]drain mode[/yellow]" if s.get("drain") else ""
    return f"{s['pending']}{cap}{drain}"


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
    table.add_row("Manual Active", f"[yellow]YES ({s['manual']} pages)[/yellow]" if s["manual_active"] else "NO")
    table.add_row("Pending Pages", _pending_text(s))
    table.add_row("In-Flight", f"{s['in_flight_batches']} batch(es), {s['in_flight']} page(s)")
    table.add_row("Missing Chapters", f"[bold yellow]{s['missing']}[/bold yellow]" if s["missing"] else "0")
    table.add_row("Failed Chapters", f"[bold red]{s['failed']}[/bold red]" if s["failed"] else "0")
    table.add_row("Chapters Saved", f"[bold green]{c['chapters_saved']}[/bold green]")
    table.add_row("Pages Received", str(c["pages_seen"]))
    table.add_row("Failed Batches", f"[bold red]{c['failures']}[/bold red]" if c["failures"] else "0")
    table.add_row("Total Tokens Reported", f"{c['tokens']:,}")

    con.console.print(table)
    _print_bars(tr, ctx.pipeline.queue_counts())


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
        ctx.pipeline.wake_workers()
        return
    tr = ctx.pipeline.tracker.snapshot()
    con.info(f"Stop goal: {_goal_status_text(tr)}")
    if tr.get("goal_state") == "none":
        con.info("Usage: goal <chapter_number>, goal +<offset>, or goal none")


def cmd_progress(ctx: ReplContext, args: list[str]) -> None:
    _print_bars(ctx.pipeline.tracker.snapshot(), ctx.pipeline.queue_counts())


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


def _set_setting(ctx: ReplContext, args: list[str], attr: str, label: str, usage: str) -> bool:
    """Shared handler for simple Config properties validated by their setters. Returns True on change."""
    if not args:
        con.warn(f"Current {label} is {getattr(ctx.cfg, attr)}. Usage: {usage}")
        return False
    try:
        setattr(ctx.cfg, attr, args[0])
    except ConfigError as e:
        con.fail(str(e))
        return False
    con.ok(f"{label[0].upper()}{label[1:]} set to [bold green]{getattr(ctx.cfg, attr)}[/bold green]")
    return True


def cmd_batch(ctx: ReplContext, args: list[str]) -> None:
    if _set_setting(ctx, args, "batch_size", "batch size", "batch <number>"):
        ctx.pipeline.wake_workers()
        con.dim("Applies to the next batch a worker sends; batches already in flight are unchanged.")


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


def _switch_book(ctx: ReplContext, bpath: str, bname: str) -> None:
    ctx.cfg.set_book(bpath, bname)
    ctx.pipeline.on_book_changed()
    con.ok(f"Switched target novel to [bold green]{escape(bname)}[/bold green]")
    pending = ctx.pipeline.queue_counts()["pending"]
    if pending:
        con.warn(f"{pending} page(s) scraped for the previous book are still pending and will be translated into this book.")


def _book_new(ctx: ReplContext, args: list[str]) -> None:
    name = " ".join(args).strip()
    if not name:
        con.warn("Usage: book new <name>")
        return
    try:
        bpath, bname = create_book(ctx.cfg.library_dir, name)
    except ValueError as e:
        con.warn(escape(str(e)))
        return
    except OSError as e:
        con.fail(f"Could not create '{escape(name)}': {escape(str(e))}")
        return
    con.ok(f"Created new book [bold green]{escape(bname)}[/bold green]")
    _switch_book(ctx, bpath, bname)


def cmd_book(ctx: ReplContext, args: list[str]) -> None:
    """book -> picker (with a new-book row); book new <name> -> create and switch."""
    if args and args[0].lower() == "new":
        _book_new(ctx, args[1:])
        return
    res = select_book(ctx.cfg.library_dir)
    if not res:
        con.info("Book unchanged.")
        return
    _switch_book(ctx, *res)


def _print_page_list(entries) -> None:
    for i, entry in enumerate(entries, start=1):
        con.console.print(
            f"  {i:>4}. [bold cyan]{escape(entry.label)}[/bold cyan] "
            f"[dim]({escape(entry.key)}, {len(entry.content):,} characters)[/dim]"
        )


def cmd_pending(ctx: ReplContext, args: list[str]) -> None:
    view = ctx.pipeline.pending_view()
    pending = view["pending"]
    limit = view["backlog_limit"]
    cap = f"scraper waits at {limit}" if limit else "no cap while a goal is set"
    drain = " · [yellow]drain mode[/yellow]" if view["drain"] else ""
    con.info(f"{len(pending)} page(s) pending (batch size {view['batch_size']}, {cap}){drain}")
    if pending:
        con.console.print(f"  Pending: [bold cyan]{escape(describe_entries(pending))}[/bold cyan]")
        if args and args[0].lower() == "all":
            _print_page_list(pending)
    for bid, entries, cancelled in view["in_flight"]:
        flag = " [yellow](cancelled)[/yellow]" if cancelled else ""
        con.console.print(f"  In flight #{bid}: {escape(describe_entries(entries))} ({len(entries)} pages){flag}")
    if view["manual"]:
        con.console.print(f"  Manual session: {escape(describe_entries(view['manual']))} ({len(view['manual'])} pages)")


def _problem_sort_key(item) -> tuple:
    key, info = item
    chapter = info.get("chapter")
    return (0, chapter, "") if isinstance(chapter, int) else (1, 0, key)


def _batch_text(batch) -> str:
    if isinstance(batch, int):
        return f"#{batch}"
    return str(batch) if batch else "?"


def _when_text(stamp) -> str:
    if not isinstance(stamp, (int, float)):
        return "?"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(stamp))


def _problem_rows(missing: dict, failed: dict) -> list[tuple]:
    rows = []
    for status, records, style in (("Missing", missing, "yellow"), ("Failed", failed, "red")):
        for key, info in sorted(records.items(), key=_problem_sort_key):
            rows.append((
                f"[{style}]{status}[/{style}]",
                escape(str(info.get("label") or key)),
                _batch_text(info.get("batch")),
                escape(str(info.get("reason") or "")),
                _when_text(info.get("time")),
            ))
    return rows


def _chapter_numbers(records: dict) -> list[int]:
    return [info["chapter"] for info in records.values() if isinstance(info.get("chapter"), int)]


def cmd_missing(ctx: ReplContext, args: list[str]) -> None:
    missing, failed = ctx.pipeline.problems()
    if not missing and not failed:
        con.ok("No missing or failed chapters.")
        return
    rows = _problem_rows(missing, failed)
    table = Table(title="Missing & Failed Chapters", border_style="red")
    for column in ("Status", "Chapter", "Batch", "Reason", "Recorded"):
        table.add_column(column)
    for row in rows[:MAX_PROBLEM_ROWS]:
        table.add_row(*row)
    con.console.print(table)
    if len(rows) > MAX_PROBLEM_ROWS:
        con.dim(f"Showing {MAX_PROBLEM_ROWS} of {len(rows)} rows.")
    if missing:
        con.info(f"Missing ({len(missing)}): {escape(format_chapter_ranges(_chapter_numbers(missing)) or 'unnumbered pages only')}")
    if failed:
        con.info(f"Failed ({len(failed)}): {escape(format_chapter_ranges(_chapter_numbers(failed)) or 'unnumbered pages only')}")
    con.info(
        "Nothing is re-queued automatically. Use [bold cyan]retry missing[/bold cyan], "
        "[bold cyan]retry failed[/bold cyan] or e.g. [bold cyan]retry 112-115[/bold cyan]."
    )


def cmd_flush(ctx: ReplContext, args: list[str]) -> None:
    con.ok(ctx.pipeline.flush())


def cmd_auto(ctx: ReplContext, args: list[str]) -> None:
    if not args or args[0].lower() not in ("on", "off"):
        con.warn("Usage: auto <on|off>")
        return
    enabled = args[0].lower() == "on"
    ctx.pipeline.set_auto_dispatch(enabled)
    if enabled:
        con.ok("Auto-dispatch enabled. Batches will be sent to the LLM automatically.")
    else:
        con.warn("Auto-dispatch paused. Pages keep collecting in the pending pool for manual or flush.")


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
    """retry -> all failed chapters; retry <chapters|ranges|missing|failed|all> -> those, sent now."""
    if args:
        print_report(ctx.pipeline.requeue(args, drain=True))
        return
    print_report(ctx.pipeline.retry_failed())


def cmd_recover(ctx: ReplContext, args: list[str]) -> None:
    """recover -> list spools; recover #8 -> batch 8; recover 112-115 -> chapters (no forced send)."""
    if not args:
        print_spooled(ctx)
        return
    if not ctx.cfg.book_dir:
        con.warn("No book directory selected.")
        return
    batch_ref = _BATCH_REF_RE.match(" ".join(args).strip())
    if batch_ref:
        ok, msg = ctx.pipeline.recover_spooled(int(batch_ref.group(1)))
        (con.ok if ok else con.warn)(msg)
        return
    print_report(ctx.pipeline.requeue(args, drain=False, verb="Recovered"))


def _term_table(rows: list[dict], title: str) -> Table:
    table = Table(title=title, border_style="magenta")
    for column in ("Chinese", "English", "Type", "Aliases", "Refers To", "Source", "First Ch."):
        table.add_column(column)
    for row in rows[:MAX_TERM_ROWS]:
        first = row.get("firstChapter")
        table.add_row(
            escape(row["chinese"]),
            f"[bold green]{escape(row['english'])}[/bold green]",
            escape(row["type"]),
            escape(", ".join(row["aliases"])),
            escape(row["refersTo"]),
            escape(row["source"]),
            str(first) if first else "",
        )
    return table


def cmd_terms(ctx: ReplContext, args: list[str]) -> None:
    """terms [search] -> glossary table, optionally filtered."""
    if not ctx.cfg.book_dir:
        con.warn("No book directory selected.")
        return
    query = " ".join(args).strip()
    rows, err = glossary.search_terms(ctx.cfg.book_dir, query)
    if err:
        con.fail(escape(err))
        return
    if not rows:
        if query:
            con.info(f"No glossary terms match '{escape(query)}'.")
        else:
            con.info("The glossary is empty. Terms are added as the model meets them, or with 'term add'.")
        return
    suffix = f" matching '{query}'" if query else ""
    con.console.print(_term_table(rows, escape(f"Glossary: {len(rows)} term(s){suffix}")))
    if len(rows) > MAX_TERM_ROWS:
        con.dim(f"Showing {MAX_TERM_ROWS} of {len(rows)} terms. Narrow it down with 'terms <search>'.")


def _split_assignment(args: list[str]) -> tuple[str, str] | None:
    """'小丑 = Joker' -> ('小丑', 'Joker'); None when either side is missing."""
    left, sep, right = " ".join(args).partition("=")
    left, right = left.strip(), right.strip()
    if not sep or not left or not right:
        return None
    return left, right


def _term_add(book_dir: str, args: list[str]) -> tuple[bool, str]:
    pair = _split_assignment(args)
    if pair is None:
        return False, TERM_USAGE
    chinese, english = pair
    words = english.split()
    term_type = None
    if len(words) > 1 and words[-1].lower().startswith("type:"):
        term_type, english = words[-1][5:], " ".join(words[:-1])
    return glossary.add_user_term(book_dir, chinese, english, term_type)


def _term_alias(book_dir: str, args: list[str]) -> tuple[bool, str]:
    pair = _split_assignment(args)
    if pair is None:
        return False, TERM_USAGE
    return glossary.add_user_alias(book_dir, pair[0], pair[1])


def _term_remove(book_dir: str, args: list[str]) -> tuple[bool, str]:
    return glossary.remove_term(book_dir, " ".join(args).strip())


_TERM_SUBCOMMANDS = {
    "add": _term_add, "set": _term_add,
    "alias": _term_alias, "link": _term_alias,
    "rm": _term_remove, "remove": _term_remove, "del": _term_remove, "delete": _term_remove,
}


def cmd_term(ctx: ReplContext, args: list[str]) -> None:
    """term add|alias|rm -> edit the glossary by hand; your entries are never changed by the model."""
    if not ctx.cfg.book_dir:
        con.warn("No book directory selected.")
        return
    handler = _TERM_SUBCOMMANDS.get(args[0].lower()) if args else None
    if handler is None:
        con.warn(escape(TERM_USAGE))
        return
    ok, msg = handler(ctx.cfg.book_dir, args[1:])
    (con.ok if ok else con.warn)(escape(msg))


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
    "pending": cmd_pending,
    "missing": cmd_missing,
    "flush": cmd_flush,
    "auto": cmd_auto,
    "manual": cmd_manual,
    "paste": cmd_paste,
    "cancel": cmd_cancel,
    "retry": cmd_retry,
    "recover": cmd_recover,
    "raw": cmd_raw,
    "terms": cmd_terms,
    "term": cmd_term,
}
COMMAND_WORDS = tuple(sorted(set(HANDLERS) | {"quit", "retranslate", "buffer", "queue", "glossary"}))


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
        bars = render_bars(ctx.pipeline.tracker.snapshot(), queue=ctx.pipeline.queue_counts())
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
