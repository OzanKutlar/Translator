"""CLI entry point for book translation service."""

import argparse
import os
import sys
import threading
import time

from booktl import console as con
from booktl.config import Config
from booktl.library import select_book, ensure_book, discover_books
from booktl.client import ApiClient
from booktl.pipeline import Pipeline
from booktl.ingest import create_ingest_app
from booktl.commands import ReplContext, run_repl, print_report
from booktl.prompts import build_messages

RETRY_DRAIN_TIMEOUT = 6 * 3600  # seconds the 'retry' subcommand waits for re-queued chapters


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Novel translation engine for OpenAI-compatible endpoints.")
    subparsers = parser.add_subparsers(dest="subcommand")

    # Common arguments helper
    def add_common_args(p):
        p.add_argument("-b", "--book", help="Target book folder name")
        p.add_argument("-l", "--library", default="../TranslateBook", help="Library root path containing book folders")
        p.add_argument("-u", "--base-url", help="OpenAI v1 base URL (e.g. http://localhost:34501/v1)")
        p.add_argument("-m", "--model", help="Model ID to invoke")
        p.add_argument("-k", "--api-key", help="Bearer authorization token")
        p.add_argument("-s", "--batch-size", type=int, help="Number of pages per translation batch (default: 10)")
        p.add_argument("-c", "--concurrency", type=int, help="Number of worker threads (default: 1)")
        p.add_argument("-g", "--goal", help="Stop goal chapter number or relative offset (e.g. 3000 or +50)")
        p.add_argument("--json-mode", choices=["auto", "on", "off"], help="JSON mode parameter behavior")
        p.add_argument("--temperature", type=float, help="Sampling temperature (default: 0.3)")
        p.add_argument("--max-tokens", type=int, help="Max completion tokens")
        p.add_argument("--timeout", type=int, help="Request timeout in seconds")
        p.add_argument("--retries", type=int, help="Request retry attempts")
        p.add_argument("--port", type=int, default=5001, help="Ingest server port (default: 5001)")
        p.add_argument("--host", default="127.0.0.1", help="Ingest server bind host")
        p.add_argument("--dry-run", action="store_true", help="Build prompts without calling LLM endpoint")
        p.add_argument("--save-raw", action="store_true", help="Save raw LLM output to _raw/ folder")

    # Default 'run' subcommand
    p_run = subparsers.add_parser("run", help="Start ingest server and worker console")
    add_common_args(p_run)

    # 'models' subcommand
    p_models = subparsers.add_parser("models", help="List available models from the configured endpoint")
    p_models.add_argument("-u", "--base-url", help="OpenAI v1 base URL")
    p_models.add_argument("-k", "--api-key", help="API key")

    # 'books' subcommand
    p_books = subparsers.add_parser("books", help="List all books in library")
    p_books.add_argument("-l", "--library", default="../TranslateBook", help="Library root path")

    # 'check' subcommand
    p_check = subparsers.add_parser("check", help="Verify endpoint connectivity and test completion")
    add_common_args(p_check)

    # 'retry' subcommand
    p_retry = subparsers.add_parser("retry", help="Reprocess failed batches from _failed/")
    add_common_args(p_retry)

    # 'prompt' subcommand
    p_prompt = subparsers.add_parser("prompt", help="Display the assembled system prompt for a book")
    p_prompt.add_argument("-b", "--book", help="Target book folder name")
    p_prompt.add_argument("-l", "--library", default="../TranslateBook", help="Library root path")

    return parser


def main():
    # Auto-insert 'run' if no subcommand was explicitly specified
    subcommands = ("run", "models", "books", "check", "retry", "prompt", "-h", "--help")
    if len(sys.argv) > 1 and not sys.argv[1].startswith("-") and sys.argv[1] not in subcommands:
        sys.argv.insert(1, "run")
    elif len(sys.argv) == 1:
        sys.argv.append("run")

    parser = build_parser()
    args = parser.parse_args()
    sub = args.subcommand or "run"

    cfg = Config.from_args(args)
    client = ApiClient()

    # Handle 'books' subcommand
    if sub == "books":
        books = discover_books(cfg.library_dir)
        con.rule(f"Library: {os.path.abspath(cfg.library_dir)}")
        if not books:
            con.warn("No book directories found.")
        else:
            for b in books:
                con.console.print(f"  • {b}")
        return

    # Handle 'models' subcommand
    if sub == "models":
        con.rule(f"Querying Models from {cfg.base_url}")
        client.print_models(cfg)
        return

    # Handle 'prompt' subcommand
    if sub == "prompt":
        book_res = select_book(cfg.library_dir, args.book)
        if not book_res:
            return
        bpath, bname = book_res
        messages = build_messages(bpath, "[Sample Chinese raw text page goes here...]")
        for m in messages:
            con.panel(m["content"], title=f"Role: {m['role'].upper()}", style="cyan")
        return

    # Handle 'check' subcommand
    if sub == "check":
        con.rule("Endpoint Verification")
        con.info(f"Base URL: {cfg.base_url}")
        try:
            models = client.list_models(cfg)
            con.ok(f"Successfully reached /v1/models ({len(models)} models available)")
        except Exception as e:
            con.fail(f"Failed to query /v1/models: {e}")
            return

        test_model = cfg.model or (models[0]["id"] if models else None)
        if not test_model:
            con.warn("No model selected to test chat completions.")
            return

        cfg.model = test_model
        con.info(f"Testing completion ping with model: [bold green]{test_model}[/bold green]...")
        t0 = time.time()
        try:
            reply, usage = client.chat(cfg, [{"role": "user", "content": "Reply with the word: pong"}])
            elapsed = time.time() - t0
            con.ok(f"Received response in {elapsed:.2f}s: [dim]{reply.strip()}[/dim]")
        except Exception as e:
            con.fail(f"Chat completion test failed: {e}")
        return

    # Main 'run' / 'retry' execution path
    con.banner("Book Translation Server", f"Listening on http://{cfg.host}:{cfg.port}")

    # 1. Target Book Selection
    book_res = select_book(cfg.library_dir, getattr(args, "book", None))
    if not book_res:
        con.warn("Book selection aborted.")
        return
    bpath, bname = book_res
    cfg.set_book(bpath, bname)
    ensure_book(bpath)
    con.ok(f"Target novel set to: [bold green]{bname}[/bold green]")

    # 2. Model Selection
    if not cfg.model:
        if not client.select_model(cfg):
            con.fail("No model chosen. Exiting.")
            return
    else:
        con.ok(f"Using model: [bold green]{cfg.model}[/bold green]")
    # 3. Pipeline & Workers Initialization
    pipeline = Pipeline(cfg, client)
    goal_arg = getattr(args, "goal", None)
    if goal_arg:
        msg = pipeline.tracker.set_goal(goal_arg)
        con.ok(msg)
    pipeline.start()

    if sub == "retry":
        con.rule("Batch Reprocessing")
        print_report(pipeline.retry_failed())
        con.info("Waiting for re-queued chapters to finish... (Ctrl+C to stop waiting)")
        try:
            finished = pipeline.wait_until_idle(RETRY_DRAIN_TIMEOUT)
        except KeyboardInterrupt:
            finished = False
            con.warn("Stopped waiting at user request.")
        pipeline.shutdown(drain=False)
        stats = pipeline.stats()
        if finished:
            con.ok(f"Retry complete. Missing chapters: {stats['missing']}, failed chapters: {stats['failed']}.")
        else:
            con.warn("Retry did not finish; unsent pending pages were saved to _raw/.")
        return

    # 4. Flask Ingest Server
    app = create_ingest_app(pipeline)
    server_thread = threading.Thread(
        target=app.run,
        kwargs={"host": cfg.host, "port": cfg.port, "use_reloader": False, "threaded": True},
        daemon=True
    )
    server_thread.start()
    con.ok(f"Ingest API ready for Tampermonkey at: [bold green]http://{cfg.host}:{cfg.port}/book-content[/bold green]")

    # 5. Interactive REPL
    ctx = ReplContext(cfg, pipeline, client)
    try:
        run_repl(ctx)
    finally:
        con.rule("Shutting Down")
        pipeline.shutdown(drain=True)
        stats = pipeline.stats()
        cnt = stats["counters"]
        tr = stats.get("tracker", {})
        curr_ch = tr.get("current_chapter")
        goal_ch = tr.get("goal_chapter")
        con.panel(
            f"Final Chapter: [bold green]{curr_ch if curr_ch is not None else 'N/A'}[/bold green]\n"
            f"Goal Chapter: {goal_ch if goal_ch is not None else '(None)'}\n"
            f"Total Chapters Saved: [bold green]{cnt['chapters_saved']}[/bold green]\n"
            f"Pages Processed: {cnt['pages_seen']}\n"
            f"Failed Batches: {cnt['failures']}\n"
            f"Missing Chapters: {stats['missing']}\n"
            f"Failed Chapters: {stats['failed']}\n"
            f"Reported Tokens: {cnt['tokens']:,}",
            title="Session Summary",
            style="blue"
        )


if __name__ == "__main__":
    main()
