"""Send the English translations and raw pages of a chapter range to 'combineCopy'.

Pick a book, then a range of chapters:

  From the start N     Chapters 1 to N
  From the end N       the last N translated chapters
  Around the middle N  N chapters around the middle chapter (N=10: middle-5 to middle+4)
  Explicit range       e.g. 140-175

Chapter 1 is always the first chapter; the last is the highest translated chapter.
combineCopy runs inside the library folder and receives paths relative to it, the English file and
then the raw page for every chapter (backslashes on Windows):

  combineCopy './SuperBase Evolution/Chapter 10.md' './SuperBase Evolution/_raw/chapters/ch_000010.txt'

Before it runs, you are asked whether to add a request for the model ([y/N], Enter means no). A request
is piped in with --prompt-pipe, like audit.py's findings. --request TEXT or --request-file PATH give one
without asking; -y and --dry-run skip the question.

Its output appears directly in this console. The range and command code lives in booktl/combine.py,
shared with audit.py.
"""

import argparse
import os
import shutil  # noqa: F401  (tests patch shutil.which through this module)
import subprocess  # noqa: F401  (tests patch subprocess.run through this module)
import sys

from rich.markup import escape

from booktl import console as con
from booktl import menu
from booktl.combine import (  # noqa: F401  (re-exported for callers and tests)
    MAX_COMMAND_CHARS, PIPE_FLAG, PIPE_LIMIT_BYTES, Selection, build_command, build_pipe_command, check_length,
    collect_files, parse_count, parse_range, ps_quote, raw_parts, relative, resolve_range, run_combine,
    translated_chapters,
)
from booktl.library import ordered_books
from booktl.parsing import format_chapter_ranges

DEFAULT_LIBRARY = "../TranslateBook"
DEFAULT_COMMAND = "combineCopy"
MAX_INPUT_ATTEMPTS = 5
MAX_REQUEST_LINES = 2_000    # lines read for one typed request
PREVIEW_PAIRS = 2            # chapters shown at each end of the command preview
EXIT_OK, EXIT_USER, EXIT_COMMAND = 0, 1, 2
MODES = ("start", "end", "middle", "range")
MODE_LABELS = ("From the start", "From the end", "Around the middle", "Explicit range")
REQUEST_QUESTION = "Add a request for the model? [y/N]: "
REQUEST_LINE_PROMPT = "[dim]>[/dim] "
PS_PIPE_NOTE = (
    "combineCopy runs through PowerShell here. If it is a function in your profile, it must forward its "
    "pipeline input or it receives no request, e.g.:\n"
    "  function combineCopy { if ($MyInvocation.ExpectingInput) { $input | combineCopy.exe @args } "
    "else { combineCopy.exe @args } }"
)
_YES = ("y", "yes")
_NO = ("n", "no", "q", "quit")


# ----------------------------------------------------------------------
# Interactive input
# ----------------------------------------------------------------------

def _ask(prompt: str) -> str | None:
    try:
        return con.console.input(f"[bold]{escape(prompt)}[/bold]").strip()
    except (KeyboardInterrupt, EOFError):
        return None


def _read_line(prompt: str) -> str | None:
    """One raw line (Rich markup prompt), or None on Ctrl+C / EOF."""
    try:
        return con.console.input(prompt)
    except (KeyboardInterrupt, EOFError):
        return None


def _numbered_choice(title: str, labels: list, hints: list) -> int | None:
    con.rule(escape(title))
    for number, (label, hint) in enumerate(zip(labels, hints), start=1):
        extra = f"  [dim]{escape(hint)}[/dim]" if hint else ""
        con.console.print(f"  [cyan]{number:2d}.[/cyan] {escape(label)}{extra}")
    for _ in range(MAX_INPUT_ATTEMPTS):
        answer = _ask("Select number ('q' to cancel): ")
        if answer is None or answer.lower() in ("q", "quit"):
            return None
        if answer.isdigit() and 1 <= int(answer) <= len(labels):
            return int(answer) - 1
        con.warn(f"Enter a number between 1 and {len(labels)}.")
    return None


def choose(title: str, labels: list, hints: list) -> int | None:
    """Arrow-key menu, or a numbered list when there is no terminal. None when cancelled."""
    try:
        return menu.select_option(title, labels, hints)
    except menu.MenuUnavailable as e:
        con.dim(f"Arrow-key menu unavailable ({escape(str(e))}); using numbered list.")
        return _numbered_choice(title, labels, hints)


def pick_book(library_dir: str, preselected: str | None = None) -> str | None:
    """Name of the chosen book folder, or None."""
    if preselected:
        valid = os.path.basename(preselected) == preselected
        if valid and os.path.isdir(os.path.join(library_dir, preselected)):
            return preselected
        con.warn(f"Book '{escape(preselected)}' not found in {escape(library_dir)}")
        return None
    books = ordered_books(library_dir)
    if not books:
        con.fail(f"No books with a Chapter 1 found in {escape(library_dir)}")
        return None
    names = [b.name for b in books]
    index = choose(f"Select book ({library_dir})", names, [b.hint for b in books])
    return names[index] if index is not None else None


def ask_span(last: int) -> tuple[int, int] | None:
    """Ask for a mode and its number or range. None when cancelled."""
    middle = (1 + last) // 2
    hints = ["Chapters 1 … N", f"the last N, up to Chapter {last}", f"N chapters around Chapter {middle}", "e.g. 140-175"]
    index = choose(f"Chapter range (Chapters 1–{last} translated)", list(MODE_LABELS), hints)
    if index is None:
        return None
    mode = MODES[index]
    prompt = "Range, e.g. 140-175 (blank to cancel): " if mode == "range" else "Number of chapters (blank to cancel): "
    for _ in range(MAX_INPUT_ATTEMPTS):
        answer = _ask(prompt)
        if not answer:
            return None
        try:
            return resolve_range(mode, answer, last)
        except ValueError as e:
            con.warn(escape(str(e)))
    con.warn("Too many invalid answers.")
    return None


def confirm(question: str, default: bool = True) -> bool:
    """Yes/no question. Enter gives default; EOF and Ctrl+C mean no."""
    for _ in range(MAX_INPUT_ATTEMPTS):
        answer = _ask(question)
        if answer is None:
            return False
        low = answer.lower()
        if not low:
            return bool(default)
        if low in _YES:
            return True
        if low in _NO:
            return False
        con.warn(f"Please answer y or n (Enter means {'yes' if default else 'no'}).")
    return False


# ----------------------------------------------------------------------
# Optional request for the model
# ----------------------------------------------------------------------

def _clean_request(text) -> str | None:
    """The request without surrounding blank space; None when empty. Raises ValueError when too large."""
    if not isinstance(text, str):
        return None
    clean = text.strip()
    if not clean:
        return None
    size = len(clean.encode("utf-8"))
    if size > PIPE_LIMIT_BYTES:
        raise ValueError(f"The request is {size:,} bytes; combineCopy reads at most {PIPE_LIMIT_BYTES:,}.")
    return clean


def _read_request_file(path: str) -> str:
    """Raises OSError/ValueError."""
    with open(path, "r", encoding="utf-8") as f:
        return f.read(PIPE_LIMIT_BYTES + 1)


def _given_request(args) -> str | None:
    """Request from --request or --request-file; None when neither was given. Raises OSError/ValueError."""
    text = getattr(args, "request", None)
    if text is not None:
        clean = _clean_request(text)
        if clean is None:
            raise ValueError("The --request text is empty.")
        return clean
    path = getattr(args, "request_file", None)
    if path:
        clean = _clean_request(_read_request_file(path))
        if clean is None:
            raise ValueError(f"The request file {path} is empty.")
        return clean
    return None


def ask_request() -> str | None:
    """A request typed line by line, ended by an empty line. None when blank, cancelled or too long."""
    con.info("Type your request; an empty line ends it (Ctrl+C cancels).")
    lines, size = [], 0
    for _ in range(MAX_REQUEST_LINES):
        line = _read_line(REQUEST_LINE_PROMPT)
        if line is None:
            con.warn("Request cancelled.")
            return None
        if not line.strip():
            break
        lines.append(line.rstrip())
        size += len(line.encode("utf-8")) + 1
        if size > PIPE_LIMIT_BYTES:
            con.warn(f"The request is longer than combineCopy's {PIPE_LIMIT_BYTES:,}-byte limit; it was dropped.")
            return None
    else:
        con.warn(f"Stopped reading after {MAX_REQUEST_LINES:,} lines.")
    try:
        return _clean_request("\n".join(lines))
    except ValueError as e:
        con.warn(escape(str(e)))
        return None


def _request(args) -> tuple[bool, str | None]:
    """(go on, request). A bad --request/--request-file stops the run; otherwise asks [y/N] when interactive."""
    try:
        given = _given_request(args)
    except (OSError, ValueError) as e:
        con.fail(f"Could not use the request: {escape(str(e))}")
        return False, None
    if given is not None:
        return True, given
    if args.yes or args.dry_run or not confirm(REQUEST_QUESTION, default=False):
        return True, None
    request = ask_request()
    if request is None:
        con.dim("No request; the files are sent on their own.")
    return True, request


# ----------------------------------------------------------------------
# Main flow
# ----------------------------------------------------------------------

def span_from_args(args, last: int) -> tuple[int, int] | None:
    """Span from --start/--end/--middle/--range, or None when none was given. Raises ValueError."""
    for mode in MODES:
        value = getattr(args, mode, None)
        if value is not None:
            return resolve_range(mode, value, last)
    return None


def preview(command: str, files: list) -> str:
    """The command as it would be typed, shortened in the middle for long ranges."""
    quoted = [ps_quote(f) for f in files]
    keep = PREVIEW_PAIRS * 2
    if len(quoted) > keep * 2:
        quoted = quoted[:keep] + ["…"] + quoted[-keep:]
    return " ".join([command, *quoted])


def _report_gaps(selection: Selection) -> None:
    if selection.no_english:
        con.warn(f"Skipped, no translation: {escape(format_chapter_ranges(selection.no_english))}")
    if selection.no_raw:
        con.warn(f"Skipped, no raw page in _raw/chapters: {escape(format_chapter_ranges(selection.no_raw))}")


def _print_dry_run(argv: list, library: str) -> None:
    con.rule("Dry run")
    con.info(f"Working folder: {escape(library)}")
    for arg in argv:
        con.console.print(f"  {escape(str(arg))}", highlight=False)


def _run_and_report(argv: list, library: str, command: str, data: bytes | None = None) -> int:
    con.rule(f"{escape(command)} output")
    code = run_combine(argv, library, data=data)
    con.rule()
    if code is None:
        return EXIT_COMMAND
    if code != 0:
        con.fail(f"{escape(command)} exited with code {code}.")
        return EXIT_COMMAND
    con.ok(f"{escape(command)} finished.")
    return EXIT_OK


def _command(command: str, files: list, windows: bool, request) -> tuple[list[str], str]:
    """argv and how it runs: the plain command, or the --prompt-pipe one when there is a request."""
    if request is None:
        return build_command(command, files, windows), "native"
    return build_pipe_command(command, files, windows)


def send_chapters(args, library: str, book: str, translated: dict, span: tuple[int, int]) -> int:
    selection = collect_files(library, book, translated, *span)
    _report_gaps(selection)
    if not selection.files:
        con.fail("No chapter in that range has both a translation and a raw page.")
        return EXIT_USER
    windows = os.name == "nt"
    try:
        build_command(args.command, [], windows)  # report a missing command before asking anything
    except (FileNotFoundError, ValueError) as e:
        con.fail(escape(str(e)))
        return EXIT_COMMAND
    count = len(selection.chapters)
    con.info(
        f"{escape(format_chapter_ranges(selection.chapters))} · {count} chapter(s) · {len(selection.files)} files"
    )
    con.dim(escape(preview(args.command, selection.files)))
    go_on, request = _request(args)
    if not go_on:
        return EXIT_USER
    try:
        argv, how = _command(args.command, selection.files, windows, request)
        check_length(argv, windows)
    except (FileNotFoundError, ValueError) as e:
        con.fail(escape(str(e)))
        return EXIT_COMMAND
    if request is not None:
        con.info(f"Request: {len(request):,} characters, piped with {PIPE_FLAG}")
    if args.dry_run:
        _print_dry_run(argv, library)
        return EXIT_OK
    attached = " with your request" if request is not None else ""
    if not args.yes and not confirm(f"Run {args.command} on {count} chapter(s){attached}? [Y/n]: "):
        con.warn("Cancelled.")
        return EXIT_USER
    if request is not None and how == "powershell":
        con.warn(escape(PS_PIPE_NOTE))
    data = request.encode("utf-8") if request is not None else None
    return _run_and_report(argv, library, args.command, data)


def add_range_args(parser: argparse.ArgumentParser):
    """--start/--end/--middle/--range, shared with audit.py. Returns the group so callers can add modes."""
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--start", metavar="N", help="Chapters 1 to N")
    group.add_argument("--end", metavar="N", help="The last N translated chapters")
    group.add_argument("--middle", metavar="N", help="N chapters around the middle chapter")
    group.add_argument("--range", metavar="A-B", help="An explicit range, e.g. 140-175")
    return group


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Send the English translations and raw pages of a chapter range to combineCopy."
    )
    parser.add_argument("-b", "--book", help="Book folder name (skips the book menu)")
    parser.add_argument("-l", "--library", default=DEFAULT_LIBRARY, help="Library folder (default: ../TranslateBook)")
    add_range_args(parser)
    parser.add_argument("-y", "--yes", action="store_true", help="Run without asking for confirmation or a request")
    parser.add_argument("--dry-run", action="store_true", help="Print the command instead of running it")
    parser.add_argument("--command", default=DEFAULT_COMMAND, help="Command to run (default: combineCopy)")
    requests = parser.add_mutually_exclusive_group()
    requests.add_argument("--request", metavar="TEXT", help="Request for the model, piped with --prompt-pipe")
    requests.add_argument("--request-file", metavar="PATH", help="File holding the request for the model")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    library = os.path.abspath(args.library)
    if not os.path.isdir(library):
        con.fail(f"Library folder not found: {escape(library)}")
        return EXIT_USER
    book = pick_book(library, args.book)
    if not book:
        con.warn("No book selected.")
        return EXIT_USER
    try:
        translated = translated_chapters(os.path.join(library, book))
    except OSError as e:
        con.fail(f"Could not read {escape(book)}: {escape(str(e))}")
        return EXIT_USER
    if not translated:
        con.fail(f"{escape(book)} has no translated chapters yet.")
        return EXIT_USER
    last = max(translated)
    con.ok(f"[bold green]{escape(book)}[/bold green]: last translated chapter is Chapter {last}.")
    try:
        span = span_from_args(args, last) or ask_span(last)
    except ValueError as e:
        con.fail(escape(str(e)))
        return EXIT_USER
    if span is None:
        con.warn("No range selected.")
        return EXIT_USER
    return send_chapters(args, library, book, translated, span)


if __name__ == "__main__":
    sys.exit(main())
