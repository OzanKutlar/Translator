"""Glossary of locked term renderings, stored in <Book>/Glossary.json.

Covers what the character system does not: titles and nicknames, skills, buffs and
debuffs, items, places, organizations, ranks and so on.

    {
      "version": 1,
      "terms": {
        "秃鹫尊者": {"en": "Venerable Vulture", "type": "title", "aliases": ["秃鹫老祖"],
                    "refersTo": "Li Yao", "note": "", "source": "model", "firstChapter": 812}
      }
    }

Each term is keyed by its canonical Chinese text; "aliases" lists other Chinese spellings
that must get the same English. Terms found in a batch are sent to the model as LOCKED
TERMS. The model may only add terms and aliases: a different rendering for a known term is
logged as a conflict and never applied, and "source": "user" terms are never touched by it.
"""

import json
import os
import re
import threading
from dataclasses import dataclass

GLOSSARY_FILENAME = "Glossary.json"
SCHEMA_VERSION = 1
MIN_KEY_LENGTH = 2            # one-character keys would match almost every page
MAX_KEY_LENGTH = 40
MAX_ENGLISH_LENGTH = 120
MAX_NOTE_LENGTH = 300
MAX_ALIASES = 20
MAX_INJECTED_TERMS = 150      # bounds the LOCKED TERMS block sent with each batch
MAX_ACTIONS_PER_REPLY = 200   # more than this in one reply is certainly a model error
MAX_DRIFT_WARNINGS = 50
MAX_NAMES_IN_SUMMARY = 5
TERM_TYPES = ("title", "skill", "item", "status", "place", "organization", "rank", "other")
_TYPE_ALIASES = {
    "nickname": "title", "epithet": "title", "alias": "title",
    "technique": "skill", "ability": "skill", "spell": "skill", "move": "skill",
    "buff": "status", "debuff": "status", "effect": "status", "statuseffect": "status", "condition": "status",
    "artifact": "item", "weapon": "item", "treasure": "item", "pill": "item", "equipment": "item",
    "location": "place", "region": "place", "city": "place",
    "sect": "organization", "faction": "organization", "guild": "organization", "clan": "organization",
    "realm": "rank", "level": "rank", "cultivation": "rank", "tier": "rank",
}

# Workers can record terms concurrently, and on Windows a file that is open cannot be
# replaced, so every read and write of Glossary.json goes through this lock.
_file_lock = threading.Lock()


@dataclass(frozen=True)
class MatchedTerm:
    """A glossary term found in a piece of raw text."""

    key: str
    english: str
    type: str
    refers_to: str
    note: str
    forms: tuple[str, ...]   # the Chinese spellings actually found in the text


# ----------------------------------------------------------------------
# File access
# ----------------------------------------------------------------------

def glossary_path(book_dir: str) -> str:
    return os.path.join(book_dir, GLOSSARY_FILENAME)


def _empty_data() -> dict:
    return {"version": SCHEMA_VERSION, "terms": {}}


def load_glossary(book_dir: str) -> tuple[dict | None, str | None]:
    """Return (data, None), or (None, error) when Glossary.json exists but is unusable.

    A missing file is not an error; it yields an empty glossary.
    """
    path = glossary_path(book_dir)
    if not os.path.exists(path):
        return _empty_data(), None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        return None, f"could not read {GLOSSARY_FILENAME}: {e}"
    if not isinstance(data, dict) or not isinstance(data.get("terms", {}), dict):
        return None, f"{GLOSSARY_FILENAME} does not have the expected structure"
    data.setdefault("version", SCHEMA_VERSION)
    data.setdefault("terms", {})
    return data, None


def save_glossary(book_dir: str, data: dict) -> None:
    """Write Glossary.json via a temp file and rename, so a crash never leaves half a file."""
    path = glossary_path(book_dir)
    tmp_path = path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp_path, path)
    except OSError:
        _discard_temp(tmp_path)
        raise


def _discard_temp(tmp_path: str) -> None:
    """Best-effort cleanup after a failed save; the caller re-raises the original error."""
    try:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
    except OSError:
        pass  # the save error being re-raised is the one that matters


def _try_save(book_dir: str, data: dict) -> str | None:
    """Save and return None, or return an error message."""
    try:
        save_glossary(book_dir, data)
    except OSError as e:
        return f"could not save {GLOSSARY_FILENAME}: {e}"
    return None


def load_terms(book_dir: str) -> dict:
    """Terms for matching; {} when there is no book, no file, or the file is unreadable.

    An unreadable file is reported loudly the next time a term is recorded.
    """
    if not book_dir:
        return {}
    with _file_lock:
        data, err = load_glossary(book_dir)
    return {} if err else data["terms"]


# ----------------------------------------------------------------------
# Field cleaning
# ----------------------------------------------------------------------

def _clean_text(value, limit: int) -> str:
    if value is None or isinstance(value, (dict, list)):
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()[:limit]


def _clean_key(value) -> str:
    """Chinese term text, or '' when it is not a usable key."""
    if not isinstance(value, str):
        return ""
    key = value.strip()
    return key if MIN_KEY_LENGTH <= len(key) <= MAX_KEY_LENGTH else ""


def _term_type(value) -> str:
    key = re.sub(r"[^a-z]", "", str(value or "").lower())
    if key in TERM_TYPES:
        return key
    return _TYPE_ALIASES.get(key, "other")


def _first_chapter(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _english_of(record) -> str:
    if not isinstance(record, dict):
        return ""
    return _clean_text(record.get("en"), MAX_ENGLISH_LENGTH)


def _aliases_of(record) -> list[str]:
    if not isinstance(record, dict) or not isinstance(record.get("aliases"), list):
        return []
    aliases = []
    for item in record["aliases"][:MAX_ALIASES]:
        key = _clean_key(item)
        if key and key not in aliases:
            aliases.append(key)
    return aliases


# ----------------------------------------------------------------------
# Lookup and matching
# ----------------------------------------------------------------------

def _build_index(terms: dict) -> dict[str, str]:
    """Every usable Chinese spelling -> canonical key. A canonical key beats an alias on clashes."""
    usable = [(k, r) for k, r in terms.items() if _clean_key(k) and _english_of(r)]
    index = {_clean_key(k): k for k, _r in usable}
    for key, record in usable:
        for alias in _aliases_of(record):
            index.setdefault(alias, key)
    return index


def _find_by_english(terms: dict, english: str) -> str | None:
    """Canonical key of the term rendered as english (case-insensitive), or None."""
    target = english.casefold()
    for key, record in terms.items():
        if _clean_key(key) and _english_of(record).casefold() == target:
            return key
    return None


def _matched(key: str, record: dict, forms: list[str]) -> MatchedTerm:
    return MatchedTerm(
        key=key,
        english=_english_of(record),
        type=_term_type(record.get("type")),
        refers_to=_clean_text(record.get("refersTo"), MAX_ENGLISH_LENGTH),
        note=_clean_text(record.get("note"), MAX_NOTE_LENGTH),
        forms=tuple(forms),
    )


def find_terms(terms: dict, text: str, limit: int = MAX_INJECTED_TERMS) -> list[MatchedTerm]:
    """Terms whose Chinese text or aliases occur in text. limit=0 returns every match.

    Longer spellings are matched first and masked out, so 小丑印记 wins over 小丑 inside it.
    """
    if not isinstance(terms, dict) or not isinstance(text, str) or not text:
        return []
    index = _build_index(terms)
    found: dict[str, list[str]] = {}
    working = text
    for form in sorted(index, key=len, reverse=True):
        if form in working:
            found.setdefault(index[form], []).append(form)
            working = working.replace(form, "\x00" * len(form))
    matched = [_matched(key, terms[key], forms) for key, forms in found.items()]
    return matched[:limit] if limit else matched


def match_terms(book_dir: str, text: str) -> list[MatchedTerm]:
    """Glossary terms that occur in text, for the LOCKED TERMS block."""
    return find_terms(load_terms(book_dir), text)


def _context_line(term: MatchedTerm) -> str:
    details = [term.type]
    if term.refers_to:
        details.append(f"refers to {term.refers_to}")
    if term.note:
        details.append(term.note)
    return f"- {' / '.join(term.forms)} → {term.english} ({'; '.join(details)})"


def get_term_context(matched: list[MatchedTerm]) -> str:
    """LOCKED TERMS block for the prompt; "" when nothing matched."""
    if not matched:
        return ""
    return (
        "These terms appear in this section and are LOCKED. Use exactly these English renderings "
        "every time, including inside longer phrases. Do not AddTerm them again:\n"
        + "\n".join(_context_line(term) for term in matched)
    )


# ----------------------------------------------------------------------
# Term actions produced by the LLM
# ----------------------------------------------------------------------

def _new_summary() -> dict:
    """added/unchanged: English names; aliased/conflicts: dicts; skipped/failed: reasons."""
    return {"added": [], "aliased": [], "unchanged": [], "conflicts": [], "skipped": [], "failed": []}


def _action_aliases(act: dict) -> list[str]:
    raw = act.get("Aliases")
    if isinstance(raw, str):
        raw = re.split(r"[,，、;；]", raw)
    if not isinstance(raw, list):
        return []
    aliases = []
    for item in raw[:MAX_ALIASES]:
        key = _clean_key(item)
        if key and key not in aliases:
            aliases.append(key)
    return aliases


def _report_existing(terms: dict, key: str, chinese: str, english: str, summary: dict) -> bool:
    """A known spelling was re-added: same rendering is a no-op, a different one a conflict."""
    current = _english_of(terms.get(key))
    if current.casefold() == english.casefold():
        summary["unchanged"].append(current)
    else:
        summary["conflicts"].append({"chinese": chinese, "current": current, "suggested": english})
    return False


def _attach_alias(terms: dict, index: dict, key: str, alias: str, summary: dict) -> bool:
    """Add alias to terms[key]. Returns True when the glossary changed."""
    owner = index.get(alias)
    if owner is not None:
        if owner != key:
            summary["skipped"].append(f"{alias} already belongs to “{_english_of(terms.get(owner))}”")
        return False
    record = terms.get(key)
    if not isinstance(record, dict):
        summary["skipped"].append(f"{alias}: target term {key} is unreadable")
        return False
    english = _english_of(record)
    if record.get("source") == "user":
        summary["skipped"].append(
            f"{alias} looks like an alias of your term “{english}”; add it with 'term alias {alias} = {key}'"
        )
        return False
    aliases = _aliases_of(record)
    if len(aliases) >= MAX_ALIASES:
        summary["skipped"].append(f"“{english}” already has {MAX_ALIASES} aliases; {alias} not added")
        return False
    record["aliases"] = aliases + [alias]
    index[alias] = key
    summary["aliased"].append({"alias": alias, "english": english})
    return True


def _new_record(act: dict, english: str, chapter) -> dict:
    return {
        "en": english,
        "type": _term_type(act.get("Type")),
        "aliases": [],
        "refersTo": _clean_text(act.get("RefersTo"), MAX_ENGLISH_LENGTH),
        "note": _clean_text(act.get("Note"), MAX_NOTE_LENGTH),
        "source": "model",
        "firstChapter": _first_chapter(chapter),
    }


def _add_term(terms: dict, index: dict, act: dict, chapter, summary: dict) -> bool:
    chinese = _clean_key(act.get("Chinese"))
    english = _clean_text(act.get("English"), MAX_ENGLISH_LENGTH)
    if not chinese or not english:
        summary["skipped"].append(f"AddTerm needs Chinese ({MIN_KEY_LENGTH}–{MAX_KEY_LENGTH} characters) and English")
        return False
    if chinese in index:
        return _report_existing(terms, index[chinese], chinese, english, summary)
    same_english = _find_by_english(terms, english)
    if same_english is not None:
        return _attach_alias(terms, index, same_english, chinese, summary)
    terms[chinese] = _new_record(act, english, chapter)
    index[chinese] = chinese
    summary["added"].append(english)
    for alias in _action_aliases(act):
        _attach_alias(terms, index, chinese, alias, summary)
    return True


def _alias_term(terms: dict, index: dict, act: dict, chapter, summary: dict) -> bool:
    alias = _clean_key(act.get("Chinese"))
    english = _clean_text(act.get("English"), MAX_ENGLISH_LENGTH)
    if not alias or not english:
        summary["skipped"].append(f"AliasTerm needs Chinese ({MIN_KEY_LENGTH}–{MAX_KEY_LENGTH} characters) and English")
        return False
    key = _find_by_english(terms, english)
    if key is None:
        return _add_term(terms, index, act, chapter, summary)  # nothing to link to yet: register it
    return _attach_alias(terms, index, key, alias, summary)


_ACTION_HANDLERS = {
    "AddTerm": _add_term,
    "AliasTerm": _alias_term,
    "AssociateTerm": _alias_term,
    "AssosiateTerm": _alias_term,
}


def _apply_action(terms: dict, index: dict, act, chapter, summary: dict) -> bool:
    if not isinstance(act, dict):
        summary["skipped"].append("term action is not an object")
        return False
    handler = _ACTION_HANDLERS.get(str(act.get("action", "")).strip())
    if handler is None:
        summary["skipped"].append(f"unknown term action '{act.get('action', '')}'")
        return False
    return handler(terms, index, act, chapter, summary)


def _merge_actions(book_dir: str, actions: list, chapter, summary: dict) -> None:
    data, err = load_glossary(book_dir)
    if err:
        summary["failed"].append(f"{err} (nothing was written; fix or remove the file)")
        return
    terms = data["terms"]
    index = _build_index(terms)
    changed = False
    for act in actions:
        changed = _apply_action(terms, index, act, chapter, summary) or changed
    if not changed:
        return
    save_err = _try_save(book_dir, data)
    if save_err:
        summary["added"].clear()
        summary["aliased"].clear()
        summary["failed"].append(save_err)


def record_term_actions(book_dir: str, actions, first_chapter=None) -> dict:
    """Apply model term actions to Glossary.json and return a summary (see _new_summary).

    first_chapter is stored on new terms for reference only.
    """
    summary = _new_summary()
    if not book_dir:
        summary["failed"].append("no book directory selected")
        return summary
    if not isinstance(actions, list):
        summary["skipped"].append("termActions is not a list")
        return summary
    if len(actions) > MAX_ACTIONS_PER_REPLY:
        summary["skipped"].append(f"{len(actions)} term actions in one reply; all ignored as implausible")
        return summary
    if actions:
        with _file_lock:
            _merge_actions(book_dir, actions, first_chapter, summary)
    return summary


def _count_text(count: int, singular: str, plural: str) -> str:
    return f"{count} {singular if count == 1 else plural}"


def format_summary(summary) -> str | None:
    """One line such as 'Added 2 terms (Joker's Mark, Venerable Vulture), linked 1 alias'."""
    if not isinstance(summary, dict):
        return None
    parts = []
    added = list(dict.fromkeys(summary.get("added", [])))
    if added:
        names = ", ".join(added[:MAX_NAMES_IN_SUMMARY]) + (", …" if len(added) > MAX_NAMES_IN_SUMMARY else "")
        parts.append(f"added {_count_text(len(added), 'term', 'terms')} ({names})")
    for kind, verb, singular, plural in (
        ("aliased", "linked", "alias", "aliases"),
        ("conflicts", "rejected", "conflicting rendering", "conflicting renderings"),
        ("skipped", "skipped", "term action", "term actions"),
    ):
        count = len(summary.get(kind, []))
        if count:
            parts.append(f"{verb} {_count_text(count, singular, plural)}")
    if not parts:
        return None
    text = ", ".join(parts)
    return text[0].upper() + text[1:]


# ----------------------------------------------------------------------
# Drift check
# ----------------------------------------------------------------------

def _pair_drift(terms: dict, pair) -> list[dict]:
    if not isinstance(pair, tuple) or len(pair) != 3:
        return []
    label, raw, translated = pair
    if not isinstance(raw, str) or not isinstance(translated, str):
        return []
    haystack = translated.casefold()
    return [
        {"label": str(label), "english": term.english, "forms": " / ".join(term.forms)}
        for term in find_terms(terms, raw, limit=0)
        if term.english.casefold() not in haystack
    ]


def check_term_usage(terms: dict, pairs) -> list[dict]:
    """Drift check for (label, raw_page, translation) pairs.

    Returns one {"label", "english", "forms"} entry per locked term whose Chinese occurs in
    the raw page while its English rendering is absent from the translation (case-insensitive).
    """
    if not isinstance(terms, dict) or not terms or not isinstance(pairs, list):
        return []
    drift = []
    for pair in pairs:
        if len(drift) >= MAX_DRIFT_WARNINGS:
            break
        drift.extend(_pair_drift(terms, pair))
    return drift[:MAX_DRIFT_WARNINGS]


# ----------------------------------------------------------------------
# Manual editing (console commands)
# ----------------------------------------------------------------------

def _edit_glossary(book_dir: str, operation) -> tuple[bool, str]:
    """Run operation(terms) -> (ok, message, changed) under the lock; save when it changed."""
    if not book_dir:
        return False, "No book directory selected."
    with _file_lock:
        data, err = load_glossary(book_dir)
        if err:
            return False, f"{err} (nothing was written; fix or remove the file)"
        ok, message, changed = operation(data["terms"])
        if ok and changed:
            save_err = _try_save(book_dir, data)
            if save_err:
                return False, save_err
        return ok, message


def _set_user_term(terms: dict, key: str, english: str, term_type) -> tuple[bool, str, bool]:
    owner = _build_index(terms).get(key)
    if owner is not None and owner != key:
        return False, (
            f"{key} is an alias of {owner} (“{_english_of(terms.get(owner))}”). "
            f"Change that term, or remove the alias first with 'term rm {key}'."
        ), False
    record = terms.get(key)
    if not isinstance(record, dict):
        record = {"en": "", "type": "other", "aliases": [], "refersTo": "", "note": "",
                  "source": "user", "firstChapter": None}
        terms[key] = record
    previous = _english_of(record)
    record["en"] = english
    record["source"] = "user"
    if term_type:
        record["type"] = _term_type(term_type)
    was = f" (was “{previous}”)" if previous and previous != english else ""
    return True, f"{key} is locked as “{english}”{was}.", True


def add_user_term(book_dir: str, chinese, english, term_type=None) -> tuple[bool, str]:
    """Add or override a term as the user's own; the model never changes it afterwards."""
    key = _clean_key(chinese)
    rendering = _clean_text(english, MAX_ENGLISH_LENGTH)
    if not key:
        return False, f"The Chinese term must be {MIN_KEY_LENGTH}–{MAX_KEY_LENGTH} characters long."
    if not rendering:
        return False, "The English rendering cannot be empty."
    return _edit_glossary(book_dir, lambda terms: _set_user_term(terms, key, rendering, term_type))


def _link_alias(terms: dict, alias: str, target: str) -> tuple[bool, str, bool]:
    index = _build_index(terms)
    key = index.get(target) or _find_by_english(terms, target)
    if key is None:
        return False, f"No term matches '{target}'. Add it first with 'term add'.", False
    owner = index.get(alias)
    if owner is not None:
        return False, f"{alias} already belongs to {owner} (“{_english_of(terms.get(owner))}”).", False
    record = terms[key]
    aliases = _aliases_of(record)
    if len(aliases) >= MAX_ALIASES:
        return False, f"{key} already has {MAX_ALIASES} aliases.", False
    record["aliases"] = aliases + [alias]
    return True, f"{alias} now renders as “{_english_of(record)}” (alias of {key}).", True


def add_user_alias(book_dir: str, alias, target) -> tuple[bool, str]:
    """Link another Chinese spelling to the term named by its Chinese or English."""
    alias_key = _clean_key(alias)
    target_text = _clean_text(target, MAX_ENGLISH_LENGTH)
    if not alias_key:
        return False, f"The alias must be {MIN_KEY_LENGTH}–{MAX_KEY_LENGTH} characters long."
    if not target_text:
        return False, "Name the term to link to, by its Chinese or its English."
    return _edit_glossary(book_dir, lambda terms: _link_alias(terms, alias_key, target_text))


def _remove_entry(terms: dict, key: str) -> tuple[bool, str, bool]:
    if key in terms:
        record = terms.pop(key)
        aliases = _aliases_of(record)
        extra = f" together with its aliases {', '.join(aliases)}" if aliases else ""
        return True, f"Removed {key} (“{_english_of(record)}”){extra}.", True
    owner = _build_index(terms).get(key)
    if owner is None:
        return False, f"{key} is not in the glossary.", False
    record = terms[owner]
    record["aliases"] = [a for a in _aliases_of(record) if a != key]
    return True, f"Removed alias {key} from {owner} (“{_english_of(record)}”).", True


def remove_term(book_dir: str, chinese) -> tuple[bool, str]:
    """Remove a term (with its aliases) or a single alias."""
    key = str(chinese or "").strip()
    if not key:
        return False, "Usage: term rm <chinese>"
    return _edit_glossary(book_dir, lambda terms: _remove_entry(terms, key))


def _row(key: str, record: dict) -> dict:
    return {
        "chinese": key,
        "english": _english_of(record),
        "type": _term_type(record.get("type")),
        "aliases": _aliases_of(record),
        "refersTo": _clean_text(record.get("refersTo"), MAX_ENGLISH_LENGTH),
        "source": _clean_text(record.get("source"), 20),
        "firstChapter": _first_chapter(record.get("firstChapter")),
    }


def _row_matches(row: dict, needle: str) -> bool:
    fields = [row["chinese"], row["english"], row["refersTo"], *row["aliases"]]
    return any(needle in text.casefold() for text in fields)


def search_terms(book_dir: str, query: str = "") -> tuple[list[dict], str | None]:
    """Glossary rows, optionally filtered by a case-insensitive substring. Returns (rows, error)."""
    if not book_dir:
        return [], "No book directory selected."
    with _file_lock:
        data, err = load_glossary(book_dir)
    if err:
        return [], err
    needle = str(query or "").strip().casefold()
    rows = []
    for key, record in data["terms"].items():
        if not isinstance(key, str) or not isinstance(record, dict):
            continue
        row = _row(key, record)
        if not needle or _row_matches(row, needle):
            rows.append(row)
    rows.sort(key=lambda r: (r["type"], r["english"].casefold()))
    return rows, None
