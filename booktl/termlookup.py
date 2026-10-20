"""Prior-usage lookup for glossary candidates.

Before a batch is translated, a short "term scout" call lists the glossary terms the model
plans to register. For each candidate this module searches the stored raw pages of chapters
that already have a translation (_raw/chapters/ch_000300.txt next to "Chapter 300.md"),
estimates where the term sits in the chapter and cuts out the matching English passage, so
the model can reuse the earlier rendering instead of coining a second one.

Positions are estimated in two steps:
  1. The hit's position as a fraction of the cleaned raw text is mapped onto the cleaned
     English text (site header/footer, TN paragraphs and the next-chapter link removed).
  2. Names and locked terms found next to the hit ("anchors") are looked for in English
     windows around that estimate; the window with the most anchors wins, the closest on ties.

Each excerpt carries surrounding context so the model can see how the term is used:
the Chinese line with the hit plus up to ZH_CONTEXT_LINES lines on each side, and the English
paragraph at the estimate plus up to EN_CONTEXT_PARAGRAPHS paragraphs on each side. A side stops
growing at the first paragraph that would exceed the excerpt's character limit.
"""

import bisect
import os
import re
import threading
from dataclasses import dataclass, field

from . import characters, glossary, storage
from .parsing import parse_chapter_number

MAX_CANDIDATES = 15            # candidates looked up per batch
MAX_OCCURRENCES = 3            # excerpts per term: earliest, most recent, spread in between
AUDIT_OCCURRENCES = 5          # excerpts shown by 'terms audit'
MAX_CHAPTER_SCAN = 50_000      # bounds every directory scan and chapter loop
MAX_PARAGRAPHS = 4_000         # bounds paragraph splitting of one chapter
MAX_CACHED_CHARS = 40_000_000  # cleaned raw text kept in memory; beyond this pages are re-read
EXCERPT_CHARS = 1_600          # English characters per excerpt
EN_CONTEXT_PARAGRAPHS = 4      # English paragraphs kept on each side of the matched one
ZH_EXCERPT_CHARS = 700         # Chinese characters per excerpt
ZH_CONTEXT_LINES = 3           # Chinese lines kept on each side of the line with the hit
CONTEXT_BUDGET = 20_000         # characters of PRIOR USAGE text sent with one batch
ANCHOR_SPREAD = 0.15           # English windows within +-15% of the estimate are scored
ANCHOR_RADIUS = 2              # paragraphs on each side used for anchors and windows
MAX_ANCHORS = 30
MIN_ANCHOR_LENGTH = 2
HEADER_SCAN_LINES = 8          # the chapter title line is looked for in the first lines only
MAX_FIELD_LENGTH = 120
MAX_WARNINGS = 20

_HEADER_RE = re.compile(r"第\s*[零〇一二两三四五六七八九十百千万\d]+\s*[章回节集篇]")
# Everything from the first line matching one of these is site text, not story.
END_MARKERS = (
    re.compile(r"^[（(]?\s*本章完\s*[)）]?$"),
)
# Single lines dropped anywhere in the page.
BOILERPLATE = (
    re.compile(r"69书吧|69shu|https?://|www\.", re.IGNORECASE),
)
_TN_RE = re.compile(r"^\(\s*TN\s*:", re.IGNORECASE)
_LINK_RE = re.compile(r"^\[\[[^\]]*\]\]$")


@dataclass(frozen=True)
class Candidate:
    """A term the scout plans to register."""

    chinese: str
    english: str   # the scout's proposed rendering, may be ""
    type: str


@dataclass(frozen=True)
class Occurrence:
    """One earlier use of a term: the Chinese lines around it and the matching English passage."""

    chapter: int
    zh: str                    # line with the hit plus neighbouring lines, joined by '\n'
    en: str                    # matched paragraph plus neighbouring paragraphs, joined by '\n'
    anchors: tuple[str, ...]   # English names/terms that confirmed the passage


@dataclass(frozen=True)
class TermUsage:
    """Every searched chapter containing a term, plus excerpts from a few of them."""

    candidate: Candidate
    chapters: tuple[int, ...]           # ascending
    occurrences: tuple[Occurrence, ...]


@dataclass
class LookupResult:
    """Outcome of one batch's lookup. Empty by default, which means 'no lookup'."""

    candidates: int = 0                              # candidates left after filtering
    usages: list = field(default_factory=list)       # TermUsage entries included in context
    context: str = ""                                # PRIOR USAGE body for the prompt
    first_seen: dict = field(default_factory=dict)   # chinese -> earliest chapter
    truncated: int = 0                               # usages left out by CONTEXT_BUDGET
    warnings: list = field(default_factory=list)


# ----------------------------------------------------------------------
# Text helpers
# ----------------------------------------------------------------------

def _clean_field(value) -> str:
    if value is None or isinstance(value, (dict, list)):
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()[:MAX_FIELD_LENGTH]


def _warn(warnings: list, message: str) -> None:
    if len(warnings) < MAX_WARNINGS:
        warnings.append(message)


def _read_text(path: str) -> str:
    """Read a UTF-8 file. Raises OSError / UnicodeDecodeError for the caller to report."""
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _lines(text) -> list[str]:
    if not isinstance(text, str):
        return []
    stripped = (line.strip() for line in text.splitlines())
    return [line for line in stripped if line][:MAX_PARAGRAPHS]


def _header_index(lines: list[str]) -> int:
    for index, line in enumerate(lines[:HEADER_SCAN_LINES]):
        if _HEADER_RE.search(line):
            return index
    return 0


def clean_raw(text) -> list[str]:
    """Story paragraphs of a raw page: from the chapter title line up to the end marker, minus site lines."""
    lines = _lines(text)
    body = []
    for line in lines[_header_index(lines):]:
        if any(pattern.search(line) for pattern in END_MARKERS):
            break
        if not any(pattern.search(line) for pattern in BOILERPLATE):
            body.append(line)
    return body


def clean_english(text) -> list[str]:
    """Paragraphs of a translated chapter without TN paragraphs and the next-chapter link."""
    return [line for line in _lines(text) if not _TN_RE.match(line) and not _LINK_RE.match(line)]


# ----------------------------------------------------------------------
# Position estimate and anchors
# ----------------------------------------------------------------------

def _starts(paragraphs: list[str]) -> tuple[list[int], int]:
    """Start offset of every paragraph and the total length (paragraphs joined by one char)."""
    starts, total = [], 0
    for paragraph in paragraphs:
        starts.append(total)
        total += len(paragraph) + 1
    return starts, total


def _paragraph_at(starts: list[int], position: float) -> int:
    index = bisect.bisect_right(starts, position) - 1
    return max(0, min(len(starts) - 1, index))


def _find_in_paragraphs(paragraphs: list[str], term: str) -> tuple[int | None, int]:
    for index, paragraph in enumerate(paragraphs):
        offset = paragraph.find(term)
        if offset >= 0:
            return index, offset
    return None, -1


def _fraction(paragraphs: list[str], index: int, offset: int) -> float:
    starts, total = _starts(paragraphs)
    return (starts[index] + offset) / total if total else 0.0


def _anchors_near(paragraphs: list[str], index: int, term: str, anchors: dict) -> tuple[str, ...]:
    """English renderings of the known names/terms that appear next to the hit."""
    text = "\n".join(paragraphs[max(0, index - ANCHOR_RADIUS): index + ANCHOR_RADIUS + 1])
    found = []
    for chinese, english in anchors.items():
        if len(found) >= MAX_ANCHORS:
            break
        if not english or chinese in term or term in chinese or chinese not in text:
            continue
        if english not in found:
            found.append(english)
    return tuple(found)


def _window_text(paragraphs: list[str], index: int) -> str:
    return "\n".join(paragraphs[max(0, index - ANCHOR_RADIUS): index + ANCHOR_RADIUS + 1]).casefold()


def _anchors_in(text: str, anchors) -> tuple[str, ...]:
    return tuple(anchor for anchor in anchors if anchor.casefold() in text)


def _best_window(paragraphs: list[str], fraction: float, anchors) -> tuple[int, tuple[str, ...]]:
    """Index of the English paragraph to centre the excerpt on, plus the anchors found there."""
    starts, total = _starts(paragraphs)
    estimate = _paragraph_at(starts, fraction * total)
    if not anchors:
        return estimate, ()
    low = _paragraph_at(starts, max(0.0, fraction - ANCHOR_SPREAD) * total)
    high = _paragraph_at(starts, min(1.0, fraction + ANCHOR_SPREAD) * total)
    best, best_hits = estimate, ()
    for index in range(low, high + 1):
        hits = _anchors_in(_window_text(paragraphs, index), anchors)
        more = len(hits) > len(best_hits)
        closer = bool(hits) and len(hits) == len(best_hits) and abs(index - estimate) < abs(best - estimate)
        if more or closer:
            best, best_hits = index, hits
    return best, best_hits


def _trim_around(text: str, limit: int) -> str:
    """Cut equally from both ends so the middle of the passage survives."""
    if len(text) <= limit:
        return text
    excess = len(text) - limit
    head = excess // 2
    return "…" + text[head: len(text) - (excess - head)].strip() + "…"


def _trim_on_hit(paragraph: str, offset: int, length: int, limit: int) -> str:
    """Cut a long paragraph to about limit characters, keeping the hit near the middle."""
    if len(paragraph) <= limit:
        return paragraph
    start = max(0, min(offset + length // 2 - limit // 2, len(paragraph) - limit))
    end = start + limit
    head = "…" if start > 0 else ""
    tail = "…" if end < len(paragraph) else ""
    return head + paragraph[start:end].strip() + tail


def _context_window(paragraphs: list[str], center: int, center_text: str, radius: int, limit: int) -> str:
    """center_text plus whole neighbouring paragraphs, up to radius per side and limit characters.

    A side stops growing at the first paragraph that would exceed limit, so the window has no gaps.
    """
    if not paragraphs or not 0 <= center < len(paragraphs):
        return center_text
    picked = {center: center_text}
    size = len(center_text)
    open_sides = [-1, 1]
    for step in range(1, max(0, radius) + 1):
        for side in tuple(open_sides):
            index = center + side * step
            if not 0 <= index < len(paragraphs) or size + len(paragraphs[index]) + 1 > limit:
                open_sides.remove(side)
                continue
            picked[index] = paragraphs[index]
            size += len(paragraphs[index]) + 1
        if not open_sides:
            break
    return "\n".join(picked[i] for i in sorted(picked))


def _excerpt(paragraphs: list[str], center: int) -> str:
    """The English paragraph at center plus up to EN_CONTEXT_PARAGRAPHS paragraphs on each side."""
    center_text = _trim_around(paragraphs[center], EXCERPT_CHARS)
    return _context_window(paragraphs, center, center_text, EN_CONTEXT_PARAGRAPHS, EXCERPT_CHARS)


def _zh_excerpt(paragraphs: list[str], index: int, offset: int, length: int) -> str:
    """The Chinese line with the hit plus up to ZH_CONTEXT_LINES lines on each side."""
    center_text = _trim_on_hit(paragraphs[index], offset, length, ZH_EXCERPT_CHARS)
    return _context_window(paragraphs, index, center_text, ZH_CONTEXT_LINES, ZH_EXCERPT_CHARS)


# ----------------------------------------------------------------------
# Corpus of stored raw pages
# ----------------------------------------------------------------------

class TermCorpus:
    """Cleaned raw chapter bodies, cached by modification time. Safe for concurrent workers."""

    def __init__(self):
        self._lock = threading.Lock()
        self._book_dir = ""
        self._bodies: dict[int, tuple[float, str]] = {}
        self._cached_chars = 0

    def clear(self) -> None:
        with self._lock:
            self._book_dir = ""
            self._bodies = {}
            self._cached_chars = 0

    def _select_book_locked(self, book_dir: str) -> None:
        if book_dir != self._book_dir:
            self._book_dir = book_dir
            self._bodies = {}
            self._cached_chars = 0

    def _store_locked(self, book_dir: str, chapter: int, stamp: float, body: str) -> None:
        if book_dir != self._book_dir:
            return
        old = self._bodies.pop(chapter, None)
        if old is not None:
            self._cached_chars -= len(old[1])
        if self._cached_chars + len(body) <= MAX_CACHED_CHARS:
            self._bodies[chapter] = (stamp, body)
            self._cached_chars += len(body)

    def body(self, book_dir: str, chapter: int) -> str | None:
        """Cleaned story text of a stored raw page ('\\n' between paragraphs), None if absent.

        Raises OSError / UnicodeDecodeError when the file exists but cannot be read.
        """
        path = storage.chapter_page_path(book_dir, chapter)
        try:
            stamp = os.stat(path).st_mtime
        except FileNotFoundError:
            return None
        with self._lock:
            self._select_book_locked(book_dir)
            cached = self._bodies.get(chapter)
            if cached is not None and cached[0] == stamp:
                return cached[1]
        body = "\n".join(clean_raw(_read_text(path)))
        with self._lock:
            self._store_locked(book_dir, chapter, stamp, body)
        return body


def _translated_index(book_dir: str) -> dict[int, str]:
    """Chapter number -> path of its translated .md file. Raises OSError when unreadable."""
    index = {}
    with os.scandir(book_dir) as entries:
        for count, entry in enumerate(entries):
            if count >= MAX_CHAPTER_SCAN:
                break
            if not entry.name.lower().endswith(".md") or not entry.is_file():
                continue
            chapter = parse_chapter_number(entry.name[:-3])
            if chapter is not None:
                index.setdefault(chapter, entry.path)
    return index


def _searchable(book_dir: str, exclude: set) -> tuple[list[int], dict[int, str]]:
    """Chapters with both a stored raw page and a translation, minus exclude, ascending."""
    translated = _translated_index(book_dir)
    stored = storage.list_scraped_chapters(book_dir)
    chapters = [c for c in stored if c in translated and c not in exclude]
    return chapters[:MAX_CHAPTER_SCAN], translated


def _hit_chapters(corpus: TermCorpus, book_dir: str, chapters: list[int], terms: list[str],
                  warnings: list) -> dict[str, list[int]]:
    """Every chapter whose cleaned raw body contains each term. Each page is read once."""
    hits = {term: [] for term in terms}
    for chapter in chapters:
        try:
            body = corpus.body(book_dir, chapter)
        except (OSError, ValueError) as e:
            _warn(warnings, f"Chapter {chapter}: could not read raw page ({e})")
            continue
        if not body:
            continue
        for term in terms:
            if term in body:
                hits[term].append(chapter)
    return hits


def _spread(chapters: list[int], limit: int) -> list[int]:
    """Earliest, latest and evenly spread chapters in between."""
    if limit <= 0 or not chapters:
        return []
    if len(chapters) <= limit:
        return list(chapters)
    if limit == 1:
        return [chapters[0]]
    step = (len(chapters) - 1) / (limit - 1)
    return sorted({chapters[round(i * step)] for i in range(limit)})


def _locate(corpus: TermCorpus, book_dir: str, chapter: int, md_path: str, term: str,
            anchors: dict) -> Occurrence | None:
    body = corpus.body(book_dir, chapter)
    raw_paragraphs = body.split("\n") if body else []
    index, offset = _find_in_paragraphs(raw_paragraphs, term)
    if index is None:
        return None
    english = clean_english(_read_text(md_path))
    if not english:
        return None
    present = _anchors_near(raw_paragraphs, index, term, anchors)
    center, matched = _best_window(english, _fraction(raw_paragraphs, index, offset), present)
    return Occurrence(
        chapter=chapter,
        zh=_zh_excerpt(raw_paragraphs, index, offset, len(term)),
        en=_excerpt(english, center),
        anchors=matched,
    )


def _usage(corpus: TermCorpus, book_dir: str, candidate: Candidate, chapters: list[int],
           translated: dict, anchors: dict, limit: int, warnings: list) -> TermUsage | None:
    occurrences = []
    for chapter in _spread(chapters, limit):
        try:
            occurrence = _locate(corpus, book_dir, chapter, translated[chapter], candidate.chinese, anchors)
        except (OSError, ValueError) as e:
            _warn(warnings, f"Chapter {chapter}: could not build excerpt for {candidate.chinese} ({e})")
            continue
        if occurrence is not None:
            occurrences.append(occurrence)
    if not occurrences:
        return None
    return TermUsage(candidate, tuple(chapters), tuple(occurrences))


def _load_anchors(book_dir: str) -> dict[str, str]:
    """Chinese spelling -> English for every known character name and glossary term."""
    anchors = {}
    char_map = characters.load_characters(book_dir)
    if isinstance(char_map, dict):
        for chinese, md_file in char_map.items():
            if not isinstance(chinese, str) or not isinstance(md_file, str):
                continue
            if len(chinese.strip()) >= MIN_ANCHOR_LENGTH:
                anchors[chinese.strip()] = os.path.splitext(md_file)[0]
    for chinese, english in glossary.spelling_map(glossary.load_terms(book_dir)).items():
        if english:
            anchors.setdefault(chinese, english)
    return anchors


# ----------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------

def filter_candidates(book_dir: str, proposed, pages_text: str) -> list[Candidate]:
    """Scout proposals that are new: not locked, not a known character, actually in the pages."""
    if not book_dir or not isinstance(proposed, list) or not isinstance(pages_text, str):
        return []
    known = set(glossary.spelling_map(glossary.load_terms(book_dir)))
    char_map = characters.load_characters(book_dir)
    names = set(char_map) if isinstance(char_map, dict) else set()
    kept, seen = [], set()
    for item in proposed:
        if len(kept) >= MAX_CANDIDATES:
            break
        if not isinstance(item, dict):
            continue
        chinese = glossary.usable_key(item.get("Chinese"))
        if not chinese or chinese in seen or chinese in known or chinese in names or chinese not in pages_text:
            continue
        seen.add(chinese)
        kept.append(Candidate(chinese, _clean_field(item.get("ProposedEnglish")), _clean_field(item.get("Type"))))
    return kept


def _labelled(label: str, text: str) -> list[str]:
    """'  ZH: first line' followed by the remaining lines indented under it."""
    lines = text.split("\n") if isinstance(text, str) and text else [""]
    pad = " " * (len(label) + 4)
    return [f"  {label}: {lines[0]}"] + [f"{pad}{line}" for line in lines[1:]]


def _occurrence_lines(occurrence: Occurrence, first: int, last: int) -> list[str]:
    if occurrence.chapter == first:
        tag = " (earliest)"
    elif occurrence.chapter == last:
        tag = " (most recent)"
    else:
        tag = ""
    if occurrence.anchors:
        confidence = "anchors matched: " + ", ".join(occurrence.anchors)
    else:
        confidence = "no anchors matched, position estimate only"
    return [
        f"- Chapter {occurrence.chapter}{tag} · {confidence}",
        *_labelled("ZH", occurrence.zh),
        *_labelled("EN", occurrence.en),
    ]


def format_usage(usage: TermUsage) -> str:
    """One PRIOR USAGE entry for the prompt.

    The scout's proposed rendering is left out on purpose: it anchors the model to its own guess.
    """
    candidate = usage.candidate
    shown = ", ".join(str(o.chapter) for o in usage.occurrences)
    lines = [
        f"## {candidate.chinese}",
        f"Found in {len(usage.chapters)} translated chapter(s); excerpts from Chapter(s) {shown}.",
    ]
    for occurrence in usage.occurrences:
        lines.extend(_occurrence_lines(occurrence, usage.chapters[0], usage.chapters[-1]))
    return "\n".join(lines)


def _format_context(usages: list[TermUsage]) -> tuple[str, list[TermUsage]]:
    """Join entries until CONTEXT_BUDGET is used; returns (text, included usages)."""
    parts, included, size = [], [], 0
    for usage in usages:
        block = format_usage(usage)
        if parts and size + len(block) > CONTEXT_BUDGET:
            continue
        parts.append(block)
        included.append(usage)
        size += len(block) + 2
    return "\n\n".join(parts), included


def build_prior_usage(corpus: TermCorpus, book_dir: str, candidates: list[Candidate],
                      batch_chapters) -> LookupResult:
    """Search earlier translated chapters for the candidates and build the PRIOR USAGE context.

    Raises OSError when the book folder itself cannot be listed; per-file problems become warnings.
    """
    result = LookupResult(candidates=len(candidates or []))
    if not book_dir or not candidates:
        return result
    chapters, translated = _searchable(book_dir, set(batch_chapters or ()))
    if not chapters:
        return result
    terms = [c.chinese for c in candidates]
    hits = _hit_chapters(corpus, book_dir, chapters, terms, result.warnings)
    anchors = _load_anchors(book_dir)
    usages = []
    for candidate in candidates:
        found = hits.get(candidate.chinese) or []
        usage = _usage(corpus, book_dir, candidate, found, translated, anchors,
                       MAX_OCCURRENCES, result.warnings) if found else None
        if usage is not None:
            usages.append(usage)
    result.context, result.usages = _format_context(usages)
    result.truncated = len(usages) - len(result.usages)
    result.first_seen = {u.candidate.chinese: u.chapters[0] for u in result.usages}
    return result


def audit_term(corpus: TermCorpus, book_dir: str, chinese: str,
               limit: int = AUDIT_OCCURRENCES) -> tuple[TermUsage | None, list[str]]:
    """Every translated chapter containing chinese, with excerpts. Returns (usage or None, warnings)."""
    warnings: list[str] = []
    if not book_dir:
        return None, ["No book directory selected."]
    if not chinese:
        return None, warnings
    chapters, translated = _searchable(book_dir, set())
    found = _hit_chapters(corpus, book_dir, chapters, [chinese], warnings).get(chinese) or []
    if not found:
        return None, warnings
    candidate = Candidate(chinese, "", "")
    usage = _usage(corpus, book_dir, candidate, found, translated, _load_anchors(book_dir), limit, warnings)
    return usage, warnings


def rendering_in_chapter(book_dir: str, chapter, english) -> bool | None:
    """Whether a chapter's translated .md file contains english (case-insensitive).

    None when it cannot be checked: bad arguments, no translated file, or an unreadable file.
    """
    if not book_dir or isinstance(chapter, bool) or not isinstance(chapter, int) or chapter <= 0:
        return None
    target = english.strip() if isinstance(english, str) else ""
    if not target:
        return None
    try:
        path = _translated_index(book_dir).get(chapter)
        if path is None:
            return None
        content = _read_text(path)
    except (OSError, ValueError):  # UnicodeDecodeError is a ValueError
        return None
    return target.casefold() in content.casefold()
