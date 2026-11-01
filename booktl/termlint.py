"""Glossary lint: checks across the whole glossary and the aligned chapters. Reports only.

  - containment: a term or character name that contains another registered name or term must
    carry that name's English (see glossary.check_containment);
  - same English: two separate terms rendered identically;
  - broken links: same-entity links (sameAs) that point at removed terms;
  - word splits: one English word used for terms built on unrelated Chinese characters;
  - corpus collisions: a term's English used in aligned paragraphs whose Chinese contains none of
    the spellings that explain it, read from <Book>/_align/.
"""

import re
from collections import Counter
from dataclasses import dataclass, field

from . import characters, glossary, segments

DEFAULT_RECENT_CHAPTERS = 300
MAX_RECENT_CHAPTERS = 5_000
MAX_PROBES = 5_000
MIN_WORD_LENGTH = 4
STOPWORDS = frozenset({"with", "from", "into", "that", "this", "their", "there", "over", "under", "upon"})
MIN_GROUP = 2
MIN_UNEXPLAINED = 3
MIN_UNEXPLAINED_SHARE = 0.3
MAX_SAMPLE_CHAPTERS = 5
MAX_BIGRAM_PARAGRAPHS = 200
MAX_LIKELY_CANDIDATES = 30
MAX_COMMON_PART = 10
MAX_REPORTS = 200
MAX_WARNINGS = 20
_WORD_RE = re.compile(r"[a-z][a-z'’-]*")
_TOKEN_RE = re.compile(r"[a-z0-9]+")
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")


@dataclass(frozen=True)
class WordSplit:
    """One English word shared by two groups of terms with no Chinese in common."""

    word: str
    first_part: str
    first_terms: tuple[str, ...]
    second_part: str
    second_terms: tuple[str, ...]


@dataclass(frozen=True)
class CorpusCollision:
    """A term's English found in paragraphs whose Chinese does not explain it."""

    english: str
    key: str
    total: int
    unexplained: int
    chapters: tuple[int, ...]
    likely: str   # Chinese most often found in the unexplained paragraphs, '' when unclear


@dataclass
class LintReport:
    containment: list = field(default_factory=list)
    same_english: list = field(default_factory=list)   # (English, (keys, …))
    broken_links: list = field(default_factory=list)   # (key, missing link)
    splits: list = field(default_factory=list)
    corpus: list = field(default_factory=list)
    chapters_scanned: int = 0
    warnings: list = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.containment or self.same_english or self.broken_links or self.splits or self.corpus)


@dataclass(eq=False)
class _Probe:
    key: str
    english: str
    pattern: re.Pattern
    explains: tuple
    total: int = 0
    unexplained: int = 0
    sampled: int = 0
    chapters: list = field(default_factory=list)
    bigrams: Counter = field(default_factory=Counter)


def _warn(warnings: list, message: str) -> None:
    if len(warnings) < MAX_WARNINGS:
        warnings.append(message)


def _usable_terms(terms: dict) -> list[tuple[str, str]]:
    """(key, English) for every usable term."""
    result = []
    for key, record in terms.items():
        english = glossary.english_of(record)
        if english and glossary.usable_key(key):
            result.append((key, english))
    return result


# ----------------------------------------------------------------------
# Glossary-only checks
# ----------------------------------------------------------------------

def _character_containment(terms: dict, names: dict) -> list[dict]:
    spellings = glossary.entity_spellings(terms, names)
    problems = []
    for chinese, english in names.items():
        problems.extend(glossary.containment_problems(chinese, english, spellings))
        if len(problems) >= MAX_REPORTS:
            break
    return problems[:MAX_REPORTS]


def _same_english(terms: dict) -> list:
    groups: dict[str, list[str]] = {}
    names: dict[str, str] = {}
    for key, english in _usable_terms(terms):
        groups.setdefault(english.casefold(), []).append(key)
        names.setdefault(english.casefold(), english)
    result = [(names[folded], tuple(sorted(keys))) for folded, keys in groups.items() if len(keys) > 1]
    return result[:MAX_REPORTS]


def _broken_links(terms: dict) -> list:
    result = []
    for key, record in terms.items():
        for link in glossary.same_as_of(record):
            if link not in terms:
                result.append((key, link))
    return result[:MAX_REPORTS]


def _words(english: str) -> set[str]:
    return {w for w in _WORD_RE.findall(english.casefold()) if len(w) >= MIN_WORD_LENGTH and w not in STOPWORDS}


def _common_part(keys) -> str:
    """Longest Chinese text shared by all keys, '' when none."""
    items = list(keys)
    if not items:
        return ""
    shortest = min(items, key=len)
    for length in range(min(len(shortest), MAX_COMMON_PART), 0, -1):
        for start in range(len(shortest) - length + 1):
            piece = shortest[start:start + length]
            if all(piece in key for key in items):
                return piece
    return ""


def _top_char(keys) -> str:
    counts = Counter(ch for key in keys for ch in dict.fromkeys(key) if _CJK_RE.match(ch))
    return counts.most_common(1)[0][0] if counts else ""


def _split_for(keys: list[str]) -> tuple[list[str], list[str]] | None:
    """Two groups of keys that share no character, each at least MIN_GROUP long, or None."""
    first_char = _top_char(keys)
    if not first_char:
        return None
    first = [k for k in keys if first_char in k]
    rest = [k for k in keys if first_char not in k]
    if len(first) < MIN_GROUP or len(rest) < MIN_GROUP:
        return None
    second_char = _top_char(rest)
    second = [k for k in rest if second_char and second_char in k]
    return (first, second) if len(second) >= MIN_GROUP else None


def _splits(terms: dict) -> list[WordSplit]:
    by_word: dict[str, set[str]] = {}
    for key, english in _usable_terms(terms):
        for word in _words(english):
            by_word.setdefault(word, set()).add(key)
    result = []
    for word, keys in sorted(by_word.items()):
        if len(keys) < 2 * MIN_GROUP:
            continue
        found = _split_for(sorted(keys))
        if found is not None:
            first, second = found
            result.append(WordSplit(word, _common_part(first), tuple(first), _common_part(second), tuple(second)))
        if len(result) >= MAX_REPORTS:
            break
    return result


# ----------------------------------------------------------------------
# Corpus check over aligned paragraphs
# ----------------------------------------------------------------------

def _entities(terms: dict, names: dict) -> list[tuple[str, tuple[str, ...]]]:
    """(English, Chinese spellings) for every term and every character."""
    result = [(english, glossary.term_spellings(terms, key)) for key, english in _usable_terms(terms)]
    by_english: dict[str, list[str]] = {}
    for chinese, english in names.items():
        if isinstance(chinese, str) and chinese.strip() and isinstance(english, str) and english.strip():
            by_english.setdefault(english, []).append(chinese.strip())
    result.extend((english, tuple(forms)) for english, forms in by_english.items())
    return result


def _phrase(english: str) -> re.Pattern:
    return re.compile(r"(?<![a-z0-9])" + re.escape(english.casefold()) + r"(?![a-z0-9])")


def _build_probes(terms: dict, names: dict) -> tuple[list[_Probe], dict[str, list[int]]]:
    """One probe per term, indexed by the first word of its English."""
    entities = _entities(terms, names)
    probes: list[_Probe] = []
    index: dict[str, list[int]] = {}
    for key, english in _usable_terms(terms)[:MAX_PROBES]:
        tokens = _TOKEN_RE.findall(english.casefold())
        if not tokens:
            continue
        pattern = _phrase(english)
        explains = tuple({form for other, forms in entities if pattern.search(other.casefold()) for form in forms})
        probes.append(_Probe(key, english, pattern, explains))
        index.setdefault(tokens[0], []).append(len(probes) - 1)
    return probes, index


def _bigrams(text: str) -> set[str]:
    pairs = set()
    for i in range(len(text) - 1):
        if _CJK_RE.match(text[i]) and _CJK_RE.match(text[i + 1]):
            pairs.add(text[i:i + 2])
    return pairs


def _record_unexplained(probe: _Probe, chapter: int, source: str) -> None:
    probe.unexplained += 1
    if chapter not in probe.chapters and len(probe.chapters) < MAX_SAMPLE_CHAPTERS:
        probe.chapters.append(chapter)
    if probe.sampled < MAX_BIGRAM_PARAGRAPHS:
        probe.bigrams.update(_bigrams(source))
        probe.sampled += 1


def _scan_paragraph(probes: list, index: dict, chapter: int, source: str, english: str) -> None:
    lowered = english.casefold()
    candidates = {i for token in set(_TOKEN_RE.findall(lowered)) for i in index.get(token, ())}
    for i in candidates:
        probe = probes[i]
        if not probe.pattern.search(lowered):
            continue
        probe.total += 1
        if source and not any(form in source for form in probe.explains):
            _record_unexplained(probe, chapter, source)


def _scan_chapter(probes: list, index: dict, chapter: int, zh: list, en: dict) -> None:
    for marker, english in en.items():
        source = zh[marker] if 0 <= marker < len(zh) else ""
        _scan_paragraph(probes, index, chapter, source, english)


def _likely(probe: _Probe) -> str:
    floor = max(2, (probe.sampled + 1) // 2)
    for bigram, count in probe.bigrams.most_common(MAX_LIKELY_CANDIDATES):
        if count < floor:
            break
        if not any(bigram in form for form in probe.explains):
            return bigram
    return ""


def _collisions(probes: list) -> list[CorpusCollision]:
    result = []
    for probe in probes:
        if probe.unexplained < MIN_UNEXPLAINED or probe.unexplained < MIN_UNEXPLAINED_SHARE * probe.total:
            continue
        result.append(CorpusCollision(probe.english, probe.key, probe.total, probe.unexplained,
                                      tuple(probe.chapters), _likely(probe)))
    result.sort(key=lambda c: -c.unexplained)
    return result[:MAX_REPORTS]


def _corpus(book_dir: str, terms: dict, names: dict, recent: int, warnings: list) -> tuple[list, int]:
    limit = max(1, min(int(recent), MAX_RECENT_CHAPTERS))
    try:
        chapters = segments.aligned_chapters(book_dir)[-limit:]
    except OSError as e:
        _warn(warnings, f"Could not list {segments.ALIGN_DIRNAME}/: {e}")
        return [], 0
    probes, index = _build_probes(terms, names)
    if not probes or not chapters:
        return [], 0
    scanned = 0
    for chapter in chapters:
        data = segments.load_alignment(book_dir, chapter)
        if data is None:
            _warn(warnings, f"Chapter {chapter}: alignment file is unreadable; skipped")
            continue
        zh, en = segments.alignment_texts(data)
        _scan_chapter(probes, index, chapter, zh, en)
        scanned += 1
    return _collisions(probes), scanned


# ----------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------

def lint(book_dir: str, recent: int = DEFAULT_RECENT_CHAPTERS) -> LintReport:
    """Every glossary problem found in the book. Raises ValueError when no book is selected."""
    if not book_dir:
        raise ValueError("No book directory selected")
    report = LintReport()
    data, err = glossary.load_glossary(book_dir)
    if err:
        report.warnings.append(err)
        return report
    terms = data["terms"]
    names = characters.name_map(book_dir)
    report.containment = (glossary.check_containment(terms, names) + _character_containment(terms, names))[:MAX_REPORTS]
    report.same_english = _same_english(terms)
    report.broken_links = _broken_links(terms)
    report.splits = _splits(terms)
    report.corpus, report.chapters_scanned = _corpus(book_dir, terms, names, recent, report.warnings)
    return report
