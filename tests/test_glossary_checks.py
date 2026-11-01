"""Tests for glossary checks, character name checks and source-error reports.
Run from the project root: python -m unittest discover tests
"""

import json
import os
import tempfile
import unittest

from booktl import characters, glossary, qalog, segments, termlint, termrename
from booktl.cancel import CancelToken
from booktl.config import Config
from booktl.pipeline import Pipeline

PAGE = "某某书名\n第120章 血色\n" + "".join(f"李耀走进第{n}座大殿，众人大惊。\n" for n in range(1, 7))


def _write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _add(chinese, english, kind="item"):
    return {"action": "AddTerm", "Chinese": chinese, "English": english, "Type": kind}


class BookTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.book = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def _characters(self, mapping):
        _write(os.path.join(self.book, "Characters", "Characters.json"), json.dumps(mapping, ensure_ascii=False))
        for md in set(mapping.values()):
            _write(os.path.join(self.book, "Characters", md), f"# {md[:-3]}\n\nA person.\n")


class GlossaryRuleTests(BookTestCase):
    def test_same_english_is_reported_not_aliased(self):
        actions = [_add("圣城", "Holy City", "place"), _add("光明城", "Holy City", "place")]
        summary = glossary.record_term_actions(self.book, actions, 10, names={})
        self.assertEqual(summary["added"], ["Holy City"])
        self.assertEqual(summary["collisions"][0]["existing"], "圣城")
        terms = glossary.load_terms(self.book)
        self.assertNotIn("光明城", terms)
        self.assertEqual(terms["圣城"]["aliases"], [])

    def test_name_inside_term_must_carry_its_english(self):
        summary = glossary.record_term_actions(self.book, [_add("张三剑", "Azure Sword")], 10, names={"张三": "Zhang San"})
        self.assertEqual(summary["containment"][0]["part"], "张三")
        self.assertIn("张三剑", glossary.load_terms(self.book))
        fine = glossary.record_term_actions(self.book, [_add("李四剑", "Li Si's Sword")], 10, names={"李四": "Li Si"})
        self.assertEqual(fine["containment"], [])

    def test_exempt_term_is_not_reported(self):
        glossary.record_term_actions(self.book, [_add("张三剑", "Azure Sword")], 10, names={})
        ok, _msg = glossary.set_containment_exempt(self.book, "张三剑")
        self.assertTrue(ok)
        terms = glossary.load_terms(self.book)
        self.assertEqual(glossary.check_containment(terms, {"张三": "Zhang San"}), [])

    def test_link_term_shows_in_locked_terms(self):
        actions = [
            _add("青龙王", "Azure Dragon King", "title"),
            _add("东海王", "King of the Eastern Sea", "title"),
            {"action": "LinkTerm", "Chinese": "东海王", "SameAs": "Azure Dragon King"},
        ]
        summary = glossary.record_term_actions(self.book, actions, 10, names={})
        self.assertEqual(len(summary["linked"]), 1)
        block = glossary.get_term_context(glossary.match_terms(self.book, "东海王来了"))
        self.assertIn("same entity as Azure Dragon King", block)

    def test_user_link_and_removal(self):
        glossary.add_user_term(self.book, "青龙王", "Azure Dragon King")
        glossary.add_user_term(self.book, "东海王", "King of the Eastern Sea")
        ok, _msg = glossary.add_user_link(self.book, "青龙王", "东海王")
        self.assertTrue(ok)
        self.assertIn("东海王", glossary.load_terms(self.book)["青龙王"]["sameAs"])
        glossary.remove_term(self.book, "东海王")
        self.assertEqual(glossary.load_terms(self.book)["青龙王"]["sameAs"], [])


class LintTests(BookTestCase):
    def test_word_split(self):
        for chinese, english in (("神殿", "Divine Hall"), ("神剑", "Divine Sword"),
                                 ("圣城", "Divine City"), ("圣火", "Divine Fire")):
            glossary.add_user_term(self.book, chinese, english)
        report = termlint.lint(self.book)
        self.assertIn("divine", [s.word for s in report.splits])

    def test_same_english_and_character_containment(self):
        data = {"version": 1, "terms": {
            "圣城": {"en": "Holy City", "type": "place", "aliases": [], "source": "model"},
            "光明城": {"en": "Holy City", "type": "place", "aliases": [], "source": "model"},
        }}
        _write(os.path.join(self.book, glossary.GLOSSARY_FILENAME), json.dumps(data, ensure_ascii=False))
        self._characters({"张三": "Zhang San.md", "张三丰": "Old Feng.md"})
        report = termlint.lint(self.book)
        self.assertEqual(report.same_english[0][1], ("光明城", "圣城"))
        self.assertTrue(any(p["part"] == "张三" and p["chinese"] == "张三丰" for p in report.containment))

    def test_corpus_collision(self):
        glossary.add_user_term(self.book, "圣城", "Holy City")
        for chapter in (1, 2, 3):
            record = {"version": segments.ALIGN_VERSION, "chapter": chapter,
                      "zh": [f"第{chapter}章", "他们来到光明城。"],
                      "en": {"0": f"# Chapter {chapter}", "1": "They reached the Holy City."}}
            segments.save_alignment(self.book, chapter, record)
        report = termlint.lint(self.book)
        self.assertEqual(report.chapters_scanned, 3)
        hit = [c for c in report.corpus if c.english == "Holy City"]
        self.assertEqual(hit[0].unexplained, 3)


class RenameTests(BookTestCase):
    def test_plan_and_apply(self):
        glossary.add_user_term(self.book, "血印", "Blood Seal", "item")
        md_path = os.path.join(self.book, "Chapter 300.md")
        _write(md_path, "# Chapter 300: Night\n\nThe Blood Seal glowed.\n\nNobody moved.\n\n[[Chapter 301]]")
        record = {"version": segments.ALIGN_VERSION, "chapter": 300, "zh": ["第300章 夜", "血印亮了。", "没人动。"],
                  "en": {"0": "# Chapter 300: Night", "1": "The Blood Seal glowed.", "2": "Nobody moved."}}
        segments.save_alignment(self.book, 300, record)
        plan, err = termrename.plan_rename(self.book, "血印", "Crimson Seal")
        self.assertIsNone(err)
        self.assertEqual(len(plan.changes), 1)
        self.assertIn("Blood Seal", _read(md_path))  # the plan alone changes nothing
        result = termrename.apply_rename(self.book, plan)
        self.assertEqual(result.changed_chapters, [300])
        self.assertIn("The Crimson Seal glowed.", _read(md_path))
        self.assertTrue(os.path.isfile(os.path.join(result.backup_dir, "Chapter 300.md")))
        self.assertEqual(glossary.load_terms(self.book)["血印"]["en"], "Crimson Seal")
        _zh, en = segments.alignment_texts(segments.load_alignment(self.book, 300))
        self.assertEqual(en[1], "The Crimson Seal glowed.")

    def test_unknown_term(self):
        plan, err = termrename.plan_rename(self.book, "不存在", "X")
        self.assertIsNone(plan)
        self.assertIn("No term", err)


class CharacterTests(BookTestCase):
    def test_longer_name_masks_shorter_one(self):
        self._characters({"张三": "Zhang San.md", "张三丰": "Old Feng.md"})
        context = characters.get_character_context(self.book, "张三丰来了")
        self.assertIn("### Old Feng", context)
        self.assertNotIn("### Zhang San", context)

    def test_name_drift(self):
        char_map = {"李耀": "Li Yao.md"}
        raw = "李耀来了。李耀走了。"
        self.assertEqual(len(characters.name_drift(char_map, raw, "He came. He left.")), 1)
        self.assertEqual(characters.name_drift(char_map, raw, "Yao came. He left."), [])
        self.assertEqual(characters.name_drift(char_map, "李耀来了。", "He came."), [])


class SourceReportTests(unittest.TestCase):
    def test_fix_checks(self):
        paragraphs = ["第1章", "他沉有说话。"]
        items = [
            {"chapterNo": "Chapter 1", "afterId": 1, "zh": "沉有", "correction": "没有"},
            {"chapterNo": "Chapter 1", "afterId": 1, "zh": "他沉有说话", "correction": "她完全不想开口"},
            {"chapterNo": "Chapter 9", "zh": "甲", "correction": "乙"},
        ]
        records, skipped = qalog.check_fixes(items, {1: paragraphs}, {1}, 5)
        self.assertEqual([r["status"] for r in records], ["verified", "unverified"])
        self.assertEqual(len(skipped), 1)

    def test_continuity_checks(self):
        items = [{"chapterNo": "Chapter 1", "afterId": 1, "zh": "不存在", "issue": "wrong count"},
                 {"chapterNo": "Chapter 1", "zh": "沉有", "issue": ""}]
        records, skipped = qalog.check_continuity(items, {1: ["第1章", "他沉有说话。"]}, {1}, 5)
        self.assertEqual(records[0]["status"], "not on page")
        self.assertEqual(len(skipped), 1)


def _reply(extra) -> str:
    paragraphs = ["⟦0⟧ # Chapter 120: Blood"] + [f"⟦{i}⟧ Li Yao entered hall {i}." for i in range(1, 7)]
    payload = {
        "chapters": [{"chapterNo": "Chapter 120", "content": "\n\n".join(paragraphs + ["[[Chapter 121]]"]), "notes": []}],
        "characterActions": [], "arcEnds": [], "termActions": [],
    }
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)


class FakeClient:
    def __init__(self, reply):
        self.reply = reply

    def chat(self, cfg, messages, max_tokens=None, cancel=None, label=None):
        return self.reply, {"total_tokens": 1}


class SourceReportPipelineTests(BookTestCase):
    def test_reports_are_logged(self):
        extra = {
            "sourceFixes": [{"chapterNo": "Chapter 120", "afterId": 2, "zh": "第2座", "correction": "第3座"}],
            "continuity": [{"chapterNo": "Chapter 120", "afterId": 4, "zh": "第4座",
                            "issue": "the hall count disagrees with Chapter 119"}],
        }
        cfg = Config()
        cfg.model = "test-model"
        cfg.term_lookup = False
        cfg.set_book(self.book, "Test Book")
        pipeline = Pipeline(cfg, FakeClient(_reply(extra)))
        entry = pipeline.pool.make_entry(120, "Chapter 120", PAGE)
        pipeline._run_batch(1, [entry], CancelToken())
        fixes, err = qalog.read_records(self.book, qalog.SOURCE_FIXES_FILENAME)
        self.assertIsNone(err)
        self.assertEqual([r["status"] for r in fixes], ["verified"])
        kept, _err = qalog.read_records(self.book, qalog.CONTINUITY_FILENAME)
        self.assertEqual(kept[0]["status"], "on page")


if __name__ == "__main__":
    unittest.main()
