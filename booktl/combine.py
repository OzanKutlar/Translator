"""Running combineCopy: chapter ranges, the file list and the command line, shared by inquire.py and audit.py.

combineCopy runs inside the library folder and receives paths relative to it, the English file and
then the raw page for every chapter. With --prompt-pipe it also reads a prompt from stdin until EOF
(at most 2 MB); the flag goes first and all paths follow it together.
"""

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field

from rich.markup import escape

from . import console as con
from .parsing import parse_chapter_number
from .storage import CHAPTER_PAGES_DIRNAME, RAW_DIRNAME

MAX_SCAN_ENTRIES = 50_000    # bounds the scan of one book folder
MAX_COMMAND_CHARS = 30_000   # Windows caps a command line at 32,767 characters
PIPE_FLAG = "--prompt-pipe"
PIPE_LIMIT_BYTES = 2_000_000  # combineCopy refuses a larger prompt
_PS_UTF8 = (
    "$OutputEncoding = [Console]::InputEncoding = [Console]::OutputEncoding = "
    "New-Object System.Text.UTF8Encoding $false; "
)
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
# Command line
# ----------------------------------------------------------------------

def _powershell() -> str | None:
    return shutil.which("pwsh") or shutil.which("powershell")


def ps_quote(text) -> str:
    """PowerShell single-quoted string."""
    return "'" + str(text).replace("'", "''") + "'"


def _require_command(command) -> None:
    if not isinstance(command, str) or not command.strip():
        raise ValueError("The command name cannot be empty.")


def _find(command: str, windows: bool) -> str | None:
    exe = shutil.which(command)
    if exe is None and windows:
        exe = shutil.which(command + ".ps1")
    return exe


def build_command(command: str, files: list, windows: bool = os.name == "nt") -> list[str]:
    """argv for combineCopy: an executable, a .ps1 script, or a PowerShell profile function.

    Raises ValueError for an empty command and FileNotFoundError when nothing can run it.
    """
    _require_command(command)
    exe = _find(command, windows)
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


def build_pipe_command(command: str, files: list, windows: bool = os.name == "nt") -> tuple[list[str], str]:
    """argv that runs 'command --prompt-pipe files…' with the prompt on stdin, and how: 'native' or 'powershell'.

    An executable gets stdin directly. A .ps1 script or a profile function runs through PowerShell, which
    switches to UTF-8 and pipes its own stdin ($input) into it; a profile function must forward that input.
    Raises ValueError for an empty command and FileNotFoundError when nothing can run it.
    """
    _require_command(command)
    exe = _find(command, windows)
    if exe is not None and not exe.lower().endswith(".ps1"):
        return [exe, PIPE_FLAG, *files], "native"
    shell = _powershell()
    if shell is None or (exe is None and not windows):
        raise FileNotFoundError(f"'{command}' was not found on PATH, and no PowerShell is available to run it.")
    target = ps_quote(exe if exe is not None else command)
    call = _PS_UTF8 + " ".join(["$input", "|", "&", target, ps_quote(PIPE_FLAG), *(ps_quote(f) for f in files)])
    profile = ["-NoProfile"] if exe is not None else []
    return [shell, "-NoLogo", *profile, "-Command", call], "powershell"


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


def run_combine(argv: list, cwd: str, data: bytes | None = None) -> int | None:
    """Run the command in cwd, its output going straight to this console. data, when given, is written
    to its stdin and the pipe is closed. Returns the exit code, or None if it could not run.
    """
    if data is not None and not isinstance(data, bytes):
        raise TypeError("data must be bytes or None")
    kwargs = {"cwd": cwd, "check": False}
    if data is not None:
        kwargs["input"] = data
    try:
        completed = subprocess.run(argv, **kwargs)
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
