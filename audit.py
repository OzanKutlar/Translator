"""Audit translated chapters offline and send the problem chapters to combineCopy with the findings.

  python audit.py -b "My Book" --range 1200-1250
  python audit.py --end 100 --align        first build missing paragraph alignments with a model
  python audit.py --middle 20 --dry-run    print the commands and save the findings only

Pick a book and a range as in inquire.py. Every chapter in the range is checked from disk (no model is
called unless --align is given). The chapters with problems are listed, the prompt is saved to
<Book>/_qa/audit/audit_<time>.md, and combineCopy runs with --prompt-pipe and the English file and raw
page of every chapter in the range, with the prompt piped in:

  combineCopy --prompt-pipe './Book/Chapter 812.md' './Book/_raw/chapters/ch_000812.txt' …

Findings larger than combineCopy's 2 MB limit, or a command line too long for Windows, are split into
parts by chapter; the script waits for Enter between parts.

The prompt also asks the model to add translator's notes the translation is missing (poem and film
sources, title references, author slips, source typos). The notes go inline, so auditor's [note]
findings are left out. With --no-extra-tns, only problem chapters are sent and no notes are asked for.
"""

import argparse
import dataclasses
import os
import sys
import time
from collections import Counter

from rich.markup import escape
from rich.table import Table

from booktl import auditor, combine, segments
from booktl import console as con
from booktl.client import ApiClient
from booktl.config import Config, ConfigError
from booktl.parsing import format_chapter_ranges
from inquire import add_range_args, ask_span, confirm, pick_book, span_from_args

DEFAULT_LIBRARY = "../TranslateBook"
DEFAULT_COMMAND = "combineCopy"
DEFAULT_PROMPT_BYTES = 1_900_000
HEADER_SLACK = 512
FINDINGS_DIRNAME = "audit"
MAX_TABLE_ROWS = 300
MAX_LISTED_ERRORS = 10
MAX_INSTRUCTIONS_CHARS = 20_000
EXIT_OK, EXIT_USER, EXIT_COMMAND = 0, 1, 2
PS_NOTE = (
    "combineCopy runs through PowerShell here. If it is a function in your profile, it must forward its "
    "pipeline input or it receives no prompt, e.g.:\n"
    "  function combineCopy { if ($MyInvocation.ExpectingInput) { $input | combineCopy.exe @args } "
    "else { combineCopy.exe @args } }\n"
    "The findings are also saved to {path}."
)
NOTE_KIND = "note"
CLEAN_CHAPTER_NOTE = "No automatic findings. Check this chapter only for missing translator's notes."
EXTRA_TN_INSTRUCTIONS = """\
## Missing translator's notes
Also read each chapter for places an English reader would miss something a Chinese reader would catch,
where the translation has no (TN: ...) yet. Add a note for:
- lines taken from poems, songs, classics or films (name the work and its author);
- idioms, sayings or wordplay whose meaning does not come through in English;
- references to the novel's own title, or to real people, places and events;
- author slips the translation copies or quietly fixes, such as a count that does not match its list or a
  number that contradicts an earlier chapter;
- typos in the source that change how a line reads.

Rules:
- Put each note in its own paragraph right after the paragraph it explains, as (TN: ...).
  There is no separate notes list.
- Quote the exact Chinese characters you explain, so a reviewer can find them in the raw page.
- Keep each note to one or two short sentences of plain English.
- State only what you can check against the raw page or a well-known source. If you are unsure where a
  line comes from, leave the note out.
- Skip places where the English already makes the meaning clear, and do not repeat a note the chapter has.
- Quote at most one line of a poem or song inside a note.
- For each note, give the English paragraph to find, and that paragraph followed by the note as the
  replacement."""


# ----------------------------------------------------------------------
# Arguments
# ----------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Audit translated chapters and send problem chapters to combineCopy.")
    parser.add_argument("-b", "--book", help="Book folder name (skips the book menu)")
    parser.add_argument("-l", "--library", default=DEFAULT_LIBRARY, help="Library folder (default: ../TranslateBook)")
    add_range_args(parser)
    parser.add_argument("-y", "--yes", action="store_true", help="Do not ask for confirmation or wait between parts")
    parser.add_argument("--dry-run", action="store_true", help="Save the findings and print the commands without running them")
    parser.add_argument("--command", default=DEFAULT_COMMAND, help="Command to run (default: combineCopy)")
    parser.add_argument("--findings-out", help="Where to save the findings (default: <Book>/_qa/audit/audit_<time>.md)")
    parser.add_argument("--instructions", help="File with your own instructions to put above the findings")
    parser.add_argument("--no-extra-tns", action="store_true",
                        help="Do not ask for missing translator's notes; send only problem chapters")
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


def _instructions(path, extra_tns: bool) -> str:
    """Base instructions, with the missing-TN request added unless turned off. Raises OSError/ValueError."""
    base = _base_instructions(path)
    if not extra_tns:
        return base
    return base.rstrip() + "\n\n" + EXTRA_TN_INSTRUCTIONS


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
    """Every readable chapter when asking for TNs, otherwise only the problem chapters."""
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


def send_parts(args, parts: list, prompts: list, library: str, findings_path: str) -> int:
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
    con.ok(f"Sent {len(parts)} part(s) to {escape(args.command)}.")
    return EXIT_OK


# ----------------------------------------------------------------------
# Main flow
# ----------------------------------------------------------------------

def _select(args, library: str):
    """(book, translated, chapters) or None."""
    book = pick_book(library, args.book)
    if not book:
        con.warn("No book selected.")
        return None
    try:
        translated = combine.translated_chapters(os.path.join(library, book))
    except OSError as e:
        con.fail(f"Could not read {escape(book)}: {escape(str(e))}")
        return None
    if not translated:
        con.fail(f"{escape(book)} has no translated chapters yet.")
        return None
    try:
        span = span_from_args(args, max(translated)) or ask_span(max(translated))
    except ValueError as e:
        con.fail(escape(str(e)))
        return None
    if span is None:
        con.warn("No range selected.")
        return None
    chapters = [c for c in range(span[0], span[1] + 1) if c in translated]
    return book, translated, chapters


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
    extra_tns = not args.no_extra_tns
    try:
        instructions = _instructions(args.instructions, extra_tns)
    except (OSError, ValueError) as e:
        con.fail(f"Could not use the instructions file: {escape(str(e))}")
        return EXIT_USER
    selected = _select(args, library)
    if selected is None:
        return EXIT_USER
    book, translated, chapters = selected
    book_dir = os.path.join(library, book)
    con.ok(f"[bold green]{escape(book)}[/bold green]: auditing {escape(format_chapter_ranges(chapters))}")
    if args.align and not run_alignment(args, book_dir, book, chapters, translated):
        con.warn("Alignment cancelled; auditing with the alignments that exist.")
    audits = audit_range(book_dir, chapters, translated)
    problems = print_summary(audits)
    to_send = _chapters_to_send(audits, problems, extra_tns)
    if not to_send:
        con.ok("Nothing to send." if extra_tns else "No problem chapters; nothing to send.")
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
    return send_parts(args, parts, prompts, library, findings_path)


def main(argv=None) -> int:
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
