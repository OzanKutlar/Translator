"""Inline arrow-key selection menu built on prompt_toolkit.

Used by the startup book picker, the `book` command and the model picker.
Callers catch MenuUnavailable and fall back to a numbered prompt when there is
no real terminal (piped input, some IDE consoles) or prompt_toolkit is missing.
"""

import sys

try:
    from prompt_toolkit.application import Application
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import HSplit, Layout, Window
    from prompt_toolkit.layout.controls import FormattedTextControl
    from prompt_toolkit.patch_stdout import patch_stdout
    from prompt_toolkit.styles import Style
    HAS_PROMPT_TOOLKIT = True
except ImportError:
    HAS_PROMPT_TOOLKIT = False

VISIBLE_ROWS = 15
PAGE_JUMP = 10
FOOTER = "↑/↓ move · PgUp/PgDn jump · Enter select · Esc cancel"


class MenuUnavailable(RuntimeError):
    """Raised when the interactive menu cannot run in the current environment."""


def is_interactive() -> bool:
    """True when prompt_toolkit is installed and stdin/stdout are real terminals."""
    if not HAS_PROMPT_TOOLKIT:
        return False
    try:
        return bool(sys.stdin and sys.stdin.isatty() and sys.stdout and sys.stdout.isatty())
    except (AttributeError, ValueError):
        return False


class _MenuState:
    """Cursor position and scroll window for the menu."""

    def __init__(self, count: int, default_index: int):
        self.count = count
        valid = isinstance(default_index, int) and 0 <= default_index < count
        self.index = default_index if valid else 0
        self.top = 0
        self._scroll()

    def move(self, delta: int, wrap: bool = True) -> None:
        if wrap:
            self.index = (self.index + delta) % self.count
        else:
            self.index = max(0, min(self.count - 1, self.index + delta))
        self._scroll()

    def jump(self, index: int) -> None:
        self.index = max(0, min(self.count - 1, index))
        self._scroll()

    def _scroll(self) -> None:
        if self.index < self.top:
            self.top = self.index
        elif self.index >= self.top + VISIBLE_ROWS:
            self.top = self.index - VISIBLE_ROWS + 1


def _row(text: str, hint: str, selected: bool) -> list:
    if selected:
        row = [("class:pointer", " ❯ "), ("class:selected", text)]
    else:
        row = [("", "   "), ("class:item", text)]
    if hint:
        row.append(("class:hint", f"  {hint}"))
    row.append(("", "\n"))
    return row


def _render(title: str, labels: list[str], hints: list[str], state: _MenuState) -> list:
    fragments = [("class:title", f" {title}\n")]
    scrolling = state.count > VISIBLE_ROWS
    end = min(state.count, state.top + VISIBLE_ROWS)
    if scrolling:
        fragments.append(("class:dim", "   ↑ more\n" if state.top > 0 else "\n"))
    for i in range(state.top, end):
        fragments.extend(_row(labels[i], hints[i], i == state.index))
    if scrolling:
        fragments.append(("class:dim", "   ↓ more\n" if end < state.count else "\n"))
    fragments.append(("class:dim", f" {state.index + 1}/{state.count} · {FOOTER}"))
    return fragments


def _build_bindings(state: _MenuState) -> "KeyBindings":
    kb = KeyBindings()

    @kb.add("up")
    @kb.add("k")
    def _up(event):
        state.move(-1)

    @kb.add("down")
    @kb.add("j")
    def _down(event):
        state.move(1)

    @kb.add("pageup")
    def _page_up(event):
        state.move(-PAGE_JUMP, wrap=False)

    @kb.add("pagedown")
    def _page_down(event):
        state.move(PAGE_JUMP, wrap=False)

    @kb.add("home")
    def _home(event):
        state.jump(0)

    @kb.add("end")
    def _end(event):
        state.jump(state.count - 1)

    @kb.add("enter")
    def _select(event):
        event.app.exit(result=state.index)

    @kb.add("escape", eager=True)
    @kb.add("q")
    @kb.add("c-c")
    def _cancel(event):
        event.app.exit(result=None)

    return kb


def _menu_style() -> "Style":
    return Style.from_dict({
        "title": "bold ansicyan",
        "pointer": "bold ansicyan",
        "selected": "reverse bold",
        "item": "",
        "hint": "ansibrightblack",
        "dim": "ansibrightblack",
    })


def _normalise_hints(hints, count: int) -> list[str]:
    if hints is None:
        return [""] * count
    if len(hints) != count:
        raise ValueError("hints must have the same length as options")
    return [str(h) if h else "" for h in hints]


def _build_app(title: str, labels: list[str], hints: list[str], state: _MenuState) -> "Application":
    control = FormattedTextControl(
        lambda: _render(title, labels, hints, state), focusable=True, show_cursor=False
    )
    window = Window(content=control, dont_extend_height=True, always_hide_cursor=True)
    return Application(
        layout=Layout(HSplit([window])),
        key_bindings=_build_bindings(state),
        style=_menu_style(),
        full_screen=False,
        mouse_support=False,
        erase_when_done=True,
    )


def select_option(title: str, options: list, hints: list | None = None, default_index: int = 0) -> int | None:
    """Show an arrow-key menu and return the chosen index, or None if cancelled or empty.

    Raises MenuUnavailable when no interactive terminal is available, so callers can fall back.
    """
    if not options:
        return None
    labels = [str(o) for o in options]
    hint_list = _normalise_hints(hints, len(labels))
    if not is_interactive():
        raise MenuUnavailable("no interactive terminal")
    state = _MenuState(len(labels), default_index)
    app = _build_app(str(title), labels, hint_list, state)
    try:
        with patch_stdout(raw=True):
            result = app.run()
    except Exception as e:  # any terminal/UI start-up failure -> caller uses the numbered fallback
        raise MenuUnavailable(f"{type(e).__name__}: {e}") from e
    return result if isinstance(result, int) else None
