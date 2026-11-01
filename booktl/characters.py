"""Character management and context tracking."""

import json
import os
import re

from .storage import sanitize_filename

CHANGE_KINDS = ("modified", "added", "associated")
MIN_NAME_HITS = 2          # a name must appear this often on the page before its absence is reported
MIN_DRIFT_NAME_LENGTH = 2
_NAME_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z'’-]{2,}")


def load_characters(book_dir: str) -> dict:
    """Load Chinese -> English markdown mapping from Characters/Characters.json."""
    path = os.path.join(book_dir, "Characters", "Characters.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}


def save_characters(book_dir: str, char_map: dict) -> None:
    """Persist character mapping back to Characters/Characters.json."""
    chars_dir = os.path.join(book_dir, "Characters")
    os.makedirs(chars_dir, exist_ok=True)
    path = os.path.join(chars_dir, "Characters.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(char_map, f, ensure_ascii=False, indent=2)


def get_character_context(book_dir: str, chinese_text: str) -> str:
    """Scan raw text for known character occurrences and assemble Markdown descriptions."""
    char_map = load_characters(book_dir)
    if not char_map:
        return ""

    chars_dir = os.path.join(book_dir, "Characters")
    seen_files = set()
    context_parts = []

    working = chinese_text if isinstance(chinese_text, str) else ""
    names = sorted((k for k in char_map if isinstance(k, str) and k), key=len, reverse=True)
    for chinese_name in names:
        md_filename = char_map[chinese_name]
        if not isinstance(md_filename, str) or chinese_name not in working:
            continue
        # Mask the name so a shorter name inside it (张三 in 张三丰) does not match again.
        working = working.replace(chinese_name, "\x00" * len(chinese_name))
        if md_filename in seen_files:
            continue
        seen_files.add(md_filename)
        md_path = os.path.join(chars_dir, md_filename)
        if os.path.exists(md_path):
            try:
                with open(md_path, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                char_label = os.path.splitext(md_filename)[0]
                context_parts.append(f"### {char_label}\n{content}")
            except IOError:
                pass  # an unreadable profile only means less context for this batch

    if not context_parts:
        return ""
    return (
        "The following characters appear in this section. "
        "Use this context to ensure consistent names and details:\n\n"
        + "\n\n".join(context_parts)
    )


def name_map(book_dir: str) -> dict[str, str]:
    """Chinese name -> English name (the character file's name) for every known spelling."""
    char_map = load_characters(book_dir) if book_dir else {}
    if not isinstance(char_map, dict):
        return {}
    return {
        chinese.strip(): os.path.splitext(md)[0]
        for chinese, md in char_map.items()
        if isinstance(chinese, str) and chinese.strip() and isinstance(md, str)
    }


def _name_hits(char_map: dict, raw: str) -> dict[str, list]:
    """Character file -> [times named, spellings found], longest spellings matched and masked first."""
    working = raw
    hits: dict[str, list] = {}
    names = sorted((k for k in char_map if isinstance(k, str) and len(k.strip()) >= MIN_DRIFT_NAME_LENGTH),
                   key=len, reverse=True)
    for chinese in names:
        md_filename = char_map[chinese]
        count = working.count(chinese) if isinstance(md_filename, str) else 0
        if not count:
            continue
        working = working.replace(chinese, "\x00" * len(chinese))
        entry = hits.setdefault(md_filename, [0, []])
        entry[0] += count
        entry[1].append(chinese)
    return hits


def _named(english: str, folded: str) -> bool:
    """True when the English name, or one of its words of 3+ letters, appears in the folded text."""
    if english.casefold() in folded:
        return True
    for token in _NAME_TOKEN_RE.findall(english):
        if re.search(r"(?<![a-z])" + re.escape(token.casefold()) + r"(?![a-z])", folded):
            return True
    return False


def name_drift(char_map, raw, translation) -> list[dict]:
    """Characters named at least MIN_NAME_HITS times on the raw page but never in the translation.

    Returns {"english", "forms", "count"} entries. Pronouns replace names legitimately, so a
    name used once is never reported, and any word of the English name counts as naming them.
    """
    if not isinstance(char_map, dict) or not isinstance(raw, str) or not isinstance(translation, str):
        return []
    folded = translation.casefold()
    drift = []
    for md_filename, (count, forms) in _name_hits(char_map, raw).items():
        if count < MIN_NAME_HITS:
            continue
        english = os.path.splitext(md_filename)[0]
        if not _named(english, folded):
            drift.append({"english": english, "forms": " / ".join(forms), "count": count})
    return drift


def get_all_characters_summary(book_dir: str) -> str:
    """Return a list of all established characters and known aliases."""
    char_map = load_characters(book_dir)
    if not char_map:
        return ""

    grouped = {}
    for zh_name, md_file in char_map.items():
        en_name = os.path.splitext(md_file)[0]
        grouped.setdefault(en_name, []).append(zh_name)

    lines = []
    for en_name, zh_names in grouped.items():
        aliases = ", ".join(zh_names)
        lines.append(f"- {en_name} (Aliases: {aliases})")

    if not lines:
        return ""

    return (
        "The following characters have already been established in the database. "
        "DO NOT use AddChar for these characters, use AssosiateChar or ModChar instead:\n"
        + "\n".join(lines)
    )


# ----------------------------------------------------------------------
# Character actions produced by the LLM
# ----------------------------------------------------------------------

def _new_summary() -> dict:
    """Names per outcome, plus reasons for skipped and failed actions."""
    return {"modified": [], "added": [], "associated": [], "skipped": [], "failed": []}


def _field(act: dict, key: str) -> str:
    value = act.get(key, "")
    return str(value).strip() if value is not None else ""


def _char_filename(english: str) -> str:
    """Markdown filename for a character, stripped of illegal path characters."""
    return sanitize_filename(f"{english}.md")


def _map_alias(char_map: dict, chinese: str, md_filename: str) -> bool:
    """Map a Chinese name to a character file. Returns False if it was already mapped."""
    if chinese in char_map:
        return False
    char_map[chinese] = md_filename
    return True


def _add_char(chars_dir: str, char_map: dict, act: dict, summary: dict) -> None:
    chinese, english = _field(act, "Chinese"), _field(act, "English")
    if not chinese or not english:
        summary["skipped"].append("AddChar missing Chinese or English name")
        return
    md_filename = _char_filename(english)
    md_path = os.path.join(chars_dir, md_filename)
    if os.path.exists(md_path):
        if _map_alias(char_map, chinese, md_filename):
            summary["associated"].append(english)
        else:
            summary["skipped"].append(f"AddChar '{english}' already exists")
        return
    try:
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(f"# {english}\n\n{_field(act, 'Description')}\n")
    except OSError as e:
        summary["failed"].append(f"AddChar '{english}': {e}")
        return
    _map_alias(char_map, chinese, md_filename)
    summary["added"].append(english)


def _modified_content(current: str, act: dict) -> tuple[str | None, str | None]:
    """Return (new_content, None) or (None, reason_for_skipping)."""
    mod_type = _field(act, "ModType").lower()
    if mod_type == "addition":
        addition = _field(act, "Content")
        if not addition:
            return None, "empty addition"
        return current.rstrip() + "\n\n" + addition + "\n", None
    if mod_type == "replacement":
        find = act.get("Find", "")
        if not isinstance(find, str) or not find or find not in current:
            return None, "replacement target not found"
        replace = act.get("Replace", "")
        return current.replace(find, str(replace) if replace is not None else "", 1), None
    return None, f"unknown ModType '{mod_type}'"


def _mod_char(chars_dir: str, char_map: dict, act: dict, summary: dict) -> None:
    english = _field(act, "English")
    if not english:
        summary["skipped"].append("ModChar missing English name")
        return
    md_path = os.path.join(chars_dir, _char_filename(english))
    if not os.path.exists(md_path):
        summary["skipped"].append(f"ModChar '{english}' has no character file")
        return
    try:
        with open(md_path, "r", encoding="utf-8") as f:
            current = f.read()
    except OSError as e:
        summary["failed"].append(f"ModChar '{english}' read: {e}")
        return
    updated, reason = _modified_content(current, act)
    if updated is None:
        summary["skipped"].append(f"ModChar '{english}': {reason}")
        return
    try:
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(updated)
    except OSError as e:
        summary["failed"].append(f"ModChar '{english}' write: {e}")
        return
    summary["modified"].append(english)


def _associate_char(chars_dir: str, char_map: dict, act: dict, summary: dict) -> None:
    chinese, english = _field(act, "Chinese"), _field(act, "English")
    if not chinese or not english:
        summary["skipped"].append("AssosiateChar missing Chinese or English name")
        return
    md_filename = _char_filename(english)
    if not os.path.exists(os.path.join(chars_dir, md_filename)):
        summary["skipped"].append(f"AssosiateChar '{english}' has no character file")
        return
    if _map_alias(char_map, chinese, md_filename):
        summary["associated"].append(english)
    else:
        summary["skipped"].append(f"AssosiateChar '{chinese}' already mapped")


_ACTION_HANDLERS = {
    "AddChar": _add_char,
    "ModChar": _mod_char,
    "AssosiateChar": _associate_char,
}


def _apply_action(chars_dir: str, char_map: dict, act, summary: dict) -> None:
    if not isinstance(act, dict):
        summary["skipped"].append("action is not an object")
        return
    name = act.get("action", "")
    handler = _ACTION_HANDLERS.get(name)
    if handler is None:
        summary["skipped"].append(f"unknown action '{name}'")
        return
    handler(chars_dir, char_map, act, summary)


def process_character_actions(book_dir: str, actions: list) -> dict:
    """Apply LLM character actions and return a summary (see _new_summary)."""
    summary = _new_summary()
    if not book_dir:
        summary["failed"].append("no book directory selected")
        return summary
    if not isinstance(actions, list):
        summary["skipped"].append("characterActions is not a list")
        return summary
    chars_dir = os.path.join(book_dir, "Characters")
    try:
        os.makedirs(chars_dir, exist_ok=True)
    except OSError as e:
        summary["failed"].append(f"could not create Characters folder: {e}")
        return summary
    char_map = load_characters(book_dir)
    for act in actions:
        _apply_action(chars_dir, char_map, act, summary)
    try:
        save_characters(book_dir, char_map)
    except OSError as e:
        summary["failed"].append(f"could not save Characters.json: {e}")
    return summary


def format_summary(summary: dict) -> str | None:
    """One line such as 'Modified 2 characters, added 1 character, associated 3 characters'."""
    if not isinstance(summary, dict):
        return None
    parts = []
    for kind in CHANGE_KINDS:
        count = len(set(summary.get(kind, [])))
        if count:
            parts.append(f"{kind} {count} {'character' if count == 1 else 'characters'}")
    skipped = len(summary.get("skipped", []))
    if skipped:
        parts.append(f"skipped {skipped} {'action' if skipped == 1 else 'actions'}")
    if not parts:
        return None
    text = ", ".join(parts)
    return text[0].upper() + text[1:]
