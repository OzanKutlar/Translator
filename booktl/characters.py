"""Character management and context tracking."""

import json
import os


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

    for chinese_name, md_filename in char_map.items():
        if chinese_name in chinese_text and md_filename not in seen_files:
            seen_files.add(md_filename)
            md_path = os.path.join(chars_dir, md_filename)
            if os.path.exists(md_path):
                try:
                    with open(md_path, "r", encoding="utf-8") as f:
                        content = f.read().strip()
                    char_label = os.path.splitext(md_filename)[0]
                    context_parts.append(f"### {char_label}\n{content}")
                except IOError:
                    pass

    if not context_parts:
        return ""
    return (
        "The following characters appear in this section. "
        "Use this context to ensure consistent names and details:\n\n"
        + "\n\n".join(context_parts)
    )


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


def process_character_actions(book_dir: str, actions: list) -> list[str]:
    """Process structured character action mutations produced by the LLM."""
    char_map = load_characters(book_dir)
    chars_dir = os.path.join(book_dir, "Characters")
    os.makedirs(chars_dir, exist_ok=True)
    results = []

    for act in actions:
        if not isinstance(act, dict):
            continue
        action = act.get("action", "")

        # AddChar
        if action == "AddChar":
            chinese = act.get("Chinese", "").strip()
            english = act.get("English", "").strip()
            description = act.get("Description", "").strip()
            if not chinese or not english:
                results.append("[yellow]⚠ AddChar skipped — missing Chinese or English name[/yellow]")
                continue

            md_filename = f"{english}.md"
            md_path = os.path.join(chars_dir, md_filename)

            if not os.path.exists(md_path):
                try:
                    with open(md_path, "w", encoding="utf-8") as f:
                        f.write(f"# {english}\n\n{description}\n")
                    results.append(f"[bold green]➕ AddChar: Created '{md_filename}' ({chinese})[/bold green]")
                except IOError as e:
                    results.append(f"[bold red]✘ AddChar failed for '{english}': {e}[/bold red]")
                    continue
            else:
                results.append(f"[cyan]ℹ AddChar: '{md_filename}' already exists — mapping updated[/cyan]")

            if chinese not in char_map:
                char_map[chinese] = md_filename

        # ModChar
        elif action == "ModChar":
            english = act.get("English", "").strip()
            mod_type = act.get("ModType", "").strip().lower()
            if not english:
                results.append("[yellow]⚠ ModChar skipped — missing English name[/yellow]")
                continue

            md_filename = f"{english}.md"
            md_path = os.path.join(chars_dir, md_filename)

            if not os.path.exists(md_path):
                results.append(f"[yellow]⚠ ModChar skipped — '{md_filename}' does not exist[/yellow]")
                continue

            try:
                with open(md_path, "r", encoding="utf-8") as f:
                    current_content = f.read()

                if mod_type == "addition":
                    new_content = act.get("Content", "").strip()
                    if new_content:
                        current_content = current_content.rstrip() + "\n\n" + new_content + "\n"
                        results.append(f"[bold green]📝 ModChar: Appended to '{md_filename}'[/bold green]")
                    else:
                        results.append(f"[yellow]⚠ ModChar addition — empty content[/yellow]")
                        continue
                elif mod_type == "replacement":
                    find_text = act.get("Find", "")
                    replace_text = act.get("Replace", "")
                    if find_text and find_text in current_content:
                        current_content = current_content.replace(find_text, replace_text, 1)
                        results.append(f"[bold green]📝 ModChar: Replaced text in '{md_filename}'[/bold green]")
                    else:
                        results.append(f"[yellow]⚠ ModChar replacement target not found in '{md_filename}'[/yellow]")
                        continue
                else:
                    results.append(f"[yellow]⚠ ModChar skipped — unknown ModType '{mod_type}'[/yellow]")
                    continue

                with open(md_path, "w", encoding="utf-8") as f:
                    f.write(current_content)
            except IOError as e:
                results.append(f"[bold red]✘ ModChar failed for '{english}': {e}[/bold red]")

        # AssosiateChar
        elif action == "AssosiateChar":
            chinese = act.get("Chinese", "").strip()
            english = act.get("English", "").strip()
            if not chinese or not english:
                results.append("[yellow]⚠ AssosiateChar skipped — missing Chinese or English name[/yellow]")
                continue

            md_filename = f"{english}.md"
            md_path = os.path.join(chars_dir, md_filename)
            if not os.path.exists(md_path):
                results.append(f"[yellow]⚠ AssosiateChar skipped — '{md_filename}' does not exist[/yellow]")
                continue

            if chinese not in char_map:
                char_map[chinese] = md_filename
                results.append(f"[bold green]🔗 AssosiateChar: '{chinese}' → '{md_filename}'[/bold green]")
            else:
                results.append(f"[cyan]ℹ AssosiateChar: '{chinese}' already mapped[/cyan]")
        else:
            results.append(f"[yellow]⚠ Unknown character action '{action}' — skipped[/yellow]")

    save_characters(book_dir, char_map)
    return results
