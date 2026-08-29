"""Interactive REPL command handlers."""

from rich.table import Table
from . import console as con
from .config import Config, ConfigError
from .library import select_book
from .pipeline import Pipeline, HAS_CLIPBOARD
from .client import ApiClient

if HAS_CLIPBOARD:
    import pyperclip


class ReplContext:
    def __init__(self, cfg: Config, pipeline: Pipeline, client: ApiClient):
        self.cfg = cfg
        self.pipeline = pipeline
        self.client = client


def print_help() -> None:
    table = Table(title="Available Commands", border_style="blue")
    table.add_column("Command", style="cyan", no_wrap=True)
    table.add_column("Description", style="white")

    table.add_row("/status", "Display live status, pipeline counters, and current settings.")
    table.add_row("/model [id]", "Switch active model directly or open the interactive model picker.")
    table.add_row("/endpoint <url>", "Change target API base URL.")
    table.add_row("/key <token|none>", "Set or clear the API authorization bearer key.")
    table.add_row("/batch <n>", "Set number of pages per translation batch (alias: /setpage).")
    table.add_row("/concurrency <n>", "Adjust active parallel worker threads.")
    table.add_row("/temp <0.0-2.0>", "Adjust model generation temperature.")
    table.add_row("/maxtokens <n|none>", "Set or clear maximum response tokens.")
    table.add_row("/jsonmode <auto|on|off>", "Configure JSON formatting parameter handling.")
    table.add_row("/book", "Switch target book directory.")
    table.add_row("/buffer", "View buffered page count and character sizes.")
    table.add_row("/flush", "Immediately dispatch all currently buffered pages.")
    table.add_row("/auto <on|off>", "Toggle auto-dispatching (turn off to accumulate for /manual).")
    table.add_row("/manual", "Copy buffered pages to clipboard for external translation.")
    table.add_row("/paste", "Ingest translation JSON response from clipboard.")
    table.add_row("/cancel", "Abort manual mode or active batch and recover pages to buffer.")
    table.add_row("/retry", "Re-process quarantined failure batches from _failed/.")
    table.add_row("/raw <on|off>", "Toggle saving raw model responses to _raw/.")
    table.add_row("/clear", "Clear terminal screen.")
    table.add_row("/quit", "Gracefully shut down workers and exit.")

    con.console.print(table)


def print_status(ctx: ReplContext) -> None:
    s = ctx.pipeline.stats()
    c = s["counters"]

    table = Table(title="Pipeline Status", border_style="cyan")
    table.add_column("Setting / Metric", style="bold white")
    table.add_column("Value", style="green")

    table.add_row("Target Book", s["book"] or "(None)")
    table.add_row("Endpoint", s["endpoint"])
    table.add_row("Model", s["model"] or "(None)")
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


def handle_command(ctx: ReplContext, line: str) -> bool:
    """Execute a REPL command line. Returns False to terminate REPL."""
    parts = line.strip().split()
    if not parts:
        return True
    cmd = parts[0].lower()
    args = parts[1:]

    if cmd in ("/quit", "/exit", "/q"):
        return False

    if cmd in ("/help", "/?"):
        print_help()
        return True

    if cmd == "/clear":
        con.console.clear()
        return True

    if cmd == "/status":
        print_status(ctx)
        return True

    if cmd == "/model":
        if args:
            new_model = " ".join(args).strip()
            ctx.cfg.model = new_model
            con.ok(f"Active model set to [bold cyan]{new_model}[/bold cyan]")
        else:
            ctx.client.select_model(ctx.cfg)
        return True

    if cmd == "/endpoint":
        if not args:
            con.warn("Usage: /endpoint <http://.../v1>")
            return True
        try:
            ctx.cfg.base_url = args[0]
            con.ok(f"Endpoint updated to [bold cyan]{ctx.cfg.base_url}[/bold cyan]")
            con.info("Re-querying model list for new endpoint...")
            ctx.client.select_model(ctx.cfg)
        except ConfigError as e:
            con.fail(str(e))
        return True

    if cmd == "/key":
        if not args:
            con.warn("Usage: /key <token|none>")
            return True
        ctx.cfg.api_key = args[0]
        if ctx.cfg.api_key:
            con.ok("API key updated.")
        else:
            con.ok("API key cleared.")
        return True

    if cmd in ("/batch", "/setpage"):
        if not args:
            con.warn(f"Current batch size is {ctx.cfg.batch_size}. Usage: /batch <number>")
            return True
        try:
            ctx.cfg.batch_size = args[0]
            con.ok(f"Batch size updated to [bold green]{ctx.cfg.batch_size}[/bold green] pages.")
        except ConfigError as e:
            con.fail(str(e))
        return True

    if cmd == "/concurrency":
        if not args:
            con.warn(f"Current concurrency is {ctx.cfg.concurrency}. Usage: /concurrency <number>")
            return True
        try:
            num = int(args[0])
            ctx.pipeline.set_concurrency(num)
            con.ok(f"Concurrency adjusted to [bold green]{num}[/bold green] workers.")
        except ConfigError as e:
            con.fail(str(e))
        except ValueError:
            con.fail("Concurrency must be an integer.")
        return True

    if cmd == "/temp":
        if not args:
            con.warn(f"Current temperature is {ctx.cfg.temperature}. Usage: /temp <0.0-2.0>")
            return True
        try:
            ctx.cfg.temperature = args[0]
            con.ok(f"Temperature set to [bold green]{ctx.cfg.temperature}[/bold green]")
        except ConfigError as e:
            con.fail(str(e))
        return True

    if cmd == "/maxtokens":
        if not args:
            con.warn(f"Current max_tokens is {ctx.cfg.max_tokens}. Usage: /maxtokens <number|none>")
            return True
        try:
            ctx.cfg.max_tokens = args[0]
            con.ok(f"Max tokens set to [bold green]{ctx.cfg.max_tokens}[/bold green]")
        except ConfigError as e:
            con.fail(str(e))
        return True

    if cmd == "/jsonmode":
        if not args:
            con.warn(f"Current json_mode is {ctx.cfg.json_mode}. Usage: /jsonmode <auto|on|off>")
            return True
        try:
            ctx.cfg.json_mode = args[0]
            con.ok(f"JSON mode set to [bold green]{ctx.cfg.json_mode}[/bold green]")
        except ConfigError as e:
            con.fail(str(e))
        return True

    if cmd == "/book":
        res = select_book(ctx.cfg.library_dir)
        if res:
            bpath, bname = res
            ctx.cfg.set_book(bpath, bname)
            con.ok(f"Switched target novel to [bold green]{bname}[/bold green]")
        return True

    if cmd == "/buffer":
        with ctx.pipeline.lock:
            buf = ctx.pipeline.buffer
            con.info(f"Buffer holds {len(buf)} page(s) (target: {ctx.cfg.batch_size}):")
            for i, p in enumerate(buf, start=1):
                con.console.print(f"  Page {i}: {len(p)} characters")
        return True

    if cmd == "/flush":
        msg = ctx.pipeline.flush()
        con.ok(msg)
        return True

    if cmd == "/auto":
        if not args or args[0].lower() not in ("on", "off"):
            con.warn("Usage: /auto <on|off>")
            return True
        mode = args[0].lower() == "on"
        ctx.pipeline.auto_dispatch = mode
        if mode:
            con.ok("Auto-dispatch enabled. Batches will be automatically sent to the LLM.")
        else:
            con.warn("Auto-dispatch paused. Pages will accumulate in buffer for /manual or /flush.")
        return True

    if cmd == "/manual":
        prompt, msg = ctx.pipeline.manual_begin()
        con.ok(msg)
        if prompt and not HAS_CLIPBOARD:
            con.rule("Manual Prompt")
            con.console.print(prompt)
        return True

    if cmd == "/paste":
        if not HAS_CLIPBOARD:
            con.fail("pyperclip not available. Please install it to use /paste from clipboard.")
            return True
        try:
            clip_text = pyperclip.paste()
        except Exception as e:
            con.fail(f"Could not read clipboard: {e}")
            return True
        msg = ctx.pipeline.manual_paste(clip_text)
        con.ok(msg)
        return True

    if cmd == "/cancel":
        msgs = ctx.pipeline.cancel()
        for m in msgs:
            con.warn(m)
        return True

    if cmd == "/retry":
        msg = ctx.pipeline.retry_failed()
        con.ok(msg)
        return True

    if cmd == "/raw":
        if not args or args[0].lower() not in ("on", "off"):
            con.warn(f"Save raw is currently {'on' if ctx.cfg.save_raw else 'off'}. Usage: /raw <on|off>")
            return True
        ctx.cfg.save_raw = args[0].lower() == "on"
        con.ok(f"Raw response storage set to {'on' if ctx.cfg.save_raw else 'off'}.")
        return True

    con.warn(f"Unknown command '{cmd}'. Type /help for available commands.")
    return True


def run_repl(ctx: ReplContext) -> None:
    con.rule("Translation Console Active")
    con.info("Type [bold cyan]/help[/bold cyan] for commands, [bold cyan]/quit[/bold cyan] to exit.\n")

    while True:
        try:
            line = con.console.input("[bold cyan]booktl> [/bold cyan]").strip()
        except (KeyboardInterrupt, EOFError):
            break

        if not line:
            continue

        if not line.startswith("/"):
            con.warn("Commands must start with '/'. Type /help for a list of commands.")
            continue

        if not handle_command(ctx, line):
            break
