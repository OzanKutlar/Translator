"""JSON repair and response parsing helpers."""

import json
import re

_VALID_JSON_KEYS = [
    "chapterNo", "content",
    "chapters", "characterActions",
    "action", "Chinese", "English", "Description",
    "ModType", "Content", "Find", "Replace",
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
