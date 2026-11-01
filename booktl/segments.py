"""Paragraph anchors: split raw pages, number their story paragraphs and read the numbers back.

Before a batch is sent, each raw page is split into its title line, its story paragraphs and
the lines after the end marker. Site text (lines above the title, boilerplate lines and the end
marker itself) is removed. The title becomes ⟦0⟧ and the story paragraphs ⟦1⟧…⟦n⟧. The model
starts every English paragraph with the marker it translates, or declares a paragraph it leaves
out as "⟦n⟧ [SKIP: reason]". After the reply arrives the markers are checked, removed from the
saved chapter, and kept in <Book>/_align/ch_000120.json:

    {"version": 1, "chapter": 120, "zh": ["第120章 …", "…"], "en": {"0": "# Chapter 120: …"},
     "skips": {"31": "author's note"}, "notes": [{"afterId": 4, "text": "(TN: …)"}],
     "created": 1760270000.0}

zh is indexed by marker number; zh[0] is "" when the page had no title line.
"""

import json
import os
import re
import threading
import time
from dataclasses import dataclass, field

ALIGN_DIRNAME = "_align"
ALIGN_VERSION = 1
MAX_LINES = 4_000             # bounds the lines read from one page or one translated chapter
MAX_TRAILER_LINES = 20        # lines after the end marker kept as evidence for arc endings
MAX_LISTED_SKIPS = 5
HEADER_SCAN_LINES = 8         # the chapter title line is looked for in the first lines only
TITLE_ID = 0
TRAILER_LABEL = "[After the chapter, not for translation]"
NO_REASON = "no reason given"

_HEADER_RE = re.compile(r"第\s*[零〇一二两三四五六七八九十百千万\d]+\s*[章回节集篇]")
# Everything from the first line matching one of these is site text, not story.
END_MARKERS = (
    re.compile(r"^[（(]?\s*本章完\s*[)）]?$"),
)
# Single lines dropped anywhere in the page.
BOILERPLATE = (
    re.compile(r"69书吧|69shu|https?://|www\.", re.IGNORECASE),
)

_FULLWIDTH_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")
_MARKER_RE = re.compile(r"^(#{1,6}\s*)?(?:⟦|\[\[|【)\s*([0-9０-９]{1,4})\s*(?:⟧|\]\]|】)")
_SKIP_RE = re.compile(r"^\[\s*SKIP\s*[:：]?\s*(.*?)\s*\]$", re.IGNORECASE | re.DOTALL)
_TN_RE = re.compile(r"^[(（]\s*TN\s*[:：]", re.IGNORECASE)
_LINK_RE = re.compile(r"^\[\[[^\]]*\]\]$")
_HEADING_RE = re.compile(r"^#{1,6}\s")
_BLANK_RUN_RE = re.compile(r"\n{3,}")

# Workers can save alignments concurrently, and on Windows a file that is open cannot be
# replaced, so every read and write of an alignment file goes through this lock.
_file_lock = threading.Lock()


@dataclass(frozen=True)
class PageParts:
    """A raw page split for marking."""

    title: str = ""                  # the 第…章 line, "" when none was found
    body: tuple[str, ...] = ()       # story paragraphs, numbered from 1
    trailer: tuple[str, ...] = ()    # lines after the end marker (evidence only)
    dropped: int = 0                 # site lines removed: header, boilerplate, end marker


@dataclass
class MarkedOutput:
    """A translated chapter read back by marker."""

    paragraphs: dict = field(default_factory=dict)    # marker -> English paragraph
    skips: dict = field(default_factory=dict)         # marker -> reason the model gave
    notes: list = field(default_factory=list)         # (marker the TN follows or None, TN text)
    unmarked: list = field(default_factory=list)      # other paragraphs without a marker
    duplicates: list = field(default_factory=list)    # markers seen more than once
    out_of_order: list = field(default_factory=list)  # markers lower than one seen before
    empty: list = field(default_factory=list)         # markers with no text after them
    marked: int = 0                                   # lines that started with a marker


@dataclass(frozen=True)
class Finding:
    """One problem found by check_anchors."""

    kind: str                     # missing, unknown, duplicate, out_of_order, unmarked, no_markers
    ids: tuple[int, ...] = ()
    count: int = 0


# ----------------------------------------------------------------------
# Splitting and marking raw pages
# ----------------------------------------------------------------------

def _lines(text) -> list[str]:
    if not isinstance(text, str):
        return []
    stripped = (line.strip() for line in text.splitlines())
    return [line for line in stripped if line][:MAX_LINES]


def _title_index(lines: list[str]) -> int | None:
    for index, line in enumerate(lines[:HEADER_SCAN_LINES]):
        if _HEADER_RE.search(line):
            return index
    return None


def _is_end(line: str) -> bool:
    return any(pattern.search(line) for pattern in END_MARKERS)


def _is_boilerplate(line: str) -> bool:
    return any(pattern.search(line) for pattern in BOILERPLATE)


def _trailer(lines: list[str]) -> list[str]:
    return [line for line in lines if not _is_boilerplate(line)][:MAX_TRAILER_LINES]


def split_page(raw) -> PageParts:
    """Title line, story paragraphs and trailer of a raw page; site text is removed and counted."""
    lines = _lines(raw)
    index = _title_index(lines)
    title = lines[index] if index is not None else ""
    dropped = index if index is not None else 0
    rest = lines[index + 1:] if index is not None else lines
    body: list[str] = []
    trailer: list[str] = []
    for position, line in enumerate(rest):
        if _is_end(line):
            dropped += 1
            trailer = _trailer(rest[position + 1:])
            break
        if _is_boilerplate(line):
            dropped += 1
        else:
            body.append(line)
    return PageParts(title, tuple(body), tuple(trailer), dropped)


def clean_raw(text) -> list[str]:
    """Story paragraphs of a raw page: the chapter title line (when found), then the body."""
    parts = split_page(text)
    return ([parts.title] if parts.title else []) + list(parts.body)


def expected_ids(parts) -> tuple[int, ...]:
    """Markers a page gets: 0 for the title when there is one, then 1…n for the body."""
    if not isinstance(parts, PageParts):
        return ()
    first = (TITLE_ID,) if parts.title else ()
    return first + tuple(range(1, len(parts.body) + 1))


def mark_page(parts) -> str:
    """The page as sent to the model: one marked paragraph per line, the trailer last."""
    if not isinstance(parts, PageParts):
        raise TypeError("parts must be a PageParts")
    lines = [f"⟦{TITLE_ID}⟧ {parts.title}"] if parts.title else []
    lines += [f"⟦{number}⟧ {text}" for number, text in enumerate(parts.body, start=1)]
    if parts.trailer:
        lines.append(TRAILER_LABEL + "\n" + "\n".join(parts.trailer))
    return "\n\n".join(lines)


# ----------------------------------------------------------------------
# Reading markers back
# ----------------------------------------------------------------------

def _heading_prefix(hashes) -> str:
    text = (hashes or "").strip()
    return f"{text} " if text else ""


def _split_marker(text: str):
    """(marker, rest of the line) when text starts with a marker, else None."""
    if not text or _LINK_RE.match(text):
        return None
    match = _MARKER_RE.match(text)
    if match is None:
        return None
    number = int(match.group(2).translate(_FULLWIDTH_DIGITS))
    rest = _heading_prefix(match.group(1)) + text[match.end():].strip()
    return number, rest.strip()


class _ReplyParser:
    """Reads a translated chapter line by line and sorts every paragraph by marker."""

    def __init__(self):
        self.out = MarkedOutput()
        self.current: int | None = None   # marker whose paragraph is still open
        self.last: int | None = None      # last marker accepted, for TN placement
        self.highest = -1
        self.seen: set[int] = set()

    def feed(self, line: str) -> None:
        text = line.strip()
        if not text or _LINK_RE.match(text):
            self.current = None
            return
        marked = _split_marker(text)
        if marked is not None:
            self._marked(*marked)
        else:
            self._unmarked(text)

    def _accept(self, marker: int) -> None:
        self.seen.add(marker)
        if marker < self.highest:
            self.out.out_of_order.append(marker)
        self.highest = max(self.highest, marker)
        self.last = marker

    def _marked(self, marker: int, text: str) -> None:
        self.out.marked += 1
        self.current = None
        if marker in self.seen:
            self.out.duplicates.append(marker)
            return
        self._accept(marker)
        skip = _SKIP_RE.match(text)
        if skip:
            self.out.skips[marker] = skip.group(1).strip() or NO_REASON
        elif not text:
            self.out.empty.append(marker)
        else:
            self.out.paragraphs[marker] = text
            self.current = marker

    def _unmarked(self, text: str) -> None:
        if _TN_RE.match(text):
            self.out.notes.append((self.last, text))
            self.current = None
        elif self.current is not None:
            self.out.paragraphs[self.current] += "\n" + text
        elif self.highest < 0 and _HEADING_RE.match(text):
            self._accept(TITLE_ID)  # a heading written without its ⟦0⟧ still counts as the title
            self.out.paragraphs[TITLE_ID] = text
        else:
            self.out.unmarked.append(text)


def parse_marked(text) -> MarkedOutput:
    """Sort a translated chapter's paragraphs by marker."""
    parser = _ReplyParser()
    if isinstance(text, str):
        for line in text.splitlines()[:MAX_LINES]:
            parser.feed(line)
    return parser.out


def has_markers(text) -> bool:
    return parse_marked(text).marked > 0


def _add_ids(findings: list, kind: str, ids) -> None:
    clean = tuple(sorted(set(ids)))
    if clean:
        findings.append(Finding(kind, clean))


def check_anchors(expected, out) -> list[Finding]:
    """Problems with a reply's markers compared with the markers that were sent. [] means clean."""
    wanted = {i for i in (expected or ()) if isinstance(i, int) and not isinstance(i, bool) and i >= 0}
    if not wanted or not isinstance(out, MarkedOutput):
        return []
    if not out.marked:
        return [Finding("no_markers")]
    found = set(out.paragraphs) | set(out.skips)
    findings: list[Finding] = []
    _add_ids(findings, "missing", wanted - found)
    _add_ids(findings, "unknown", found - wanted - {TITLE_ID})
    _add_ids(findings, "duplicate", out.duplicates)
    _add_ids(findings, "out_of_order", out.out_of_order)
    if out.unmarked:
        findings.append(Finding("unmarked", count=len(out.unmarked)))
    return findings


def strip_markers(text) -> str:
    """The chapter without markers; declared [SKIP: …] lines are removed."""
    if not isinstance(text, str):
        return ""
    kept = []
    for line in text.splitlines():
        marked = _split_marker(line.strip())
        if marked is None:
            kept.append(line.rstrip())
        elif not _SKIP_RE.match(marked[1]):
            kept.append(marked[1])
    return _BLANK_RUN_RE.sub("\n\n", "\n".join(kept)).strip()


# ----------------------------------------------------------------------
# Editing marked text (repair splices, note placement)
# ----------------------------------------------------------------------

def _is_break(text: str) -> bool:
    """A line that starts a new paragraph instead of continuing the one above it."""
    return bool(_split_marker(text) or _TN_RE.match(text) or _LINK_RE.match(text))


def _next_filled(lines: list[str], start: int) -> int | None:
    for index in range(max(0, start), len(lines)):
        if lines[index].strip():
            return index
    return None


def _unmarked_heading(lines: list[str]) -> int | None:
    """Line of a heading written without its ⟦0⟧, when it opens the chapter."""
    first = _next_filled(lines, 0)
    if first is None:
        return None
    text = lines[first].strip()
    return first if _HEADING_RE.match(text) and _split_marker(text) is None else None


def _anchor_line(lines: list[str], marker: int) -> int | None:
    """Line of the paragraph with the highest marker not above marker (its first copy)."""
    best, best_marker = None, -1
    for index, line in enumerate(lines):
        found = _split_marker(line.strip())
        if found is not None and best_marker < found[0] <= marker:
            best, best_marker = index, found[0]
    if best is None and marker >= TITLE_ID:
        best = _unmarked_heading(lines)
    return best


def _paragraph_end(lines: list[str], start: int) -> int:
    """Last line of the paragraph at start, including continuation lines and the TNs right after it."""
    end = start
    for index in range(start + 1, len(lines)):
        text = lines[index].strip()
        if not text or _is_break(text):
            break
        end = index
    for _ in range(len(lines)):
        following = _next_filled(lines, end + 1)
        if following is None or not _TN_RE.match(lines[following].strip()):
            break
        end = following
    return end


def _end_line(lines: list[str]) -> int:
    """Line after which a block goes when it belongs at the end: before a closing wikilink."""
    for index in range(len(lines) - 1, -1, -1):
        text = lines[index].strip()
        if text:
            return index - 1 if _LINK_RE.match(text) else index
    return -1


def _splice(lines: list[str], after: int, block: str) -> str:
    """lines with block as its own paragraph after line `after` (-1 puts it first)."""
    before = lines[:after + 1]
    rest = lines[after + 1:]
    middle = ([""] if before else []) + [block] + ([""] if rest and rest[0].strip() else [])
    return _BLANK_RUN_RE.sub("\n\n", "\n".join(before + middle + rest)).strip()


def insert_after(text, marker, block) -> str:
    """text with block as its own paragraph after paragraph `marker`, or the nearest lower one present.

    marker None puts the block at the end, before a closing wikilink. When no paragraph at or below
    marker exists, the block goes first.
    """
    if not isinstance(text, str) or not isinstance(block, str) or not block.strip():
        return text
    lines = text.split("\n")
    if marker is None:
        return _splice(lines, _end_line(lines), block.strip())
    anchor = _anchor_line(lines, marker)
    after = _paragraph_end(lines, anchor) if anchor is not None else -1
    return _splice(lines, after, block.strip())


def _single_paragraph(text) -> str:
    """Text as one paragraph: a leading marker removed, blank lines dropped."""
    if not isinstance(text, str):
        return ""
    found = _split_marker(text.strip())
    body = found[1] if found is not None else text
    return "\n".join(line.strip() for line in body.splitlines() if line.strip())


def insert_paragraph(text, marker: int, paragraph) -> str:
    """text with '⟦marker⟧ paragraph' spliced in after the nearest lower marker present."""
    if isinstance(marker, bool) or not isinstance(marker, int) or marker < 0:
        raise ValueError(f"Invalid marker: {marker!r}")
    body = _single_paragraph(paragraph)
    if not body:
        return text
    return insert_after(text, marker - 1, f"⟦{marker}⟧ {body}")


def remove_line(text, line) -> str:
    """text without the first line equal to line (surrounding spaces ignored)."""
    if not isinstance(text, str) or not isinstance(line, str) or not line.strip():
        return text
    target = line.strip()
    lines = text.split("\n")
    for index, current in enumerate(lines):
        if current.strip() == target:
            del lines[index]
            return _BLANK_RUN_RE.sub("\n\n", "\n".join(lines)).strip()
    return text


# ----------------------------------------------------------------------
# Descriptions for the batch log
# ----------------------------------------------------------------------

def format_ids(ids) -> str:
    """'⟦14–17⟧, ⟦20⟧' for a list of markers; '' when there are none."""
    nums = sorted({i for i in (ids or ()) if isinstance(i, int) and not isinstance(i, bool) and i >= 0})
    if not nums:
        return ""
    ranges = []
    start = prev = nums[0]
    for num in nums[1:]:
        if num != prev + 1:
            ranges.append((start, prev))
            start = num
        prev = num
    ranges.append((start, prev))
    return ", ".join(f"⟦{a}⟧" if a == b else f"⟦{a}–{b}⟧" for a, b in ranges)


_FINDING_TEXT = {
    "missing": "missing {ids}",
    "unknown": "unknown markers {ids}",
    "duplicate": "repeated {ids}",
    "out_of_order": "out of order {ids}",
}


def describe_findings(findings) -> str:
    """'missing ⟦14–17⟧, 2 unmarked paragraphs' for a chapter's findings."""
    parts = []
    for finding in findings or ():
        if not isinstance(finding, Finding):
            continue
        if finding.kind == "no_markers":
            parts.append("no paragraph markers, so it could not be checked")
        elif finding.kind == "unmarked":
            noun = "paragraph" if finding.count == 1 else "paragraphs"
            parts.append(f"{finding.count} unmarked {noun}")
        elif finding.kind in _FINDING_TEXT:
            parts.append(_FINDING_TEXT[finding.kind].format(ids=format_ids(finding.ids)))
    return ", ".join(parts)


def describe_skips(skips, limit: int = MAX_LISTED_SKIPS) -> str:
    """'⟦31⟧ (author's note), ⟦32⟧ (site notice)' for declared skips."""
    if not isinstance(skips, dict):
        return ""
    items = sorted((k, v) for k, v in skips.items() if isinstance(k, int))
    shown = [f"⟦{marker}⟧ ({reason})" for marker, reason in items[:max(1, limit)]]
    extra = len(items) - len(shown)
    return ", ".join(shown) + (f", … {extra} more" if extra > 0 else "")


# ----------------------------------------------------------------------
# Alignment files
# ----------------------------------------------------------------------

def alignment_path(book_dir: str, chapter: int) -> str:
    """Path of a chapter's alignment file (it may not exist). Raises ValueError."""
    if not book_dir:
        raise ValueError("No book directory selected")
    if isinstance(chapter, bool) or not isinstance(chapter, int) or chapter <= 0:
        raise ValueError(f"Invalid chapter number: {chapter!r}")
    return os.path.join(book_dir, ALIGN_DIRNAME, f"ch_{chapter:06d}.json")


def build_alignment(chapter: int, parts: PageParts, out: MarkedOutput) -> dict:
    """The alignment record for a chapter. Raises TypeError on bad input."""
    if not isinstance(parts, PageParts) or not isinstance(out, MarkedOutput):
        raise TypeError("build_alignment needs a PageParts and a MarkedOutput")
    return {
        "version": ALIGN_VERSION,
        "chapter": chapter,
        "zh": [parts.title, *parts.body],
        "en": {str(k): v for k, v in sorted(out.paragraphs.items())},
        "skips": {str(k): v for k, v in sorted(out.skips.items())},
        "notes": [{"afterId": after, "text": text} for after, text in out.notes],
        "created": time.time(),
    }


def _discard_temp(tmp_path: str) -> None:
    """Best-effort cleanup after a failed save; the caller re-raises the original error."""
    try:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
    except OSError:
        pass  # the save error being re-raised is the one that matters


def _write_atomic(path: str, data: dict) -> None:
    """Write via a temp file and rename, so a crash never leaves half a file."""
    tmp_path = path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp_path, path)
    except OSError:
        _discard_temp(tmp_path)
        raise


def save_alignment(book_dir: str, chapter: int, data: dict) -> str:
    """Write a chapter's alignment file. Raises OSError/ValueError/TypeError."""
    path = alignment_path(book_dir, chapter)
    if not isinstance(data, dict):
        raise TypeError("Alignment data must be a dict")
    with _file_lock:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _write_atomic(path, data)
    return path


def load_alignment(book_dir: str, chapter: int) -> dict | None:
    """A chapter's alignment record, or None when there is none or it is unusable. Never raises."""
    try:
        path = alignment_path(book_dir, chapter)
    except ValueError:
        return None
    if not os.path.isfile(path):
        return None
    with _file_lock:
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return None  # an unusable file only means the position estimate is used instead
    if not isinstance(data, dict) or data.get("version") != ALIGN_VERSION:
        return None
    return data


def remove_alignment(book_dir: str, chapter: int) -> bool:
    """Delete a chapter's alignment file. Returns True when one existed. Raises OSError/ValueError."""
    path = alignment_path(book_dir, chapter)
    with _file_lock:
        if not os.path.isfile(path):
            return False
        os.remove(path)
    return True


def alignment_texts(data) -> tuple[list[str], dict[int, str]]:
    """(Chinese paragraphs indexed by marker, English paragraphs by marker) from an alignment record."""
    if not isinstance(data, dict):
        return [], {}
    raw_zh = data.get("zh")
    zh = [p if isinstance(p, str) else "" for p in raw_zh[:MAX_LINES]] if isinstance(raw_zh, list) else []
    raw_en = data.get("en")
    en: dict[int, str] = {}
    if isinstance(raw_en, dict):
        for key, text in list(raw_en.items())[:MAX_LINES]:
            if isinstance(key, str) and key.isdigit() and isinstance(text, str) and text.strip():
                en[int(key)] = text
    return zh, en
