"""Book folder resolution, creation and interactive selection.

The picker lists recently translated books first (see activity.py), then the rest
alphabetically, and ends with a row for creating a new book.
"""

import json
import os
from dataclasses import dataclass

from rich.markup import escape

from . import activity
from . import console as con
from . import menu
from .parsing import parse_chapter_number

MAX_NUMBERED_ATTEMPTS = 5
MAX_NAME_ATTEMPTS = 5
MAX_BOOK_NAME_LENGTH = 100
NEW_BOOK_LABEL = "＋ New book…"
NEW_BOOK_ANSWERS = ("n", "new")
_ILLEGAL_NAME_CHARS = frozenset('<>:"/\\|?*')
_RESERVED_NAMES = frozenset(
    ["con", "prn", "aux", "nul"]
    + [f"com{i}" for i in range(1, 10)]
    + [f"lpt{i}" for i in range(1, 10)]
)
_NEW_BOOK = object()  # sentinel: the picker's new-book row was chosen


@dataclass(frozen=True)
class BookInfo:
    """A library folder plus when it was last translated."""

    name: str
    last_translated: float | None = None
    last_chapter: int | None = None

    @property
    def is_recent(self) -> bool:
        return activity.is_recent(self.last_translated)

    @property
    def hint(self) -> str:
        """Picker hint such as 'translated 2h ago · ch 1240'; '' when never translated."""
        age = activity.format_age(self.last_translated)
        if not age:
            return ""
        text = f"translated {age}"
        return f"{text} · ch {self.last_chapter}" if self.last_chapter else text


# ----------------------------------------------------------------------
# Discovery and ordering
# ----------------------------------------------------------------------

_COMMON_CH1_NAMES = frozenset({
    "1.md",
    "chapter 1.md",
    "chapter_1.md",
    "chapter-1.md",
    "ch 1.md",
    "ch1.md",
    "ch_000001.md",
    "0001.md",
    "01.md",
})


def has_chapter_one(book_dir: str) -> bool:
    """Fast check for whether a directory contains a Chapter 1 markdown file."""
    if not os.path.isdir(book_dir):
        return False
    try:
        with os.scandir(book_dir) as entries:
            for entry in entries:
                if not entry.name.lower().endswith(".md"):
                    continue
                low_name = entry.name.lower()
                if low_name in _COMMON_CH1_NAMES:
                    return True
                if parse_chapter_number(entry.name[:-3]) == 1:
                    return True
    except OSError:
        return False
    return False


def discover_books(library_dir: str) -> list[str]:
    """Return sorted directory names inside the library folder that contain a Chapter 1 file."""
    abs_lib = os.path.abspath(library_dir)
    if not os.path.exists(abs_lib):
        return []
    books = []
    for d in os.listdir(abs_lib):
        if d.startswith("."):
            continue
        dir_path = os.path.join(abs_lib, d)
        if os.path.isdir(dir_path) and has_chapter_one(dir_path):
            books.append(d)
    return sorted(books, key=str.lower)


def _list_existing_dirs(library_dir: str) -> list[str]:
    """List all directory names in library_dir regardless of contents for collision check."""
    abs_lib = os.path.abspath(library_dir)
    if not os.path.isdir(abs_lib):
        return []
    try:
        return [
            d for d in os.listdir(abs_lib)
            if os.path.isdir(os.path.join(abs_lib, d)) and not d.startswith(".")
        ]
    except OSError:
        return []


def _book_sort_key(info: BookInfo) -> tuple:
    """Recent books first, newest first; everything else alphabetically."""
    if info.is_recent:
        return (0, -info.last_translated, info.name.lower())
    return (1, 0.0, info.name.lower())


def ordered_books(library_dir: str) -> list[BookInfo]:
    """Books with their last translation, in picker order."""
    abs_lib = os.path.abspath(library_dir)
    infos = []
    for name in discover_books(abs_lib):
        stamp, chapter = activity.last_translated(os.path.join(abs_lib, name))
        infos.append(BookInfo(name, stamp, chapter))
    infos.sort(key=_book_sort_key)
    return infos


def ensure_book(book_dir: str) -> None:
    """Ensure target directory and Characters/Characters.json structure exists."""
    os.makedirs(book_dir, exist_ok=True)
    chars_dir = os.path.join(book_dir, "Characters")
    os.makedirs(chars_dir, exist_ok=True)
    chars_json = os.path.join(chars_dir, "Characters.json")
    if not os.path.exists(chars_json):
        with open(chars_json, "w", encoding="utf-8") as f:
            json.dump({}, f, ensure_ascii=False, indent=2)


# ----------------------------------------------------------------------
# Creating books
# ----------------------------------------------------------------------

def validate_book_name(name, existing=()) -> str | None:
    """Return why name cannot be a new book folder, or None when it can."""
    if not isinstance(name, str):
        return "The book name must be text."
    clean = name.strip()
    if not clean:
        return "The book name cannot be empty."
    if len(clean) > MAX_BOOK_NAME_LENGTH:
        return f"The book name must be at most {MAX_BOOK_NAME_LENGTH} characters long."
    if clean.startswith(".") or clean.endswith("."):
        return "The book name cannot start or end with '.'."
    if any(ch in _ILLEGAL_NAME_CHARS or ord(ch) < 32 for ch in clean):
        return 'The book name cannot contain < > : " / \\ | ? * or control characters.'
    if clean.split(".")[0].strip().lower() in _RESERVED_NAMES:
        return f"'{clean}' is a reserved name on Windows."
    if clean.casefold() in {str(b).casefold() for b in existing}:
        return f"A book named '{clean}' already exists."
    return None


def create_book(library_dir: str, name: str) -> tuple[str, str]:
    """Create a new book folder in the library. Returns (book_dir, book_name).

    Raises ValueError for an unusable or taken name, OSError when the folder cannot be created.
    """
    abs_lib = os.path.abspath(library_dir)
    error = validate_book_name(name, _list_existing_dirs(abs_lib))
    if error:
        raise ValueError(error)
    clean = name.strip()
    path = os.path.join(abs_lib, clean)
    if os.path.exists(path):
        raise ValueError(f"'{clean}' already exists in the library.")
    os.makedirs(abs_lib, exist_ok=True)
    ensure_book(path)
    return path, clean


def _prompt_new_book(abs_lib: str) -> tuple[str, str] | None:
    """Ask for a name and create the book. Returns (book_dir, book_name) or None if cancelled."""
    for _ in range(MAX_NAME_ATTEMPTS):
        try:
            ans = con.console.input("[bold]New book name (blank to cancel): [/bold]").strip()
        except (KeyboardInterrupt, EOFError):
            return None
        if not ans:
            return None
        try:
            created = create_book(abs_lib, ans)
        except ValueError as e:
            con.warn(escape(str(e)))
            continue
        except OSError as e:
            con.fail(f"Could not create '{escape(ans)}': {escape(str(e))}")
            continue
        con.ok(f"Created new book [bold green]{escape(created[1])}[/bold green] in {escape(abs_lib)}")
        return created
    con.warn("Too many invalid names; book creation cancelled.")
    return None


# ----------------------------------------------------------------------
# Selection
# ----------------------------------------------------------------------

def select_book(library_dir: str, preselected: str = None) -> tuple[str, str] | None:
    """Pick a book: -b preselection, otherwise an arrow-key menu with a new-book row.

    An empty library goes straight to the new-book prompt.
    Returns (book_dir, book_name), or None if cancelled.
    """
    abs_lib = os.path.abspath(library_dir)
    books = ordered_books(abs_lib)

    if preselected:
        resolved = _resolve_preselected(abs_lib, [b.name for b in books], preselected)
        if resolved:
            return resolved

    if not books:
        con.info(f"No books in [cyan]{escape(abs_lib)}[/cyan] yet. Enter a name to create your first one.")
        return _prompt_new_book(abs_lib)

    choice = _choose_book(abs_lib, books)
    if choice is _NEW_BOOK:
        return _prompt_new_book(abs_lib)
    return _open_book(abs_lib, choice) if choice else None


def _open_book(abs_lib: str, name: str) -> tuple[str, str]:
    path = os.path.join(abs_lib, name)
    ensure_book(path)
    return path, name


def _resolve_preselected(abs_lib: str, names: list[str], preselected: str) -> tuple[str, str] | None:
    if preselected in names:
        return _open_book(abs_lib, preselected)
    if os.path.isdir(preselected):
        path = os.path.abspath(preselected)
        ensure_book(path)
        return path, os.path.basename(path)
    con.warn(f"Book '{escape(preselected)}' not found in {escape(abs_lib)}")
    return None


def _choose_book(abs_lib: str, books: list[BookInfo]):
    """Returns a book name, the _NEW_BOOK sentinel, or None. Falls back to a numbered prompt."""
    labels = [b.name for b in books] + [NEW_BOOK_LABEL]
    hints = [b.hint for b in books] + [""]
    try:
        idx = menu.select_option(f"Select target book ({abs_lib})", labels, hints)
    except menu.MenuUnavailable as e:
        con.dim(f"Arrow-key menu unavailable ({escape(str(e))}); using numbered list.")
        return _choose_book_numbered(books)
    if idx is None:
        return None
    return _NEW_BOOK if idx >= len(books) else books[idx].name


def _choose_book_numbered(books: list[BookInfo]):
    con.rule("Select Target Book")
    for idx, info in enumerate(books, start=1):
        hint = f"  [dim]{escape(info.hint)}[/dim]" if info.hint else ""
        con.console.print(f"  [cyan]{idx:2d}.[/cyan] {escape(info.name)}{hint}")
    con.console.print(f"  [cyan] n.[/cyan] {escape(NEW_BOOK_LABEL)}")
    names = [b.name for b in books]
    for _ in range(MAX_NUMBERED_ATTEMPTS):
        try:
            ans = con.console.input("\n[bold]Select book number ('n' for a new book, 'q' to quit): [/bold]").strip()
        except (KeyboardInterrupt, EOFError):
            return None
        low = ans.lower()
        if low in ("q", "quit"):
            return None
        if low in NEW_BOOK_ANSWERS and ans not in names:
            return _NEW_BOOK
        choice = _parse_book_answer(names, ans)
        if choice:
            return choice
        con.warn(f"Invalid selection. Enter a number between 1 and {len(names)}, a folder name, or 'n'.")
    return None


def _parse_book_answer(books: list[str], ans: str) -> str | None:
    if ans in books:
        return ans
    try:
        idx = int(ans) - 1
    except ValueError:
        return None
    return books[idx] if 0 <= idx < len(books) else None
