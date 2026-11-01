"""Send the English translations and raw pages of a chapter range to 'combineCopy'.

Pick a book, then a range of chapters:

  From the start N     Chapters 1 to N
  From the end N       the last N translated chapters
  Around the middle N  N chapters around the middle chapter (N=10: middle-5 to middle+4)
  Explicit range       e.g. 140-175

Chapter 1 is always the first chapter; the last is the highest translated chapter.
combineCopy runs inside the library folder and receives paths relative to it, the English
file and then the raw page for every chapter (backslashes on Windows):

  combineCopy './SuperBase Evolution/Chapter 10.md' './SuperBase Evolution/_raw/chapters/ch_000010.txt'

Its output appears directly in this console.
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field

from rich.markup import escape

from booktl import console as con
from booktl import menu
from booktl.library import ordered_books
from booktl.parsing import format_chapter_ranges, parse_chapter_number
from booktl.storage import CHAPTER_PAGES_DIRNAME, RAW_DIRNAME

DEFAULT_LIBRARY = "../TranslateBook"
DEFAULT_COMMAND = "combineCopy"
MAX_INPUT_ATTEMPTS = 5
MAX_SCAN_ENTRIES = 50_000    # bounds the scan of one book folder
MAX_COMMAND_CHARS = 30_000   # Windows caps a command line at 32,767 characters
PREVIEW_PAIRS = 2            # chapters shown at each end of the command preview
EXIT_OK, EXIT_USER, EXIT_COMMAND = 0, 1, 2
MODES = ("start", "end", "middle", "range")
MODE_LABELS = ("From the start", "From the end", "Around the middle", "Explicit range")
_YES = ("", "y", "yes")
_NO = ("n", "no", "q", "quit")
_RANGE_RE = re.compile(
    r"^(?:ch(?:apter)?\.?\s*)?([0-9]{1,7})(?:\s*[-–—~]\s*(?:ch(?:apter)?\.?\s*)?([0-9]{1,7}))?$",
    re.IGNORECASE,
)


@dataclass
class Selection:
    """Files to send plus the chapters left out because a file is missing."""

    files: list = field(default_factory=list)       # relative paths: English then raw, per chapter
    chapters: list = field(default_factory=list)    # chapters that were included
    no_english: list = field(default_factory=list)
    no_raw: list = field(default_factory=list)


# ----------------------------------------------------------------------
# Range parsing
# ----------------------------------------------------------------------

def parse_count(value) -> int:
    """A positive whole number of chapters. Raises ValueError."""
    text = str(value).strip() if value is not None else ""
    if not re.fullmatch(r"[0-9]{1,7}", text) or int(text) <= 0:
        raise ValueError(f"'{text}' is not a positive whole number of chapters.")
    return int(text)


def parse_range(value) -> tuple[int, int]:
    """'140-175', 'ch140-175', '140 – 175' or '140' -> (low, high). Raises ValueError."""
    text = str(value).strip() if value is not None else ""
    match = _RANGE_RE.match(text)
    if not match:
        raise ValueError(f"Could not read '{text}'. Use e.g. 140-175.")
    low = int(match.group(1))
    high = int(match.group(2) or match.group(1))
    if low <= 0:
        raise ValueError("Chapter numbers start at 1.")
    if high < low:
        raise ValueError(f"'{text}' runs backwards; write it as {high}-{low}.")
    return low, high


def _middle_window(count: int, last: int) -> tuple[int, int]:
    """count chapters around the middle chapter: middle-count//2 onwards, kept inside 1..last."""
    if count >= last:
        return 1, last
    middle = (1 + last) // 2
    low = max(1, min(middle - count // 2, last - count + 1))
    return low, low + count - 1


def resolve_range(mode: str, value, last: int) -> tuple[int, int]:
    """Inclusive (low, high) chapter span for a mode and its value. Raises ValueError."""
    if isinstance(last, bool) or not isinstance(last, int) or last <= 0:
        raise ValueError("The book has no translated chapters.")
    if mode == "range":
        low, high = parse_range(value)
        if low > last:
            raise ValueError(f"Chapter {low} is past the last translated chapter ({last}).")
        return low, min(high, last)
    count = parse_count(value)
    if mode == "start":
        return 1, min(count, last)
    if mode == "end":
        return max(1, last - count + 1), last
    if mode == "middle":
        return _middle_window(count, last)
    raise ValueError(f"Unknown range mode {mode!r}.")


# ----------------------------------------------------------------------
# Chapter files
# ----------------------------------------------------------------------

def _chapter_of(filename: str) -> int | None:
    """Chapter number of a translated file such as 'Chapter 12.md', else None."""
    if not filename.lower().endswith(".md"):
        return None
    chapter = parse_chapter_number(filename[:-3])
    return chapter if chapter and chapter > 0 else None


def _is_file(entry) -> bool:
    try:
        return entry.is_file()
    except OSError:
        return False  # removed or unreadable during the scan: treated as absent


def translated_chapters(book_dir: str) -> dict[int, str]:
    """Chapter number -> file name of its translation. Raises OSError when the folder is unreadable."""
    found = {}
    with os.scandir(book_dir) as entries:
        for count, entry in enumerate(entries):
            if count >= MAX_SCAN_ENTRIES:
                break
            chapter = _chapter_of(entry.name)
            if chapter is None or not _is_file(entry):
                continue
            if chapter not in found or entry.name == f"Chapter {chapter}.md":
                found[chapter] = entry.name
    return found


def raw_parts(book_name: str, chapter: int) -> tuple[str, ...]:
    return (book_name, RAW_DIRNAME, CHAPTER_PAGES_DIRNAME, f"ch_{chapter:06d}.txt")


def relative(*parts: str) -> str:
    """Path relative to the library folder, e.g. './Book/Chapter 10.md'."""
    return os.path.join(os.curdir, *parts)


def collect_files(library_dir: str, book_name: str, translated: dict, low: int, high: int) -> Selection:
    """English file and raw page for every chapter in low..high that has both."""
    if low < 1 or high < low:
        raise ValueError(f"Invalid chapter span {low}-{high}.")
    selection = Selection()
    for chapter in range(low, high + 1):
        english = translated.get(chapter)
        raw = raw_parts(book_name, chapter)
        has_raw = os.path.isfile(os.path.join(library_dir, *raw))
        if english is None:
            selection.no_english.append(chapter)
        if not has_raw:
            selection.no_raw.append(chapter)
        if english is None or not has_raw:
            continue
        selection.files += [relative(book_name, english), relative(*raw)]
        selection.chapters.append(chapter)
    return selection


# ----------------------------------------------------------------------
# Running combineCopy
# ----------------------------------------------------------------------

def _powershell() -> str | None:
    return shutil.which("pwsh") or shutil.which("powershell")


def ps_quote(text) -> str:
    """PowerShell single-quoted string."""
    return "'" + str(text).replace("'", "''") + "'"


def build_command(command: str, files: list, windows: bool = os.name == "nt") -> list[str]:
    """argv for combineCopy: an executable, a .ps1 script, or a PowerShell profile function.

    Raises ValueError for an empty command and FileNotFoundError when nothing can run it.
    """
    if not isinstance(command, str) or not command.strip():
        raise ValueError("The command name cannot be empty.")
    exe = shutil.which(command)
    if exe is None and windows:
        exe = shutil.which(command + ".ps1")
    if exe is not None and not exe.lower().endswith(".ps1"):
        return [exe, *files]
    shell = _powershell()
    if exe is not None and shell:
        return [shell, "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", exe, *files]
    if exe is None and windows and shell:
        # Not on PATH: it may be a function or alias defined in the PowerShell profile.
        call = " ".join(["&", ps_quote(command), *(ps_quote(f) for f in files)])
        return [shell, "-NoLogo", "-Command", call]
    raise FileNotFoundError(f"'{command}' was not found on PATH, and no PowerShell is available to run it.")


def check_length(argv: list, windows: bool = os.name == "nt") -> None:
    """Refuse commands longer than Windows allows. Raises ValueError."""
    if not windows:
        return
    length = len(subprocess.list2cmdline(argv))
    if length > MAX_COMMAND_CHARS:
        raise ValueError(
            f"The command would be {length:,} characters long; Windows allows about {MAX_COMMAND_CHARS:,}. "
            "Pick a smaller range."
        )


def run_combine(argv: list, cwd: str) -> int | None:
    """Run the command in cwd with its output going straight to this console. None if it could not run."""
    try:
        completed = subprocess.run(argv, cwd=cwd, check=False)
    except FileNotFoundError as e:
        con.fail(f"Could not start {escape(str(argv[0]))}: {escape(str(e))}")
        return None
    except OSError as e:
        con.fail(f"Running {escape(str(argv[0]))} failed: {escape(str(e))}")
        return None
    except KeyboardInterrupt:
        con.warn("Interrupted; the command may not have finished.")
        return None
    return completed.returncode


# ----------------------------------------------------------------------
# Interactive input
# ----------------------------------------------------------------------

def _ask(prompt: str) -> str | None:
    try:
        return con.console.input(f"[bold]{escape(prompt)}[/bold]").strip()
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


def confirm(question: str) -> bool:
    """Yes/no question; Enter means yes, EOF and Ctrl+C mean no."""
    for _ in range(MAX_INPUT_ATTEMPTS):
        answer = _ask(question)
        if answer is None:
            return False
        if answer.lower() in _YES:
            return True
        if answer.lower() in _NO:
            return False
        con.warn("Please answer y or n (Enter means yes).")
    return False


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


def _run_and_report(argv: list, library: str, command: str) -> int:
    con.rule(f"{escape(command)} output")
    code = run_combine(argv, library)
    con.rule()
    if code is None:
        return EXIT_COMMAND
    if code != 0:
        con.fail(f"{escape(command)} exited with code {code}.")
        return EXIT_COMMAND
    con.ok(f"{escape(command)} finished.")
    return EXIT_OK


def send_chapters(args, library: str, book: str, translated: dict, span: tuple[int, int]) -> int:
    selection = collect_files(library, book, translated, *span)
    _report_gaps(selection)
    if not selection.files:
        con.fail("No chapter in that range has both a translation and a raw page.")
        return EXIT_USER
    windows = os.name == "nt"
    try:
        argv = build_command(args.command, selection.files, windows)
        check_length(argv, windows)
    except (FileNotFoundError, ValueError) as e:
        con.fail(escape(str(e)))
        return EXIT_COMMAND
    count = len(selection.chapters)
    con.info(
        f"{escape(format_chapter_ranges(selection.chapters))} · {count} chapter(s) · {len(selection.files)} files"
    )
    con.dim(escape(preview(args.command, selection.files)))
    if args.dry_run:
        _print_dry_run(argv, library)
        return EXIT_OK
    if not args.yes and not confirm(f"Run {args.command} on {count} chapter(s)? [Y/n]: "):
        con.warn("Cancelled.")
        return EXIT_USER
    return _run_and_report(argv, library, args.command)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Send the English translations and raw pages of a chapter range to combineCopy."
    )
    parser.add_argument("-b", "--book", help="Book folder name (skips the book menu)")
    parser.add_argument("-l", "--library", default=DEFAULT_LIBRARY, help="Library folder (default: ../TranslateBook)")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--start", metavar="N", help="Chapters 1 to N")
    group.add_argument("--end", metavar="N", help="The last N translated chapters")
    group.add_argument("--middle", metavar="N", help="N chapters around the middle chapter")
    group.add_argument("--range", metavar="A-B", help="An explicit range, e.g. 140-175")
    parser.add_argument("-y", "--yes", action="store_true", help="Run without asking for confirmation")
    parser.add_argument("--dry-run", action="store_true", help="Print the command instead of running it")
    parser.add_argument("--command", default=DEFAULT_COMMAND, help="Command to run (default: combineCopy)")
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
