"""Progress bar formatting for live terminal status."""


def _make_bar(pct: float | None, width: int = 24) -> str:
    if pct is None:
        return "─" * width
    pct = max(0.0, min(1.0, pct))
    filled = int(round(pct * width))
    empty = width - filled
    return "█" * filled + "░" * empty


def render_bars(snapshot: dict, bar_width: int = 22) -> str:
    """Render two progress bar lines for Scraped and Translated states."""
    sc_done, sc_total, sc_pct = snapshot.get("scrape_progress", (0, None, None))
    tr_done, tr_total, tr_pct = snapshot.get("translate_progress", (0, None, None))

    curr_ch = snapshot.get("current_chapter")
    is_inf = snapshot.get("is_inferred", False)
    ch_prefix = "~" if is_inf else ""
    curr_str = f"ch {ch_prefix}{curr_ch}" if curr_ch is not None else "ch --"

    tr_ch = snapshot.get("highest_translated")
    tr_str = f"ch {tr_ch}" if tr_ch is not None else "ch --"

    goal = snapshot.get("goal_chapter")
    pending = snapshot.get("pending_goal_offset")

    # Line 1: Scraped
    sc_bar = _make_bar(sc_pct, bar_width)
    if sc_total is not None:
        sc_num = f"{sc_done}/{sc_total}"
        sc_pct_str = f"({int(round((sc_pct or 0) * 100)):>3d}%)"
    else:
        sc_num = f"{snapshot.get('total_pages_seen', 0)} pgs"
        sc_pct_str = f"[Goal: +{pending}]" if pending else "[No Goal]"

    # Line 2: Translated
    tr_bar = _make_bar(tr_pct, bar_width)
    if tr_total is not None:
        tr_num = f"{tr_done}/{tr_total}"
        tr_pct_str = f"({int(round((tr_pct or 0) * 100)):>3d}%)"
    else:
        tr_num = f"{snapshot.get('total_chapters_saved', 0)} chs"
        tr_pct_str = f"[Target: {goal}]" if goal else ""

    line1 = f" Scraped    {sc_bar}  {sc_num:<7}  {sc_pct_str:<6}  {curr_str}"
    line2 = f" Translated {tr_bar}  {tr_num:<7}  {tr_pct_str:<6}  {tr_str}"

    return f"{line1}\n{line2}"
