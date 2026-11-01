"""Per-book style guide and a style check that only reports.

<Book>/Style.md holds choices specific to one book. Everything outside <!-- --> comments is sent to
the translator and the reviewer as the STYLE GUIDE, except the "Capped phrases" section, which sets
how often a phrase may appear before the style check reports it:

    ## Capped phrases
    - actually: 2       at most 2 uses per 1,000 words
    - somewhat: off     turn a built-in check off

The style check never changes text. It reports:
  - phrases used more often than their cap (information only; often translationese, sometimes voice);
  - "a"/"an" before the wrong sound, doubled words and Chinese characters left in the English
    (clear errors, which the reviewer may fix).
Built-in caps are used for detection only and are never sent to the model.
"""

import os
import re
from dataclasses import dataclass, field

STYLE_FILENAME = "Style.md"
CAPS_HEADING = "capped phrases"
DEFAULT_CAPS = {
    "actually": 2.0,
    "somewhat": 1.5,
    "couldn't help but": 1.0,
    "a trace of": 1.5,
    "at this moment": 1.5,
    "in the blink of an eye": 1.0,
}
MIN_PHRASE_COUNT = 3          # fewer uses than this are never reported, whatever the rate
MAX_STYLE_CHARS = 6_000       # longest STYLE GUIDE text sent to the model
MAX_CAPS = 100
MAX_LINES = 20_000
MAX_EXAMPLES = 3
ALLOWED_DOUBLES = frozenset({
    "had", "that", "no", "yes", "so", "very", "bye", "ha", "hah", "haha", "he", "hehe", "heh", "hey",
    "oh", "ah", "um", "uh", "hmm", "well", "go", "come", "run", "quick", "more", "again", "now",
    "boom", "bang", "knock", "tsk", "wow", "die", "kill", "next",
})
SILENT_H = ("hour", "honest", "honor", "honour", "heir", "herb")
YOU_SOUND = ("uni", "use", "usu", "uti", "ute", "ure", "uro", "eu", "ewe", "one", "once", "ufo")

_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_SECTION_RE = re.compile(r"^##\s+(.+?)\s*$")
_CAP_RE = re.compile(r"^[-*]\s*(.+?)\s*[:：]\s*(off|\d+(?:\.\d+)?)\s*$", re.IGNORECASE)
_TN_RE = re.compile(r"^[(（]\s*TN\s*[:：]", re.IGNORECASE)
_LINK_RE = re.compile(r"^\[\[[^\]]*\]\]$")
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z']*")
_ARTICLE_RE = re.compile(r"\b(a|an|An)\s+([A-Za-z][A-Za-z'-]*)")
_DOUBLE_RE = re.compile(r"\b([A-Za-z]+)[ \t]+(\1)\b", re.IGNORECASE)
_CJK_RUN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")


@dataclass
class StyleGuide:
    """A book's Style.md, read for the prompt and for the style check."""

    path: str = ""
    exists: bool = False
    rules: str = ""                                  # sections sent to the model
    caps: dict = field(default_factory=dict)          # phrase -> uses per 1,000 words, built-in and file
    file_caps: dict = field(default_factory=dict)     # caps written in Style.md (None = off)
    errors: list = field(default_factory=list)

    def prompt_text(self) -> str:
        """The STYLE GUIDE text for the model; '' when Style.md asks for nothing."""
        parts = [self.rules] if self.rules else []
        sparing = [f"- {p} (at most {c:g} per 1,000 words)" for p, c in self.file_caps.items() if c is not None]
        if sparing:
            parts.append("Use these phrases sparingly; they are counted:\n" + "\n".join(sparing))
        return "\n\n".join(parts)


@dataclass(frozen=True)
class StyleFinding:
    kind: str        # phrase, article, doubled, chinese
    text: str        # display text, e.g. '“actually” ×9 (4.5 per 1,000 words; cap 2)'
    count: int
    grammar: bool    # True for clear errors the reviewer may fix


# ----------------------------------------------------------------------
# Style.md
# ----------------------------------------------------------------------

def style_path(book_dir: str) -> str:
    return os.path.join(book_dir, STYLE_FILENAME)


def template() -> str:
    """A Style.md that sends nothing until the user writes outside the comments."""
    builtins = ", ".join(f"{p} ({c:g})" for p, c in DEFAULT_CAPS.items())
    return (
        "# Style guide\n\n"
        "<!-- Everything outside these comments is sent to the translator and the reviewer as this book's\n"
        "     STYLE GUIDE. Write only choices specific to this book, and delete what you do not need. -->\n\n"
        "## Translation rules\n\n"
        "<!-- Examples:\n"
        "- Profanity: match the strength of the Chinese; never soften or intensify it.\n"
        "- Pronouns: a character who speaks of himself with an imperial pronoun keeps \"We\" and \"Our\";\n"
        "  slips into \"I\" in the Chinese stay slips.\n"
        "- Names: transliterate personal names in pinyin; translate titles, races and places by meaning.\n"
        "-->\n\n"
        "## Capped phrases\n\n"
        "<!-- One phrase per line. \"- phrase: N\" lets the style check allow N uses per 1,000 words before it\n"
        f"     reports the phrase; \"- phrase: off\" turns a built-in check off. Built-in: {builtins}.\n"
        "     Phrases listed here are also sent to the translator as phrases to use sparingly. -->\n"
    )


def create_template(book_dir: str) -> tuple[bool, str]:
    """Write the template unless Style.md exists. Returns (ok, message)."""
    if not book_dir:
        return False, "No book directory selected."
    path = style_path(book_dir)
    if os.path.exists(path):
        return False, f"{STYLE_FILENAME} already exists: {path}"
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(template())
    except OSError as e:
        return False, f"Could not create {STYLE_FILENAME}: {e}"
    return True, f"Created {path}. Everything you write outside the <!-- --> comments is sent to the model."


def _sections(text: str) -> list[tuple[str, list[str]]]:
    """(heading, lines) per '## ' section; the part before the first heading has heading ''."""
    sections, heading, lines = [], "", []
    for line in text.splitlines()[:MAX_LINES]:
        match = _SECTION_RE.match(line.strip())
        if match:
            sections.append((heading, lines))
            heading, lines = match.group(1).strip(), []
        else:
            lines.append(line)
    sections.append((heading, lines))
    return sections


def _normal_phrase(text: str) -> str:
    return " ".join(text.lower().replace("’", "'").split())


def _parse_caps(lines: list[str], guide: StyleGuide) -> None:
    for line in lines:
        text = line.strip()
        if not text:
            continue
        match = _CAP_RE.match(text)
        if not match or not _normal_phrase(match.group(1)):
            guide.errors.append(f"Capped phrases: could not read “{text[:60]}” (write \"- phrase: N\" or \"- phrase: off\")")
            continue
        if len(guide.file_caps) >= MAX_CAPS:
            guide.errors.append(f"Capped phrases: only the first {MAX_CAPS} are used")
            return
        value = match.group(2).lower()
        guide.file_caps[_normal_phrase(match.group(1))] = None if value == "off" else float(value)


def _rules(sections, guide: StyleGuide) -> str:
    rules = []
    for heading, lines in sections:
        if heading.casefold() == CAPS_HEADING:
            _parse_caps(lines, guide)
            continue
        body = "\n".join(line for line in lines if not line.startswith("# ")).strip()
        if body:
            rules.append(f"{heading}\n{body}" if heading else body)
    text = "\n\n".join(rules)
    if len(text) > MAX_STYLE_CHARS:
        guide.errors.append(f"The style guide is longer than {MAX_STYLE_CHARS:,} characters; only the start is sent")
    return text[:MAX_STYLE_CHARS]


def load_style(book_dir: str) -> StyleGuide:
    """The book's style guide; built-in caps only when there is no Style.md. Never raises."""
    guide = StyleGuide(caps=dict(DEFAULT_CAPS))
    if not book_dir:
        return guide
    guide.path = style_path(book_dir)
    if not os.path.isfile(guide.path):
        return guide
    guide.exists = True
    try:
        with open(guide.path, "r", encoding="utf-8") as f:
            raw = f.read()
    except (OSError, ValueError) as e:
        guide.errors.append(f"Could not read {STYLE_FILENAME}: {e}")
        return guide
    guide.rules = _rules(_sections(_COMMENT_RE.sub("", raw)), guide)
    for phrase, cap in guide.file_caps.items():
        if cap is None:
            guide.caps.pop(phrase, None)
        else:
            guide.caps[phrase] = cap
    return guide


# ----------------------------------------------------------------------
# Style check
# ----------------------------------------------------------------------

def _body(text) -> str:
    """Story text of a chapter: no heading, Translator's Notes or wikilink."""
    keep = []
    for line in str(text or "").splitlines()[:MAX_LINES]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or _TN_RE.match(stripped) or _LINK_RE.match(stripped):
            continue
        keep.append(stripped.replace("’", "'"))
    return "\n".join(keep)


def _examples(items: list[str]) -> str:
    unique = list(dict.fromkeys(items))
    shown = ", ".join(f"“{item}”" for item in unique[:MAX_EXAMPLES])
    return shown + (", …" if len(unique) > MAX_EXAMPLES else "")


def _phrase_findings(body: str, caps: dict, words: int) -> list[StyleFinding]:
    findings = []
    for phrase, cap in caps.items():
        if not phrase or not isinstance(cap, (int, float)):
            continue
        pattern = re.compile(r"(?<![A-Za-z])" + re.escape(phrase) + r"(?![A-Za-z])", re.IGNORECASE)
        count = len(pattern.findall(body))
        rate = count * 1000 / max(words, 1)
        if count >= MIN_PHRASE_COUNT and rate > cap:
            text = f"“{phrase}” ×{count} ({rate:.1f} per 1,000 words; cap {cap:g})"
            findings.append(StyleFinding("phrase", text, count, False))
    return findings


def _wrong_article(article: str, word: str) -> bool:
    if len(word) == 1 or word.isupper():
        return False  # letters and acronyms are read by their names
    lower = word.lower()
    vowel = lower[0] in "aeiou"
    if article.lower() == "an":
        return not vowel and not lower.startswith(SILENT_H)
    return vowel and not lower.startswith(YOU_SOUND)


def _grammar_findings(body: str) -> list[StyleFinding]:
    findings = []
    articles = [m.group(0) for m in _ARTICLE_RE.finditer(body) if _wrong_article(m.group(1), m.group(2))]
    if articles:
        findings.append(StyleFinding("article", f"wrong article: {_examples(articles)}", len(articles), True))
    doubled = [m.group(0) for m in _DOUBLE_RE.finditer(body) if m.group(1).lower() not in ALLOWED_DOUBLES]
    if doubled:
        findings.append(StyleFinding("doubled", f"doubled word: {_examples(doubled)}", len(doubled), True))
    chinese = _CJK_RUN_RE.findall(body)
    if chinese:
        count = sum(len(run) for run in chinese)
        findings.append(StyleFinding("chinese", f"{count} Chinese character(s) left in the English: {_examples(chinese)}", count, True))
    return findings


def check_text(text, caps=None) -> list[StyleFinding]:
    """Style findings for one translated chapter. caps defaults to the built-in caps."""
    body = _body(text)
    if not body:
        return []
    words = len(_WORD_RE.findall(body))
    limits = DEFAULT_CAPS if caps is None else caps
    return _grammar_findings(body) + _phrase_findings(body, limits, words)


def describe(findings) -> str:
    """All findings of one chapter on one line."""
    return "; ".join(f.text for f in findings or [] if isinstance(f, StyleFinding))
