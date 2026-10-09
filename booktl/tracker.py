"""In-memory chapter progress tracking and goal state."""

import threading

from . import console as con
from .parsing import parse_chapter_number

MAX_CONSECUTIVE_INFERENCES = 25
CLEAR_GOAL_VALUES = ("none", "clear", "off", "0", "")


class ChapterTracker:
    """Tracks scraped and translated chapters for the current session, plus the stop goal.

    Goal progress counts the distinct chapter numbers handled this session that fall
    between the session's first scraped chapter and the goal chapter, both inclusive.
    """

    def __init__(self):
        self._lock = threading.RLock()
        self.reset()

    def reset(self) -> None:
        """Reset all session state (used at startup and when switching books)."""
        with self._lock:
            self.current_chapter: int | None = None
            self.highest_scraped: int | None = None
            self.highest_translated: int | None = None
            self.is_inferred = False
            self.consecutive_inferences = 0
            self.total_pages_seen = 0
            self.total_chapters_saved = 0
            self.session_start: int | None = None
            self.scraped_chapters: set[int] = set()
            self.translated_chapters: set[int] = set()
            self.goal_chapter: int | None = None
            self.pending_goal_offset: int | None = None
            self.goal_reached_flag = False

    # ------------------------------------------------------------------
    # Goal configuration
    # ------------------------------------------------------------------

    def set_goal(self, target: str | int | None) -> str:
        """Configure an absolute (N) or relative (+N) stop chapter, or clear it."""
        raw = "" if target is None else str(target).strip()
        with self._lock:
            if raw.lower() in CLEAR_GOAL_VALUES:
                self._clear_goal()
                return "Goal cleared."
            if raw.startswith("+"):
                return self._set_relative_goal(raw)
            return self._set_absolute_goal(raw)

    def _clear_goal(self) -> None:
        self.goal_chapter = None
        self.pending_goal_offset = None
        self.goal_reached_flag = False

    def _set_relative_goal(self, raw: str) -> str:
        try:
            offset = int(raw[1:])
        except ValueError:
            return f"Invalid relative goal '{raw}'. Use '+N'."
        if offset <= 0:
            return "Relative goal offset must be greater than 0."
        if self.current_chapter is None:
            self.goal_chapter = None
            self.pending_goal_offset = offset
            self.goal_reached_flag = False
            return f"Pending goal of {offset} chapters set (counting starts at the first scraped chapter)."
        self.pending_goal_offset = None
        detail = f" (+{offset} after current Chapter {self.current_chapter})"
        return self._apply_goal(self.current_chapter + offset, detail)

    def _set_absolute_goal(self, raw: str) -> str:
        try:
            value = int(raw)
        except ValueError:
            return f"Invalid goal value '{raw}'. Use an integer, '+N', or 'none'."
        if value <= 0:
            return "Goal chapter must be a positive number."
        self.pending_goal_offset = None
        return self._apply_goal(value, "")

    def _apply_goal(self, chapter: int, detail: str) -> str:
        self.goal_chapter = chapter
        self.goal_reached_flag = False
        msg = f"Goal set to Chapter {chapter}{detail}."
        if self.session_start is None:
            return msg + " Progress will count from the first chapter scraped."
        if chapter < self.session_start:
            return msg + f" Warning: this is before the session's first chapter ({self.session_start}), so the scraper will stop."
        msg += f" Counting from session start (Chapter {self.session_start})."
        if self.current_chapter is not None and chapter <= self.current_chapter:
            msg += f" Note: already at or past it (current Chapter {self.current_chapter})."
        return msg

    def _resolve_pending_goal_if_needed(self) -> None:
        """Activate a +N goal set before any chapter was seen; the first chapter counts as 1 of N."""
        if self.pending_goal_offset is None or self.current_chapter is None:
            return
        offset = self.pending_goal_offset
        self.pending_goal_offset = None
        self.goal_chapter = self.current_chapter + offset - 1
        self.goal_reached_flag = False
        con.ok(
            f"Activated pending goal: [bold green]Chapter {self.goal_chapter}[/bold green] "
            f"({offset} chapters starting at Chapter {self.current_chapter})."
        )

    # ------------------------------------------------------------------
    # Observations
    # ------------------------------------------------------------------

    def observe_page(self, title: str | None, content: str) -> tuple[int | None, str]:
        """Process an incoming raw page. Returns (chapter_number, confidence)."""
        parsed = parse_chapter_number(title) if title else None
        if parsed is None:
            parsed = parse_chapter_number(content)
        with self._lock:
            self.total_pages_seen += 1
            confidence = self._update_position(parsed)
            if confidence != "unknown" and self.current_chapter is not None:
                self._record_scraped(self.current_chapter)
            self._resolve_pending_goal_if_needed()
            return self.current_chapter, confidence

    def _update_position(self, parsed: int | None) -> str:
        """Advance current_chapter from a parsed number, or infer it. Returns the confidence."""
        if parsed is None:
            return self._infer_next()
        if self.is_inferred and self.current_chapter is not None and abs(parsed - self.current_chapter) > 1:
            con.dim(f"Chapter counter resynced from ~{self.current_chapter} to {parsed}")
        self.current_chapter = parsed
        self.is_inferred = False
        self.consecutive_inferences = 0
        return "parsed"

    def _infer_next(self) -> str:
        """Sequentially infer the next chapter when a page has no readable number."""
        self.is_inferred = True
        if self.current_chapter is None:
            return "unknown"
        if self.consecutive_inferences >= MAX_CONSECUTIVE_INFERENCES:
            con.warn(f"Chapter inference halted after {MAX_CONSECUTIVE_INFERENCES} consecutive unparsed pages.")
            return "unknown"
        self.current_chapter += 1
        self.consecutive_inferences += 1
        return "inferred"

    def _record_scraped(self, chapter: int) -> None:
        if self.session_start is None:
            self.session_start = chapter
        self.scraped_chapters.add(chapter)
        if self.highest_scraped is None or chapter > self.highest_scraped:
            self.highest_scraped = chapter

    def observe_translated(self, chapter_identifier: str) -> int | None:
        """Record a translated chapter produced by the LLM. Returns its number if readable."""
        num = parse_chapter_number(str(chapter_identifier)) if chapter_identifier else None
        with self._lock:
            self.total_chapters_saved += 1
            if num is None:
                return None
            self.translated_chapters.add(num)
            if self.highest_translated is None or num > self.highest_translated:
                self.highest_translated = num
            return num

    # ------------------------------------------------------------------
    # Goal checks and progress
    # ------------------------------------------------------------------

    def goal_state(self) -> str:
        """One of: none, pending (+N waiting), waiting (no chapter yet), behind, active."""
        with self._lock:
            if self.pending_goal_offset is not None:
                return "pending"
            if self.goal_chapter is None:
                return "none"
            if self.session_start is None:
                return "waiting"
            if self.goal_chapter < self.session_start:
                return "behind"
            return "active"

    def goal_reached(self) -> bool:
        """Check if the scraper has ingested the goal chapter."""
        with self._lock:
            if self.goal_chapter is None or self.current_chapter is None:
                return False
            return self.current_chapter >= self.goal_chapter

    def _progress(self, chapters: set[int]) -> tuple[int, int | None, float | None]:
        """Inclusive count of session chapters in [session_start, goal]."""
        if self.goal_state() != "active":
            return 0, None, None
        start, goal = self.session_start, self.goal_chapter
        total = goal - start + 1
        done = sum(1 for ch in chapters if start <= ch <= goal)
        return done, total, done / total

    def scrape_progress(self) -> tuple[int, int | None, float | None]:
        """Return (done, total, pct) for scraping."""
        with self._lock:
            return self._progress(self.scraped_chapters)

    def translate_progress(self) -> tuple[int, int | None, float | None]:
        """Return (done, total, pct) for translation."""
        with self._lock:
            return self._progress(self.translated_chapters)

    def snapshot(self) -> dict:
        """Consistent snapshot of tracker metrics."""
        with self._lock:
            return {
                "current_chapter": self.current_chapter,
                "highest_scraped": self.highest_scraped,
                "highest_translated": self.highest_translated,
                "is_inferred": self.is_inferred,
                "session_start": self.session_start,
                "goal_chapter": self.goal_chapter,
                "pending_goal_offset": self.pending_goal_offset,
                "goal_state": self.goal_state(),
                "goal_reached": self.goal_reached(),
                "scrape_progress": self.scrape_progress(),
                "translate_progress": self.translate_progress(),
                "total_pages_seen": self.total_pages_seen,
                "total_chapters_saved": self.total_chapters_saved,
                "scraped_count": len(self.scraped_chapters),
                "translated_count": len(self.translated_chapters),
            }
