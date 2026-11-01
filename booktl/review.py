"""Independent review of saved translations by a second model, with search/replace fixes.

After each batch, every saved chapter is checked again from disk (missing paragraphs, odd lengths,
name drift, missing locked terms, unverified typo fixes). Chapters with findings are sent to the
reviewer model; 'review all on' sends every chapter. Each chapter goes out as its Chinese and English
paragraphs side by side, with the findings as leads and the book's locked terms and character notes
as context. The reviewer answers with search/replace edits, which edits.py checks and applies.
"""

from dataclasses import dataclass, field

from rich.markup import escape

from . import characters, edits, glossary, parsing, prompts, qa, qalog, segments, style, termlookup
from . import console as con
from .cancel import ApiCancelled
from .client import ApiError
from .parsing import format_chapter_ranges

MAX_REQUEST_CHARS = 60_000   # chapters per call are packed up to this size
MAX_REVIEW_CALLS = 10
MAX_CHAPTERS = 200
MAX_HINTS = 12
MAX_LOGGED = 8
CLIP_CHARS = 200

REVIEW_SYSTEM = (
    "You are an independent reviewer of Chinese-to-English novel translations. Another model translated the "
    "chapters you are given. Compare each English paragraph with its Chinese source and fix real errors only.\n\n"
    "Fix:\n"
    "  - omission: Chinese content missing from the English (a paragraph, a sentence, a line of dialogue, a detail);\n"
    "  - addition: English content that is not in the Chinese;\n"
    "  - mistranslation: English that changes the meaning (wrong subject, action or number, or a reversed meaning);\n"
    "  - name / term: a character name or locked term rendered differently from the book context;\n"
    "  - voice: a character's lines that ignore the Voice notes in the character context;\n"
    "  - grammar: plain English errors such as \"an foreign\", a doubled word, or Chinese characters left in the English.\n\n"
    "Do NOT:\n"
    "  - rewrite for style or polish the prose. The translation keeps the author's style, tone, pacing, repetition "
    "and paragraph breaks on purpose, even where they are plain or clumsy. If the meaning is right, leave it;\n"
    "  - change Translator's Notes (TN lines), or remove a locked term or character name the Chinese contains;\n"
    "  - correct the author's own mistakes (a wrong name, a contradiction); they are translated as written on purpose;\n"
    "  - edit because of findings marked \"Information only\" (how often a phrase is used); they are context, "
    "not requests. Follow the STYLE GUIDE only to fix a clear break of one of its rules.\n"
    "When unsure, change nothing. An empty edit list is a good answer.\n\n"
    "=== EDITS ===\n"
    "Every edit is an exact search/replace on the saved English chapter:\n"
    "  - \"search\" is copied character for character from an EN line you were shown (without the \"EN: \" label), "
    "is long enough to occur only once in the chapter, and stays inside one paragraph;\n"
    "  - \"replace\" is the corrected text; keep everything you are not fixing exactly as it was;\n"
    "  - to add a missing paragraph ⟦n⟧, search for the complete EN text of the nearest earlier paragraph that is "
    "present (or its last TN line) and replace it with that same text, a blank line, then ⟦n⟧ and the new "
    "paragraph. A ⟦n⟧ marker may only start such an inserted paragraph.\n"
    "Edits that break these rules are rejected automatically, and every rejection is reported.\n\n"
    "RESPONSE FORMAT - respond with ONLY a valid JSON object. No preamble, no explanation, no markdown code fences.\n"
    "{\"phase\": \"REVIEW\", \"summary\": \"One or two sentences on what you found.\", \"edits\": [{\"chapter\": 812, "
    "\"kind\": \"omission\", \"reason\": \"⟦15⟧ was missing\", \"search_replace\": [{\"search\": \"He nodded.\", "
    "\"replace\": \"He nodded.\\n\\n⟦15⟧ She left the hall.\"}]}]}\n"
    "kind is one of: omission, addition, mistranslation, name, term, voice, grammar."
)

REVIEW_TURN = (
    "Review these chapters. Each Chinese paragraph (ZH) is shown with its saved English (EN), the Translator's "
    "Notes after it (TN), MISSING when no English exists, and SKIPPED when the translator declared it untranslated. "
    "Automatic check results come first; treat them as leads, not facts."
)


@dataclass
class ReviewTarget:
    source: edits.ChapterSource
    hints: list


@dataclass
class ReviewOutcome:
    model: str = ""
    targets: int = 0
    clean: int = 0
    calls: int = 0
    tokens: int = 0
    results: list = field(default_factory=list)     # edits.ChapterResult
    rejected: list = field(default_factory=list)    # edits that could not be read
    summaries: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    completed: list = field(default_factory=list)   # chapters whose missing paragraphs are now all filled
    cancelled: bool = False


@dataclass
class _HintContext:
    terms: dict
    char_map: dict
    usual: float
    unverified: dict
    caps: dict = field(default_factory=dict)


def _clip(text, limit: int = CLIP_CHARS) -> str:
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[:limit - 1].rstrip() + "…"


# ----------------------------------------------------------------------
# Choosing chapters
# ----------------------------------------------------------------------

def _hint_context(book_dir: str, warnings: list) -> _HintContext:
    sample, err = qa.load_ratio_sample(book_dir)
    if err:
        warnings.append(err)
    records, err = qalog.read_records(book_dir, qalog.SOURCE_FIXES_FILENAME)
    if err:
        warnings.append(err)
    unverified: dict[int, list[str]] = {}
    for record in records:
        if record.get("status") == "unverified" and isinstance(record.get("chapter"), int):
            unverified.setdefault(record["chapter"], []).append(qalog.describe_fix(record))
    char_map = characters.load_characters(book_dir)
    guide = style.load_style(book_dir)
    warnings.extend(f"{style.STYLE_FILENAME}: {error}" for error in guide.errors)
    return _HintContext(glossary.load_terms(book_dir), char_map if isinstance(char_map, dict) else {},
                        qa.usual_ratio(sample), unverified, guide.caps)


def _alignment_hints(source: edits.ChapterSource, usual: float) -> list[str]:
    if not source.aligned:
        return ["This chapter has no paragraph alignment, so omissions could not be checked; compare the whole chapter."]
    hints = []
    if source.missing:
        hints.append(f"Paragraphs {segments.format_ids(source.missing)} are missing from the English.")
    flags = qa.length_flags(qa.paragraph_ratios(source.zh, source.en), usual)
    if flags:
        hints.append(f"Unusual English length (possible omission or addition): {qa.describe_length_flags(flags)}.")
    return hints


def chapter_hints(source: edits.ChapterSource, ctx: _HintContext) -> list[str]:
    """Findings of the automatic checks for one saved chapter; [] when it passes them all."""
    hints = _alignment_hints(source, ctx.usual)
    zh_text = "\n".join(source.zh)
    for ev in characters.name_drift(ctx.char_map, zh_text, source.text):
        hints.append(f"The Chinese names {ev['forms']} {ev['count']} times, but “{ev['english']}” never appears in the English.")
    for ev in glossary.check_term_usage(ctx.terms, [(f"Chapter {source.chapter}", zh_text, source.text)]):
        hints.append(f"Locked term {ev['forms']} should appear as “{ev['english']}”, but that rendering is missing.")
    for text in ctx.unverified.get(source.chapter, []):
        hints.append(f"Unverified typo fix: {text}. Check that the English follows the Chinese.")
    for finding in style.check_text(source.text, ctx.caps):
        if finding.grammar:
            hints.append(f"Style check: {finding.text}.")
    return hints[:MAX_HINTS]


def low_priority_hints(source: edits.ChapterSource, ctx: _HintContext) -> list[str]:
    """Phrase-frequency findings: shown when a chapter is reviewed anyway, never a reason to review it."""
    findings = style.check_text(source.text, ctx.caps)
    return [f"Information only, do not edit for this: {f.text}." for f in findings if not f.grammar][:MAX_HINTS]


def collect_targets(book_dir: str, chapters, force: bool, outcome: ReviewOutcome) -> list[ReviewTarget]:
    """Chapters to review: those with findings, or all of them when force is set."""
    try:
        translated = termlookup.translated_index(book_dir)
    except OSError as e:
        outcome.errors.append(f"Could not list the book folder: {e}")
        return []
    ctx = _hint_context(book_dir, outcome.warnings)
    targets = []
    for chapter in sorted({c for c in chapters or () if isinstance(c, int) and c > 0})[:MAX_CHAPTERS]:
        source, err = edits.load_source(book_dir, chapter, translated.get(chapter))
        if source is None:
            outcome.warnings.append(err)
            continue
        hints = chapter_hints(source, ctx)
        if hints or force:
            targets.append(ReviewTarget(source, hints + low_priority_hints(source, ctx)))
        else:
            outcome.clean += 1
    return targets


# ----------------------------------------------------------------------
# Building the request
# ----------------------------------------------------------------------

def _paired_lines(source: edits.ChapterSource) -> list[str]:
    notes: dict = {}
    for after, text in source.notes:
        notes.setdefault(after, []).append(text)
    lines = []
    for marker, chinese in enumerate(source.zh):
        if not chinese:
            continue
        lines.append(f"⟦{marker}⟧ ZH: {chinese}")
        if marker in source.en:
            lines.append(f"    EN: {source.en[marker]}")
        elif marker in source.skips:
            lines.append(f"    EN: SKIPPED ({source.skips[marker]})")
        else:
            lines.append("    EN: MISSING")
        lines.extend(f"    TN: {note}" for note in notes.get(marker, []))
    return lines


def render_target(target: ReviewTarget) -> str:
    """One chapter's part of the review request."""
    source = target.source
    lines = [f"=== Chapter {source.chapter} ===", "Automatic checks (they can be false alarms):"]
    lines += [f"- {hint}" for hint in target.hints] or ["- nothing flagged; full review requested"]
    if source.aligned:
        lines += _paired_lines(source)
    else:
        lines += ["ZH (whole chapter):", *source.zh, "", "EN (whole chapter, as saved):", source.text]
    lines.append(f"=== END Chapter {source.chapter} ===")
    return "\n".join(lines)


def _groups(targets: list) -> list[list[ReviewTarget]]:
    groups, current, size = [], [], 0
    for target in targets:
        block = len(render_target(target))
        if current and size + block > MAX_REQUEST_CHARS:
            groups.append(current)
            current, size = [], 0
        current.append(target)
        size += block
    if current:
        groups.append(current)
    return groups


def build_messages(book_dir: str, group: list) -> list[dict]:
    """Reviewer instructions, book context for these chapters, and the chapters themselves."""
    body = "\n\n".join(render_target(t) for t in group)
    chinese = "\n".join("\n".join(t.source.zh) for t in group)
    messages = [{"role": "system", "content": REVIEW_SYSTEM}]
    context = prompts.book_context(book_dir, chinese)
    if context:
        messages.append({"role": "system", "content": context})
    messages.append({"role": "user", "content": f"{REVIEW_TURN}\n\n{body}"})
    return messages


# ----------------------------------------------------------------------
# Running a review
# ----------------------------------------------------------------------

def _tokens(usage) -> int:
    total = usage.get("total_tokens") if isinstance(usage, dict) else None
    return int(total) if isinstance(total, (int, float)) and not isinstance(total, bool) else 0


def _apply_chapter(book_dir: str, source, chapter_edits: list, batch_ref, model: str, outcome: ReviewOutcome) -> None:
    result = edits.apply_chapter(book_dir, source, chapter_edits)
    outcome.results.append(result)
    try:
        edits.log_result(book_dir, result, batch_ref, model)
    except (OSError, ValueError) as e:
        outcome.warnings.append(f"Could not write {qa.QA_DIRNAME}/{qalog.REVIEW_FILENAME}: {e}")
    if source.missing and result.applied and not result.missing_after:
        outcome.completed.append(source.chapter)


def _review_group(client, cfg, group: list, batch_ref, cancel, label: str, outcome: ReviewOutcome) -> None:
    """One reviewer call and its edits. Raises ApiCancelled."""
    book_dir, model = cfg.book_dir, cfg.reviewer_model
    span = format_chapter_ranges([t.source.chapter for t in group])
    con.sent(f"{escape(label)}: sent {escape(span)} to [bold]{escape(model)}[/bold]")
    try:
        raw, usage = client.chat(cfg, build_messages(book_dir, group), cancel=cancel, label=label, model=model)
    except ApiError as e:
        outcome.errors.append(f"Review of {span} failed: {e.summary}")
        return
    outcome.calls += 1
    outcome.tokens += _tokens(usage)
    summary, items, err = parsing.parse_review_reply(raw if isinstance(raw, str) else "")
    if err:
        outcome.errors.append(f"Review of {span}: unusable reply ({err})")
        return
    if summary:
        outcome.summaries.append(_clip(summary))
    sources = {t.source.chapter: t.source for t in group}
    found, unreadable = edits.normalise_edits(items, set(sources))
    outcome.rejected.extend(unreadable)
    for chapter in sorted({e.chapter for e in found}):
        chapter_edits = [e for e in found if e.chapter == chapter]
        _apply_chapter(book_dir, sources[chapter], chapter_edits, batch_ref, model, outcome)


def run(client, cfg, chapters, force: bool, batch_ref, cancel, label: str) -> ReviewOutcome:
    """Review chapters with cfg.reviewer_model and apply its edits. A cancel ends it early; never raises it."""
    outcome = ReviewOutcome(model=getattr(cfg, "reviewer_model", "") or "")
    if not cfg.book_dir or not outcome.model:
        outcome.errors.append("No book or no reviewer model selected.")
        return outcome
    targets = collect_targets(cfg.book_dir, chapters, force, outcome)
    outcome.targets = len(targets)
    groups = _groups(targets)
    if len(groups) > MAX_REVIEW_CALLS:
        outcome.warnings.append(f"Only {MAX_REVIEW_CALLS} review calls were sent; review the rest with 'review <chapters>'.")
    for group in groups[:MAX_REVIEW_CALLS]:
        if cancel is not None and cancel.is_cancelled:
            outcome.cancelled = True
            break
        try:
            _review_group(client, cfg, group, batch_ref, cancel, label, outcome)
        except ApiCancelled:
            outcome.cancelled = True
            break
    return outcome


# ----------------------------------------------------------------------
# Batch log
# ----------------------------------------------------------------------

def _log_head(log, outcome: ReviewOutcome) -> None:
    if not outcome.targets:
        log.head("info", f"No review needed: {outcome.clean} chapter(s) passed every check")
        return
    applied = sum(len(r.applied) for r in outcome.results)
    rejected = sum(len(r.rejected) for r in outcome.results) + len(outcome.rejected)
    text = f"Reviewed {outcome.targets} chapter(s) with [bold]{escape(outcome.model)}[/bold] · {applied} edit(s) applied"
    if rejected:
        text += f", [yellow]{rejected} rejected[/yellow]"
    if outcome.clean:
        text += f" · {outcome.clean} chapter(s) passed every check"
    log.head("warn" if outcome.errors or rejected else "ok", text)


def _log_result(log, result) -> None:
    for edit in result.applied[:MAX_LOGGED]:
        log.detail(f"Chapter {result.chapter}: {edit.kind} · {escape(_clip(edit.reason))}")
    for edit, why in result.rejected[:MAX_LOGGED]:
        log.warn(f"Chapter {result.chapter}: rejected {edit.kind} edit ({escape(why)}) · {escape(_clip(edit.reason))}")
    if result.error:
        log.warn(f"Chapter {result.chapter}: {escape(result.error)}")


def log_outcome(log, outcome: ReviewOutcome) -> None:
    """Headline plus one line per edit, rejection, error and warning."""
    _log_head(log, outcome)
    for summary in outcome.summaries:
        log.detail(f"Reviewer: {escape(summary)}")
    for result in outcome.results:
        _log_result(log, result)
    for reason in outcome.rejected[:MAX_LOGGED]:
        log.warn(f"Unreadable edit: {escape(reason)}")
    for error in outcome.errors:
        log.fail(escape(error))
    for warning in outcome.warnings[:MAX_LOGGED]:
        log.warn(escape(warning))
    if outcome.completed:
        log.detail(f"Now complete: {format_chapter_ranges(outcome.completed)}")
    if outcome.cancelled:
        log.warn("Review cancelled; edits already applied were kept ('review undo <chapter>' restores a chapter)")
