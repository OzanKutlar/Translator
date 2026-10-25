"""Grouped log output for one batch: a headline with indented detail lines, printed together.

Workers can finish batches at the same time. Collecting a batch's lines and printing them in
one go keeps the output of different batches from interleaving.
"""

from . import console as con

MAX_DETAILS = 40          # bounds one block; more lines than this means a bug upstream
INDENT = "   "
DEFAULT_HEAD = ("info", "Finished")
_HEAD_ICONS = {
    "ok": "[green]✔[/green]",
    "info": "[cyan]ℹ[/cyan]",
    "warn": "[yellow]⚠[/yellow]",
    "fail": "[bold red]✘[/bold red]",
}
_DETAIL_ICONS = {
    "info": "",
    "warn": "[yellow]⚠[/yellow] ",
    "fail": "[bold red]✘[/bold red] ",
}


class BatchLog:
    """Collects one batch's log lines. Text is Rich markup; callers escape model and user text."""

    def __init__(self, label: str):
        if not isinstance(label, str) or not label.strip():
            raise ValueError("label must be a non-empty string")
        self.label = label.strip()
        self._head = DEFAULT_HEAD
        self._details: list[tuple[str, str]] = []
        self._dropped = 0

    def head(self, level: str, text: str) -> None:
        """Set the headline (the one line with an icon), e.g. ('ok', 'Saved 15 chapters')."""
        if level not in _HEAD_ICONS:
            raise ValueError(f"unknown level {level!r}")
        self._head = (level, str(text))

    def detail(self, text) -> None:
        self._add("info", text)

    def warn(self, text) -> None:
        self._add("warn", text)

    def fail(self, text) -> None:
        self._add("fail", text)

    def _add(self, level: str, text) -> None:
        if text is None or not str(text).strip():
            return
        if len(self._details) >= MAX_DETAILS:
            self._dropped += 1
            return
        self._details.append((level, str(text)))

    def lines(self) -> list[str]:
        """The headline followed by the indented detail lines, as Rich markup."""
        level, text = self._head
        result = [f"{_HEAD_ICONS[level]} {self.label}: {text}"]
        result.extend(f"{INDENT}{_DETAIL_ICONS[lvl]}{msg}" for lvl, msg in self._details)
        if self._dropped:
            result.append(f"{INDENT}[dim]… {self._dropped} more line(s) not shown[/dim]")
        return result

    def emit(self) -> None:
        con.block(self.lines())
