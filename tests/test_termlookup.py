"""Tests for the prior-usage term lookup. Run from the project root: python -m unittest discover tests"""

import json
import os
import tempfile
import unittest

from booktl import glossary, parsing, termlookup

RAW_300 = "\n".join([
    "某某书名",
    "第300章 血色",
    "李耀走进大殿。",
    "苍穹宗的长老看着他。",
    "李耀只觉胸口一烫，那枚血魔印竟然再次亮了起来。",
    "众人大惊。",
    "(本章完)",
    "推荐一本好书：血魔印传说",
])
EN_300 = "\n\n".join([
    "# Chapter 300: Blood Red",
    "Li Yao walked into the great hall.",
    "An elder of the Azure Sect looked at him.",
    "Li Yao felt his chest burn. The Blood Demon Seal had lit up once again.",
    "Everyone was shocked.",
    "(TN: A note.)",
    "[[Chapter 301]]",
])


def _write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


class LookupTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.book = self._tmp.name
        _write(os.path.join(self.book, "_raw", "chapters", "ch_000300.txt"), RAW_300)
        _write(os.path.join(self.book, "Chapter 300.md"), EN_300)
        chars = json.dumps({"李耀": "Li Yao.md"}, ensure_ascii=False)
        _write(os.path.join(self.book, "Characters", "Characters.json"), chars)
        ok, msg = glossary.add_user_term(self.book, "苍穹宗", "Azure Sect", "organization")
        self.assertTrue(ok, msg)
        self.corpus = termlookup.TermCorpus()

    def tearDown(self):
        self._tmp.cleanup()

    def test_clean_raw_drops_site_header_and_footer(self):
        body = termlookup.clean_raw(RAW_300)
        self.assertEqual(body[0], "第300章 血色")
        self.assertNotIn("推荐一本好书：血魔印传说", body)

    def test_clean_english_drops_notes_and_links(self):
        paragraphs = termlookup.clean_english(EN_300)
        self.assertEqual(paragraphs[0], "# Chapter 300: Blood Red")
        self.assertFalse(any(p.startswith("(TN:") or p.startswith("[[") for p in paragraphs))

    def test_filter_drops_locked_characters_and_absent_terms(self):
        pages = "李耀拿出血魔印，苍穹宗众人大惊。"
        names = ("血魔印", "苍穹宗", "李耀", "不存在的词", "血魔印")
        proposed = [{"Chinese": n, "ProposedEnglish": "", "Type": ""} for n in names]
        kept = termlookup.filter_candidates(self.book, proposed, pages)
        self.assertEqual([c.chinese for c in kept], ["血魔印"])

    def test_prior_usage_found_with_anchors(self):
        candidates = [termlookup.Candidate("血魔印", "Blood Fiend Mark", "item")]
        result = termlookup.build_prior_usage(self.corpus, self.book, candidates, {900})
        self.assertEqual(result.first_seen, {"血魔印": 300})
        occ = result.usages[0].occurrences[0]
        self.assertIn("Blood Demon Seal", occ.en)
        self.assertIn("血魔印", occ.zh)
        self.assertIn("Li Yao", occ.anchors)
        self.assertIn("Azure Sect", occ.anchors)
        self.assertNotIn("Blood Fiend Mark", result.context)  # the proposal is never shown back

    def test_current_batch_is_excluded(self):
        candidates = [termlookup.Candidate("血魔印", "", "")]
        result = termlookup.build_prior_usage(self.corpus, self.book, candidates, {300})
        self.assertEqual(result.usages, [])
        self.assertEqual(result.context, "")

    def test_footer_mention_is_not_a_hit(self):
        candidates = [termlookup.Candidate("血魔印传说", "", "")]
        result = termlookup.build_prior_usage(self.corpus, self.book, candidates, set())
        self.assertEqual(result.usages, [])

    def test_best_window_moves_to_anchor(self):
        paragraphs = ["a" * 18 for _ in range(20)]
        paragraphs[14] = "Azure Sect arrives"
        index, hits = termlookup._best_window(paragraphs, 0.5, ("Azure Sect",))
        self.assertEqual(hits, ("Azure Sect",))
        self.assertEqual(index, 12)

    def test_first_seen_sets_first_chapter(self):
        actions = [{"action": "AddTerm", "Chinese": "血魔印", "English": "Blood Demon Seal", "Type": "item"}]
        summary = glossary.record_term_actions(self.book, actions, 900, {"血魔印": 300})
        self.assertEqual(summary["recovered"], [{"chinese": "血魔印", "english": "Blood Demon Seal", "chapter": 300}])
        data, err = glossary.load_glossary(self.book)
        self.assertIsNone(err)
        self.assertEqual(data["terms"]["血魔印"]["firstChapter"], 300)

    def test_audit_lists_chapters(self):
        usage, warnings = termlookup.audit_term(self.corpus, self.book, "血魔印")
        self.assertEqual(warnings, [])
        self.assertEqual(usage.chapters, (300,))

    def test_rendering_in_chapter(self):
        self.assertTrue(termlookup.rendering_in_chapter(self.book, 300, "blood demon seal"))
        self.assertFalse(termlookup.rendering_in_chapter(self.book, 300, "Blood Fiend Mark"))
        self.assertIsNone(termlookup.rendering_in_chapter(self.book, 301, "Blood Demon Seal"))
        self.assertIsNone(termlookup.rendering_in_chapter(self.book, 300, "  "))
        self.assertIsNone(termlookup.rendering_in_chapter(self.book, True, "Blood Demon Seal"))


class ScoutParsingTests(unittest.TestCase):
    def test_fenced_reply(self):
        fence = "`" * 3
        body = '{"candidateTerms": [{"Chinese": "血魔印", "ProposedEnglish": "Blood Fiend Mark", "Type": "item"}, {"Chinese": ""}]}'
        entries, err = parsing.parse_scout_reply(fence + "json\n" + body + "\n" + fence)
        self.assertIsNone(err)
        self.assertEqual(entries, [{"Chinese": "血魔印", "ProposedEnglish": "Blood Fiend Mark", "Type": "item"}])

    def test_garbage_reply(self):
        entries, err = parsing.parse_scout_reply("I think the terms are...")
        self.assertEqual(entries, [])
        self.assertIsNotNone(err)


if __name__ == "__main__":
    unittest.main()
