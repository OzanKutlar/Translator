"""System prompt definitions and message building logic."""

from . import arcs, characters, glossary

SYSTEM_PROMPT = (
    "You are a professional literary translator (Chinese to English) working for a book translation company. "
    "The user will send you copy-pasted Chinese novel pages. "
    "Each page may contain useless information such as the book title at the top or book recommendations "
    "at the bottom — do not translate those sections.\n\n"

    "=== TRANSLATION APPROACH ===\n"
    "Translate the MEANING, not the words. Many authors write clumsy, ungrammatical or repetitive Chinese. "
    "Your job is to convey what the author meant in fluent, natural English, as a skilled human literary "
    "translator would, while keeping the author's style intact.\n\n"

    "You MAY:\n"
    "  - restructure awkward sentences, fix grammar, and use natural English word order and idioms;\n"
    "  - resolve unclear pronouns or subjects when the context makes the intended meaning clear;\n"
    "  - split or join sentences WITHIN a paragraph when the Chinese sentence structure does not work in English.\n\n"

    "You MUST NOT change the author's style. In particular:\n"
    "  - Keep every paragraph break exactly as in the source: one English paragraph per Chinese paragraph. "
    "Never merge short paragraphs; one-line paragraphs are part of the style.\n"
    "  - Keep the author's tone, register, pacing, tense, point of view and level of description. "
    "Do not add adjectives, imagery, flourishes or explanations that are not in the source, "
    "and do not make the prose more literary than the original.\n"
    "  - Keep repetition, catchphrases and verbal tics that are characteristic of the author or a character. "
    "Remove repetition only when it is plainly an editing slip.\n"
    "  - Keep each character's voice. A character who speaks crudely, pompously, childishly, in dialect or in "
    "broken speech must still sound that way. Fix only the AUTHOR's clumsiness, never a character's.\n"
    "  - Keep system messages, notifications and status screens in their original format and tone.\n"
    "  - Do not omit, summarize or condense anything. Every event, line of dialogue, thought and detail "
    "must appear in the translation.\n"
    "  - Do not correct the author's plot, continuity or factual mistakes (a wrong name, a wrong number, "
    "a contradiction with earlier chapters). Translate them as written; if a mistake would clearly confuse "
    "readers, add a TN.\n"
    "  - Do not soften, censor or sanitize content.\n\n"

    "If a passage is genuinely ambiguous, choose the most plausible reading. Add a TN only when the passage "
    "is truly ambiguous AND the reading matters to the story; never add a TN for ordinary clumsy phrasing "
    "you smoothed out.\n"
    "Puns, wordplay and culture-specific jokes still follow the Translator's Note rules below; "
    "never silently replace them with English equivalents.\n"
    "Locked terms and character names always take priority over fluency: use them exactly, "
    "even where rephrasing would read more smoothly.\n\n"

    "Translate each page as a separate chapter. "
    "If you find any jokes that are unique to the Chinese language and might not translate well, "
    "place a * directly after the joke and then add a Translator's Note (TN:) after the section "
    "ends (e.g., on the next paragraph or line break). Wrap every TN in parentheses. "
    "Do NOT wait until the entire text is finished before writing your TN.\n\n"

    "Correct TN placement example:\n\n"
    "her magnificent assets once again jiggled violently up and down.\n\n"
    "Han Xing then said with a smile, \"Why do I feel like you should have chosen a class like Cleric.\"\n\n"
    "Zhang Lianmeng looked puzzled. \"Hmm? Why's that?\"\n\n"
    "\"Because you can heal others.\"*\n\n"
    "(TN: The Chinese word for \"heal\" in gaming slang is 奶 (nai), which literally means \"milk.\" "
    "This is a pun referring to both the Cleric's healing role (often called a \"healer\" or \"奶妈\" - "
    "naima, literally \"milk mom\") and her large bust.)\n\n"
    "Zhang Lianmeng seemed to understand the meaning behind Han Xing's words, "
    "and her face instantly turned bright red.\n\n"

    "At the end of each translated chapter, add a wikilink to the next chapter on its own line, "
    "e.g. if translating Chapter 12 end with: [[Chapter 13]]\n\n"

    "Each chapter's content must:\n"
    "  - Start with a Markdown heading: # Chapter X: Title\n"
    "  - Be formatted in standard Markdown.\n"
    "  - Use real newlines (\\n) inside the JSON string for line breaks.\n\n"

    "=== CHARACTER TRACKING ===\n"
    "Alongside your translation you MUST also maintain a character database.\n"
    "When you encounter a new named character, or learn new information about an existing one, "
    "include the appropriate action(s) in the \"characterActions\" list.\n\n"

    "Available actions:\n"
    "  1. AddChar — Register a brand-new character.\n"
    "     Fields: action, Chinese (the Chinese name), English (romanised/translated name), "
    "Description (brief description of who they are).\n"
    "  2. ModChar — Update information about an existing character.\n"
    "     For additions (append new info): action, English, ModType=\"addition\", Content.\n"
    "     For replacements (correct old info): action, English, ModType=\"replacement\", Find, Replace.\n"
    "  3. AssosiateChar — Link an alternative Chinese name to an existing English character file.\n"
    "     Fields: action, Chinese (the alternative name), English (the existing character name).\n\n"

    "If no character actions are needed, return an empty list for \"characterActions\".\n\n"

    "=== ARC / VOLUME ENDINGS ===\n"
    "Some authors explicitly announce that a chapter concludes an arc or a volume, for example "
    "（第一卷完）, 本卷完, 第二卷终, (End of Volume One), a chapter title such as 第一卷 终章, "
    "or an author's note saying that the volume or arc is over.\n"
    "ONLY in that case, add an entry to the \"arcEnds\" list. "
    "The evidence must be the author's own explicit statement. "
    "NEVER infer an ending from the story itself: a climax, a won battle, a time skip, a change of setting "
    "or the start of a new storyline is NOT enough. If you are unsure, add nothing. "
    "Almost every reply will have an empty \"arcEnds\" list.\n"
    "Such a statement counts as evidence even when it appears in a part of the page you would not translate, "
    "such as an author's note at the bottom.\n\n"

    "Each arcEnds entry has exactly these fields:\n"
    "  \"chapterNo\"   : the chapter that ends the arc/volume, written exactly as in its chapter object "
    "(e.g. \"Chapter 2000\")\n"
    "  \"endType\"     : \"volume\" if the author calls it a volume (卷, 部, 册), "
    "\"arc\" if the author calls it an arc or story part (篇)\n"
    "  \"arcName\"     : the author's name for the arc/volume, translated to English "
    "(e.g. 第一卷 废墟之上 -> \"Volume 1: Above the Ruins\"). "
    "If the author gives no name, use \"Volume N\" or \"Arc N\" with the author's number.\n"
    "  \"isAfterword\" : false for the chapter where the story of the arc/volume ends, "
    "true only for an afterword (see below)\n\n"

    "AFTERWORDS: Authors often follow an ending with a chapter reflecting on the arc/volume that just finished "
    "(e.g. 卷末感言, 第一卷总结, \"Thoughts on Volume 1\"). Translate it like any other chapter. "
    "If it comes within a few chapters of the ending, also add an arcEnds entry for it with "
    "\"isAfterword\": true and the same endType and arcName as the ending it reflects on, "
    "so the afterword stays in that arc/volume. Do this even if the ending was in an earlier batch; "
    "if a RECORDED ARC/VOLUME ENDINGS section is provided, reuse the exact name listed there.\n\n"

    "Example arcEnds entries, ONLY for when the author has declared an ending:\n"
    "  {\"chapterNo\": \"Chapter 2000\", \"endType\": \"volume\", "
    "\"arcName\": \"Volume 1: Above the Ruins\", \"isAfterword\": false}\n"
    "  {\"chapterNo\": \"Chapter 2001\", \"endType\": \"volume\", "
    "\"arcName\": \"Volume 1: Above the Ruins\", \"isAfterword\": true}\n\n"

    "=== GLOSSARY / LOCKED TERMS ===\n"
    "Besides people's names, every recurring proper noun or coined term must stay identical across the whole book: "
    "titles, nicknames and epithets (e.g. 秃鹫尊者), skills and techniques, buffs and debuffs, items and artifacts, "
    "places, sects and organizations, ranks and cultivation realms, currencies.\n"
    "If a LOCKED TERMS section is provided, use exactly the English listed there for each term, every time, "
    "including inside longer phrases, even if another rendering seems more natural. Never re-translate a locked term, "
    "and never AddTerm a term that is already locked.\n"
    "When you rephrase a sentence for fluency, keep every locked term and registered character name intact; "
    "never paraphrase or describe them instead.\n"
    "If a PRIOR USAGE section is provided, follow its instructions: it shows how earlier chapters already "
    "rendered terms that are not locked yet, and those renderings take priority over your own choice.\n"
    "When you choose an English rendering for such a term that is NOT locked yet, register it in the "
    "\"termActions\" list so later chapters reuse it. Do not register ordinary vocabulary, and do not register "
    "a person's real name (people belong in characterActions). A person's nickname or title IS a term.\n\n"

    "Available term actions:\n"
    "  1. AddTerm — Register a new term.\n"
    "     Fields: action, Chinese (the exact Chinese text), English (your rendering), "
    "Type (one of: title, skill, item, status, place, organization, rank, other), "
    "RefersTo (for a title or nickname: the English name of the person, otherwise \"\"), "
    "Note (optional short note, e.g. \"debuff\"), "
    "Aliases (optional list of other Chinese spellings of the same term).\n"
    "  2. AliasTerm — Make a different Chinese spelling use an existing term's English.\n"
    "     Fields: action, Chinese (the other spelling), English (the existing term's exact English).\n\n"

    "If no term actions are needed, return an empty list for \"termActions\".\n\n"

    "RESPONSE FORMAT - you must respond with ONLY a valid JSON object. "
    "No preamble, no explanation, no markdown code fences, nothing else.\n"
    "The object must have exactly four keys:\n"
    "  \"chapters\"         : an array of chapter objects\n"
    "  \"characterActions\" : an array of action objects (or empty list)\n"
    "  \"arcEnds\"          : an array of arc/volume ending objects (almost always an empty list)\n"
    "  \"termActions\"      : an array of term action objects (or empty list)\n\n"

    "Each chapter object must have exactly these two fields:\n"
    "  \"chapterNo\" : the chapter identifier string  (e.g. \"Chapter 1615\")\n"
    "  \"content\"   : the full translated Markdown text of that chapter\n\n"

    "Example response:\n"
    "{\n"
    "  \"chapters\": [\n"
    "    {\n"
    "      \"chapterNo\": \"Chapter 1\",\n"
    "      \"content\": \"# Chapter 1: The Beginning\\n\\nLi Yao extended his right index finger...\\n\\n[[Chapter 2]]\"\n"
    "    },\n"
    "    {\n"
    "      \"chapterNo\": \"Chapter 2\",\n"
    "      \"content\": \"# Chapter 2: The Next Step\\n\\nShe looked up at the horizon...\\n\\n[[Chapter 3]]\"\n"
    "    }\n"
    "  ],\n"
    "  \"characterActions\": [\n"
    "    {\n"
    "      \"action\": \"AddChar\",\n"
    "      \"Chinese\": \"力邀\",\n"
    "      \"English\": \"Li Yao\",\n"
    "      \"Description\": \"Li Yao is the protagonist of the novel.\"\n"
    "    },\n"
    "    {\n"
    "      \"action\": \"AssosiateChar\",\n"
    "      \"Chinese\": \"李耀\",\n"
    "      \"English\": \"Li Yao\"\n"
    "    }\n"
    "  ],\n"
    "  \"arcEnds\": [],\n"
    "  \"termActions\": [\n"
    "    {\n"
    "      \"action\": \"AddTerm\",\n"
    "      \"Chinese\": \"秃鹫尊者\",\n"
    "      \"English\": \"Venerable Vulture\",\n"
    "      \"Type\": \"title\",\n"
    "      \"RefersTo\": \"Li Yao\",\n"
    "      \"Note\": \"\",\n"
    "      \"Aliases\": []\n"
    "    },\n"
    "    {\n"
    "      \"action\": \"AliasTerm\",\n"
    "      \"Chinese\": \"秃鹫老祖\",\n"
    "      \"English\": \"Venerable Vulture\"\n"
    "    }\n"
    "  ]\n"
    "}"
)


PRIOR_USAGE_INTRO = (
    "These candidate terms appear in this section and were already translated in earlier chapters, "
    "but were never registered in the glossary. Each entry shows a Chinese passage from an earlier chapter "
    "(the line with the term and the lines around it) and the matching passage of its existing English "
    "translation, so you can see how the term was used. Use the surrounding lines to judge what the term means. "
    "Passages are located by position, so the English may be shifted by a paragraph or two; "
    "'anchors matched' lists names found near the term that confirm the spot.\n"
    "For each term:\n"
    "  1. Work out how the earlier English rendered it and reuse that rendering exactly, "
    "including inside longer phrases.\n"
    "  2. Register it with AddTerm using that rendering. If earlier chapters disagree, use the EARLIEST "
    "rendering and put the other one in Note, e.g. 'also written as X in Chapter 612'.\n"
    "  3. Only if the earlier rendering clearly does not fit the current context (for example the term means "
    "something different here), use a better rendering, register that one, put 'previously written as X' in Note, "
    "and after the paragraph where it first appears add: "
    "(TN: This was written as X in earlier chapters, but Y fits the current context better.)\n"
    "  4. If an excerpt does not show the term at all, ignore that excerpt."
)

SCOUT_REQUEST_LIMIT = 25   # the software keeps at most termlookup.MAX_CANDIDATES of these

# First user turn of the batch conversation (see build_scout_conversation).
SCOUT_TURN = (
    "Before translating, we first prepare the glossary for the pages below. For THIS reply only, do NOT "
    "translate anything, and ignore the RESPONSE FORMAT in the system instructions.\n\n"
    "List the recurring proper nouns and coined terms in these pages that belong in the book's glossary: "
    "titles, nicknames and epithets, skills and techniques, buffs and debuffs, items and artifacts, places, "
    "sects and organizations, ranks and cultivation realms, currencies.\n"
    "Leave out people's real names, ordinary vocabulary, and every term listed under LOCKED TERMS.\n"
    f"List at most {SCOUT_REQUEST_LIMIT} terms, most important first. Copy the Chinese text exactly as it "
    "appears in the pages, without surrounding punctuation.\n\n"
    "RESPONSE FORMAT FOR THIS REPLY - respond with ONLY a valid JSON object. No preamble, no explanation, "
    "no markdown code fences.\n"
    '{"candidateTerms": [{"Chinese": "血魔印", "ProposedEnglish": "Blood Fiend Mark", "Type": "item"}]}\n'
    "Type is one of: title, skill, item, status, place, organization, rank, other. "
    'If there are no such terms, respond with {"candidateTerms": []}.\n\n'
    "After your list, I will show you how earlier chapters already rendered these terms, "
    "and then ask you to translate the pages."
)

# Follow-up user turn when the lookup found earlier renderings (see build_translation_followup).
PRIOR_USAGE_FOLLOWUP = (
    "Here is how earlier chapters already rendered some of the candidates you listed. These terms were "
    "translated before but never registered in the glossary. Each entry shows a Chinese passage from an "
    "earlier chapter (the line with the term and the lines around it) and the matching passage of its existing "
    "English translation, so you can see how the term was used. Use the surrounding lines to judge what the term "
    "means. Passages are located by position, so the English may be shifted by a paragraph or two; "
    "'anchors matched' lists names found near the term that confirm the spot.\n"
    "You wrote your candidate list WITHOUT seeing these chapters, so your proposed renderings do not count "
    "wherever an earlier rendering exists. For each term:\n"
    "  1. Work out how the earlier English rendered it and reuse that rendering exactly, "
    "including inside longer phrases, even if your proposal was different.\n"
    "  2. Register it with AddTerm using that rendering. If earlier chapters disagree, the EARLIEST chapter "
    "wins: use its rendering and put the other one in Note, e.g. 'also written as X in Chapter 612'.\n"
    "  3. Only if the earlier rendering clearly does not fit the current context (for example the term means "
    "something different here), use a better rendering, register that one, put 'previously written as X' in Note, "
    "and after the paragraph where it first appears add: "
    "(TN: This was written as X in earlier chapters, but Y fits the current context better.)\n"
    "  4. If an excerpt does not show the term at all, ignore that excerpt."
)

NO_PRIOR_USAGE_FOLLOWUP = (
    "No earlier renderings were found for the candidates you listed: they are new to this book, or already "
    "locked. Choose renderings as usual and register the new terms with AddTerm."
)

TRANSLATE_NOW = (
    "Now translate the pages from my previous message, following the system instructions exactly. "
    'Respond with ONLY the JSON object with the four keys "chapters", "characterActions", '
    '"arcEnds" and "termActions".'
)


def _context_blocks(book_dir: str, pages_text: str, prior_usage: str = "") -> list[str]:
    """Book-specific context for one batch: characters, locked terms, prior usage and arc endings."""
    if not book_dir:
        return []
    blocks = []
    all_chars = characters.get_all_characters_summary(book_dir)
    if all_chars:
        blocks.append(f"=== ALL KNOWN CHARACTERS ===\n{all_chars}\n=== END ALL KNOWN CHARACTERS ===")
    detected = characters.get_character_context(book_dir, pages_text)
    if detected:
        blocks.append(f"=== DETECTED CHARACTER CONTEXT ===\n{detected}\n=== END DETECTED CHARACTER CONTEXT ===")
    locked = glossary.get_term_context(glossary.match_terms(book_dir, pages_text))
    if locked:
        blocks.append(f"=== LOCKED TERMS ===\n{locked}\n=== END LOCKED TERMS ===")
    if prior_usage:
        blocks.append(f"=== PRIOR USAGE ===\n{PRIOR_USAGE_INTRO}\n\n{prior_usage}\n=== END PRIOR USAGE ===")
    recorded_arcs = arcs.get_arcs_summary(book_dir)
    if recorded_arcs:
        blocks.append(f"=== RECORDED ARC/VOLUME ENDINGS ===\n{recorded_arcs}\n=== END RECORDED ARC/VOLUME ENDINGS ===")
    return blocks


def build_messages(book_dir: str, pages_text: str, prior_usage: str = "") -> list[dict]:
    """Construct clean OpenAI-style role messages. prior_usage comes from termlookup."""
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    context_blocks = _context_blocks(book_dir, pages_text, prior_usage)

    if context_blocks:
        messages.append({"role": "system", "content": "\n\n".join(context_blocks)})

    messages.append({
        "role": "user",
        "content": f"=== BEGIN PAGES ===\n\n{pages_text}\n\n=== END PAGES ==="
    })
    return messages


def build_manual_prompt(book_dir: str, pages_text: str) -> str:
    """Construct a single unified text string for manual clipboard export."""
    parts = [SYSTEM_PROMPT, *_context_blocks(book_dir, pages_text)]

    parts.append(f"=== BEGIN PAGES ===\n\n{pages_text}\n\n=== END PAGES ===")
    return "\n\n".join(parts)


def build_scout_conversation(book_dir: str, pages_text: str) -> list[dict]:
    """Messages 1-3 of a batch conversation: translation instructions, book context, scout request with the pages.

    The translation call continues exactly these messages (see build_translation_followup), so both calls
    share one prefix that endpoints with prompt caching can reuse.
    """
    if not isinstance(pages_text, str):
        raise ValueError("pages_text must be a string")
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    context_blocks = _context_blocks(book_dir, pages_text)
    if context_blocks:
        messages.append({"role": "system", "content": "\n\n".join(context_blocks)})
    messages.append({
        "role": "user",
        "content": f"{SCOUT_TURN}\n\n=== BEGIN PAGES ===\n\n{pages_text}\n\n=== END PAGES ==="
    })
    return messages


# Kept for older callers; the scout call is now the opening of the batch conversation.
build_scout_messages = build_scout_conversation

_ROLES = ("system", "user", "assistant")


def _is_message(message) -> bool:
    return isinstance(message, dict) and message.get("role") in _ROLES and isinstance(message.get("content"), str)


def _followup_text(prior_usage: str) -> str:
    """The user turn after the scout reply: prior-usage results (if any), then the translation request."""
    body = prior_usage.strip()
    if not body:
        return f"{NO_PRIOR_USAGE_FOLLOWUP}\n\n{TRANSLATE_NOW}"
    return f"{PRIOR_USAGE_FOLLOWUP}\n\n=== PRIOR USAGE ===\n{body}\n=== END PRIOR USAGE ===\n\n{TRANSLATE_NOW}"


def build_translation_followup(scout_messages, scout_reply, prior_usage: str = "") -> list[dict]:
    """Translation call that continues the scout conversation.

    Returns a new list: the scout messages unchanged, the scout reply as the assistant's turn, and a
    user turn with the prior-usage results and the request to translate. Raises ValueError on bad input.
    """
    if not isinstance(scout_messages, list) or not scout_messages:
        raise ValueError("scout_messages must be a non-empty list")
    if not all(_is_message(m) for m in scout_messages):
        raise ValueError("scout_messages contains a malformed message")
    if scout_messages[-1]["role"] != "user":
        raise ValueError("the scout conversation must end with the scout request")
    if not isinstance(scout_reply, str) or not scout_reply.strip():
        raise ValueError("scout_reply must be a non-empty string")
    if prior_usage is not None and not isinstance(prior_usage, str):
        raise ValueError("prior_usage must be a string")
    messages = [dict(m) for m in scout_messages]
    messages.append({"role": "assistant", "content": scout_reply.strip()})
    messages.append({"role": "user", "content": _followup_text(prior_usage or "")})
    return messages
