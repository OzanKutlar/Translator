"""Reviewer edits: exact search/replace changes to saved chapters, checked before they are applied.

The reviewer model answers with edits shaped like a code tool's search/replace:

    {"chapter": 812, "kind": "omission", "reason": "…",
     "search_replace": [{"search": "He nodded.", "replace": "He nodded.\\n\\n⟦15⟧ She left the hall."}]}

The file is chosen from the chapter number, never from a path in the reply. An edit is applied
only when every pair passes:
  - its search text occurs exactly once in the chapter (after the edit's earlier pairs);
  - search and replace stay under the size caps;
  - a ⟦n⟧ marker only starts an inserted paragraph, and only for a paragraph that is missing;
  - it removes no Translator's Note and lowers the count of no locked term or character name
    whose Chinese is in the chapter.
Edits are atomic: one failing pair rejects the whole edit. Before the first change, the chapter
and its alignment file are copied to <Book>/_qa/backups/review/ch_000812/<stamp>.*; undo_review()
restores the newest copy. Every applied, rejected and undone edit is logged to _qa/review.jsonl.
"""

import json
import os
import re
import time
from dataclasses import dataclass, field

from . import characters, glossary, parsing, qa, qalog, segments

EDIT_KINDS = ("omission", "addition", "mistranslation", "name", "term", "voice", "grammar")
MAX_EDITS_PER_REPLY = 60
MAX_PAIRS_PER_EDIT = 10
MAX_SEARCH_CHARS = 3_000
MAX_REPLACE_CHARS = 6_000
MAX_REASON_CHARS = 300
LOG_PREVIEW_CHARS = 400
MAX_BACKUPS_LISTED = 10_000
BACKUP_DIRNAME = "backups"
REVIEW_BACKUP_DIRNAME = "review"
_TN_LINE_RE = re.compile(r"^[(（]\s*TN\s*[:：]", re.IGNORECASE)
_MARKER_LINE_RE = re.compile(r"^\s*⟦\s*(\d{1,4})\s*⟧\s*(.*)$")
_SPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class Pair:
    search: str
    replace: str


@dataclass(frozen=True)
class Edit:
    chapter: int
    kind: str
    reason: str
    pairs: tuple


@dataclass
class ChapterSource:
    """A saved chapter as the reviewer sees it."""

    chapter: int
    md_path: str
    text: str                                   # the saved .md file
    zh: list                                    # Chinese paragraphs (by marker when aligned)
    en: dict                                    # marker -> English paragraph (aligned chapters only)
    skips: dict = field(default_factory=dict)   # marker -> declared reason
    notes: list = field(default_factory=list)   # (afterId, TN text)
    aligned: bool = False

    @property
    def missing(self) -> tuple:
        return missing_markers(self.zh, self.en, self.skips) if self.aligned else ()


@dataclass
class ChapterResult:
    chapter: int
    applied: list = field(default_factory=list)    # Edit
    rejected: list = field(default_factory=list)   # (Edit, why)
    backup: str = ""
    missing_after: tuple = ()
    error: str = ""


# ----------------------------------------------------------------------
# Files
# ----------------------------------------------------------------------

def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _write_atomic(path: str, text: str) -> None:
    """Write via a temp file and rename, so a crash never leaves half a file."""
    tmp_path = path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp_path, path)
    except OSError:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass  # the write error being re-raised is the one that matters
        raise


def missing_markers(zh, en, skips) -> tuple[int, ...]:
    """Markers with Chinese but neither English nor a declared skip."""
    if not isinstance(zh, list):
        return ()
    expected = ([0] if zh and zh[0] else []) + list(range(1, len(zh)))
    return tuple(m for m in expected if m not in (en or {}) and m not in (skips or {}))


def _skips(data) -> dict[int, str]:
    raw = data.get("skips") if isinstance(data, dict) else None
    if not isinstance(raw, dict):
        return {}
    return {int(k): str(v) for k, v in raw.items() if isinstance(k, str) and k.isdigit()}


def _notes(data) -> list:
    raw = data.get("notes") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return []
    notes = []
    for item in raw[:qa.MAX_NOTES_PER_CHAPTER * 4]:
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            after = item.get("afterId")
            notes.append((after if isinstance(after, int) and not isinstance(after, bool) else None, item["text"]))
    return notes


def load_source(book_dir: str, chapter: int, md_path) -> tuple[ChapterSource | None, str]:
    """The saved chapter with its alignment, or its raw page when it has none. Returns (source, error)."""
    if not md_path:
        return None, f"Chapter {chapter}: no translated file"
    try:
        text = _read(md_path)
    except (OSError, ValueError) as e:
        return None, f"Chapter {chapter}: could not read the translation ({e})"
    data = segments.load_alignment(book_dir, chapter)
    if data is not None:
        zh, en = segments.alignment_texts(data)
        return ChapterSource(chapter, md_path, text, zh, en, _skips(data), _notes(data), True), ""
    try:
        page = storage_page(book_dir, chapter)
    except (OSError, ValueError) as e:
        return None, f"Chapter {chapter}: could not read the raw page ({e})"
    if not page:
        return None, f"Chapter {chapter}: no raw page to compare with"
    return ChapterSource(chapter, md_path, text, segments.clean_raw(page), {}), ""


def storage_page(book_dir: str, chapter: int) -> str | None:
    from . import storage  # local import keeps this module's dependencies small
    return storage.load_scraped_page(book_dir, chapter)


# ----------------------------------------------------------------------
# Reading the reviewer's edits
# ----------------------------------------------------------------------

def _clean(value, limit: int) -> str:
    if value is None or isinstance(value, (dict, list)):
        return ""
    return _SPACE_RE.sub(" ", str(value)).strip()[:limit]


def _pairs(raw):
    """A tuple of Pairs, or a string saying why the list is unusable."""
    if not isinstance(raw, list) or not raw:
        return "the edit has no search_replace pairs"
    if len(raw) > MAX_PAIRS_PER_EDIT:
        return f"more than {MAX_PAIRS_PER_EDIT} search/replace pairs in one edit"
    pairs = []
    for item in raw:
        if not isinstance(item, dict):
            return "a search_replace entry is not an object"
        search, replace = item.get("search"), item.get("replace", "")
        if not isinstance(search, str) or not search.strip():
            return "a search text is empty"
        if not isinstance(replace, str):
            return "a replace text is not a string"
        pairs.append(Pair(search, replace))
    return tuple(pairs)


def normalise_edits(items, allowed) -> tuple[list[Edit], list[str]]:
    """Edits for chapters under review, plus reasons for entries that could not be read."""
    if not isinstance(items, list):
        return [], ["'edits' is not a list"]
    problems = []
    if len(items) > MAX_EDITS_PER_REPLY:
        problems.append(f"{len(items)} edits in one reply; only the first {MAX_EDITS_PER_REPLY} were read")
    found = []
    for item in items[:MAX_EDITS_PER_REPLY]:
        if not isinstance(item, dict):
            problems.append("an edit is not an object")
            continue
        chapter = parsing.chapter_value(item.get("chapter", item.get("chapterNo")))
        if chapter is None or chapter not in allowed:
            problems.append(f"edit for chapter {item.get('chapter')!r}: not a chapter under review")
            continue
        pairs = _pairs(item.get("search_replace"))
        if isinstance(pairs, str):
            problems.append(f"Chapter {chapter}: {pairs}")
            continue
        kind = str(item.get("kind") or "").strip().lower()
        found.append(Edit(chapter, kind if kind in EDIT_KINDS else "other", _clean(item.get("reason"), MAX_REASON_CHARS), pairs))
    return found, problems


# ----------------------------------------------------------------------
# Checking and applying
# ----------------------------------------------------------------------

def protected_names(book_dir: str, zh_text: str) -> list[str]:
    """English of the locked terms and character names whose Chinese is in the chapter."""
    names = []
    for term in glossary.find_terms(glossary.load_terms(book_dir), zh_text, limit=0):
        if term.english and term.english not in names:
            names.append(term.english)
    for chinese, english in characters.name_map(book_dir).items():
        if len(chinese) >= 2 and chinese in zh_text and english not in names:
            names.append(english)
    return names


def _tn_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if _TN_LINE_RE.match(line.strip())]


def _split_inserted(replace: str, missing: set) -> tuple[str, dict, str]:
    """replace without its ⟦n⟧ markers, the inserted paragraphs by marker, and a problem or ''."""
    if "⟦" not in replace:
        return replace, {}, ""
    lines, inserted, current = [], {}, None
    for line in replace.split("\n"):
        match = _MARKER_LINE_RE.match(line)
        if match:
            marker, body = int(match.group(1)), match.group(2).strip()
            if marker not in missing or marker in inserted:
                return "", {}, f"⟦{marker}⟧ is not a missing paragraph"
            if not body:
                return "", {}, f"⟦{marker}⟧ has no text"
            inserted[marker], current = body, marker
            lines.append(body)
            continue
        if "⟦" in line:
            return "", {}, "a ⟦n⟧ marker may only start an inserted paragraph"
        if current is not None and line.strip():
            inserted[current] += "\n" + line.strip()
        else:
            current = None
        lines.append(line)
    return "\n".join(lines), inserted, ""


def _apply_pair(text: str, pair: Pair, missing: set, inserted: dict) -> tuple[str, str, tuple]:
    """(new text, problem, (search, cleaned replace)). On a problem the text is returned unchanged."""
    if len(pair.search) > MAX_SEARCH_CHARS:
        return text, f"search is longer than {MAX_SEARCH_CHARS} characters", ()
    if len(pair.replace) > MAX_REPLACE_CHARS:
        return text, f"replace is longer than {MAX_REPLACE_CHARS} characters", ()
    count = text.count(pair.search)
    if count != 1:
        return text, "search text not found" if count == 0 else f"search text found {count} times", ()
    replace, new_paragraphs, problem = _split_inserted(pair.replace, missing - set(inserted))
    if problem:
        return text, problem, ()
    inserted.update(new_paragraphs)
    return text.replace(pair.search, replace, 1), "", (pair.search, replace)


def _guard(before: str, after: str, names: list, tns: list) -> str:
    """Why the change removes something protected, or ''."""
    folded_before, folded_after = before.casefold(), after.casefold()
    for name in names:
        folded = name.casefold()
        if folded_after.count(folded) < folded_before.count(folded):
            return f"removes “{name}”"
    for tn in tns:
        if tn not in after:
            return "removes a Translator's Note"
    return ""


def try_edit(text: str, edit: Edit, missing: set, names: list, tns: list) -> tuple[str, list, dict, str]:
    """(new text, cleaned pairs, inserted paragraphs, problem). Nothing changes when there is a problem."""
    working, cleaned, inserted = text, [], {}
    for pair in edit.pairs:
        working, problem, clean = _apply_pair(working, pair, missing, inserted)
        if problem:
            return text, [], {}, problem
        cleaned.append(clean)
    problem = _guard(text, working, names, tns)
    if problem:
        return text, [], {}, problem
    return working, cleaned, inserted, ""


def _backup_dir(book_dir: str, chapter: int) -> str:
    return os.path.join(book_dir, qa.QA_DIRNAME, BACKUP_DIRNAME, REVIEW_BACKUP_DIRNAME, f"ch_{chapter:06d}")


def backup_chapter(book_dir: str, source: ChapterSource) -> str:
    """Copy the chapter and its alignment file before a change. Returns the .md copy. Raises OSError."""
    folder = _backup_dir(book_dir, source.chapter)
    os.makedirs(folder, exist_ok=True)
    stamp = str(time.time_ns())
    md_copy = os.path.join(folder, f"{stamp}.md")
    _write_atomic(md_copy, source.text)
    data = segments.load_alignment(book_dir, source.chapter)
    if data is not None:
        _write_atomic(os.path.join(folder, f"{stamp}.align.json"), json.dumps(data, ensure_ascii=False, indent=2))
    return md_copy


def _update_alignment(book_dir: str, source: ChapterSource, changes: list, inserted: dict, result: ChapterResult) -> None:
    """Carry the edits into the alignment file and recompute which paragraphs are still missing."""
    data = segments.load_alignment(book_dir, source.chapter)
    if data is None or not isinstance(data.get("en"), dict):
        result.error = "the chapter changed, but its alignment file could not be updated"
        return
    en = data["en"]
    for search, replace in changes:
        for key in list(en):
            if isinstance(en[key], str) and search in en[key]:
                en[key] = en[key].replace(search, replace, 1).split("\n\n")[0].strip()
                break
    for marker, paragraph in inserted.items():
        en[str(marker)] = paragraph
        if isinstance(data.get("skips"), dict):
            data["skips"].pop(str(marker), None)
    zh, english = segments.alignment_texts(data)
    result.missing_after = missing_markers(zh, english, _skips(data))
    try:
        segments.save_alignment(book_dir, source.chapter, data)
    except (OSError, ValueError, TypeError) as e:
        result.error = f"the chapter changed, but saving its alignment failed ({e})"


def _write_chapter(book_dir: str, source: ChapterSource, text: str, changes: list, inserted: dict,
                   result: ChapterResult) -> None:
    try:
        result.backup = backup_chapter(book_dir, source)
        _write_atomic(source.md_path, text)
    except OSError as e:
        result.error = f"could not write the chapter ({e}); nothing was changed"
        result.rejected.extend((edit, "the chapter could not be written") for edit in result.applied)
        result.applied = []
        return
    if source.aligned:
        _update_alignment(book_dir, source, changes, inserted, result)


def apply_chapter(book_dir: str, source: ChapterSource, chapter_edits: list) -> ChapterResult:
    """Check and apply one chapter's edits in order; each edit sees the result of the ones before it."""
    if not isinstance(source, ChapterSource):
        raise TypeError("source must be a ChapterSource")
    result = ChapterResult(source.chapter, missing_after=source.missing)
    names = protected_names(book_dir, "\n".join(source.zh))
    tns = _tn_lines(source.text)
    missing = set(source.missing)
    text, changes, inserted = source.text, [], {}
    for edit in chapter_edits or []:
        new_text, cleaned, new_paragraphs, problem = try_edit(text, edit, missing - set(inserted), names, tns)
        if problem:
            result.rejected.append((edit, problem))
            continue
        text = new_text
        changes.extend(cleaned)
        inserted.update(new_paragraphs)
        result.applied.append(edit)
    if result.applied:
        _write_chapter(book_dir, source, text, changes, inserted, result)
    return result


# ----------------------------------------------------------------------
# Log and undo
# ----------------------------------------------------------------------

def _preview(text: str) -> str:
    return text if len(text) <= LOG_PREVIEW_CHARS else text[:LOG_PREVIEW_CHARS - 1] + "…"


def _record(chapter: int, edit: Edit, status: str, why: str, batch, model: str) -> dict:
    return {
        "chapter": chapter, "kind": edit.kind, "reason": edit.reason, "status": status, "why": why,
        "pairs": [{"search": _preview(p.search), "replace": _preview(p.replace)} for p in edit.pairs],
        "batch": batch, "model": model, "time": time.time(),
    }


def log_result(book_dir: str, result: ChapterResult, batch, model: str) -> None:
    """Append the chapter's applied and rejected edits to _qa/review.jsonl. Raises OSError/ValueError."""
    records = [_record(result.chapter, edit, "applied", "", batch, model) for edit in result.applied]
    records += [_record(result.chapter, edit, "rejected", why, batch, model) for edit, why in result.rejected]
    qalog.append_records(book_dir, qalog.REVIEW_FILENAME, records)


def _stamps(folder: str) -> list[str]:
    if not os.path.isdir(folder):
        return []
    names = os.listdir(folder)[:MAX_BACKUPS_LISTED]
    return sorted((n[:-3] for n in names if n.endswith(".md") and n[:-3].isdigit()), key=int)


def _restore(book_dir: str, chapter: int, md_path: str, folder: str, stamp: str) -> None:
    """Put a backup back and delete it. Raises OSError/ValueError/TypeError."""
    md_copy = os.path.join(folder, f"{stamp}.md")
    align_copy = os.path.join(folder, f"{stamp}.align.json")
    _write_atomic(md_path, _read(md_copy))
    if os.path.isfile(align_copy):
        with open(align_copy, "r", encoding="utf-8") as f:
            segments.save_alignment(book_dir, chapter, json.load(f))
        os.remove(align_copy)
    os.remove(md_copy)


def undo_review(book_dir: str, chapter, md_path) -> tuple[bool, str]:
    """Restore the chapter's newest review backup. Returns (ok, message)."""
    if isinstance(chapter, bool) or not isinstance(chapter, int) or chapter <= 0:
        return False, "Chapter must be a positive whole number."
    if not book_dir or not md_path:
        return False, f"Chapter {chapter} has no translated file."
    folder = _backup_dir(book_dir, chapter)
    try:
        stamps = _stamps(folder)
    except OSError as e:
        return False, f"Could not read the review backups of Chapter {chapter}: {e}"
    if not stamps:
        return False, f"Chapter {chapter} has no review backup to restore."
    try:
        _restore(book_dir, chapter, md_path, folder, stamps[-1])
    except (OSError, ValueError, TypeError) as e:
        return False, f"Could not restore Chapter {chapter}: {e}"
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(int(stamps[-1]) / 1e9))
    message = f"Chapter {chapter} is back to its version from before the review at {when}. {len(stamps) - 1} older backup(s) left."
    try:
        qalog.append_records(book_dir, qalog.REVIEW_FILENAME, [{"chapter": chapter, "status": "undone", "time": time.time()}])
    except (OSError, ValueError) as e:
        message += f" (Could not log the undo: {e})"
    return True, message
