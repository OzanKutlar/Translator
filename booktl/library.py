"""Book folder resolution and interactive selection."""

import json
import os
from . import console as con


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
    """Interactive numbered selector for books. Returns (book_dir, book_name) or None."""
    abs_lib = os.path.abspath(library_dir)
    books = discover_books(abs_lib)

    if not books:
        con.fail(f"No book directories found in library: [cyan]{abs_lib}[/cyan]")
        con.info("Please create a folder for your novel first.")
        return None

    if preselected:
        if preselected in books:
            book_path = os.path.join(abs_lib, preselected)
            ensure_book(book_path)
            return book_path, preselected
        elif os.path.isdir(preselected):
            ensure_book(preselected)
            return os.path.abspath(preselected), os.path.basename(os.path.abspath(preselected))
        else:
            con.warn(f"Book '{preselected}' not found in {abs_lib}")

    if len(books) == 1:
        book_name = books[0]
        book_path = os.path.join(abs_lib, book_name)
        ensure_book(book_path)
        con.info(f"Auto-selected single book in library: [bold green]{book_name}[/bold green]")
        return book_path, book_name

    con.rule("Select Target Book")
    for idx, name in enumerate(books, start=1):
        con.console.print(f"  [cyan]{idx:2d}.[/cyan] {name}")

    for _ in range(5):
        ans = con.console.input("\n[bold]Select book number (or 'q' to quit): [/bold]").strip()
        if ans.lower() in ("q", "quit"):
            return None
        try:
            choice = int(ans) - 1
            if 0 <= choice < len(books):
                book_name = books[choice]
                book_path = os.path.join(abs_lib, book_name)
                ensure_book(book_path)
                return book_path, book_name
            con.warn(f"Please enter a number between 1 and {len(books)}.")
        except ValueError:
            if ans in books:
                book_path = os.path.join(abs_lib, ans)
                ensure_book(book_path)
                return book_path, ans
            con.warn("Invalid selection. Enter a number or folder name.")

    return None
