"""JSON repair and response parsing helpers."""
import json
import re
from dataclasses import dataclass, field

_CHINESE_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3,
    "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_CHINESE_UNITS = {
    "十": 10, "百": 100, "千": 1000, "万": 10000
}


def _chinese_to_int(s: str) -> int | None:
    """Convert Chinese numeral string (e.g. '三千零一', '二十五') to integer."""
    if not s or not isinstance(s, str):
        return None
    s = s.strip()
    if not s:
        return None

    # Handle pure digit representations if mixed
    if s.isdigit():
        return int(s)

    total = 0
    section = 0
    curr_digit = 0
    has_digit = False

    for ch in s:
        if ch in _CHINESE_DIGITS:
            curr_digit = _CHINESE_DIGITS[ch]
            has_digit = True
        elif ch in _CHINESE_UNITS:
            unit_val = _CHINESE_UNITS[ch]
            if unit_val == 10000:
                section = (section + (curr_digit if has_digit else 0)) * unit_val
                total += section
                section = 0
            else:
                if not has_digit and unit_val == 10 and section == 0:
                    curr_digit = 1
                    has_digit = True
                section += (curr_digit if has_digit else 1) * unit_val
            curr_digit = 0
            has_digit = False
        else:
            return None

    if has_digit:
        section += curr_digit

    total += section
    return total if total > 0 else None


def parse_chapter_number(text: str) -> int | None:
    """Extract chapter integer from string (checks first ~250 chars)."""
    if not text or not isinstance(text, str):
        return None

    sample = text[:250].strip()

    # Pattern 1: Arabic digits with Chinese chapter markers (e.g. 第2945章 / 第 2945 回 / 第2945节)
    m_ar = re.search(r'第\s*(\d{1,7})\s*[章回节集篇]', sample)
    if m_ar:
        try:
            return int(m_ar.group(1))
        except ValueError:
            pass

    # Pattern 2: English chapter identifier (e.g. Chapter 2945 / Ch. 2945 / chapter 2945)
    m_en = re.search(r'(?:Chapter|Ch\.?)\s*(\d{1,7})\b', sample, re.IGNORECASE)
    if m_en:
        try:
            return int(m_en.group(1))
        except ValueError:
            pass

    # Pattern 3: Chinese numerals (e.g. 第两千八百四十一章)
    m_zh = re.search(r'第\s*([零〇一二两三四五六七八九十百千万\d]+)\s*[章回节集篇]', sample)
    if m_zh:
        val = _chinese_to_int(m_zh.group(1))
        if val is not None:
            return val

    # Pattern 4: Bare leading digits followed by chapter keyword
    m_bare = re.search(r'^\s*(\d{1,7})\s*[.、\s]+', sample)
    if m_bare:
        try:
            return int(m_bare.group(1))
        except ValueError:
            pass

    return None


def format_chapter_ranges(numbers: list[int]) -> str:
    """Compress chapter numbers into text like 'Chapters 10–14, 17' or 'Chapter 5'."""
    nums = sorted(set(n for n in numbers if isinstance(n, int) and n > 0))
    if not nums:
        return ""
    ranges = []
    start = prev = nums[0]
    for num in nums[1:]:
        if num != prev + 1:
            ranges.append((start, prev))
            start = num
        prev = num
    ranges.append((start, prev))
    parts = [str(a) if a == b else f"{a}–{b}" for a, b in ranges]
    noun = "Chapter" if len(nums) == 1 else "Chapters"
    return f"{noun} {', '.join(parts)}"


_VALID_JSON_KEYS = [
    "chapterNo", "content",
    "chapters", "characterActions",
    "action", "Chinese", "English", "Description",
    "ModType", "Content", "Find", "Replace",
    "arcEnds", "endType", "arcName", "isAfterword",
    "termActions", "Type", "RefersTo", "Note", "Aliases",
    "candidateTerms", "ProposedEnglish",
]


def _strip_fences(s: str) -> str:
    """Remove markdown code fences (```json ... ```) if present."""
    if not s.startswith("```"):
        return s
    lines = s.splitlines()
    inner = []
    skip_first = True
    for line in lines:
        if skip_first and line.startswith("```"):
            skip_first = False
            continue
        if line.strip() == "```" and inner:
            break
        inner.append(line)
    return "\n".join(inner)


def repair_json(raw: str) -> str:
    """Attempt to normalize unescaped quotes and common LLM JSON errors."""
    s = raw.strip()
    s = _strip_fences(s)
    s = re.sub(r',\s*,', ',', s)
    s = s.replace('\\"', '"')
    s = s.replace('"', '\\"')

    for key in _VALID_JSON_KEYS:
        s = s.replace(f'\\"{key}\\"', f'"{key}"')

    for key in _VALID_JSON_KEYS:
        s = re.sub(rf'"{key}"\s*:\s*\\"', f'"{key}": "', s)

    s = re.sub(r'\\"\s*,', '",', s)
    s = re.sub(r'\\"\s*([}\]])', r'"\1', s)

    for key in _VALID_JSON_KEYS:
        s = re.sub(rf'"{key}"\s*:\s*\\"\[', f'"{key}": [', s)

    s = re.sub(r',\s*([\]}])', r'\1', s)
    return s


def parse_response(raw_response: str) -> tuple[list | None, list | None, str | None]:
    """Parse the LLM JSON response. Returns (chapters, character_actions, error_message)."""
    if not raw_response or not raw_response.strip():
        return None, None, "Empty response payload received from model"

    cleaned = _strip_fences(raw_response.strip())
    parsed = None

    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        try:
            repaired = repair_json(raw_response)
            parsed = json.loads(repaired)
        except json.JSONDecodeError as e:
            return None, None, f"JSON parse failed: {e}"

    if isinstance(parsed, dict):
        chapters = parsed.get("chapters", [])
        char_actions = parsed.get("characterActions", [])
        if not isinstance(chapters, list):
            return None, None, "'chapters' key must be a JSON array"
        return chapters, char_actions if isinstance(char_actions, list) else [], None

    if isinstance(parsed, list):
        return parsed, [], None

    return None, None, "Unexpected JSON top-level structure (expected object or array)"


def _load_payload(raw_response: str) -> tuple[object, str | None]:
    """Decode a reply, falling back to repair_json. Returns (parsed, error_message)."""
    if not raw_response or not raw_response.strip():
        return None, "Empty response payload received from model"
    cleaned = _strip_fences(raw_response.strip())
    try:
        return json.loads(cleaned), None
    except json.JSONDecodeError:
        repaired = repair_json(raw_response)  # second attempt after fixing common LLM quoting mistakes
    try:
        return json.loads(repaired), None
    except json.JSONDecodeError as e:
        return None, f"JSON parse failed: {e}"


def _list_field(parsed: dict, key: str) -> list:
    value = parsed.get(key, [])
    return value if isinstance(value, list) else []


@dataclass
class ParsedReply:
    """Decoded model reply. chapters is None when the reply could not be used (see error)."""

    chapters: list | None
    character_actions: list = field(default_factory=list)
    arc_ends: list = field(default_factory=list)
    term_actions: list = field(default_factory=list)
    error: str | None = None


def parse_reply(raw_response: str) -> ParsedReply:
    """Parse the LLM JSON response including arc endings and term actions.

    Missing "characterActions", "arcEnds" or "termActions" keys become empty lists, so older
    replies still parse. parse_response() above keeps its 3-tuple shape for any existing callers.
    """
    parsed, err = _load_payload(raw_response)
    if err:
        return ParsedReply(None, error=err)
    if isinstance(parsed, list):
        return ParsedReply(parsed)
    if not isinstance(parsed, dict):
        return ParsedReply(None, error="Unexpected JSON top-level structure (expected object or array)")
    chapters = parsed.get("chapters", [])
    if not isinstance(chapters, list):
        return ParsedReply(None, error="'chapters' key must be a JSON array")
    return ParsedReply(
        chapters,
        character_actions=_list_field(parsed, "characterActions"),
        arc_ends=_list_field(parsed, "arcEnds"),
        term_actions=_list_field(parsed, "termActions"),
    )


MAX_SCOUT_ENTRIES = 100   # the scout is asked for far fewer; more is certainly a model error
MAX_SCOUT_FIELD = 120


def _scout_text(value) -> str:
    if value is None or isinstance(value, (dict, list)):
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()[:MAX_SCOUT_FIELD]


def _scout_entry(item) -> dict | None:
    if not isinstance(item, dict):
        return None
    chinese = _scout_text(item.get("Chinese"))
    if not chinese:
        return None
    english = _scout_text(item.get("ProposedEnglish")) or _scout_text(item.get("English"))
    return {"Chinese": chinese, "ProposedEnglish": english, "Type": _scout_text(item.get("Type"))}


def parse_scout_reply(raw_response: str) -> tuple[list[dict], str | None]:
    """Candidate glossary terms from the term-scout reply. Returns (entries, error_message).

    Each entry is {"Chinese", "ProposedEnglish", "Type"}; a bare JSON array is accepted too.
    """
    parsed, err = _load_payload(raw_response)
    if err:
        return [], err
    items = parsed.get("candidateTerms") if isinstance(parsed, dict) else parsed
    if not isinstance(items, list):
        return [], "'candidateTerms' key must be a JSON array"
    entries = []
    for item in items[:MAX_SCOUT_ENTRIES]:
        entry = _scout_entry(item)
        if entry is not None:
            entries.append(entry)
    return entries, None
