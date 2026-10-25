# Book Translator CLI

A streamlined CLI translation tool designed to accept raw book pages from browser scrapers (via Tampermonkey) and translate them using any OpenAI v1 compatible API endpoint.

## Installation

```bash
pip install -r requirements.txt
```

## Quick Start

1. Ensure your OpenAI-compatible endpoint is running (e.g. `http://localhost:34501/v1`).
2. Run the translation CLI:

```bash
python translate.py
```

3. Pick your target book from `../TranslateBook/`, or choose **＋ New book…** to create one, and your model from the live model list. Both pickers use the arrow keys: ↑/↓ to move, Enter to select, Esc to cancel. When input isn't a terminal (e.g. piped), a numbered list is shown instead; type `n` there for a new book. See [Choosing or Creating a Book](#choosing-or-creating-a-book).
4. Install `bookContent.js` in Tampermonkey and navigate to your book page on 69shuba.

## Subcommands

- `python translate.py run` (default) — Starts the ingest server and interactive REPL.
- `python translate.py models` — Prints the models available on your configured endpoint.
- `python translate.py books` — Lists books in your library, most recently translated first.
- `python translate.py check` — Tests endpoint connectivity and runs a lightweight ping completion.
- `python translate.py retry -b <BookName>` — Re-queues every Failed chapter (and any older `_failed/` batches), waits for them, then exits.
- `python translate.py prompt -b <BookName>` — Inspects the generated translation prompt.

## Choosing or Creating a Book

The book picker lists the books you translated recently first, newest at the top, with a hint such as `translated 2h ago · ch 1240`. Pressing Enter on the first row therefore opens the book you worked on last. Books without a recorded translation follow in alphabetical order.

- The last row, **＋ New book…**, asks for a name and creates the folder, with `Characters/Characters.json`, inside the library. In the numbered fallback, type `n`.
- An empty or missing library goes straight to the new-book prompt.
- Names may use any characters, including Chinese, except `< > : " / \ | ? *`. They cannot start or end with `.`, cannot be a reserved Windows name such as `CON` or `NUL`, and cannot match an existing book (ignoring case).
- In the console, `book` reopens the picker and `book new <name>` creates a book and switches to it.

Each time a batch saves chapters, the time and the highest chapter in that batch are written to `<Book>/_activity.json`. Books from before this file existed use the modification time of their newest chapter `.md` file instead. To count only a time window as recent, set `RECENT_WINDOW_DAYS` in `booktl/activity.py` (e.g. `30`); older books then sort alphabetically.

## Reopening a Book

The scraper sends the address of every page along with its content, and the latest one is saved as the book's **reopen address** in `<Book>/_resume.json`. When you open the book again, at startup or with `book`, the tool asks:

    ↪ Reopen Chapter 1240 at https://www.69shuba.com/txt/12345/67890? [Y/n]:

- Press Enter (yes) to open the page in your default browser. At startup it opens once the ingest server is ready.
- Type `n` to skip. Non-interactive runs never open a browser.
- The first page that arrives from the reopen address is ignored, because it was already scraped last session, and the scraper moves on to the next chapter. This also works when you open the page yourself.
- Only that first request is ignored; refreshing the page later queues it normally.
- If the ignored chapter has no translated file yet (for example, it was still pending when you quit), the log says so. Run `retry <n>` to translate it from its stored raw page.
- Pages taken with `accept` have no address and never change the reopen address.

This needs `bookContent.js` 2.4 or later.

## How Pages Flow

Every scraped page goes into one **pending pool**. Batches are not formed in advance: a worker cuts a batch from the front of the pool only when it is free to send one, using the batch size set at that moment. Running `batch 5` therefore applies to the very next batch.

- **With a goal**, the scraper keeps going until the goal chapter, then stops. The remaining pending pages are sent, including a final partial batch.
- **Without a goal**, the scraper is told to wait once **4 × batch size** pages are pending. Pages already in flight don't count toward that limit. Scraping resumes automatically as the pool shrinks.

Partial batches are only sent in *drain mode*, which turns on when the goal is reached, or when you run `flush`, `retry`, or shut down. It turns off by itself once the pool is empty.

### Missing and Failed Chapters

After each reply, the chapters that were sent are compared with the chapters that came back.

- **Missing**: a chapter that was sent but not returned. It is logged under the batch, e.g. `⚠ Missing Chapters 123, 125: left out of the reply (not re-queued; use 'retry missing')`.
- **Failed**: every chapter in a batch that hit an API or JSON error. The batch is also quarantined to `_failed/`.

Neither list is re-queued automatically. Use `missing` to see them, and `retry` to send them again. A chapter leaves both lists once it is translated successfully or re-queued. The lists are stored in `<Book>/_queue_state.json`, so they survive restarts.

Pages with no readable chapter number are keyed as `page:<id>`. They can only be checked for missing by count, and `retry page:<id>` works only during the session that scraped them.

## Interactive Console Commands

Everything typed into the console is a command, so no `/` prefix is needed (a leading `/` is still accepted). Press Tab to complete command names, and type `help` for the full list:
- `book` — Reopen the book picker (recent books first, **＋ New book…** last).
- `book new <name>` — Create a new book in the library and switch to it.
- `goal <n|+n|none>` — Set the stop chapter (e.g. `goal 3000`, or `goal +50` for the next 50 chapters).
- `progress` — Render current scraping and translation progress bars plus the queue line.
- `status` — View pipeline status, pending/in-flight pages, missing/failed counts, goal progress, and metrics.
- `model [id]` — Switch model directly or reopen the arrow-key model picker.
- `endpoint <url>` — Change the target API base URL.
- `batch <n>` — Adjust batch page size (default: 10). Applies to the next batch sent.
- `concurrency <n>` — Adjust active worker threads.
- `pending [all]` — Show pending chapters and in-flight batches (aliases: `buffer`, `queue`).
- `missing` — Table of Missing and Failed chapters with batch and reason.
- `retry [chapters]` — Re-queue chapters at the front of the pool and send them now. Ranges include both ends:
  - `retry 112-115` — Chapters 112, 113, 114 and 115.
  - `retry 120 130`, `retry ch120` — Individual chapters.
  - `retry missing`, `retry failed`, `retry all` — Everything on the Missing list, the Failed list, or both.
  - `retry` — With no argument, re-queues all Failed chapters.
  - Aliases: `retranslate`, `rt`.
- `recover [#batch|chapters]` — With no argument, lists spooled batches. `recover #8` loads spooled batch 8. `recover 112-115` puts those chapters at the front of the pool *without* forcing a send, for `manual` or `flush`.
- `manual` — Copy the next batch of pending pages to clipboard for external translation.
- `paste` — Ingest translated JSON from clipboard (missing chapters are detected here too).
- `accept [force] [n]` — Take the text on the clipboard as a raw chapter page and queue it (aliases: `grab`, `clip`). See [Manual Chapter Accept](#manual-chapter-accept-clipboard).
- `accept <on|off|status>` — Watch the clipboard and queue every newly copied chapter page.
- `cancel` — Cancel manual mode or in-flight batches; their pages return to the front of the pool. A cancelled batch leaves the in-flight count at once. Its request is abandoned, with no further retries, and the worker is free for the next batch. A request the endpoint already received may still finish on the endpoint's side; its reply is ignored.
- `terms [search]` — Table of glossary terms, optionally filtered (alias: `glossary`).
- `term add <chinese> = <english> [type:<type>]` — Add or override a term and lock it as yours, e.g. `term add 秃鹫尊者 = Venerable Vulture type:title`.
- `term alias <chinese> = <term>` — Make another Chinese spelling render like an existing term (named by its Chinese or English).
- `term rm <chinese>` — Remove a term together with its aliases, or a single alias.
- `terms audit <chinese>` — Every translated chapter whose raw page contains the term, with the surrounding Chinese lines and the matching English passage.
- `lookup <on|off>` — Turn the prior-usage term lookup on or off (see below).
- `flush` — Send all pending pages now, including a partial batch.
- `auto <on|off>` — Pause or resume automatic sending.
- `quit` — Gracefully drain and shut down workers.

`retry` and `recover` find source pages in this order: in-flight batches, `_raw/chapters/`, older `_raw/batch_XXXX_pages.json` spools (newest first), then `_failed/` records. Chapters with no source are reported, not silently skipped.

### Manual Chapter Accept (Clipboard)

Pages don't have to come from the scraper. Copy a raw chapter page from any site or reader app (including the `第…章` title line if you can) and run `accept`. The clipboard text is queued exactly like a scraped page: it gets the same chapter detection, is stored in `_raw/chapters/` so `retry` works later, counts toward the goal, and is batched and sent with everything else in the pending pool.

- `accept` — Take what is on the clipboard now.
- `accept 120` / `accept ch120` — Same, but use Chapter 120 whatever the text says. Use it when the copy has no title line. Running it while the same text is still copied replaces the copy that was just queued under the wrong number.
- `accept force [n]` — Skip the Chinese-text check below.
- `accept on` — Watch the clipboard: every newly copied text is checked and queued, so you can copy chapter after chapter without typing. Whatever is on the clipboard when you turn it on is skipped. `accept off` stops watching; `accept status` and `status` show whether it is on.
- Aliases: `grab`, `clip`.

Copied text is accepted only when it:

- has at least 50 Chinese characters (unless `force`) and at most 200,000 characters;
- is not a translation prompt. The text `manual` copies is always skipped, so watch mode and `manual` work together;
- is not JSON. Copy a model reply and use `paste` as usual;
- is not the same text that was accepted just before.

In watch mode rejected copies only produce a dim log line, since most things you copy aren't chapters. Copied pages are accepted even when the pool is above the scraper's backlog limit. A page without a readable chapter number is queued as `Chapter ~N` (after the last chapter) or `Chapter ?` with a warning. Watch mode stops itself after 5 failed clipboard reads in a row. On Linux, `pyperclip` needs `xclip`, `xsel` or `wl-clipboard`.

### Goal progress

Progress bars count from the first chapter the scraper sends after the program starts, and both ends are included. For example, if the session starts at chapter 10 and you run `goal 30`, scraping chapters 10–20 shows `11/21` on the Scraped bar. Each chapter is counted once, even when it is retried or re-scraped.

`goal +50` set before the first page arrives covers 50 chapters starting with the first one. Set later, it means the 50 chapters after the current one.

### Logs

Each batch prints a few short lines: `→` marks a request going out, `←` the term query's answer. Everything about the reply is then printed as one block, a headline plus indented details, so batches finishing at the same time never interleave:

    → Batch #78: Term query sent (15 pages · Chapters 846–860)
    ← Batch #78: Term query returned 15 new term(s), 3 used before (梦魇圣车 ← Ch 844, …)
    → Batch #78: Translation sent to Gemini Studio
    ✔ Batch #78: Saved 15 chapters (Chapters 846–860) to 'SuperBase Evolution' in 84s · 23,410 tokens
       Added 1 character
       Added 21 terms (…) · reused 3 earlier renderings: 梦魇圣车 → Nightmare holy carriage (Ch 844), …
       ⚠ Term drift: “Seraphim” (炽天使) missing from Chapters 847–849 · “Earth Law” (地法) missing from Chapter 851

Only problems get a ⚠, and they are grouped: one Term drift line per batch (up to 3 terms, each with its chapters), one line for reused renderings missing from their first chapter, one for kept locked terms, and one for Missing chapters. API retries print one short line such as `Batch #7: Term query attempt 1/3 failed: HTTP 503 · The model is overloaded. · retrying in 1s`; the full error body is kept in the `_failed/` record. `paste` prints the same block under `Manual paste`.

### Files Written per Book

- `_raw/chapters/ch_000120.txt` — Latest raw page for each scraped or accepted chapter (source for `retry`).
- `_raw/batch_XXXX_pages.json` — Pages of every batch sent, written before the API call.
- `_raw/unsent_*.json` — Pending pages that were never sent when the program exited.
- `_failed/` — Quarantined failed batches with the model reply and error.
- `_queue_state.json` — Missing and Failed chapter lists.
- `Glossary.json` — Locked term renderings (see below).
- `_activity.json` — When the book was last translated and the highest chapter in that batch; orders the book picker.
- `_resume.json` — The reopen address: the URL and chapter of the last page the scraper sent.

### Translation Approach

The model translates the author's meaning rather than word for word, so clumsy, ungrammatical or repetitive Chinese comes out as fluent English. It may restructure awkward sentences, fix grammar, resolve unclear subjects, and split or join sentences within a paragraph.

The author's style is kept:

- Paragraph breaks stay 1:1 with the source; short paragraphs are never merged.
- Tone, register, pacing, tense, point of view and level of description stay the same, with no added flourishes or explanations.
- Characteristic repetition, catchphrases and every character's voice are preserved. Only the author's clumsiness is fixed, never a character's.
- Nothing is omitted or summarized, and the author's plot or continuity mistakes are translated as written (with a TN if they would confuse readers).
- Puns still get a `*` and a TN, and locked terms and names always take priority over fluency.

The 1:1 paragraph rule also keeps the term-drift check and the prior-usage excerpts accurate, because both rely on the English text lining up with the raw page. Edit the `TRANSLATION APPROACH` section of `SYSTEM_PROMPT` in `booktl/prompts.py` to adjust it.

### Glossary / Locked Terms

Character files keep people's names consistent. Everything else that must stay the same across the book lives in `<Book>/Glossary.json`: titles and nicknames (秃鹫尊者 → "Venerable Vulture"), skills, buffs and debuffs, items, places, organizations and ranks.

    {
      "version": 1,
      "terms": {
        "小丑印记": { "en": "Joker's Mark", "type": "status", "aliases": ["小丑标记"], "refersTo": "", "note": "debuff", "source": "user", "firstChapter": 1040 }
      }
    }

- Before each batch, every term whose Chinese text or one of its `aliases` appears in the pages is sent to the model as **LOCKED TERMS**, which it must use exactly. Longer spellings are matched first, so 小丑印记 wins over 小丑.
- The model registers new terms with `AddTerm` and links other spellings with `AliasTerm`. If it adds a new spelling with an English rendering that already exists, the spelling becomes an alias of that term.
- The model can never change an existing term. A different rendering is logged under the batch as `⚠ Kept locked terms: 小丑印记 stays “Joker's Mark” (model suggested …)`.
- Terms you add with `term add` are marked `"source": "user"`. The model never edits them, not even to add an alias; it logs a hint instead.
- After each reply, every chapter whose raw page contains a locked term is checked for that term's English. If it is missing, the batch's single `⚠ Term drift` line lists the term with the chapters it is missing from, e.g. `“Joker's Mark” (小丑印记) missing from Chapters 831–845`; use `retry <chapter>` if the translation really drifted. A term reported in every chapter usually means the locked English is not what the model writes; check it with `terms audit <chinese>`. Text the model skips (site headers, recommendations) can occasionally cause a false warning.
- Chinese keys and aliases must be 2–40 characters; one-character keys would match almost every page.

### Prior-Usage Lookup

A term the model never registered may still have been translated before, e.g. 血魔印 as "Blood Demon Seal" in Chapter 300. To stop Chapter 900 from coining a second rendering, each batch makes two calls while the lookup is on:

1. **Term scout.** The batch opens a conversation with the model: the full translation instructions and book context, then a user turn asking it to list the glossary terms it plans to register for these pages (`candidateTerms`). Nothing is translated yet.
2. **Lookup.** Candidates that are already locked, known characters, or not in the pages are dropped, and at most 15 are kept. Each one is searched in `_raw/chapters/` among chapters that already have a translated `.md` file; the current batch is skipped. For up to 3 hits (earliest, most recent and one in between) an excerpt is cut out with surrounding context: the Chinese line with the term plus up to 3 lines on each side (at most 700 characters), and the matching English paragraph plus up to 4 paragraphs on each side (at most 1,600 characters). Adjust `ZH_CONTEXT_LINES`, `ZH_EXCERPT_CHARS`, `EN_CONTEXT_PARAGRAPHS`, `EXCERPT_CHARS` and `CONTEXT_BUDGET` in `booktl/termlookup.py` to change this. The passage is placed by position in the chapter, then moved onto the paragraphs that mention the names and locked terms found next to the hit ("anchors").
3. **Translation.** The translation call continues the same conversation. The scout's reply goes back in as the model's own turn, followed by a user turn with the **PRIOR USAGE** results and the request to translate the pages. The model therefore sees both what it proposed and what earlier chapters actually wrote, and is told that its proposals were made without seeing those chapters: wherever an earlier rendering exists, it replaces the proposal. The model reuses it and registers it with `AddTerm`; `firstChapter` becomes the earliest chapter found, and the batch's terms line shows `reused 1 earlier rendering: 血魔印 → Blood Demon Seal (Ch 300)`.

- If earlier chapters disagree, the earliest rendering wins and the other one is put in the term's note.
- If the model decides the old rendering does not fit the current context, it uses a new one, notes `previously written as X`, and adds `(TN: This was written as X in earlier chapters, but Y fits the current context better.)` to the chapter.
- After a term is registered this way, the translation of its `firstChapter` is checked for the registered English. If it is not there, the batch warns, e.g. `⚠ Reused but not in its first chapter: “Fire Seed” (火种, Ch 1)`. Check with `terms audit <chinese>` and fix it with `term add`. A deliberate change (the TN case above) triggers the same warning and can be ignored.
- If the term query fails or its reply is not usable JSON, it is re-sent once (the normal HTTP retries still apply inside each attempt). If that fails too, the batch is not translated: it is quarantined to `_failed/` and its chapters are listed as Failed, so `retry failed` sends them again later. If the local search itself fails, the batch is marked Failed at once, because re-sending cannot fix it. With `lookup off`, batches go out as a single translation call.
- Both calls start with the same messages: the scout call carries the full system prompt and book context, and the translation call repeats them plus the two scout turns. The pages are therefore sent twice per batch, and the scout call is a few thousand tokens larger than a bare term list would need. Endpoints with prompt caching (for example Gemini's implicit caching) can bill the repeated prefix at the cached rate. Turn the lookup off with `lookup off`, `--no-term-lookup` or `TL_TERM_LOOKUP=off`.
- `python translate.py prompt -b <BookName>` shows the single-call messages, the scout conversation, and a sample of the follow-up turn.
- Only chapters with a stored raw page in `_raw/chapters/` are searched. `manual` / `paste` and `--dry-run` skip the lookup.
- Site text is ignored: everything above the `第…章` title line, everything from `(本章完)` on, and lines with site names or URLs. Extend `END_MARKERS` / `BOILERPLATE` in `booktl/termlookup.py` for other sites.
- `terms audit <chinese>` uses the same search, so you can check a rendering before `term add`.

### Arc / Volume Tracking

When the author explicitly marks a chapter as the end of an arc or volume (for example `（第一卷完）` or an author's note saying the volume is over), the model reports it in an `arcEnds` list and the translator records it in `<Book>/Arcs.json`. The model is told never to guess endings from the plot, so a book only gains an entry when the author actually declares one.

    {
      "version": 1,
      "bookName": "",
      "author": "",
      "arcs": [
        { "endChapter": 2001, "storyEndChapter": 2000, "name": "Volume 1: Above the Ruins", "type": "volume", "source": "model" }
      ]
    }

- Each arc starts one chapter after the previous one ends, so start chapters are not stored.
- An ending is only accepted for a chapter that was in the same reply. Re-translating a chapter never creates a duplicate.
- An author's afterword ("Thoughts on Volume 1", 卷末感言) up to 5 chapters after an ending is folded into that volume. `endChapter` moves to the afterword and `storyEndChapter` keeps the chapter where the story ended.
- `bookName` and `author` are left empty by the translator; the PDF tool fills them in. Arcs you define yourself (`"source": "user"`) are never moved.
- Recorded endings are sent to the model as context so afterwords reuse the exact same name.
- Every change is logged under the batch, e.g. `Volume end recorded at Chapter 2000: “Volume 1: Above the Ruins”`.
