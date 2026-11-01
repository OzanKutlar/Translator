"""Rename a glossary term across translated chapters, using the paragraph alignment files.

Only aligned paragraphs whose Chinese contains one of the term's spellings are changed, and only
the old English inside them. plan_rename() shows what would change; apply_rename() writes it after
copying every changed chapter to <Book>/_qa/backups/rename_<time>/, updates the alignment files and
locks the term as the user's own with the new English.
"""

import os
import re
import time
from dataclasses import dataclass, field

from . import glossary, qa, segments, storage, termlookup

BACKUP_DIRNAME = "backups"
MAX_CHANGES = 20_000
MAX_UNALIGNED_SCAN = 50_000
MAX_WARNINGS = 20


@dataclass(frozen=True)
class Change:
    chapter: int
    marker: int
    before: str
    after: str


@dataclass
class RenamePlan:
    key: str
    old: str
    new: str
    spellings: tuple
    changes: list = field(default_factory=list)
    untouched: list = field(default_factory=list)   # (chapter, marker): Chinese found, old English not
    unaligned: list = field(default_factory=list)   # chapters with the term but no alignment file
    warnings: list = field(default_factory=list)

    @property
    def chapters(self) -> list[int]:
        return sorted({c.chapter for c in self.changes})


@dataclass
class RenameResult:
    backup_dir: str
    changed_chapters: list = field(default_factory=list)
    changed_paragraphs: int = 0
    skipped: list = field(default_factory=list)
    glossary_ok: bool = False
    glossary_message: str = ""


def _pattern(english: str) -> re.Pattern:
    return re.compile(r"(?<![A-Za-z0-9])" + re.escape(english) + r"(?![A-Za-z0-9])", re.IGNORECASE)


def _plan_chapter(book_dir: str, chapter: int, plan: RenamePlan, pattern: re.Pattern) -> None:
    zh, en = segments.alignment_texts(segments.load_alignment(book_dir, chapter))
    for marker, text in sorted(en.items()):
        if marker >= len(zh) or not any(form in zh[marker] for form in plan.spellings):
            continue
        after, count = pattern.subn(lambda _match: plan.new, text)
        if count:
            plan.changes.append(Change(chapter, marker, text, after))
        else:
            plan.untouched.append((chapter, marker))


def _unaligned_hits(book_dir: str, aligned: set, spellings, warnings: list) -> list[int]:
    """Translated chapters whose stored raw page contains the term but which have no alignment file."""
    try:
        translated = termlookup.translated_index(book_dir)
        stored = storage.list_scraped_chapters(book_dir)
    except OSError as e:
        warnings.append(f"Could not look for unaligned chapters: {e}")
        return []
    hits = []
    for chapter in stored[:MAX_UNALIGNED_SCAN]:
        if chapter in aligned or chapter not in translated:
            continue
        try:
            page = storage.load_scraped_page(book_dir, chapter)
        except (OSError, ValueError) as e:
            if len(warnings) < MAX_WARNINGS:
                warnings.append(f"Chapter {chapter}: could not read raw page ({e})")
            continue
        if page and any(form in page for form in spellings):
            hits.append(chapter)
    return hits


def plan_rename(book_dir: str, chinese, new_english) -> tuple[RenamePlan | None, str | None]:
    """What renaming the term would change. Returns (plan, None) or (None, error)."""
    if not book_dir:
        return None, "No book directory selected."
    new = " ".join(str(new_english or "").split())
    if not new:
        return None, "The new English rendering cannot be empty."
    data, err = glossary.load_glossary(book_dir)
    if err:
        return None, err
    terms = data["terms"]
    key = glossary.resolve_term(terms, chinese)
    if key is None:
        return None, f"No term matches '{chinese}'."
    old = glossary.english_of(terms[key])
    if old == new:
        return None, f"{key} is already rendered as “{new}”."
    plan = RenamePlan(key, old, new, glossary.term_spellings(terms, key))
    try:
        aligned = segments.aligned_chapters(book_dir)
    except OSError as e:
        return None, f"Could not list {segments.ALIGN_DIRNAME}/: {e}"
    pattern = _pattern(old)
    for chapter in aligned:
        if len(plan.changes) >= MAX_CHANGES:
            plan.warnings.append(f"Stopped after {MAX_CHANGES} paragraphs; run the rename again afterwards.")
            break
        _plan_chapter(book_dir, chapter, plan, pattern)
    plan.unaligned = _unaligned_hits(book_dir, set(aligned), plan.spellings, plan.warnings)
    return plan, None


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


def _splice_changes(text: str, chapter: int, changes: list, result: RenameResult) -> tuple[str, list]:
    applied = []
    for change in changes:
        count = text.count(change.before)
        if count != 1:
            result.skipped.append(f"Chapter {chapter} ⟦{change.marker}⟧: paragraph found {count} times in the file; not changed")
            continue
        text = text.replace(change.before, change.after, 1)
        applied.append(change)
    return text, applied


def _update_alignment(book_dir: str, chapter: int, applied: list, result: RenameResult) -> None:
    data = segments.load_alignment(book_dir, chapter)
    if data is None or not isinstance(data.get("en"), dict):
        result.skipped.append(f"Chapter {chapter}: file changed, but its alignment file could not be updated")
        return
    for change in applied:
        data["en"][str(change.marker)] = change.after
    try:
        segments.save_alignment(book_dir, chapter, data)
    except (OSError, ValueError, TypeError) as e:
        result.skipped.append(f"Chapter {chapter}: file changed, but saving its alignment failed ({e})")


def _apply_chapter(book_dir: str, chapter: int, changes: list, md_path, result: RenameResult) -> None:
    if md_path is None:
        result.skipped.append(f"Chapter {chapter}: no translated file")
        return
    try:
        original = _read(md_path)
    except (OSError, ValueError) as e:
        result.skipped.append(f"Chapter {chapter}: could not read the file ({e})")
        return
    text, applied = _splice_changes(original, chapter, changes, result)
    if not applied:
        return
    try:
        os.makedirs(result.backup_dir, exist_ok=True)
        _write_atomic(os.path.join(result.backup_dir, os.path.basename(md_path)), original)
        _write_atomic(md_path, text)
    except OSError as e:
        result.skipped.append(f"Chapter {chapter}: could not write ({e}); left unchanged")
        return
    _update_alignment(book_dir, chapter, applied, result)
    result.changed_chapters.append(chapter)
    result.changed_paragraphs += len(applied)


def apply_rename(book_dir: str, plan: RenamePlan) -> RenameResult:
    """Write a plan. Raises TypeError for a bad plan and OSError when the book folder is unreadable."""
    if not isinstance(plan, RenamePlan):
        raise TypeError("plan must be a RenamePlan")
    translated = termlookup.translated_index(book_dir)
    backup = os.path.join(book_dir, qa.QA_DIRNAME, BACKUP_DIRNAME, f"rename_{int(time.time())}")
    result = RenameResult(backup)
    by_chapter: dict[int, list[Change]] = {}
    for change in plan.changes:
        by_chapter.setdefault(change.chapter, []).append(change)
    for chapter, changes in sorted(by_chapter.items()):
        _apply_chapter(book_dir, chapter, changes, translated.get(chapter), result)
    result.glossary_ok, result.glossary_message = glossary.add_user_term(book_dir, plan.key, plan.new)
    return result
