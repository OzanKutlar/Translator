"""Tests for the offline audit, audit.py and the shared combineCopy code.
Run from the project root: python -m unittest discover tests
"""

import json
import os
import tempfile
import unittest
from unittest import mock

import audit
from booktl import auditor, combine, segments, storage
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


if __name__ == "__main__":
    unittest.main()
