"""Progress bar formatting for live terminal status (plain text, no Rich markup)."""


def _make_bar(pct: float | None, width: int = 24) -> str:
    if pct is None:
        return "─" * width
    pct = max(0.0, min(1.0, pct))
    filled = int(round(pct * width))
    return "█" * filled + "░" * (width - filled)


def _pct_text(pct: float | None) -> str:
    return f"({int(round((pct or 0.0) * 100)):>3d}%)"


def _chapter_text(chapter: int | None, inferred: bool = False) -> str:
    if chapter is None:
        return "ch --"
    return f"ch {'~' if inferred else ''}{chapter}"


def _goal_line(snapshot: dict) -> str:
    """Describe the goal and where counting starts. Empty when no goal is set."""
    state = snapshot.get("goal_state", "none")
    goal = snapshot.get("goal_chapter")
    start = snapshot.get("session_start")
    if state == "pending":
        return f" Goal: next {snapshot.get('pending_goal_offset')} chapters · waiting for first chapter"
    if state == "waiting":
        return f" Goal: Chapter {goal} · waiting for first chapter"
    if state == "behind":
        return f" Goal: Chapter {goal} is before session start (Chapter {start})"
    if state == "active":
        return f" Goal: Chapter {goal} · counting from Chapter {start}"
    return ""


def _queue_line(queue) -> str:
    """One-line pool summary: pending / in-flight pages and Missing / Failed chapters."""
    if not isinstance(queue, dict):
        return ""
    parts = [
        f"Pending {queue.get('pending', 0)}",
        f"In-flight {queue.get('in_flight', 0)}",
        f"Missing {queue.get('missing', 0)}",
        f"Failed {queue.get('failed', 0)}",
    ]
    if queue.get("manual"):
        parts.append(f"Manual {queue.get('manual')}")
    return " Queue: " + " · ".join(parts)


def _bar_line(label: str, progress, fallback_num: str, fallback_extra: str, position: str, width: int) -> str:
    done, total, pct = progress
    if total is not None:
        num, extra = f"{done}/{total}", _pct_text(pct)
    else:
        num, extra = fallback_num, fallback_extra
    return f" {label:<10} {_make_bar(pct, width)}  {num:<9}  {extra:<9}  {position}"


def render_bars(snapshot: dict, bar_width: int = 22, queue: dict | None = None) -> str:
    """Render Scraped and Translated bars, a goal line when a goal exists, and the queue line."""
    if not isinstance(snapshot, dict):
        return ""
    has_goal = snapshot.get("goal_state", "none") != "none"
    scraped = _bar_line(
        "Scraped",
        snapshot.get("scrape_progress", (0, None, None)),
        f"{snapshot.get('total_pages_seen', 0)} pgs",
        "" if has_goal else "[No Goal]",
        _chapter_text(snapshot.get("current_chapter"), snapshot.get("is_inferred", False)),
        bar_width,
    )
    translated = _bar_line(
        "Translated",
        snapshot.get("translate_progress", (0, None, None)),
        f"{snapshot.get('total_chapters_saved', 0)} chs",
        "",
        _chapter_text(snapshot.get("highest_translated")),
        bar_width,
    )
    lines = [scraped, translated]
    goal_line = _goal_line(snapshot)
    if goal_line:
        lines.append(goal_line)
    queue_line = _queue_line(queue)
    if queue_line:
        lines.append(queue_line)
    return "\n".join(lines)
