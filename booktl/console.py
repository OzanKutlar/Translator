"""Thread-safe Rich console logging helpers."""

import threading
from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule

console = Console()
_lock = threading.RLock()


def ok(message: str) -> None:
    with _lock:
        console.print(f"[green]✔[/green] {message}")


def info(message: str) -> None:
    with _lock:
        console.print(f"[cyan]ℹ[/cyan] {message}")


def warn(message: str) -> None:
    with _lock:
        console.print(f"[yellow]⚠[/yellow] {message}")


def fail(message: str) -> None:
    with _lock:
        console.print(f"[bold red]✘[/bold red] {message}")


def rule(title: str = "") -> None:
    with _lock:
        console.print(Rule(f"[bold blue]{title}[/bold blue]" if title else ""))


def banner(title: str, subtitle: str = "") -> None:
    text = f"[bold]{title}[/bold]"
    if subtitle:
        text += f"\n[dim]{subtitle}[/dim]"
    with _lock:
        console.print(Panel(text, border_style="blue"))
def dim(message: str) -> None:
    with _lock:
        console.print(f"[dim]{message}[/dim]")


def panel(message: str, title: str = "", style: str = "green") -> None:
    with _lock:
        console.print(Panel(message, title=title if title else None, border_style=style))
