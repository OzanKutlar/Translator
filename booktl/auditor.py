"""Offline audit of saved translations: the pipeline's checks run over a range of chapters.

Nothing is sent to a model unless align_chapter() is called. For each chapter the audit reads its
translation, its raw page and, when it has one, its alignment file, then collects findings:
  omission    paragraphs with no English (aligned), or far fewer English paragraphs (unaligned)
  addition    English paragraphs an audit alignment could not match to any Chinese paragraph
  length      paragraphs or chapters far shorter or longer than the book usually translates to
  note        Translator's Notes that make a typo claim or only say the text was kept as written
  name/term   character names and locked terms missing from the English
  source      unverified typo fixes
  review      reviewer edits that were rejected
  incomplete  chapters on the Incomplete list
  style       grammar findings
Information-only findings (continuity notes, declared skips, phrase counts) never make a chapter a
problem chapter by themselves. render_chapter() and render_header() build the findings prompt.

align_chapter() builds an alignment file for an older chapter: the model maps each Chinese paragraph
to numbered English paragraphs and only ever answers with numbers, so it cannot invent text.
"""

import re
import time
from dataclasses import dataclass, field

from . import characters, edits, glossary, parsing, qa, qalog, segments, storage, style
from .client import ApiError

MAX_FINDINGS = 30
MAX_QUOTES = 3
QUOTE_CHARS = 300
MIN_PARAGRAPH_GAP = 2           # unaligned: fewer English paragraphs than this below the Chinese is fine
PARAGRAPH_GAP_SHARE = 0.1
CHAPTER_SHORT = 0.6             # unaligned: whole-chapter length ratio compared with the usual one
CHAPTER_LONG = 1.8
MIN_CHAPTER_ZH = 200
MAX_RECORDS_PER_CHAPTER = 3
MAX_BLOCKS = 4_000
MAX_UNMATCHED = 20
MAX_ALIGN_CHARS = 120_000
ALIGN_MAX_TOKENS = 8_192
CUT_NOTE = "… (cut here: this chapter's findings are longer than one prompt allows; the full list is in the saved findings file)"
_TN_RE = re.compile(r"^[(（]\s*TN\s*[:：]", re.IGNORECASE)
_LINK_RE = re.compile(r"^\[\[[^\]]*\]\]$")
_BLOCK_SPLIT_RE = re.compile(r"\n\s*\n")

DEFAULT_INSTRUCTIONS = (
    "You are checking a Chinese-to-English novel translation. Each chapter below is attached as two files: its "
    "English translation (Chapter N.md) and then its raw Chinese web page (ch_00000N.txt; ignore any site text "
    "above the chapter title and from (本章完) on). Automatic checks flagged the problems listed under each "
    "chapter. ⟦n⟧ numbers the story paragraphs of the raw page: ⟦0⟧ is the chapter title line, ⟦1⟧ the first "
    "paragraph after it; site lines are not counted. The quoted ZH/EN lines show where to look.\n\n"
    "For each numbered finding: compare the English with the Chinese and say whether the problem is real. "
    "If it is, give the exact fix as the English text to find and the English text to replace it with, and "
    "translate missing paragraphs in the style of the surrounding English. Findings under \"Information only\" "
    "are context; do not change text because of them. Do not rewrite anything for style, and keep names and "
    "terms exactly as the translation uses them unless a finding is about them."
)

ALIGN_SYSTEM = (
    "You align a Chinese novel chapter with its existing English translation. Map every Chinese paragraph ⟦n⟧ "
    "to the English paragraph or paragraphs [E#] that translate it.\n"
    "Rules:\n"
    "  - list every ⟦n⟧ exactly once, in order;\n"
    "  - give [] when no English paragraph translates it;\n"
    "  - when one English paragraph translates several consecutive Chinese paragraphs, give its number for "
    "each of them;\n"
    "  - English numbers never go backwards.\n"
    "Respond with ONLY a valid JSON object, no preamble and no markdown code fences:\n"
    "{\"map\": [{\"zh\": 0, \"en\": [1]}, {\"zh\": 1, \"en\": [2, 3]}, {\"zh\": 2, \"en\": []}]}"
)


@dataclass(frozen=True)
class Finding:
    kind: str
    text: str
    quotes: tuple = ()
    info: bool = False


@dataclass
class ChapterAudit:
    chapter: int
    aligned: bool = False
    findings: list = field(default_factory=list)
    error: str = ""

    @property
    def problems(self) -> list:
        return [f for f in self.findings if not f.info]


@dataclass
class AuditContext:
    terms: dict
    char_map: dict
    caps: dict
    usual: float
    fixes: dict        # chapter -> unverified source-fix records
    kept: dict         # chapter -> continuity records
    rejected: dict     # chapter -> rejected reviewer edits
    incomplete: dict   # chapter -> reason
    warnings: list = field(default_factory=list)


def _clip(text, limit: int = QUOTE_CHARS) -> str:
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[:limit - 1].rstrip() + "…"


# ----------------------------------------------------------------------
# Context shared by all chapters
# ----------------------------------------------------------------------

def _by_chapter(records) -> dict:
    grouped: dict[int, list] = {}
    for record in records or []:
        chapter = record.get("chapter") if isinstance(record, dict) else None
        if isinstance(chapter, int) and not isinstance(chapter, bool):
            grouped.setdefault(chapter, []).append(record)
    return {c: items[-MAX_RECORDS_PER_CHAPTER:] for c, items in grouped.items()}


def _incomplete(state) -> dict:
    records = state.get("incomplete") if isinstance(state, dict) else None
    if not isinstance(records, dict):
        return {}
    return {
        int(key): str(info.get("reason") or "")
        for key, info in records.items()
        if isinstance(key, str) and key.isdigit() and isinstance(info, dict)
    }


def _read_log(book_dir: str, filename: str, warnings: list) -> list:
    records, err = qalog.read_records(book_dir, filename)
    if err:
        warnings.append(err)
    return records


def load_context(book_dir: str) -> AuditContext:
    """Glossary, characters, style caps, length ratios and the QA logs, read once for a whole audit."""
    warnings: list[str] = []
    sample, err = qa.load_ratio_sample(book_dir)
    if err:
        warnings.append(err)
    state, err = storage.load_queue_state(book_dir)
    if err:
        warnings.append(err)
    guide = style.load_style(book_dir)
    warnings.extend(f"{style.STYLE_FILENAME}: {e}" for e in guide.errors)
    fixes = _read_log(book_dir, qalog.SOURCE_FIXES_FILENAME, warnings)
    reviews = _read_log(book_dir, qalog.REVIEW_FILENAME, warnings)
    char_map = characters.load_characters(book_dir)
    return AuditContext(
        terms=glossary.load_terms(book_dir),
        char_map=char_map if isinstance(char_map, dict) else {},
        caps=guide.caps,
        usual=qa.usual_ratio(sample),
        fixes=_by_chapter(r for r in fixes if r.get("status") == "unverified"),
        kept=_by_chapter(_read_log(book_dir, qalog.CONTINUITY_FILENAME, warnings)),
        rejected=_by_chapter(r for r in reviews if r.get("status") == "rejected"),
        incomplete=_incomplete(state),
        warnings=warnings,
    )


# ----------------------------------------------------------------------
# Checks
# ----------------------------------------------------------------------

def english_blocks(text) -> tuple[list[str], list[tuple[int, str]]]:
    """Paragraphs of a translated chapter, and its TNs as (paragraphs before the TN, TN text)."""
    paras, notes = [], []
    for block in _BLOCK_SPLIT_RE.split(str(text or ""))[:MAX_BLOCKS]:
        stripped = block.strip()
        if not stripped or _LINK_RE.match(stripped):
            continue
        if _TN_RE.match(stripped):
            notes.append((len(paras), stripped))
            continue
        paras.append(stripped)
    return paras, notes


def _zh_quote(source, forms) -> tuple:
    for index, paragraph in enumerate(source.zh):
        if paragraph and any(form and form in paragraph for form in forms):
            prefix = f"⟦{index}⟧ " if source.aligned else ""
            return (f"{prefix}ZH: {_clip(paragraph)}",)
    return ()


def _pair_quote(source, marker: int) -> str:
    chinese = source.zh[marker] if 0 <= marker < len(source.zh) else ""
    english = source.en.get(marker, "MISSING")
    return f"⟦{marker}⟧ ZH: {_clip(chinese)}\n     EN: {_clip(english)}"


def _aligned_omissions(source, data) -> list[Finding]:
    found = []
    missing = source.missing
    if missing:
        quotes = tuple(f"⟦{m}⟧ ZH: {_clip(source.zh[m])}" for m in missing[:MAX_QUOTES])
        found.append(Finding("omission", f"Paragraphs {segments.format_ids(missing)} have no English.", quotes))
    unmatched = data.get("unmatchedEnglish") if isinstance(data, dict) else None
    texts = [t for t in unmatched if isinstance(t, str)] if isinstance(unmatched, list) else []
    if texts:
        quotes = tuple(f"EN: {_clip(t)}" for t in texts[:MAX_QUOTES])
        found.append(Finding("addition", f"{len(texts)} English paragraph(s) match no Chinese paragraph.", quotes))
    if source.skips:
        quotes = tuple(f"⟦{m}⟧ ZH: {_clip(source.zh[m])}" for m in sorted(source.skips)[:MAX_QUOTES] if m < len(source.zh))
        found.append(Finding("skip", f"Declared untranslated: {segments.describe_skips(source.skips)}.", quotes, True))
    return found


def _count_omissions(source) -> list[Finding]:
    paras, _notes = english_blocks(source.text)
    zh_count = sum(1 for p in source.zh if p)
    gap = max(MIN_PARAGRAPH_GAP, int(zh_count * PARAGRAPH_GAP_SHARE))
    counts = f"{len(paras)} English paragraphs for {zh_count} Chinese ones"
    if len(paras) < zh_count - gap:
        text = f"No alignment file, and {counts}: paragraphs may be missing or merged. audit.py --align gives exact checks."
        return [Finding("omission", text)]
    return [Finding("omission", f"No alignment file; omissions were checked by paragraph count only ({counts}).", info=True)]


def _length_findings(source, usual: float) -> list[Finding]:
    if source.aligned:
        flags = qa.length_flags(qa.paragraph_ratios(source.zh, source.en), usual)
        if not flags:
            return []
        quotes = tuple(_pair_quote(source, marker) for marker, _f in flags[:MAX_QUOTES])
        return [Finding("length", f"Unusual English length: {qa.describe_length_flags(flags)}.", quotes)]
    zh_len = sum(len("".join(p.split())) for p in source.zh[1:])
    if zh_len < MIN_CHAPTER_ZH:
        return []
    paras, _notes = english_blocks(source.text)
    factor = sum(len(p) for p in paras if not p.startswith("#")) / zh_len / usual
    if CHAPTER_SHORT <= factor <= CHAPTER_LONG:
        return []
    word = "short" if factor < 1 else "long"
    return [Finding("length", f"The whole chapter is {word} ({factor:.2g}× the usual English length).")]


def _note_findings(source) -> list[Finding]:
    found = []
    for line in source.text.splitlines():
        stripped = line.strip()
        if not _TN_RE.match(stripped):
            continue
        reason = qa.check_inline_note(stripped)
        if reason:
            found.append(Finding("note", f"Translator's Note to check: {reason}.", (f"TN: {_clip(stripped)}",)))
    return found[:MAX_QUOTES]


def _name_term_findings(source, ctx: AuditContext) -> list[Finding]:
    zh_text = "\n".join(source.zh)
    found = []
    for ev in characters.name_drift(ctx.char_map, zh_text, source.text):
        text = f"The Chinese names {ev['forms']} {ev['count']} times, but “{ev['english']}” never appears in the English."
        found.append(Finding("name", text, _zh_quote(source, ev["forms"].split(" / "))))
    for ev in glossary.check_term_usage(ctx.terms, [(f"Chapter {source.chapter}", zh_text, source.text)]):
        text = f"Locked term {ev['forms']} should appear as “{ev['english']}”, but it does not."
        found.append(Finding("term", text, _zh_quote(source, ev["forms"].split(" / "))))
    return found


def _review_finding(record: dict) -> Finding:
    pairs = record.get("pairs") if isinstance(record.get("pairs"), list) else []
    quotes = tuple(
        f"Search: {_clip(p.get('search'))}\nReplace: {_clip(p.get('replace'))}" for p in pairs[:1] if isinstance(p, dict)
    )
    text = f"A reviewer edit was rejected ({record.get('why')}): {record.get('kind')} · {_clip(record.get('reason'))}"
    return Finding("review", text, quotes)


def _record_findings(chapter: int, ctx: AuditContext) -> list[Finding]:
    found = []
    for record in ctx.fixes.get(chapter, []):
        text = f"Unverified typo fix {qalog.describe_fix(record)} ({record.get('reason')}); the English may follow a wrong correction."
        found.append(Finding("source", text))
    found += [_review_finding(r) for r in ctx.rejected.get(chapter, [])]
    if chapter in ctx.incomplete:
        found.append(Finding("incomplete", f"On the Incomplete list: {ctx.incomplete[chapter]}."))
    for record in ctx.kept.get(chapter, []):
        found.append(Finding("continuity", f"Kept as written: {qalog.describe_continuity(record)}", info=True))
    return found


def _style_findings(source, ctx: AuditContext) -> list[Finding]:
    return [Finding("style", f.text, info=not f.grammar) for f in style.check_text(source.text, ctx.caps)]


def audit_chapter(book_dir: str, chapter: int, md_path, ctx: AuditContext) -> ChapterAudit:
    """Every finding for one saved chapter; error is set when it cannot be read."""
    source, err = edits.load_source(book_dir, chapter, md_path)
    if source is None:
        return ChapterAudit(chapter, error=err)
    data = segments.load_alignment(book_dir, chapter) if source.aligned else None
    findings = _aligned_omissions(source, data) if source.aligned else _count_omissions(source)
    findings += _length_findings(source, ctx.usual)
    findings += _note_findings(source)
    findings += _name_term_findings(source, ctx)
    findings += _record_findings(chapter, ctx)
    findings += _style_findings(source, ctx)
    return ChapterAudit(chapter, source.aligned, findings[:MAX_FINDINGS])


# ----------------------------------------------------------------------
# Findings prompt
# ----------------------------------------------------------------------

def _indent(text: str, pad: str) -> list[str]:
    return [pad + line for line in str(text).split("\n")]


def render_chapter(audit: ChapterAudit, files) -> str:
    """One chapter's block of the findings prompt."""
    lines = [f"## Chapter {audit.chapter}"]
    if files:
        lines.append("Files: " + ", ".join(files))
    lines.append("Alignment: exact (paragraph alignment file)" if audit.aligned
                 else "Alignment: none (paragraphs compared by count only)")
    for number, finding in enumerate(audit.problems, start=1):
        lines.append(f"{number}. [{finding.kind}] {finding.text}")
        for quote in finding.quotes:
            lines += _indent(quote, "   ")
    info = [f for f in audit.findings if f.info]
    if info:
        lines.append("Information only:")
        for finding in info:
            lines.append(f"- [{finding.kind}] {finding.text}")
            for quote in finding.quotes:
                lines += _indent(quote, "  ")
    return "\n".join(lines)


def render_header(book: str, chapters, part: int, parts: int, instructions: str = DEFAULT_INSTRUCTIONS) -> str:
    span = parsing.format_chapter_ranges(list(chapters)) or "no chapters"
    part_text = f" (part {part} of {parts})" if parts > 1 else ""
    return f"# Translation audit: {book}, {span}{part_text}\n\n{str(instructions).strip()}\n"


def _fit_block(text: str, limit: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    keep = max(0, limit - len(CUT_NOTE.encode("utf-8")) - 2)
    return encoded[:keep].decode("utf-8", "ignore").rstrip() + "\n" + CUT_NOTE


def plan_parts(blocks, max_bytes: int, reserve: int, fits) -> list[list[tuple]]:
    """Pack (chapter, text, files) blocks into parts whose prompt stays under max_bytes (reserve is kept
    for the header) and whose file list passes fits(files). A block too big alone is cut with a note.
    """
    if max_bytes <= reserve:
        raise ValueError("max_bytes must be larger than the header reserve")
    parts, current, size = [], [], reserve
    for chapter, text, files in blocks:
        block = _fit_block(text, max_bytes - reserve)
        need = len(block.encode("utf-8")) + 2
        candidate = [f for _c, _t, part_files in current for f in part_files] + list(files)
        if current and (size + need > max_bytes or not fits(candidate)):
            parts.append(current)
            current, size = [], reserve
        current.append((chapter, block, list(files)))
        size += need
    if current:
        parts.append(current)
    return parts


# ----------------------------------------------------------------------
# Building an alignment file for an older chapter
# ----------------------------------------------------------------------

def build_align_messages(zh: list, paras: list) -> list[dict]:
    chinese = "\n".join(f"⟦{i}⟧ {p}" for i, p in enumerate(zh) if p)
    english = "\n".join(f"[E{i}] {' '.join(p.split())}" for i, p in enumerate(paras, start=1))
    return [
        {"role": "system", "content": ALIGN_SYSTEM},
        {"role": "user", "content": f"=== CHINESE ===\n{chinese}\n\n=== ENGLISH ===\n{english}"},
    ]


def parse_align_reply(raw) -> tuple[list, str | None]:
    parsed, err = parsing.load_json_reply(raw if isinstance(raw, str) else "")
    if err:
        return [], err
    items = parsed.get("map") if isinstance(parsed, dict) else parsed
    if not isinstance(items, list):
        return [], "'map' key must be a JSON array"
    return items[:MAX_BLOCKS], None


def _ints(value) -> list[int]:
    values = value if isinstance(value, list) else [value]
    return [v for v in values if isinstance(v, int) and not isinstance(v, bool)]


def _entries(items, zh_count: int, en_count: int) -> dict[int, list[int]]:
    entries: dict[int, list[int]] = {}
    for item in items or []:
        marker = item.get("zh") if isinstance(item, dict) else None
        if isinstance(marker, bool) or not isinstance(marker, int) or not 0 <= marker < zh_count or marker in entries:
            continue
        entries[marker] = sorted({e for e in _ints(item.get("en")) if 1 <= e <= en_count})
    return entries


def map_paragraphs(items, zh_count: int, en_count: int) -> tuple[dict, dict, list[str]]:
    """(marker -> English numbers, merged marker -> owning marker, problems) from the model's map."""
    mapping, merged, problems = {}, {}, []
    last, owner = 0, None
    entries = _entries(items, zh_count, en_count)
    for marker in sorted(entries):
        ids = entries[marker]
        if not ids:
            continue
        if ids == [last] and owner is not None:
            merged[marker] = owner
            continue
        if ids[0] <= last:
            problems.append(f"⟦{marker}⟧ points back to E{ids[0]}; left without English")
            continue
        mapping[marker] = ids
        last, owner = ids[-1], marker
    return mapping, merged, problems


def _note_owner(before: int, owner_of: dict) -> int | None:
    for number in range(before, 0, -1):
        if number in owner_of:
            return owner_of[number]
    return None


def alignment_record(chapter: int, zh: list, paras: list, notes: list, mapping: dict, merged: dict) -> dict:
    """An alignment file built from a validated map."""
    owner_of = {e: m for m, ids in mapping.items() for e in ids}
    unmatched = [p for i, p in enumerate(paras, start=1) if i not in owner_of and not p.startswith("#")]
    return {
        "version": segments.ALIGN_VERSION,
        "chapter": chapter,
        "zh": list(zh),
        "en": {str(m): "\n\n".join(paras[e - 1] for e in ids) for m, ids in mapping.items()},
        "skips": {str(m): f"merged into ⟦{o}⟧ (audit alignment)" for m, o in merged.items()},
        "notes": [{"afterId": _note_owner(before, owner_of), "text": text} for before, text in notes],
        "unmatchedEnglish": unmatched[:MAX_UNMATCHED],
        "created": time.time(),
        "source": "audit",
    }


def _read_pair(book_dir: str, chapter: int, md_path) -> tuple[str, str]:
    """(translation, raw page). Raises OSError/ValueError."""
    with open(md_path, "r", encoding="utf-8") as f:
        text = f.read()
    return text, storage.load_scraped_page(book_dir, chapter) or ""


def align_chapter(client, cfg, book_dir: str, chapter: int, md_path, cancel=None) -> tuple[bool, str]:
    """Ask cfg.model to align an existing chapter and save its alignment file. Returns (ok, message)."""
    if not md_path:
        return False, f"Chapter {chapter}: no translated file"
    try:
        text, page = _read_pair(book_dir, chapter, md_path)
    except (OSError, ValueError) as e:
        return False, f"Chapter {chapter}: could not read ({e})"
    parts = segments.split_page(page)
    zh = [parts.title, *parts.body]
    paras, notes = english_blocks(text)
    if not page or not parts.body or not paras:
        return False, f"Chapter {chapter}: nothing to align (no raw page or no text)"
    messages = build_align_messages(zh, paras)
    if sum(len(m["content"]) for m in messages) > MAX_ALIGN_CHARS:
        return False, f"Chapter {chapter}: too long to align in one call"
    try:
        raw, _usage = client.chat(cfg, messages, max_tokens=ALIGN_MAX_TOKENS, cancel=cancel, label=f"Align Chapter {chapter}")
    except ApiError as e:
        return False, f"Chapter {chapter}: {e.summary}"
    items, err = parse_align_reply(raw)
    if err:
        return False, f"Chapter {chapter}: unusable reply ({err})"
    mapping, merged, problems = map_paragraphs(items, len(zh), len(paras))
    record = alignment_record(chapter, zh, paras, notes, mapping, merged)
    try:
        segments.save_alignment(book_dir, chapter, record)
    except (OSError, ValueError, TypeError) as e:
        return False, f"Chapter {chapter}: could not save the alignment ({e})"
    missing = edits.missing_markers(zh, {int(k): v for k, v in record["en"].items()}, {int(k): v for k, v in record["skips"].items()})
    message = (
        f"Chapter {chapter}: aligned {len(mapping)} paragraph(s), {len(merged)} merged, {len(missing)} without English, "
        f"{len(record['unmatchedEnglish'])} English paragraph(s) unmatched"
    )
    if problems:
        message += f"; {len(problems)} map entr{'y' if len(problems) == 1 else 'ies'} refused ({problems[0]})"
    return True, message
