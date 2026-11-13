"""Tests for inquire.py. Run from the project root: python -m unittest discover tests"""

import os
import tempfile
import unittest
from unittest import mock

import inquire


def _touch(path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("x")


class ResolveRangeTests(unittest.TestCase):
    def test_start(self):
        self.assertEqual(inquire.resolve_range("start", "10", 100), (1, 10))
        self.assertEqual(inquire.resolve_range("start", 500, 100), (1, 100))

    def test_end(self):
        self.assertEqual(inquire.resolve_range("end", "10", 100), (91, 100))
        self.assertEqual(inquire.resolve_range("end", "200", 100), (1, 100))

    def test_middle_is_exactly_n(self):
        self.assertEqual(inquire.resolve_range("middle", "10", 100), (45, 54))  # middle 50: 50-5 to 50+4
        self.assertEqual(inquire.resolve_range("middle", "5", 100), (48, 52))
        self.assertEqual(inquire.resolve_range("middle", "10", 4), (1, 4))
        self.assertEqual(inquire.resolve_range("middle", "1", 100), (50, 50))

    def test_explicit_range(self):
        self.assertEqual(inquire.resolve_range("range", "140-175", 200), (140, 175))
        self.assertEqual(inquire.resolve_range("range", "ch140 – 175", 200), (140, 175))
        self.assertEqual(inquire.resolve_range("range", "150-300", 200), (150, 200))
        self.assertEqual(inquire.resolve_range("range", "140", 200), (140, 140))

    def test_bad_input(self):
        cases = (
            ("start", "0"), ("start", "abc"), ("end", "-3"), ("range", "175-140"),
            ("range", "300-310"), ("range", "x"), ("bogus", "1"),
        )
        for mode, value in cases:
            with self.assertRaises(ValueError, msg=(mode, value)):
                inquire.resolve_range(mode, value, 200)
        with self.assertRaises(ValueError):
            inquire.resolve_range("start", "5", 0)


class FileTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.library = self._tmp.name
        self.book = "Test Book"
        book_dir = os.path.join(self.library, self.book)
        for n in (1, 2, 3):
            _touch(os.path.join(book_dir, f"Chapter {n}.md"))
        for n in (1, 3):
            _touch(os.path.join(book_dir, "_raw", "chapters", f"ch_{n:06d}.txt"))
        _touch(os.path.join(book_dir, "notes.md"))
        _touch(os.path.join(book_dir, "Characters", "Li Yao.md"))
        self.translated = inquire.translated_chapters(book_dir)

    def tearDown(self):
        self._tmp.cleanup()

    def test_translated_chapters(self):
        self.assertEqual(self.translated, {1: "Chapter 1.md", 2: "Chapter 2.md", 3: "Chapter 3.md"})

    def test_collect_orders_english_then_raw_and_skips_gaps(self):
        selection = inquire.collect_files(self.library, self.book, self.translated, 1, 3)
        raw = lambda n: os.path.join(os.curdir, self.book, "_raw", "chapters", f"ch_{n:06d}.txt")
        english = lambda n: os.path.join(os.curdir, self.book, f"Chapter {n}.md")
        self.assertEqual(selection.files, [english(1), raw(1), english(3), raw(3)])
        self.assertEqual(selection.chapters, [1, 3])
        self.assertEqual(selection.no_raw, [2])
        self.assertEqual(selection.no_english, [])

    def test_collect_rejects_bad_span(self):
        with self.assertRaises(ValueError):
            inquire.collect_files(self.library, self.book, self.translated, 3, 1)


class CommandTests(unittest.TestCase):
    def _which(self, table):
        return mock.patch("inquire.shutil.which", side_effect=lambda name: table.get(name))

    def test_executable_on_path(self):
        with self._which({"combineCopy": "/bin/combineCopy"}):
            argv = inquire.build_command("combineCopy", ["a", "b"], windows=False)
        self.assertEqual(argv, ["/bin/combineCopy", "a", "b"])

    def test_ps1_script_runs_through_powershell(self):
        table = {"combineCopy.ps1": "C:/tools/combineCopy.ps1", "pwsh": "C:/pwsh.exe"}
        with self._which(table):
            argv = inquire.build_command("combineCopy", ["a"], windows=True)
        self.assertEqual(argv[0], "C:/pwsh.exe")
        self.assertIn("-File", argv)
        self.assertEqual(argv[-2:], ["C:/tools/combineCopy.ps1", "a"])

    def test_profile_function_fallback(self):
        with self._which({"powershell": "C:/powershell.exe"}):
            argv = inquire.build_command("combineCopy", ["./Book/it's.md"], windows=True)
        self.assertEqual(argv[:3], ["C:/powershell.exe", "-NoLogo", "-Command"])
        self.assertEqual(argv[3], "& 'combineCopy' './Book/it''s.md'")

    def test_not_found(self):
        with self._which({}):
            with self.assertRaises(FileNotFoundError):
                inquire.build_command("combineCopy", ["a"], windows=True)

    def test_length_cap(self):
        argv = ["combineCopy"] + ["x" * 100] * 400
        with self.assertRaises(ValueError):
            inquire.check_length(argv, windows=True)
        inquire.check_length(argv, windows=False)

    def test_run_returns_exit_code_and_uses_library_folder(self):
        with mock.patch("inquire.subprocess.run", return_value=mock.Mock(returncode=3)) as run:
            self.assertEqual(inquire.run_combine(["combineCopy", "a"], "/lib"), 3)
        self.assertEqual(run.call_args.kwargs["cwd"], "/lib")
        self.assertNotIn("capture_output", run.call_args.kwargs)

    def test_run_reports_start_failure(self):
        with mock.patch("inquire.subprocess.run", side_effect=FileNotFoundError("nope")):
            self.assertIsNone(inquire.run_combine(["combineCopy"], "/lib"))


class ConfirmTests(unittest.TestCase):
    def _confirm(self, answers, default):
        with mock.patch.object(inquire.con.console, "input", side_effect=answers):
            return inquire.confirm("Go? ", default=default)

    def test_default_no(self):
        self.assertFalse(self._confirm([""], False))
        self.assertTrue(self._confirm(["y"], False))
        self.assertFalse(self._confirm(EOFError(), False))
        self.assertFalse(self._confirm(["maybe", "n"], False))

    def test_default_yes_is_unchanged(self):
        self.assertTrue(self._confirm([""], True))
        self.assertFalse(self._confirm(["n"], True))


class AskRequestTests(unittest.TestCase):
    def _ask(self, answers):
        with mock.patch.object(inquire.con.console, "input", side_effect=answers):
            return inquire.ask_request()

    def test_lines_until_empty_line(self):
        self.assertEqual(self._ask(["  Check names.", "Keep TNs.  ", ""]), "Check names.\nKeep TNs.")

    def test_blank_and_cancelled(self):
        self.assertIsNone(self._ask([""]))
        self.assertIsNone(self._ask(["text", KeyboardInterrupt()]))

    def test_size_cap(self):
        with mock.patch.object(inquire, "PIPE_LIMIT_BYTES", 10):
            self.assertIsNone(self._ask(["x" * 20, ""]))


class RequestSendTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.library = self._tmp.name
        self.book = "Test Book"
        book_dir = os.path.join(self.library, self.book)
        _touch(os.path.join(book_dir, "Chapter 1.md"))
        _touch(os.path.join(book_dir, "_raw", "chapters", "ch_000001.txt"))
        self.translated = inquire.translated_chapters(book_dir)

    def tearDown(self):
        self._tmp.cleanup()

    def _send(self, argv, answers):
        args = inquire.build_parser().parse_args(argv)
        table = {"combineCopy": "/bin/combineCopy"}
        with mock.patch("inquire.shutil.which", side_effect=lambda name: table.get(name)), \
                mock.patch("inquire.subprocess.run", return_value=mock.Mock(returncode=0)) as run, \
                mock.patch.object(inquire.con.console, "input", side_effect=answers):
            code = inquire.send_chapters(args, self.library, self.book, self.translated, (1, 1))
        return code, run

    def test_request_flag_pipes_the_text(self):
        code, run = self._send(["--request", "Check names", "-y"], AssertionError("should not ask"))
        self.assertEqual(code, inquire.EXIT_OK)
        self.assertEqual(run.call_args.args[0][:2], ["/bin/combineCopy", "--prompt-pipe"])
        self.assertEqual(run.call_args.kwargs["input"], b"Check names")

    def test_enter_means_no_request(self):
        code, run = self._send([], ["", ""])
        self.assertEqual(code, inquire.EXIT_OK)
        self.assertNotIn("--prompt-pipe", run.call_args.args[0])
        self.assertNotIn("input", run.call_args.kwargs)

    def test_yes_reads_a_request(self):
        code, run = self._send([], ["y", "Line one", "Line two", "", ""])
        self.assertEqual(code, inquire.EXIT_OK)
        self.assertEqual(run.call_args.kwargs["input"], "Line one\nLine two".encode("utf-8"))

    def test_y_flag_never_asks(self):
        code, run = self._send(["-y"], AssertionError("should not ask"))
        self.assertEqual(code, inquire.EXIT_OK)
        self.assertNotIn("input", run.call_args.kwargs)

    def test_empty_request_stops(self):
        code, run = self._send(["--request", "   "], AssertionError("should not ask"))
        self.assertEqual(code, inquire.EXIT_USER)
        run.assert_not_called()

    def test_request_flags_conflict(self):
        with mock.patch("sys.stderr"):
            with self.assertRaises(SystemExit):
                inquire.build_parser().parse_args(["--request", "a", "--request-file", "b"])


if __name__ == "__main__":
    unittest.main()
