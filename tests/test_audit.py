"""Tests for the offline audit, audit.py and the shared combineCopy code.
Run from the project root: python -m unittest discover tests
"""

import json
import os
import tempfile
import unittest
from unittest import mock

import audit
from booktl import auditlog, auditor, combine, segments, storage
from booktl.config import Config

RAW = "某某书名\n第1章 夜\n" + "".join(f"张三走进第{n}座大殿，众人大惊。\n" for n in range(1, 7)) + "(本章完)\n"
FULL_MD = "# Chapter 1: Night\n\n" + "\n\n".join(f"Zhang San entered hall {n}." for n in range(1, 7)) + "\n\n[[Chapter 2]]"
SHORT_MD = "# Chapter 1: Night\n\nZhang San entered a hall.\n\n[[Chapter 2]]"


def _write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


class BookCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.library = self._tmp.name
        self.book = os.path.join(self.library, "Book")
        os.makedirs(self.book)

    def tearDown(self):
        self._tmp.cleanup()

    def _chapter(self, number: int, md: str, raw: str = RAW) -> str:
        path = os.path.join(self.book, f"Chapter {number}.md")
        _write(path, md)
        storage.save_scraped_page(self.book, number, raw.replace("第1章", f"第{number}章"))
        return path

    def _audit(self, number: int, path: str):
        return auditor.audit_chapter(self.book, number, path, auditor.load_context(self.book))


class ChapterAuditTests(BookCase):
    def test_unaligned_chapter_with_fewer_paragraphs(self):
        result = self._audit(1, self._chapter(1, SHORT_MD))
        self.assertEqual([f.kind for f in result.problems], ["omission"])
        self.assertFalse(result.aligned)

    def test_complete_unaligned_chapter_has_only_information(self):
        result = self._audit(1, self._chapter(1, FULL_MD))
        self.assertEqual(result.problems, [])
        self.assertTrue(any(f.info for f in result.findings))

    def test_aligned_missing_paragraph_is_quoted(self):
        path = self._chapter(1, FULL_MD)
        parts = segments.split_page(RAW)
        english = {str(i): f"Zhang San entered hall {i}." for i in (1, 2, 4, 5, 6)}
        record = {"version": segments.ALIGN_VERSION, "chapter": 1, "zh": [parts.title, *parts.body],
                  "en": {"0": "# Chapter 1: Night", **english}, "skips": {}, "notes": []}
        segments.save_alignment(self.book, 1, record)
        result = self._audit(1, path)
        omission = [f for f in result.problems if f.kind == "omission"][0]
        self.assertIn("⟦3⟧", omission.text)
        self.assertIn("第3座", omission.quotes[0])
        block = auditor.render_chapter(result, ["a.md", "b.txt"])
        self.assertIn("## Chapter 1", block)
        self.assertIn("1. [omission]", block)

    def test_typo_claim_note_is_flagged(self):
        md = FULL_MD.replace("[[Chapter 2]]", "(TN: The raw has a typo here; it probably meant something else.)\n\n[[Chapter 2]]")
        result = self._audit(1, self._chapter(1, md))
        self.assertIn("note", [f.kind for f in result.problems])


class FakeClient:
    def __init__(self, reply):
        self.reply = reply

    def chat(self, cfg, messages, max_tokens=None, cancel=None, label=None, model=None):
        return self.reply, {"total_tokens": 1}


class AlignTests(BookCase):
    def test_map_paragraphs(self):
        items = [{"zh": 0, "en": [1]}, {"zh": 1, "en": [2]}, {"zh": 2, "en": [2]}, {"zh": 3, "en": [1]},
                 {"zh": 4, "en": []}, {"zh": 5, "en": [3, 4]}]
        mapping, merged, problems = auditor.map_paragraphs(items, 6, 4)
        self.assertEqual(mapping, {0: [1], 1: [2], 5: [3, 4]})
        self.assertEqual(merged, {2: 1})
        self.assertEqual(len(problems), 1)

    def test_align_chapter_writes_alignment(self):
        raw = "第1章 夜\n张三走进来。\n李四抬头。\n没人动。"
        md = "# Chapter 1: Night\n\nZhang San walked in and Li Si looked up.\n\n(TN: A note.)\n\nAn extra line.\n\n[[Chapter 2]]"
        path = self._chapter(1, md, raw)
        reply = json.dumps({"map": [{"zh": 0, "en": [1]}, {"zh": 1, "en": [2]}, {"zh": 2, "en": [2]}, {"zh": 3, "en": []}]})
        cfg = Config()
        cfg.model = "aligner"
        ok, message = auditor.align_chapter(FakeClient(reply), cfg, self.book, 1, path)
        self.assertTrue(ok, message)
        data = segments.load_alignment(self.book, 1)
        self.assertEqual(data["en"]["1"], "Zhang San walked in and Li Si looked up.")
        self.assertIn("merged", data["skips"]["2"])
        self.assertEqual(data["notes"][0]["afterId"], 1)
        self.assertEqual(data["unmatchedEnglish"], ["An extra line."])
        kinds = [f.kind for f in self._audit(1, path).problems]
        self.assertIn("omission", kinds)
        self.assertIn("addition", kinds)


class PartTests(unittest.TestCase):
    def test_parts_split_by_size_and_command_length(self):
        blocks = [(n, "x" * 400, [f"{n}.md", f"{n}.txt"]) for n in range(1, 6)]
        parts = auditor.plan_parts(blocks, 1_000, 100, lambda files: True)
        self.assertEqual([len(p) for p in parts], [2, 2, 1])
        parts = auditor.plan_parts(blocks, 100_000, 100, lambda files: len(files) <= 4)
        self.assertEqual([len(p) for p in parts], [2, 2, 1])

    def test_oversized_block_is_cut_with_a_note(self):
        parts = auditor.plan_parts([(1, "字" * 5_000, [])], 2_000, 100, lambda files: True)
        text = parts[0][0][1]
        self.assertLessEqual(len(text.encode("utf-8")), 2_000)
        self.assertIn("cut here", text)


class CombineTests(unittest.TestCase):
    def _which(self, table):
        return mock.patch("booktl.combine.shutil.which", side_effect=lambda name: table.get(name))

    def test_native_pipe_command(self):
        with self._which({"combineCopy": "/bin/combineCopy"}):
            argv, how = combine.build_pipe_command("combineCopy", ["a", "b"], windows=False)
        self.assertEqual((argv, how), (["/bin/combineCopy", "--prompt-pipe", "a", "b"], "native"))

    def test_powershell_function_pipe_command(self):
        with self._which({"powershell": "C:/powershell.exe"}):
            argv, how = combine.build_pipe_command("combineCopy", ["./Book/it's.md"], windows=True)
        self.assertEqual(how, "powershell")
        self.assertNotIn("-NoProfile", argv)
        self.assertIn("UTF8Encoding", argv[-1])
        self.assertTrue(argv[-1].endswith("$input | & 'combineCopy' '--prompt-pipe' './Book/it''s.md'"))

    def test_run_combine_sends_bytes(self):
        with mock.patch("booktl.combine.subprocess.run", return_value=mock.Mock(returncode=0)) as run:
            self.assertEqual(combine.run_combine(["x"], "/lib", data=b"hello"), 0)
        self.assertEqual(run.call_args.kwargs["input"], b"hello")
        with self.assertRaises(TypeError):
            combine.run_combine(["x"], "/lib", data="text")


class ScriptTests(BookCase):
    def test_dry_run_saves_findings_for_problem_chapters_only(self):
        self._chapter(1, SHORT_MD)
        self._chapter(2, FULL_MD.replace("Chapter 1", "Chapter 2"))
        out = os.path.join(self.library, "findings.md")
        args = ["-l", self.library, "-b", "Book", "--range", "1-2", "--dry-run", "--findings-out", out]
        with mock.patch("booktl.combine.shutil.which", side_effect=lambda name: {"combineCopy": "/bin/combineCopy"}.get(name)):
            self.assertEqual(audit.main(args), audit.EXIT_OK)
        text = _read(out)
        self.assertIn("## Chapter 1", text)
        self.assertNotIn("## Chapter 2", text)
        self.assertIn("Chapter 1.md", text)

    def test_extra_tns_sends_every_chapter(self):
        self._chapter(1, SHORT_MD)
        self._chapter(2, FULL_MD.replace("Chapter 1", "Chapter 2"))
        out = os.path.join(self.library, "findings.md")
        args = ["-l", self.library, "-b", "Book", "--range", "1-2", "--dry-run", "--extra-tns", "--findings-out", out]
        with mock.patch("booktl.combine.shutil.which", side_effect=lambda name: {"combineCopy": "/bin/combineCopy"}.get(name)):
            self.assertEqual(audit.main(args), audit.EXIT_OK)
        text = _read(out)
        self.assertIn("## Chapter 2", text)
        self.assertIn("Missing translator's notes", text)

    def test_no_problems_sends_nothing(self):
        self._chapter(1, FULL_MD)
        with mock.patch("booktl.combine.subprocess.run") as run:
            self.assertEqual(audit.main(["-l", self.library, "-b", "Book", "--range", "1", "-y"]), audit.EXIT_OK)
        run.assert_not_called()
        progress, err = auditlog.load_progress(self.book)
        self.assertIsNone(err)
        self.assertEqual(progress.audited_through, 1)

    def _which(self):
        table = {"combineCopy": "/bin/combineCopy"}
        return mock.patch("booktl.combine.shutil.which", side_effect=lambda name: table.get(name))

    def _base(self, *extra):
        return ["-l", self.library, "-b", "Book", *extra]

    def test_dry_run_records_nothing(self):
        self._chapter(1, SHORT_MD)
        out = os.path.join(self.library, "findings.md")
        with self._which():
            self.assertEqual(audit.main(self._base("--range", "1", "--dry-run", "--findings-out", out)), audit.EXIT_OK)
        self.assertFalse(os.path.isfile(auditlog.progress_path(self.book)))

    def test_failed_part_records_nothing(self):
        self._chapter(1, SHORT_MD)
        out = os.path.join(self.library, "findings.md")
        with self._which(), mock.patch("booktl.combine.subprocess.run", return_value=mock.Mock(returncode=1)):
            code = audit.main(self._base("--range", "1", "-y", "--findings-out", out))
        self.assertEqual(code, audit.EXIT_COMMAND)
        self.assertFalse(os.path.isfile(auditlog.progress_path(self.book)))

    def test_sent_part_is_recorded_and_next_continues(self):
        self._chapter(1, SHORT_MD)
        self._chapter(2, FULL_MD.replace("Chapter 1", "Chapter 2"))
        self._chapter(3, SHORT_MD.replace("Chapter 1", "Chapter 3"))
        first = os.path.join(self.library, "a.md")
        with self._which(), mock.patch("booktl.combine.subprocess.run", return_value=mock.Mock(returncode=0)) as run:
            self.assertEqual(audit.main(self._base("--range", "1-2", "-y", "--findings-out", first)), audit.EXIT_OK)
        run.assert_called_once()
        progress, _err = auditlog.load_progress(self.book)
        self.assertEqual(progress.audited_through, 2)
        self.assertEqual(progress.last_run.findings, "a.md")
        second = os.path.join(self.library, "b.md")
        with self._which():
            self.assertEqual(audit.main(self._base("--next", "5", "--dry-run", "--findings-out", second)), audit.EXIT_OK)
        text = _read(second)
        self.assertIn("## Chapter 3", text)
        self.assertNotIn("## Chapter 1\n", text)

    def test_next_when_everything_is_audited(self):
        self._chapter(1, FULL_MD)
        auditlog.record_audit(self.book, 1, 1)
        self.assertEqual(audit.main(self._base("--next", "3", "--dry-run")), audit.EXIT_USER)

    def test_default_asks_for_notes_and_no_tns_leaves_them_out(self):
        self._chapter(1, SHORT_MD)
        for extra, present in (([], True), (["--no-tns"], False)):
            out = os.path.join(self.library, f"findings_{int(present)}.md")
            with self._which():
                audit.main(self._base("--range", "1", "--dry-run", "--findings-out", out, *extra))
            text = _read(out)
            self.assertEqual("鬼精鬼精" in text, present)
            self.assertEqual("Missing translator's notes" in text, present)

    def test_no_tns_and_extra_tns_conflict(self):
        with mock.patch("sys.stderr"):
            with self.assertRaises(SystemExit):
                audit.build_parser().parse_args(["--no-tns", "--extra-tns"])


class ProgressTests(BookCase):
    def test_next_span(self):
        self.assertEqual(auditlog.next_span(None, 10, 100), (1, 10))
        self.assertEqual(auditlog.next_span(50, 10, 100), (51, 60))
        self.assertEqual(auditlog.next_span(95, 10, 100), (96, 100))
        for args in ((100, 1, 100), (5, 0, 100), (5, 3, 0), (5, True, 100)):
            with self.assertRaises(ValueError, msg=args):
                auditlog.next_span(*args)

    def test_record_keeps_highest_and_bounds_history(self):
        auditlog.record_audit(self.book, 10, 20, "/x/audit_a.md")
        progress = auditlog.record_audit(self.book, 1, 5)
        self.assertEqual(progress.audited_through, 20)
        self.assertEqual((progress.last_run.low, progress.last_run.high), (1, 5))
        self.assertEqual(progress.history[0].findings, "audit_a.md")
        with mock.patch.object(auditlog, "MAX_HISTORY", 3):
            for chapter in range(30, 36):
                auditlog.record_audit(self.book, chapter, chapter)
        loaded, err = auditlog.load_progress(self.book)
        self.assertIsNone(err)
        self.assertEqual(len(loaded.history), 3)
        self.assertEqual(loaded.audited_through, 35)
        self.assertIn("Chapter 35", auditlog.describe(loaded))

    def test_bad_span_is_rejected(self):
        for low, high in ((5, 2), (0, 3), (True, 3)):
            with self.assertRaises(ValueError):
                auditlog.record_audit(self.book, low, high)

    def test_corrupt_file_is_reported(self):
        _write(auditlog.progress_path(self.book), "{broken")
        progress, err = auditlog.load_progress(self.book)
        self.assertIsNone(progress)
        self.assertIn("progress.json", err)
        with self.assertRaises(ValueError):
            auditlog.record_audit(self.book, 1, 2)

    def test_part_spans_cover_the_range(self):
        parts = [[(3, "", []), (5, "", [])], [(9, "", [])]]
        self.assertEqual(audit._part_spans(parts, (1, 12)), [(1, 5), (6, 12)])
        self.assertEqual(audit._part_spans([[(4, "", [])]], (4, 4)), [(4, 4)])


if __name__ == "__main__":
    unittest.main()
