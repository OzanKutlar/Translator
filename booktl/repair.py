"""Bounded repair of paragraphs a translation left out.

When a reply is read back by marker (see segments.py), chapters with missing paragraphs are patched
before anything is saved. Each round sends one call asking for just the missing paragraphs, with up
to CONTEXT_RADIUS neighbouring paragraphs on each side in Chinese and in the English already written.
The answers are spliced in by marker. At most REPAIR_ROUNDS rounds run, and a round that fills
nothing ends the repair. Whatever is still missing makes the chapter Incomplete.
"""

from dataclasses import dataclass, field

from . import parsing, qa, segments

REPAIR_ROUNDS = 2
MAX_MISSING_PER_CHAPTER = 40   # more gaps than this needs a full retranslation, not a patch
MAX_REPAIR_PARAGRAPHS = 80     # missing paragraphs asked for in one call
MAX_REQUEST_CHARS = 60_000     # chapters beyond this wait for the next round
CONTEXT_RADIUS = 2
MAX_DESCRIBED = 5


@dataclass(frozen=True)
class RepairTarget:
    """A chapter to patch this round and the markers it is missing."""

    work: qa.ChapterWork
    missing: tuple[int, ...]


@dataclass
class RepairOutcome:
    filled: dict = field(default_factory=dict)   # chapter -> markers filled this round
    ignored: int = 0                               # entries that were unreadable or not asked for
    error: str | None = None


def select_targets(works) -> tuple[list[RepairTarget], list[qa.ChapterWork]]:
    """Chapters to patch this round, and chapters with too many gaps to patch at all."""
    targets, too_many, total = [], [], 0
    for work in works or []:
        missing = work.missing_ids()
        if not missing or work.parts is None or work.chapter is None:
            continue
        if len(missing) > MAX_MISSING_PER_CHAPTER:
            too_many.append(work)
            continue
        if total + len(missing) > MAX_REPAIR_PARAGRAPHS:
            continue  # waits for the next round
        targets.append(RepairTarget(work, missing))
        total += len(missing)
    return targets, too_many


def _window(missing, size: int) -> list[int]:
    markers = set()
    for marker in missing:
        markers.update(range(marker - CONTEXT_RADIUS, marker + CONTEXT_RADIUS + 1))
    return sorted(m for m in markers if 0 <= m < size)


def _english_for(work: qa.ChapterWork, marker: int) -> str:
    if marker in work.parsed.paragraphs:
        return " ".join(work.parsed.paragraphs[marker].split("\n"))
    if marker in work.parsed.skips:
        return f"[SKIP: {work.parsed.skips[marker]}]"
    return "(not available)"


def chapter_block(target: RepairTarget) -> str:
    """One chapter's part of the repair request."""
    work = target.work
    zh = work.zh
    missing = set(target.missing)
    lines = [f"=== {work.chapter_no} ==="]
    for marker in _window(target.missing, len(zh)):
        if not zh[marker]:
            continue
        lines.append(f"⟦{marker}⟧ ZH: {zh[marker]}")
        english = "MISSING - translate this paragraph" if marker in missing else _english_for(work, marker)
        lines.append(f"     EN: {english}")
    return "\n".join(lines)


def build_request(targets) -> tuple[str, list[RepairTarget]]:
    """The request text and the targets it covers; targets past MAX_REQUEST_CHARS are left out."""
    blocks, used, size = [], [], 0
    for target in targets or []:
        block = chapter_block(target)
        if blocks and size + len(block) > MAX_REQUEST_CHARS:
            continue
        blocks.append(block)
        used.append(target)
        size += len(block) + 2
    return "\n\n".join(blocks), used


def chinese_text(targets) -> str:
    """The Chinese of the targeted chapters, used to pick character and term context."""
    return "\n".join("\n".join(t.work.zh) for t in targets or [])


def describe_targets(targets, limit: int = MAX_DESCRIBED) -> str:
    """'Chapter 812 ⟦14–17⟧ · Chapter 815 ⟦3⟧'."""
    items = list(targets or [])
    shown = [f"{t.work.chapter_no} {segments.format_ids(t.missing)}" for t in items[:max(1, limit)]]
    extra = len(items) - len(shown)
    return " · ".join(shown) + (f" · {extra} more" if extra > 0 else "")


def _apply_paragraph(by_chapter: dict, item: dict, outcome: RepairOutcome) -> None:
    target = by_chapter.get(item["chapter"])
    done = outcome.filled.get(item["chapter"], [])
    if target is None or item["id"] not in target.missing or item["id"] in done:
        outcome.ignored += 1
        return
    target.work.text = segments.insert_paragraph(target.work.text, item["id"], item["text"])
    outcome.filled.setdefault(item["chapter"], []).append(item["id"])


def _attach_note(by_chapter: dict, note: dict, outcome: RepairOutcome) -> None:
    target = by_chapter.get(parsing.chapter_value(note.get("chapterNo")))
    if target is None:
        outcome.ignored += 1
        return
    target.work.notes.append(note)


def apply_reply(targets, raw) -> RepairOutcome:
    """Splice a repair reply into the targeted chapters and re-read their markers."""
    outcome = RepairOutcome()
    paragraphs, notes, error, unreadable = parsing.parse_repair_reply(raw)
    if error:
        outcome.error = error
        return outcome
    outcome.ignored += unreadable
    by_chapter = {t.work.chapter: t for t in targets or []}
    for item in sorted(paragraphs, key=lambda p: (p["chapter"], p["id"])):
        _apply_paragraph(by_chapter, item, outcome)
    for note in notes:
        _attach_note(by_chapter, note, outcome)
    for target in targets or []:
        target.work.refresh()
    return outcome
