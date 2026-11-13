"""Audit translated chapters offline and send the problem chapters to combineCopy with the findings.

  python audit.py -b "My Book" --range 1200-1250
  python audit.py --next 25                 the 25 chapters after the last audit
  python audit.py --end 100 --align         first build missing paragraph alignments with a model
  python audit.py --middle 20 --dry-run     print the commands and save the findings only

Pick a book and a range as in inquire.py, or continue after the last audit with --next N. Every chapter
in the range is checked from disk (no model is called unless --align is given). The chapters with
problems are listed, the prompt is saved to <Book>/_qa/audit/audit_<time>.md, and combineCopy runs with
--prompt-pipe and the English file and raw page of every chapter sent, with the prompt piped in:

  combineCopy --prompt-pipe './Book/Chapter 812.md' './Book/_raw/chapters/ch_000812.txt' …

Findings larger than combineCopy's 2 MB limit, or a command line too long for Windows, are split into
parts by chapter; the script waits for Enter between parts. A part counts as audited once combineCopy
finishes it, and <Book>/_qa/audit/progress.json records how far the book has been audited.

By default the prompt also asks the receiving model to add the Translator's Notes a sent chapter is
missing, with worked examples; --no-tns leaves that out. --extra-tns also sends the chapters without
findings and asks for poem and film sources, title references and author slips. Translator's Notes stay
inline, so auditor's [note] findings are left out either way.
"""

import argparse
import dataclasses
import os
import sys
import time
from collections import Counter

from rich.markup import escape
from rich.table import Table

from booktl import auditlog, auditor, combine, segments
from booktl import console as con
from booktl.client import ApiClient
from booktl.config import Config, ConfigError
from booktl.parsing import format_chapter_ranges
from inquire import (
    MAX_INPUT_ATTEMPTS, MODE_LABELS, MODES, add_range_args, ask_span, choose, confirm, pick_book, span_from_args,
)

DEFAULT_LIBRARY = "../TranslateBook"
DEFAULT_COMMAND = "combineCopy"
DEFAULT_PROMPT_BYTES = 1_900_000
HEADER_SLACK = 512
FINDINGS_DIRNAME = "audit"
MAX_TABLE_ROWS = 300
MAX_LISTED_ERRORS = 10
MAX_INSTRUCTIONS_CHARS = 20_000
EXIT_OK, EXIT_USER, EXIT_COMMAND = 0, 1, 2
NEXT_LABEL = "Next N after the last audit"
PS_NOTE = (
    "combineCopy runs through PowerShell here. If it is a function in your profile, it must forward its "
    "pipeline input or it receives no prompt, e.g.:\n"
    "  function combineCopy { if ($MyInvocation.ExpectingInput) { $input | combineCopy.exe @args } "
    "else { combineCopy.exe @args } }\n"
    "The findings are also saved to {path}."
)
NOTE_KIND = "note"
CLEAN_CHAPTER_NOTE = (
    "No automatic findings. Check this chapter only for missing translator's notes (see the examples above)."
)
EXTRA_TN_INSTRUCTIONS = """\
## More translator's notes
Some chapters below have no automatic findings; they are included only for the note check above. In every
chapter, also add notes for:
- lines taken from poems, songs, classics or films (name the work and its author; quote at most one line);
- references to the novel's own title, or to real people, places and events;
- author slips the translation copies or quietly fixes, such as a count that does not match its list or a
  number that contradicts an earlier chapter;
- typos in the source that change how a line reads.
Follow the rules above, and leave a note out when you are unsure where a line comes from."""


# ----------------------------------------------------------------------
# Arguments
# ----------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Audit translated chapters and send problem chapters to combineCopy.")
    parser.add_argument("-b", "--book", help="Book folder name (skips the book menu)")
    parser.add_argument("-l", "--library", default=DEFAULT_LIBRARY, help="Library folder (default: ../TranslateBook)")
    ranges = add_range_args(parser)
    ranges.add_argument("--next", dest="next_count", metavar="N", help="The N chapters after the last audit")
    parser.add_argument("-y", "--yes", action="store_true", help="Do not ask for confirmation or wait between parts")
    parser.add_argument("--dry-run", action="store_true", help="Save the findings and print the commands without running them")
    parser.add_argument("--command", default=DEFAULT_COMMAND, help="Command to run (default: combineCopy)")
    parser.add_argument("--findings-out", help="Where to save the findings (default: <Book>/_qa/audit/audit_<time>.md)")
    parser.add_argument("--instructions", help="File with your own instructions to put above the findings")
    notes = parser.add_mutually_exclusive_group()
    notes.add_argument("--no-tns", action="store_true",
                       help="Do not ask the receiving model for missing translator's notes")
    notes.add_argument("--extra-tns", action="store_true",
                       help="Also send chapters without findings for the note check; costs far more tokens")
    parser.add_argument("--no-extra-tns", action="store_true",
                        help="The default; accepted so older commands keep working")
    parser.add_argument("--max-prompt-bytes", type=int, default=DEFAULT_PROMPT_BYTES,
                        help=f"Largest prompt per combineCopy run (at most {combine.PIPE_LIMIT_BYTES:,})")
    parser.add_argument("--align", action="store_true", help="First build paragraph alignments for chapters without one (one model call each)")
    parser.add_argument("--realign", action="store_true", help="With --align, also rebuild existing alignments")
    parser.add_argument("-u", "--base-url", help="OpenAI v1 base URL for --align")
    parser.add_argument("-m", "--model", help="Model for --align")
    parser.add_argument("-k", "--api-key", help="API key for --align")
    return parser


def _base_instructions(path) -> str:
    """Instructions text from --instructions, or the default. Raises OSError/ValueError."""
    if not path:
        return auditor.DEFAULT_INSTRUCTIONS
    with open(path, "r", encoding="utf-8") as f:
        text = f.read().strip()
    if not text or len(text) > MAX_INSTRUCTIONS_CHARS:
        raise ValueError(f"Instructions must be 1–{MAX_INSTRUCTIONS_CHARS:,} characters long.")
    return text


def _instructions(path, tns: bool, extra_tns: bool) -> str:
    """Base instructions, the note check unless turned off, and the extra note topics. Raises OSError/ValueError."""
    parts = [_base_instructions(path).rstrip()]
    if tns or extra_tns:
        parts.append(auditor.TN_PASS_INSTRUCTIONS)
    if extra_tns:
        parts.append(EXTRA_TN_INSTRUCTIONS)
    return "\n\n".join(parts)


# ----------------------------------------------------------------------
# Choosing the range
# ----------------------------------------------------------------------

def _through(progress) -> int | None:
    return progress.audited_through if progress is not None else None


def _show_progress(book_dir: str):
    """Print how far the book has been audited and return its Progress (None when nothing is recorded)."""
    progress, err = auditlog.load_progress(book_dir)
    if err:
        con.warn(escape(err))
    if progress is not None:
        con.info(escape(auditlog.describe(progress)))
    return progress


def _ask_value(mode: str, last: int, through) -> tuple[int, int] | None:
    """Ask for the number or range of a mode. None when cancelled."""
    prompt = "Range, e.g. 140-175 (blank to cancel): " if mode == "range" else "Number of chapters (blank to cancel): "
    for _ in range(MAX_INPUT_ATTEMPTS):
        try:
            answer = con.console.input(f"[bold]{escape(prompt)}[/bold]").strip()
        except (KeyboardInterrupt, EOFError):
            return None
        if not answer:
            return None
        try:
            if mode == "next":
                return auditlog.next_span(through, combine.parse_count(answer), last)
            return combine.resolve_range(mode, answer, last)
        except ValueError as e:
            con.warn(escape(str(e)))
    con.warn("Too many invalid answers.")
    return None


def _ask_audit_span(last: int, progress) -> tuple[int, int] | None:
    """The range menu, with 'Next N after the last audit' first when there is something left to audit."""
    through = _through(progress)
    if through is None:
        return ask_span(last)
    if through >= last:
        con.info(f"Every translated chapter up to Chapter {last} is already audited.")
        return ask_span(last)
    middle = (1 + last) // 2
    labels = [NEXT_LABEL, *MODE_LABELS]
    hints = [
        f"from Chapter {through + 1}, up to Chapter {last}", "Chapters 1 … N",
        f"the last N, up to Chapter {last}", f"N chapters around Chapter {middle}", "e.g. 140-175",
    ]
    title = f"Chapter range (Chapters 1–{last} translated, audited through Chapter {through})"
    index = choose(title, labels, hints)
    if index is None:
        return None
    return _ask_value("next" if index == 0 else MODES[index - 1], last, through)


def _span(args, last: int, progress) -> tuple[int, int] | None:
    """(low, high) from the arguments or the menu; None when cancelled. Raises ValueError."""
    if args.next_count is not None:
        if progress is None:
            con.info("No earlier audit is recorded, so --next starts at Chapter 1.")
        return auditlog.next_span(_through(progress), combine.parse_count(args.next_count), last)
    return span_from_args(args, last) or _ask_audit_span(last, progress)


# ----------------------------------------------------------------------
# Alignment pass
# ----------------------------------------------------------------------

def _align_targets(book_dir: str, chapters: list, realign: bool) -> list[int]:
    return [c for c in chapters if realign or segments.load_alignment(book_dir, c) is None]


def _align_config(args, book_dir: str, book: str):
    try:
        cfg = Config.from_args(args)
    except ConfigError as e:
        con.fail(escape(str(e)))
        return None, None
    cfg.set_book(book_dir, book)
    client = ApiClient()
    if not cfg.model and not client.select_model(cfg):
        return None, None
    return cfg, client


def run_alignment(args, book_dir: str, book: str, chapters: list, translated: dict) -> bool:
    """Build alignment files with a model. Returns False when the user cancelled."""
    targets = _align_targets(book_dir, chapters, args.realign)
    if not targets:
        con.info("Every chapter in the range already has a paragraph alignment.")
        return True
    cfg, client = _align_config(args, book_dir, book)
    if cfg is None:
        return False
    question = f"Align {len(targets)} chapter(s) with {cfg.model}? One call per chapter. [Y/n]: "
    if not args.yes and not confirm(question):
        return False
    try:
        for chapter in targets:
            ok, message = auditor.align_chapter(client, cfg, book_dir, chapter, os.path.join(book_dir, translated[chapter]))
            (con.ok if ok else con.warn)(escape(message))
    except KeyboardInterrupt:
        con.warn("Alignment stopped; chapters already aligned keep their files.")
    return True


# ----------------------------------------------------------------------
# Audit and report
# ----------------------------------------------------------------------

def _drop_note_findings(audit):
    """The audit without [note] findings: TNs stay inline here, so auditor's notes-list advice does not apply."""
    if audit.error or not any(f.kind == NOTE_KIND for f in audit.findings):
        return audit
    kept = [f for f in audit.findings if f.kind != NOTE_KIND]
    return dataclasses.replace(audit, findings=kept)


def audit_range(book_dir: str, chapters: list, translated: dict) -> list:
    ctx = auditor.load_context(book_dir)
    for warning in ctx.warnings[:MAX_LISTED_ERRORS]:
        con.warn(escape(warning))
    audits = [auditor.audit_chapter(book_dir, c, os.path.join(book_dir, translated[c]), ctx) for c in chapters]
    return [_drop_note_findings(a) for a in audits]


def _kinds(audit) -> str:
    counts = Counter(f.kind for f in audit.problems)
    return ", ".join(f"{kind} ×{n}" if n > 1 else kind for kind, n in counts.items())


def print_summary(audits: list) -> list:
    """Print skipped chapters and the problem table. Returns the problem chapters."""
    errors = [a.error for a in audits if a.error]
    for error in errors[:MAX_LISTED_ERRORS]:
        con.warn(f"Skipped: {escape(error)}")
    if len(errors) > MAX_LISTED_ERRORS:
        con.dim(f"… and {len(errors) - MAX_LISTED_ERRORS} more skipped chapter(s).")
    problems = [a for a in audits if not a.error and a.problems]
    if problems:
        table = Table(title=f"Problem chapters ({len(problems)} of {len(audits)})", border_style="red")
        for column in ("Chapter", "Alignment", "Problems", "Info"):
            table.add_column(column)
        for audit in problems[:MAX_TABLE_ROWS]:
            info = sum(1 for f in audit.findings if f.info)
            table.add_row(str(audit.chapter), "exact" if audit.aligned else "count only", escape(_kinds(audit)), str(info or ""))
        con.console.print(table)
    clean = len(audits) - len(problems) - len(errors)
    con.info(f"{clean} chapter(s) without problems, {len(problems)} with problems, {len(errors)} skipped.")
    return problems


def _render_clean(chapter: int, files: list) -> str:
    names = ", ".join(str(f) for f in files)
    return f"## Chapter {chapter}\nFiles: {names}\n{CLEAN_CHAPTER_NOTE}"


def _blocks(library: str, book: str, translated: dict, audits: list) -> list:
    blocks = []
    for audit in audits:
        files = combine.collect_files(library, book, translated, audit.chapter, audit.chapter).files
        text = auditor.render_chapter(audit, files) if audit.problems else _render_clean(audit.chapter, files)
        blocks.append((audit.chapter, text, files))
    return blocks


def _chapters_to_send(audits: list, problems: list, extra_tns: bool) -> list:
    """Every readable chapter with --extra-tns, otherwise only the problem chapters."""
    if not extra_tns:
        return problems
    return [a for a in audits if not a.error]


def _fits_function(command: str, windows: bool):
    def fits(files) -> bool:
        try:
            combine.check_length(combine.build_pipe_command(command, files, windows)[0], windows)
        except (FileNotFoundError, ValueError):
            return False
        return True
    return fits


def _prompts(book: str, parts: list, instructions: str) -> list[str]:
    texts = []
    for number, part in enumerate(parts, start=1):
        header = auditor.render_header(book, [c for c, _t, _f in part], number, len(parts), instructions)
        texts.append(header + "\n" + "\n\n".join(text for _c, text, _f in part) + "\n")
    return texts


def save_findings(path: str, prompts: list[str]) -> bool:
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n\n---\n\n".join(prompts))
    except OSError as e:
        con.fail(f"Could not save the findings to {escape(path)}: {escape(str(e))}")
        return False
    con.ok(f"Findings saved to {escape(path)}")
    return True


def _findings_path(args, book_dir: str) -> str:
    if args.findings_out:
        return os.path.abspath(args.findings_out)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return os.path.join(book_dir, "_qa", FINDINGS_DIRNAME, f"audit_{stamp}.md")


# ----------------------------------------------------------------------
# Audit progress
# ----------------------------------------------------------------------

def _record(book_dir: str, span: tuple[int, int], findings_path: str) -> None:
    """Record chapters as audited; a failure only warns."""
    try:
        progress = auditlog.record_audit(book_dir, span[0], span[1], findings_path)
    except (OSError, ValueError) as e:
        con.warn(f"Could not save the audit progress: {escape(str(e))}")
        return
    con.dim(f"Audited through Chapter {progress.audited_through}.")


def _part_spans(parts: list, span: tuple[int, int]) -> list[tuple[int, int]]:
    """The chapters each part covers: the first from the range start, the last up to the range end."""
    low, high = span
    spans, start = [], low
    for index, part in enumerate(parts):
        top = high if index == len(parts) - 1 else max(c for c, _t, _f in part)
        top = min(max(top, start), high)
        spans.append((min(start, high), top))
        start = top + 1
    return spans


def _recorder(book_dir: str, spans: list, findings_path: str):
    """Callback that records part number index (0-based) once combineCopy finished it."""
    def record(index: int) -> None:
        if 0 <= index < len(spans):
            _record(book_dir, spans[index], findings_path)
    return record


# ----------------------------------------------------------------------
# Running combineCopy
# ----------------------------------------------------------------------

def _wait_next(number: int, total: int) -> bool:
    try:
        answer = con.console.input(f"[bold]Press Enter to send part {number} of {total}, or q to stop: [/bold]")
    except (KeyboardInterrupt, EOFError):
        return False
    return answer.strip().lower() not in ("q", "quit", "n", "no")


def _print_dry_run(parts: list, prompts: list, command: str, windows: bool, library: str) -> None:
    con.rule("Dry run")
    con.info(f"Working folder: {escape(library)}")
    for number, (part, prompt) in enumerate(zip(parts, prompts), start=1):
        files = [f for _c, _t, part_files in part for f in part_files]
        argv, _how = combine.build_pipe_command(command, files, windows)
        size = len(prompt.encode("utf-8"))
        con.info(f"Part {number} of {len(parts)}: {len(part)} chapter(s), prompt {size:,} bytes")
        con.console.print("  " + escape(" ".join(str(a) for a in argv)), highlight=False)


def _send_part(command: str, files: list, prompt: str, windows: bool, library: str, state: dict) -> bool:
    argv, how = combine.build_pipe_command(command, files, windows)
    combine.check_length(argv, windows)
    if how == "powershell" and not state.get("warned"):
        con.warn(escape(PS_NOTE.replace("{path}", state.get("path", "the findings file"))))
        state["warned"] = True
    con.rule(f"{escape(command)} output")
    code = combine.run_combine(argv, library, data=prompt.encode("utf-8"))
    con.rule()
    if code != 0:
        con.fail(f"{escape(command)} {'could not run' if code is None else f'exited with code {code}'}.")
        return False
    return True


def send_parts(args, parts: list, prompts: list, library: str, findings_path: str, on_sent=None) -> int:
    """Run combineCopy once per part. on_sent(index) is called after each part that finished."""
    if on_sent is not None and not callable(on_sent):
        raise TypeError("on_sent must be callable")
    windows = os.name == "nt"
    state = {"path": findings_path}
    for number, (part, prompt) in enumerate(zip(parts, prompts), start=1):
        if number > 1 and not args.yes and not _wait_next(number, len(parts)):
            con.warn(f"Stopped before part {number}; the rest of the findings are in {escape(findings_path)}.")
            return EXIT_USER
        files = [f for _c, _t, part_files in part for f in part_files]
        try:
            sent = _send_part(args.command, files, prompt, windows, library, state)
        except (FileNotFoundError, ValueError) as e:
            con.fail(escape(str(e)))
            return EXIT_COMMAND
        if not sent:
            return EXIT_COMMAND
        if on_sent is not None:
            on_sent(number - 1)
    con.ok(f"Sent {len(parts)} part(s) to {escape(args.command)}.")
    return EXIT_OK


# ----------------------------------------------------------------------
# Main flow
# ----------------------------------------------------------------------

def _select(args, library: str):
    """(book, translated, chapters, span) or None."""
    book = pick_book(library, args.book)
    if not book:
        con.warn("No book selected.")
        return None
    book_dir = os.path.join(library, book)
    try:
        translated = combine.translated_chapters(book_dir)
    except OSError as e:
        con.fail(f"Could not read {escape(book)}: {escape(str(e))}")
        return None
    if not translated:
        con.fail(f"{escape(book)} has no translated chapters yet.")
        return None
    progress = _show_progress(book_dir)
    try:
        span = _span(args, max(translated), progress)
    except ValueError as e:
        con.fail(escape(str(e)))
        return None
    if span is None:
        con.warn("No range selected.")
        return None
    chapters = [c for c in range(span[0], span[1] + 1) if c in translated]
    return book, translated, chapters, span


def _plan(args, book: str, library: str, translated: dict, audits: list, instructions: str):
    windows = os.name == "nt"
    try:
        combine.build_pipe_command(args.command, [], windows)
        fits, command_error = _fits_function(args.command, windows), ""
    except (FileNotFoundError, ValueError) as e:
        fits, command_error = (lambda files: True), str(e)
    reserve = len(auditor.render_header(book, [c.chapter for c in audits], 99, 99, instructions).encode("utf-8")) + HEADER_SLACK
    limit = max(reserve + 1, min(args.max_prompt_bytes, combine.PIPE_LIMIT_BYTES))
    parts = auditor.plan_parts(_blocks(library, book, translated, audits), limit, reserve, fits)
    return parts, _prompts(book, parts, instructions), command_error


def run(args) -> int:
    library = os.path.abspath(args.library)
    if not os.path.isdir(library):
        con.fail(f"Library folder not found: {escape(library)}")
        return EXIT_USER
    tns, extra_tns = not args.no_tns, bool(args.extra_tns)
    try:
        instructions = _instructions(args.instructions, tns, extra_tns)
    except (OSError, ValueError) as e:
        con.fail(f"Could not use the instructions file: {escape(str(e))}")
        return EXIT_USER
    selected = _select(args, library)
    if selected is None:
        return EXIT_USER
    book, translated, chapters, span = selected
    book_dir = os.path.join(library, book)
    con.ok(f"[bold green]{escape(book)}[/bold green]: auditing {escape(format_chapter_ranges(chapters))}")
    if args.align and not run_alignment(args, book_dir, book, chapters, translated):
        con.warn("Alignment cancelled; auditing with the alignments that exist.")
    audits = audit_range(book_dir, chapters, translated)
    problems = print_summary(audits)
    to_send = _chapters_to_send(audits, problems, extra_tns)
    if not to_send:
        con.ok("Nothing to send." if extra_tns else "No problem chapters; nothing to send.")
        if not args.dry_run:
            _record(book_dir, span, "")
        return EXIT_OK
    parts, prompts, command_error = _plan(args, book, library, translated, to_send, instructions)
    findings_path = _findings_path(args, book_dir)
    save_findings(findings_path, prompts)
    if command_error:
        con.fail(f"{escape(command_error)} The findings are saved; nothing was sent.")
        return EXIT_OK if args.dry_run else EXIT_COMMAND
    if args.dry_run:
        _print_dry_run(parts, prompts, args.command, os.name == "nt", library)
        return EXIT_OK
    question = (f"Send {len(to_send)} chapter(s) ({len(problems)} with problems) to {args.command} "
                f"in {len(parts)} part(s)? [Y/n]: ")
    if not args.yes and not confirm(question):
        con.warn("Cancelled; the findings are saved.")
        return EXIT_USER
    on_sent = _recorder(book_dir, _part_spans(parts, span), findings_path)
    return send_parts(args, parts, prompts, library, findings_path, on_sent)


def main(argv=None) -> int:
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
