"""In-memory chapter progress tracking and goal state."""

from . import console as con
from .parsing import parse_chapter_number

MAX_CONSECUTIVE_INFERENCES = 25


class ChapterTracker:
    """Tracks current/highest scraped and translated chapter positions."""

    def __init__(self):
        self.current_chapter: int | None = None
        self.highest_scraped: int | None = None
        self.highest_translated: int | None = None
        self.is_inferred: bool = False
        self.consecutive_inferences: int = 0
        self.total_pages_seen: int = 0
        self.total_chapters_saved: int = 0

        self.goal_chapter: int | None = None
        self.goal_origin: int | None = None
        self.pending_goal_offset: int | None = None
        self.goal_reached_flag: bool = False

    def reset(self) -> None:
        """Reset state for switching books."""
        self.current_chapter = None
        self.highest_scraped = None
        self.highest_translated = None
        self.is_inferred = False
        self.consecutive_inferences = 0
        self.total_pages_seen = 0
        self.total_chapters_saved = 0
        self.goal_chapter = None
        self.goal_origin = None
        self.pending_goal_offset = None
        self.goal_reached_flag = False

    def set_goal(self, target: str | int | None) -> str:
        """Configure absolute or relative (+N) chapter goal."""
        if target is None or str(target).strip().lower() in ("none", "clear", "off", "0", ""):
            self.goal_chapter = None
            self.goal_origin = None
            self.pending_goal_offset = None
            self.goal_reached_flag = False
            return "Goal cleared."

        raw = str(target).strip()
        if raw.startswith("+"):
            try:
                offset = int(raw[1:])
                if offset <= 0:
                    return "Relative goal offset must be greater than 0."
            except ValueError:
                return f"Invalid relative goal '{raw}'."

            if self.current_chapter is not None:
                self.goal_origin = self.current_chapter
                self.goal_chapter = self.current_chapter + offset
                self.pending_goal_offset = None
                self.goal_reached_flag = False
                return f"Goal set to Chapter {self.goal_chapter} (+{offset} chapters from current {self.current_chapter})."
            else:
                self.pending_goal_offset = offset
                return f"Pending relative goal of +{offset} chapters set (will activate on first chapter detected)."

        try:
            abs_val = int(raw)
            if abs_val <= 0:
                return "Goal chapter must be a positive number."
            self.goal_chapter = abs_val
            self.goal_origin = self.current_chapter or abs_val
            self.pending_goal_offset = None
            self.goal_reached_flag = False
            msg = f"Goal set to Chapter {self.goal_chapter}."
            if self.current_chapter is not None and abs_val <= self.current_chapter:
                msg += f" (Note: already at or past Chapter {self.current_chapter})"
            return msg
        except ValueError:
            return f"Invalid goal value '{raw}'. Use an integer or '+N'."

    def _resolve_pending_goal_if_needed(self) -> None:
        if self.pending_goal_offset is not None and self.current_chapter is not None:
            self.goal_origin = self.current_chapter
            self.goal_chapter = self.current_chapter + self.pending_goal_offset
            offset = self.pending_goal_offset
            self.pending_goal_offset = None
            con.ok(f"Activated pending goal: [bold green]Chapter {self.goal_chapter}[/bold green] (+{offset} chapters).")

    def observe_page(self, title: str | None, content: str) -> tuple[int | None, str]:
        """Process incoming raw page and return (detected_chapter_number, confidence)."""
        self.total_pages_seen += 1

        # 1. Parse from document title
        parsed_num = parse_chapter_number(title) if title else None
        # 2. Parse from first ~250 chars of content
        if parsed_num is None:
            parsed_num = parse_chapter_number(content)

        confidence = "parsed"
        if parsed_num is not None:
            if self.is_inferred and self.current_chapter is not None and abs(parsed_num - self.current_chapter) > 1:
                con.dim(f"Chapter counter resynced from ~{self.current_chapter} to {parsed_num}")
            self.current_chapter = parsed_num
            self.is_inferred = False
            self.consecutive_inferences = 0
        else:
            # 3. Fallback sequential inference
            if self.current_chapter is not None and self.consecutive_inferences < MAX_CONSECUTIVE_INFERENCES:
                self.current_chapter += 1
                self.is_inferred = True
                self.consecutive_inferences += 1
                confidence = "inferred"
            else:
                self.is_inferred = True
                confidence = "unknown"
                if self.consecutive_inferences >= MAX_CONSECUTIVE_INFERENCES:
                    con.warn("Chapter inference halted after 25 consecutive unparsed pages.")

        if self.current_chapter is not None:
            if self.highest_scraped is None or self.current_chapter > self.highest_scraped:
                self.highest_scraped = self.current_chapter

        self._resolve_pending_goal_if_needed()
        return self.current_chapter, confidence

    def observe_translated(self, chapter_identifier: str) -> int | None:
        """Record translated chapter output produced by the LLM."""
        self.total_chapters_saved += 1
        num = parse_chapter_number(chapter_identifier)
        if num is not None:
            if self.highest_translated is None or num > self.highest_translated:
                self.highest_translated = num
        return num

    def goal_reached(self) -> bool:
        """Check if scraper has ingested the target goal chapter."""
        if self.goal_chapter is None or self.current_chapter is None:
            return False
        return self.current_chapter >= self.goal_chapter

    def translated_goal_reached(self) -> bool:
        """Check if translated chapters have reached or surpassed the goal."""
        if self.goal_chapter is None or self.highest_translated is None:
            return False
        return self.highest_translated >= self.goal_chapter

    def scrape_progress(self) -> tuple[int, int | None, float | None]:
        """Return (done, total, pct) for scraping."""
        if self.goal_chapter is None or self.goal_origin is None:
            return self.total_pages_seen, None, None
        total = max(1, self.goal_chapter - self.goal_origin)
        current = (self.current_chapter - self.goal_origin) if self.current_chapter is not None else 0
        done = max(0, min(current, total))
        pct = min(1.0, done / total)
        return done, total, pct

    def translate_progress(self) -> tuple[int, int | None, float | None]:
        """Return (done, total, pct) for translation."""
        if self.goal_chapter is None or self.goal_origin is None:
            return self.total_chapters_saved, None, None
        total = max(1, self.goal_chapter - self.goal_origin)
        current = (self.highest_translated - self.goal_origin) if self.highest_translated is not None else 0
        done = max(0, min(current, total))
        pct = min(1.0, done / total)
        return done, total, pct

    def snapshot(self) -> dict:
        """Snapshot of current tracker metrics."""
        return {
            "current_chapter": self.current_chapter,
            "highest_scraped": self.highest_scraped,
            "highest_translated": self.highest_translated,
            "is_inferred": self.is_inferred,
            "goal_chapter": self.goal_chapter,
            "goal_origin": self.goal_origin,
            "pending_goal_offset": self.pending_goal_offset,
            "goal_reached": self.goal_reached(),
            "scrape_progress": self.scrape_progress(),
            "translate_progress": self.translate_progress(),
            "total_pages_seen": self.total_pages_seen,
            "total_chapters_saved": self.total_chapters_saved,
        }
