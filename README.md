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

3. Pick your target book from `../TranslateBook/` and your model from the live model list. Both pickers use the arrow keys: ↑/↓ to move, Enter to select, Esc to cancel. When input isn't a terminal (e.g. piped), a numbered list is shown instead.
4. Install `bookContent.js` in Tampermonkey and navigate to your book page on 69shuba.

## Subcommands

- `python translate.py run` (default) — Starts the ingest server and interactive REPL.
- `python translate.py models` — Prints the models available on your configured endpoint.
- `python translate.py books` — Lists books in your library directory.
- `python translate.py check` — Tests endpoint connectivity and runs a lightweight ping completion.
- `python translate.py retry -b <BookName>` — Re-queues every Failed chapter (and any older `_failed/` batches), waits for them, then exits.
- `python translate.py prompt -b <BookName>` — Inspects the generated translation prompt.

## How Pages Flow

Every scraped page goes into one **pending pool**. Batches are not formed in advance: a worker cuts a batch from the front of the pool only when it is free to send one, using the batch size set at that moment. Running `batch 5` therefore applies to the very next batch.

- **With a goal**, the scraper keeps going until the goal chapter, then stops. The remaining pending pages are sent, including a final partial batch.
- **Without a goal**, the scraper is told to wait once **4 × batch size** pages are pending. Pages already in flight don't count toward that limit. Scraping resumes automatically as the pool shrinks.

Partial batches are only sent in *drain mode*, which turns on when the goal is reached, or when you run `flush`, `retry`, or shut down. It turns off by itself once the pool is empty.

### Missing and Failed Chapters

After each reply, the chapters that were sent are compared with the chapters that came back.

- **Missing**: a chapter that was sent but not returned. It is logged, e.g. `Batch #7: ⚠ Missing Chapters 123, 125 (not re-queued …)`.
- **Failed**: every chapter in a batch that hit an API or JSON error. The batch is also quarantined to `_failed/`.

Neither list is re-queued automatically. Use `missing` to see them, and `retry` to send them again. A chapter leaves both lists once it is translated successfully or re-queued. The lists are stored in `<Book>/_queue_state.json`, so they survive restarts.

Pages with no readable chapter number are keyed as `page:<id>`. They can only be checked for missing by count, and `retry page:<id>` works only during the session that scraped them.

## Interactive Console Commands

Everything typed into the console is a command, so no `/` prefix is needed (a leading `/` is still accepted). Press Tab to complete command names, and type `help` for the full list:
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
- `cancel` — Cancel manual mode or in-flight batches; their pages return to the front of the pool.
- `terms [search]` — Table of glossary terms, optionally filtered (alias: `glossary`).
- `term add <chinese> = <english> [type:<type>]` — Add or override a term and lock it as yours, e.g. `term add 秃鹫尊者 = Venerable Vulture type:title`.
- `term alias <chinese> = <term>` — Make another Chinese spelling render like an existing term (named by its Chinese or English).
- `term rm <chinese>` — Remove a term together with its aliases, or a single alias.
- `flush` — Send all pending pages now, including a partial batch.
- `auto <on|off>` — Pause or resume automatic sending.
- `quit` — Gracefully drain and shut down workers.

`retry` and `recover` find source pages in this order: in-flight batches, `_raw/chapters/`, older `_raw/batch_XXXX_pages.json` spools (newest first), then `_failed/` records. Chapters with no source are reported, not silently skipped.

### Goal progress

Progress bars count from the first chapter the scraper sends after the program starts, and both ends are included. For example, if the session starts at chapter 10 and you run `goal 30`, scraping chapters 10–20 shows `11/21` on the Scraped bar. Each chapter is counted once, even when it is retried or re-scraped.

`goal +50` set before the first page arrives covers 50 chapters starting with the first one. Set later, it means the 50 chapters after the current one.

### Logs

Each finished batch logs one chapter summary, such as `Batch #4: Saved 10 chapters (Chapters 10–19)`, and one character summary, such as `Modified 2 characters, added 1 character, associated 3 characters`. If the reply left chapters out, one extra line lists them as Missing.

### Files Written per Book

- `_raw/chapters/ch_000120.txt` — Latest raw page for each scraped chapter (source for `retry`).
- `_raw/batch_XXXX_pages.json` — Pages of every batch sent, written before the API call.
- `_raw/unsent_*.json` — Pending pages that were never sent when the program exited.
- `_failed/` — Quarantined failed batches with the model reply and error.
- `_queue_state.json` — Missing and Failed chapter lists.
- `Glossary.json` — Locked term renderings (see below).

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
- The model can never change an existing term. A different rendering is logged as `⚠ Term conflict … kept “Joker's Mark”`.
- Terms you add with `term add` are marked `"source": "user"`. The model never edits them, not even to add an alias; it logs a hint instead.
- After each reply, every chapter whose raw page contains a locked term is checked for that term's English. If it is missing, a `⚠ Term drift` warning names the chapter; use `retry <chapter>` if the translation really drifted. Text the model skips (site headers, recommendations) can occasionally cause a false warning.
- Chinese keys and aliases must be 2–40 characters; one-character keys would match almost every page.

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
- Every change is logged, e.g. `Batch #4: 📕 Volume end recorded at Chapter 2000 — “Volume 1: Above the Ruins”`.
