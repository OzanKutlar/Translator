"""Author-declared arc / volume endings, stored in <Book>/Arcs.json.

The file is shared with the bookpdf tool:

    {
      "version": 1,
      "bookName": "",
      "author": "",
      "arcs": [
        {"endChapter": 2000, "name": "Volume 1", "type": "volume", "source": "model"}
      ]
    }

Start chapters are not stored: each arc starts one chapter after the previous one ends.
When an afterword is folded into an arc, endChapter moves to the afterword and
storyEndChapter keeps the chapter where the story itself ended.
The translator never touches bookName/author and preserves keys it does not know.
"""

import json
import os
import re
import threading

from .parsing import parse_chapter_number

ARCS_FILENAME = "Arcs.json"
SCHEMA_VERSION = 1
AFTERWORD_WINDOW = 5         # an afterword may extend an ending by at most this many chapters
MAX_NAME_LENGTH = 200
MAX_ENTRIES_PER_REPLY = 20   # more endings than this in one reply is certainly a model error
SUMMARY_ARC_LIMIT = 10       # recorded endings shown to the model as context

_END_TYPE_ALIASES = {
    "arc": "arc", "arcend": "arc", "saga": "arc", "part": "arc",
    "volume": "volume", "volumeend": "volume", "vol": "volume", "book": "volume",
}

# Workers can record endings concurrently, and on Windows a file that is open cannot be
# replaced, so every read and write of Arcs.json goes through this lock.
_file_lock = threading.Lock()


# ----------------------------------------------------------------------
# File access
# ----------------------------------------------------------------------

def arcs_path(book_dir: str) -> str:
    return os.path.join(book_dir, ARCS_FILENAME)


def _empty_data() -> dict:
    return {"version": SCHEMA_VERSION, "bookName": "", "author": "", "arcs": []}


def load_arcs(book_dir: str) -> tuple[dict | None, str | None]:
    """Return (data, None), or (None, error) when Arcs.json exists but is unusable.

    A missing file is not an error; it yields an empty structure.
    """
    path = arcs_path(book_dir)
    if not os.path.exists(path):
        return _empty_data(), None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        return None, f"could not read {ARCS_FILENAME}: {e}"
    if not isinstance(data, dict) or not isinstance(data.get("arcs", []), list):
        return None, f"{ARCS_FILENAME} does not have the expected structure"
    for key, default in _empty_data().items():
        data.setdefault(key, default)
    return data, None


def save_arcs(book_dir: str, data: dict) -> None:
    """Write Arcs.json via a temp file and rename, so a crash never leaves half a file."""
    path = arcs_path(book_dir)
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


# ----------------------------------------------------------------------
# Validation of model entries
# ----------------------------------------------------------------------

def _chapter_int(value) -> int | None:
    """Chapter number from 2000, "2000" or "Chapter 2000"; None when unreadable."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    text = str(value or "").strip()
    number = int(text) if text.isdecimal() else parse_chapter_number(text)
    return number if number and number > 0 else None


def chapter_numbers(chapters: list) -> set[int]:
    """Chapter numbers of the chapter objects in a reply."""
    if not isinstance(chapters, list):
        return set()
    numbers = set()
    for entry in chapters:
        number = _chapter_int(entry.get("chapterNo")) if isinstance(entry, dict) else None
        if number is not None:
            numbers.add(number)
    return numbers


def _end_type(value) -> str | None:
    key = re.sub(r"[^a-z]", "", str(value or "").lower())
    return _END_TYPE_ALIASES.get(key)


def _clean_name(value) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()[:MAX_NAME_LENGTH]


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "yes", "1")


def normalise_arc_end(entry) -> tuple[dict | None, str | None]:
    """Validate one model entry.

    Returns ({"chapter", "endType", "name", "isAfterword"}, None) or (None, reason).
    """
    if not isinstance(entry, dict):
        return None, "entry is not an object"
    chapter = _chapter_int(entry.get("chapterNo"))
    if chapter is None:
        return None, f"unreadable chapterNo {entry.get('chapterNo')!r}"
    end_type = _end_type(entry.get("endType"))
    if end_type is None:
        return None, f"Chapter {chapter}: unknown endType {entry.get('endType')!r}"
    return {
        "chapter": chapter,
        "endType": end_type,
        "name": _clean_name(entry.get("arcName")),
        "isAfterword": _truthy(entry.get("isAfterword")),
    }, None


def _validated_items(entries, valid_chapters: set, summary: dict) -> list[dict]:
    """Keep entries that are well-formed and refer to a chapter returned in the same reply."""
    if not isinstance(entries, list):
        summary["skipped"].append("arcEnds is not a list")
        return []
    if len(entries) > MAX_ENTRIES_PER_REPLY:
        summary["skipped"].append(f"{len(entries)} arcEnds entries in one reply; all ignored as implausible")
        return []
    items = []
    for entry in entries:
        item, reason = normalise_arc_end(entry)
        if item is None:
            summary["skipped"].append(reason)
        elif item["chapter"] not in valid_chapters:
            summary["skipped"].append(f"Chapter {item['chapter']} is not one of the chapters in this reply")
        else:
            items.append(item)
    # Story endings first, so an afterword in the same reply can attach to its ending.
    items.sort(key=lambda it: (it["isAfterword"], it["chapter"]))
    return items


# ----------------------------------------------------------------------
# Merging into the arc list
# ----------------------------------------------------------------------

def _end_of(arc) -> int | None:
    if not isinstance(arc, dict):
        return None
    value = arc.get("endChapter")
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _sort_key(arc) -> tuple[bool, int]:
    end = _end_of(arc)
    return end is None, end or 0


def _find_existing(arc_list: list, chapter: int) -> dict | None:
    """Arc ending at this chapter, or whose story ended here before an afterword was attached."""
    for arc in arc_list:
        if _end_of(arc) == chapter or (isinstance(arc, dict) and arc.get("storyEndChapter") == chapter):
            return arc
    return None


def _find_afterword_target(arc_list: list, chapter: int) -> dict | None:
    """Latest arc ending within AFTERWORD_WINDOW chapters before an afterword chapter."""
    best, best_end = None, 0
    for arc in arc_list:
        end = _end_of(arc)
        if end is not None and chapter - AFTERWORD_WINDOW <= end < chapter and end > best_end:
            best, best_end = arc, end
    return best


def _default_name(end_type: str, arc_list: list, chapter: int) -> str:
    earlier = sum(1 for arc in arc_list if (_end_of(arc) or 0) < chapter)
    return f"{end_type.title()} {earlier + 1}"


def _apply_ending(arc_list: list, item: dict, summary: dict) -> bool:
    """Record a story ending. Returns True when the list changed."""
    chapter = item["chapter"]
    existing = _find_existing(arc_list, chapter)
    if existing is not None:
        summary["unchanged"].append({"chapter": chapter, "name": str(existing.get("name") or "")})
        return False
    name = item["name"] or _default_name(item["endType"], arc_list, chapter)
    arc_list.append({"endChapter": chapter, "name": name, "type": item["endType"], "source": "model"})
    summary["recorded"].append({"chapter": chapter, "name": name, "type": item["endType"]})
    return True


def _apply_afterword(arc_list: list, item: dict, summary: dict) -> bool:
    """Fold an afterword into the arc it follows. Returns True when the list changed."""
    chapter = item["chapter"]
    if _find_existing(arc_list, chapter) is not None:
        return _apply_ending(arc_list, item, summary)  # already recorded -> logged as unchanged
    target = _find_afterword_target(arc_list, chapter)
    if target is None:
        # The ending itself was never recorded (e.g. translated before arc tracking existed).
        return _apply_ending(arc_list, item, summary)
    name = str(target.get("name") or "")
    if target.get("source") != "model":
        summary["skipped"].append(f"afterword Chapter {chapter} follows your own arc '{name}'; left unchanged")
        return False
    previous_end = _end_of(target)
    target.setdefault("storyEndChapter", previous_end)
    target["endChapter"] = chapter
    summary["extended"].append({"name": name, "from": previous_end, "to": chapter})
    return True


def _new_summary() -> dict:
    """Events per outcome: recorded/extended/unchanged hold dicts, skipped/failed hold reasons."""
    return {"recorded": [], "extended": [], "unchanged": [], "skipped": [], "failed": []}


def _merge_into_file(book_dir: str, items: list[dict], summary: dict) -> None:
    data, err = load_arcs(book_dir)
    if err:
        summary["failed"].append(f"{err} (nothing was written; fix or remove the file)")
        return
    arc_list = data["arcs"]
    changed = False
    for item in items:
        apply = _apply_afterword if item["isAfterword"] else _apply_ending
        changed = apply(arc_list, item, summary) or changed
    if not changed:
        return
    arc_list.sort(key=_sort_key)
    try:
        save_arcs(book_dir, data)
    except OSError as e:
        summary["recorded"].clear()
        summary["extended"].clear()
        summary["failed"].append(f"could not save {ARCS_FILENAME}: {e}")


def record_arc_ends(book_dir: str, entries, valid_chapters: set) -> dict:
    """Merge model-reported endings into Arcs.json and return a summary (see _new_summary).

    Entries are only accepted for chapters returned in the same reply, so a hallucinated
    chapter number can never reach the file. Re-reporting an ending is a no-op.
    """
    summary = _new_summary()
    if not book_dir:
        summary["failed"].append("no book directory selected")
        return summary
    items = _validated_items(entries, valid_chapters, summary)
    if not items:
        return summary
    with _file_lock:
        _merge_into_file(book_dir, items, summary)
    return summary


def get_arcs_summary(book_dir: str) -> str:
    """Recorded endings as model context.

    Returns "" when there are none. An unreadable file also yields "" here; the same
    problem is reported loudly the next time an ending is recorded.
    """
    if not book_dir:
        return ""
    with _file_lock:
        data, err = load_arcs(book_dir)
    if err or not data["arcs"]:
        return ""
    lines = []
    for arc in data["arcs"][-SUMMARY_ARC_LIMIT:]:
        end = _end_of(arc)
        if end is not None:
            lines.append(f"- Chapter {end}: {arc.get('name', '')} ({arc.get('type', 'arc')})")
    if not lines:
        return ""
    return (
        "These arc/volume endings are already recorded. Reuse these exact names "
        "(for example for an afterword), and never invent endings to match them:\n"
        + "\n".join(lines)
    )
