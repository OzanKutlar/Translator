"""Book folder resolution and interactive selection."""

import json
import os

from rich.markup import escape

from . import console as con
from . import menu

MAX_NUMBERED_ATTEMPTS = 5


def discover_books(library_dir: str) -> list[str]:
    """Return sorted directory names inside the library folder."""
    abs_lib = os.path.abspath(library_dir)
    if not os.path.exists(abs_lib):
        return []
    return sorted(
        [
            d for d in os.listdir(abs_lib)
            if os.path.isdir(os.path.join(abs_lib, d)) and not d.startswith(".")
        ],
        key=str.lower,
    )


def ensure_book(book_dir: str) -> None:
    """Ensure target directory and Characters/Characters.json structure exists."""
    os.makedirs(book_dir, exist_ok=True)
    chars_dir = os.path.join(book_dir, "Characters")
    os.makedirs(chars_dir, exist_ok=True)
    chars_json = os.path.join(chars_dir, "Characters.json")
    if not os.path.exists(chars_json):
        with open(chars_json, "w", encoding="utf-8") as f:
            json.dump({}, f, ensure_ascii=False, indent=2)


def select_book(library_dir: str, preselected: str = None) -> tuple[str, str] | None:
    """Pick a book: -b preselection, single-book auto-select, otherwise an arrow-key menu.

    Returns (book_dir, book_name), or None if cancelled.
    """
    abs_lib = os.path.abspath(library_dir)
    books = discover_books(abs_lib)
    if not books:
        con.fail(f"No book directories found in library: [cyan]{escape(abs_lib)}[/cyan]")
        con.info("Please create a folder for your novel first.")
        return None

    if preselected:
        resolved = _resolve_preselected(abs_lib, books, preselected)
        if resolved:
            return resolved

    if len(books) == 1:
        con.info(f"Auto-selected single book in library: [bold green]{escape(books[0])}[/bold green]")
        return _open_book(abs_lib, books[0])

    name = _choose_book(abs_lib, books)
    return _open_book(abs_lib, name) if name else None


def _open_book(abs_lib: str, name: str) -> tuple[str, str]:
    path = os.path.join(abs_lib, name)
    ensure_book(path)
    return path, name


def _resolve_preselected(abs_lib: str, books: list[str], preselected: str) -> tuple[str, str] | None:
    if preselected in books:
        return _open_book(abs_lib, preselected)
    if os.path.isdir(preselected):
        path = os.path.abspath(preselected)
        ensure_book(path)
        return path, os.path.basename(path)
    con.warn(f"Book '{escape(preselected)}' not found in {escape(abs_lib)}")
    return None


def _choose_book(abs_lib: str, books: list[str]) -> str | None:
    """Arrow-key menu, falling back to the numbered prompt without a terminal."""
    try:
        idx = menu.select_option(f"Select target book ({abs_lib})", books)
    except menu.MenuUnavailable as e:
        con.dim(f"Arrow-key menu unavailable ({escape(str(e))}); using numbered list.")
        return _choose_book_numbered(books)
    return books[idx] if idx is not None else None


def _choose_book_numbered(books: list[str]) -> str | None:
    con.rule("Select Target Book")
    for idx, name in enumerate(books, start=1):
        con.console.print(f"  [cyan]{idx:2d}.[/cyan] {escape(name)}")
    for _ in range(MAX_NUMBERED_ATTEMPTS):
        try:
            ans = con.console.input("\n[bold]Select book number (or 'q' to quit): [/bold]").strip()
        except (KeyboardInterrupt, EOFError):
            return None
        if ans.lower() in ("q", "quit"):
            return None
        choice = _parse_book_answer(books, ans)
        if choice:
            return choice
        con.warn(f"Invalid selection. Enter a number between 1 and {len(books)} or a folder name.")
    return None


def _parse_book_answer(books: list[str], ans: str) -> str | None:
    if ans in books:
        return ans
    try:
        idx = int(ans) - 1
    except ValueError:
        return None
    return books[idx] if 0 <= idx < len(books) else None
