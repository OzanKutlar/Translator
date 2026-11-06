"""Checks on translated chapters: completeness, Translator's Notes and paragraph lengths.

ChapterWork holds one returned chapter while it is checked, repaired and finished. Its text keeps
the ⟦n⟧ markers until finished() removes them.

Translator's Notes arrive in each chapter's "notes" list and are placed by the software directly
after their paragraph. Each note is checked first; a note that fails is removed and reported:
  - a note that only says the text was kept as written adds nothing;
  - the Chinese a note is about ("zh", quotation marks and punctuation ignored) is looked for on the
    page. When it sits in another paragraph than "afterId", the note moves to the paragraph that
    contains it;
  - a "source" note (an error in the Chinese) must give text that is on the page and a correction that
    differs from it by one or two characters;
  - any other note whose Chinese is not found stays after "afterId" and is reported, so cultural notes
    are never lost to a slightly different quote. It is removed only when it cannot be placed.
Notes the model still writes inside the text stay where they are, unless they make a typo claim
(which cannot be checked there) or only say the text was kept as written.

Length check: each paragraph's English/Chinese character ratio is compared with the book's usual
ratio, the median of recent paragraphs kept in <Book>/_qa/ratios.json. Paragraphs far shorter or
longer are reported, never changed.
"""

import json
import os
import re
import statistics
import threading
from dataclasses import dataclass, field, replace

from . import segments

QA_DIRNAME = "_qa"
RATIOS_FILENAME = "ratios.json"
RATIOS_VERSION = 1
NOTE_KINDS = ("pun", "culture", "term", "source")
MAX_NOTES_PER_CHAPTER = 30
MAX_NOTE_CHARS = 1_500
MAX_ZH_CHARS = 200            # longest "zh" or "correction" kept from a note
MAX_TYPO_CHARS = 40
MAX_TYPO_EDITS = 2
NOTE_PREVIEW_CHARS = 60
DEFAULT_RATIO = 3.0           # English characters per Chinese character until the book has enough samples
MIN_SAMPLES = 100
MAX_SAMPLES = 3_000
MIN_ZH_CHARS = 15             # shorter Chinese paragraphs give ratios too noisy to judge
SHORT_FACTOR = 0.35
LONG_FACTOR = 3.0
MAX_LISTED_FLAGS = 5
MAX_TEXT_RUNS = 5             # Chinese runs from a note's text tried when it cannot be placed by zh

_KIND_ALIASES = {
    "pun": "pun", "joke": "pun", "wordplay": "pun",
    "culture": "culture", "cultural": "culture", "background": "culture", "reference": "culture",
    "term": "term", "terminology": "term", "glossary": "term",
    "source": "source", "typo": "source", "error": "source", "sourceerror": "source",
}
_AS_WRITTEN_RE = re.compile(
    r"\b(?:translated|kept|left|rendered|retained|preserved)\b[^.()]{0,40}?\bas[- ](?:written|is)\b",
    re.IGNORECASE,
)
_TYPO_CLAIM_RE = re.compile(
    r"\btypo\b|\bmisprint\b|\bprobably meant\b|\blikely meant\b|\bshould (?:be|read)\b|错字|笔误",
    re.IGNORECASE,
)
_SPACE_RE = re.compile(r"\s+")
_CJK_RUN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]{2,}")
_ZH_QUOTES = "「」『』“”‘’《》〈〉\"'"
_ZH_EDGES = "（）()【】，。、！？：；…—·,.!?:; \u3000"

# Workers can record ratios concurrently, and on Windows a file that is open cannot be
# replaced, so every read and write of ratios.json goes through this lock.
_file_lock = threading.Lock()


# ----------------------------------------------------------------------
# One chapter while it is checked
# ----------------------------------------------------------------------

@dataclass
class ChapterWork:
    """One returned chapter while it is checked, repaired and finished. text keeps its ⟦n⟧ markers."""

    entry: dict                                  # the chapter object from the reply
    chapter: int | None                          # chapter number read from its chapterNo
    text: str                                    # marked text, changed by repair and note placement
    parts: segments.PageParts | None             # the raw page it translates; None when unknown
    notes: list = field(default_factory=list)    # structured notes still to be checked and placed
    parsed: segments.MarkedOutput = field(default_factory=segments.MarkedOutput)
    findings: list = field(default_factory=list)

    @property
    def chapter_no(self) -> str:
        value = self.entry.get("chapterNo") if isinstance(self.entry, dict) else None
        text = str(value or "").strip()
        if text:
            return text
        return f"Chapter {self.chapter}" if self.chapter else "Chapter ?"

    @property
    def zh(self) -> list[str]:
        """Chinese paragraphs indexed by marker; [] when the raw page is unknown."""
        if self.parts is None:
            return []
        return [self.parts.title, *self.parts.body]

    def refresh(self) -> None:
        """Read the markers again after the text changed."""
        self.parsed = segments.parse_marked(self.text)
        ids = segments.expected_ids(self.parts)
        self.findings = segments.check_anchors(ids, self.parsed) if ids else []

    def missing_ids(self) -> tuple[int, ...]:
        for finding in self.findings:
            if finding.kind == "missing":
                return finding.ids
        return ()

    def unmarked_reply(self) -> bool:
        return any(finding.kind == "no_markers" for finding in self.findings)

    def finished(self) -> dict:
        """The chapter object to save: markers removed and the notes list dropped."""
        base = {k: v for k, v in self.entry.items() if k != "notes"} if isinstance(self.entry, dict) else {}
        base["content"] = segments.strip_markers(self.text)
        return base


def incomplete_reason(work: ChapterWork, repaired: bool) -> str:
    """Why a saved chapter still has gaps, or '' when it has none."""
    if work.unmarked_reply():
        return "no paragraph markers, so missing paragraphs cannot be found"
    missing = work.missing_ids()
    if not missing:
        return ""
    how = "after repair" if repaired else "(not repaired)"
    return f"missing {segments.format_ids(missing)} {how}"


# ----------------------------------------------------------------------
# Translator's Notes
# ----------------------------------------------------------------------

@dataclass(frozen=True)
class Note:
    after_id: int | None
    kind: str
    zh: str
    correction: str
    text: str


@dataclass(frozen=True)
class NoteVerdict:
    note: Note | None          # None when the note is removed
    reason: str = ""
    moved: bool = False
    unmatched: bool = False    # kept at afterId although its Chinese was not found on the page


@dataclass
class NoteReport:
    """What happened to the notes of one batch."""

    placed: int = 0
    moved: int = 0
    inline: int = 0
    removed: list = field(default_factory=list)     # 'Chapter 812 after ⟦4⟧: “(TN: …” (reason)'
    kinds: dict = field(default_factory=dict)       # note kind -> notes placed
    unmatched: list = field(default_factory=list)   # 'Chapter 812 ⟦4⟧: “(TN: …”'


def _clean(value, limit: int) -> str:
    if value is None or isinstance(value, (dict, list)):
        return ""
    return _SPACE_RE.sub(" ", str(value)).strip()[:limit]


def _marker_value(value) -> int | None:
    if isinstance(value, str) and value.strip().isdecimal():
        value = int(value.strip())
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _note_text(value) -> str:
    """The note as one '(TN: …)' paragraph; '' when there is no text."""
    text = _clean(value, MAX_NOTE_CHARS)
    if not text:
        return ""
    if text.upper().startswith("TN:"):
        text = f"({text}"
    elif not text.startswith("("):
        text = f"(TN: {text}"
    return text if text.endswith(")") else text + ")"


def clean_zh(value) -> str:
    """Chinese quoted by a note, without quotation marks and surrounding punctuation."""
    text = _clean(value, MAX_ZH_CHARS)
    for char in _ZH_QUOTES:
        text = text.replace(char, "")
    return text.strip(_ZH_EDGES)


def normalise_note(item) -> tuple[Note | None, str]:
    """A note from the model as a Note, or (None, reason) when it cannot be one."""
    if not isinstance(item, dict):
        return None, "the note is not an object"
    kind = _KIND_ALIASES.get(re.sub(r"[^a-z]", "", str(item.get("kind") or "").lower()))
    if kind is None:
        return None, f"unknown kind {item.get('kind')!r} (use {', '.join(NOTE_KINDS)})"
    text = _note_text(item.get("text") or item.get("note") or item.get("claim"))
    if not text:
        return None, "the note has no text"
    zh = clean_zh(item.get("zh"))
    correction = _clean(item.get("correction"), MAX_ZH_CHARS)
    return Note(_marker_value(item.get("afterId")), kind, zh, correction, text), ""


def edit_distance(a: str, b: str) -> int:
    """Levenshtein distance between two short strings."""
    previous = list(range(len(b) + 1))
    for i, char_a in enumerate(a, start=1):
        current = [i]
        for j, char_b in enumerate(b, start=1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (char_a != char_b)))
        previous = current
    return previous[-1]


def _typo_problem(note: Note) -> str:
    if not note.zh or not note.correction:
        return "a source note needs the text as written (zh) and the correction"
    if max(len(note.zh), len(note.correction)) > MAX_TYPO_CHARS:
        return "too long to be a typo"
    edits = edit_distance(note.zh, note.correction)
    if edits == 0:
        return "the correction is the same as the text"
    if edits > MAX_TYPO_EDITS:
        return f"a typo changes one or two characters; this correction changes {edits}"
    return ""


def _paragraph_with(paragraphs: list[str], zh: str, preferred: int | None) -> int | None:
    if preferred is not None and 0 <= preferred < len(paragraphs) and zh in paragraphs[preferred]:
        return preferred
    for index, paragraph in enumerate(paragraphs):
        if zh in paragraph:
            return index
    return None


def check_note(note: Note, zh_paragraphs: list[str]) -> NoteVerdict:
    """Keep, move or remove a note. zh_paragraphs is the page by marker ([] when unknown)."""
    if not isinstance(note, Note):
        return NoteVerdict(None, "not a note")
    if _AS_WRITTEN_RE.search(note.text):
        return NoteVerdict(None, "it only says the text was kept as written")
    if note.kind == "source":
        problem = _typo_problem(note)
        if problem:
            return NoteVerdict(None, problem)
    if not zh_paragraphs:
        return NoteVerdict(note)  # no raw page to check against: placed where the model asked
    if note.kind == "source":
        return _place_strict(note, zh_paragraphs)
    return _place_loose(note, zh_paragraphs)


def _valid_marker(marker, paragraphs: list[str]) -> bool:
    return isinstance(marker, int) and not isinstance(marker, bool) and 0 <= marker < len(paragraphs)


def _text_runs(text) -> list[str]:
    """Chinese runs of two or more characters in a note's text, longest first."""
    runs = list(dict.fromkeys(_CJK_RUN_RE.findall(str(text or ""))))
    return sorted(runs, key=len, reverse=True)[:MAX_TEXT_RUNS]


def _found_at(note: Note, paragraphs: list[str], zh: str) -> NoteVerdict | None:
    """The note placed at the paragraph containing zh, preferring afterId; None when zh is not on the page."""
    place = _paragraph_with(paragraphs, zh, note.after_id)
    if place is None:
        return None
    return NoteVerdict(replace(note, after_id=place), moved=place != note.after_id)


def _place_strict(note: Note, paragraphs: list[str]) -> NoteVerdict:
    """Source notes make a claim about the page, so their Chinese must be on it."""
    if not note.zh:
        return NoteVerdict(None, "it does not name the Chinese it explains")
    verdict = _found_at(note, paragraphs, note.zh)
    if verdict is None:
        return NoteVerdict(None, f"its Chinese “{note.zh}” is not on the page")
    return verdict


def _place_loose(note: Note, paragraphs: list[str]) -> NoteVerdict:
    """Culture, pun and term notes: placed by their Chinese when found, otherwise kept at afterId."""
    if note.zh:
        verdict = _found_at(note, paragraphs, note.zh)
        if verdict is not None:
            return verdict
    runs = _text_runs(note.text)
    if _valid_marker(note.after_id, paragraphs):
        matched = not note.zh and any(run in paragraphs[note.after_id] for run in runs)
        return NoteVerdict(note, unmatched=not matched)
    for run in runs:
        verdict = _found_at(note, paragraphs, run)
        if verdict is not None:
            return verdict
    named = f"its Chinese “{note.zh}” is not on the page" if note.zh else "it names no Chinese found on the page"
    where = f"⟦{note.after_id}⟧ is not a paragraph of this chapter" if note.after_id is not None else "it names no paragraph"
    return NoteVerdict(None, f"{named}, and {where}")


def check_inline_note(text) -> str:
    """Why a note written inside the chapter text must go, or '' to leave it where it is."""
    if not isinstance(text, str) or not text.strip():
        return "the note is empty"
    if _AS_WRITTEN_RE.search(text):
        return "it only says the text was kept as written"
    if _TYPO_CLAIM_RE.search(text):
        return "a typo claim inside the text cannot be checked; source notes go in the notes list"
    return ""


def _removed(work: ChapterWork, after, text, reason: str) -> str:
    where = f"{work.chapter_no} after ⟦{after}⟧" if after is not None else work.chapter_no
    return f"{where}: “{_clean(text, NOTE_PREVIEW_CHARS)}” ({reason})"


def _place_one(work: ChapterWork, item, report: NoteReport) -> None:
    note, reason = normalise_note(item)
    verdict = check_note(note, work.zh) if note is not None else NoteVerdict(None, reason)
    if verdict.note is None:
        after = note.after_id if note is not None else None
        text = note.text if note is not None else str(item)
        report.removed.append(_removed(work, after, text, verdict.reason))
        return
    work.text = segments.insert_after(work.text, verdict.note.after_id, verdict.note.text)
    report.placed += 1
    report.moved += int(verdict.moved)
    kind = verdict.note.kind
    report.kinds[kind] = report.kinds.get(kind, 0) + 1
    if verdict.unmatched:
        preview = _clean(verdict.note.text, NOTE_PREVIEW_CHARS)
        report.unmatched.append(f"{work.chapter_no} ⟦{verdict.note.after_id}⟧: “{preview}”")


def place_notes(work: ChapterWork, report: NoteReport) -> None:
    """Check the chapter's notes, place the good ones after their paragraph and record the rest."""
    if not isinstance(work, ChapterWork) or not isinstance(report, NoteReport):
        raise TypeError("place_notes needs a ChapterWork and a NoteReport")
    for after, text in list(work.parsed.notes):
        reason = check_inline_note(text)
        if reason:
            work.text = segments.remove_line(work.text, text)
            report.removed.append(_removed(work, after, text, reason))
        else:
            report.inline += 1
    for index, item in enumerate(work.notes):
        if index >= MAX_NOTES_PER_CHAPTER:
            extra = len(work.notes) - index
            report.removed.append(f"{work.chapter_no}: {extra} more notes (limit {MAX_NOTES_PER_CHAPTER})")
            break
        _place_one(work, item, report)
    work.refresh()


# ----------------------------------------------------------------------
# Paragraph lengths
# ----------------------------------------------------------------------

def _valid_ratio(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and 0 < value < 100


def paragraph_ratios(zh_paragraphs, en_by_id) -> dict[int, float]:
    """English characters per Chinese character, per body paragraph long enough to judge."""
    ratios: dict[int, float] = {}
    if not isinstance(zh_paragraphs, list) or not isinstance(en_by_id, dict):
        return ratios
    for marker, english in en_by_id.items():
        if not isinstance(marker, int) or marker <= 0 or marker >= len(zh_paragraphs):
            continue
        zh_length = len("".join(str(zh_paragraphs[marker]).split()))
        if zh_length < MIN_ZH_CHARS or not isinstance(english, str):
            continue
        ratios[marker] = len(english.strip()) / zh_length
    return ratios


def usual_ratio(sample) -> float:
    """The book's median ratio, or DEFAULT_RATIO until MIN_SAMPLES paragraphs are recorded."""
    values = [v for v in (sample or []) if _valid_ratio(v)]
    return statistics.median(values) if len(values) >= MIN_SAMPLES else DEFAULT_RATIO


def length_flags(ratios, usual) -> list[tuple[int, float]]:
    """(marker, ratio ÷ usual) for paragraphs under SHORT_FACTOR or over LONG_FACTOR."""
    if not isinstance(ratios, dict) or not _valid_ratio(usual):
        return []
    flags = []
    for marker, ratio in sorted(ratios.items()):
        factor = ratio / usual
        if factor < SHORT_FACTOR or factor > LONG_FACTOR:
            flags.append((marker, factor))
    return flags


def describe_length_flags(flags, limit: int = MAX_LISTED_FLAGS) -> str:
    """'⟦14⟧ short (0.3× usual), ⟦20⟧ long (3.4× usual)'."""
    items = list(flags or [])
    shown = [f"⟦{m}⟧ {'short' if f < 1 else 'long'} ({f:.2g}× usual)" for m, f in items[:max(1, limit)]]
    extra = len(items) - len(shown)
    return ", ".join(shown) + (f", … {extra} more" if extra > 0 else "")


def ratios_path(book_dir: str) -> str:
    return os.path.join(book_dir, QA_DIRNAME, RATIOS_FILENAME)


def _read_ratios(path: str) -> list[float]:
    """Raises OSError/ValueError."""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    values = data.get("ratios") if isinstance(data, dict) else None
    if not isinstance(values, list):
        raise ValueError(f"{RATIOS_FILENAME} has no 'ratios' list")
    return [float(v) for v in values[-MAX_SAMPLES:] if _valid_ratio(v)]


def load_ratio_sample(book_dir: str) -> tuple[list[float], str | None]:
    """The book's recent ratios; ([], error) when the file exists but cannot be read."""
    if not book_dir:
        return [], None
    path = ratios_path(book_dir)
    if not os.path.isfile(path):
        return [], None
    with _file_lock:
        try:
            return _read_ratios(path), None
        except (OSError, ValueError) as e:
            return [], f"Could not read {QA_DIRNAME}/{RATIOS_FILENAME} ({e}); fix or remove the file"


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
            json.dump(data, f, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp_path, path)
    except OSError:
        _discard_temp(tmp_path)
        raise


def add_ratios(book_dir: str, ratios) -> None:
    """Append ratios to the book's sample, keeping the latest MAX_SAMPLES. Raises OSError/ValueError."""
    if not book_dir:
        raise ValueError("No book directory selected")
    fresh = [round(float(r), 3) for r in (ratios or []) if _valid_ratio(r)]
    if not fresh:
        return
    path = ratios_path(book_dir)
    with _file_lock:
        sample = _read_ratios(path) if os.path.isfile(path) else []
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _write_atomic(path, {"version": RATIOS_VERSION, "ratios": (sample + fresh)[-MAX_SAMPLES:]})
