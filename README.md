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
- `python translate.py retry -b <BookName>` — Re-queues failed translation batches from `_failed/`.
- `python translate.py prompt -b <BookName>` — Inspects the generated translation prompt.
## Interactive Console Commands

Everything typed into the console is a command, so no `/` prefix is needed (a leading `/` is still accepted). Press Tab to complete command names, and type `help` for the full list:
- `goal <n|+n|none>` — Set the stop chapter (e.g. `goal 3000`, or `goal +50` for the next 50 chapters).
- `progress` — Render current scraping and translation progress bars.
- `status` — View pipeline status, queue depth, session start, goal progress, and metrics.
- `model [id]` — Switch model directly or reopen the arrow-key model picker.
- `endpoint <url>` — Change the target API base URL.
- `batch <n>` — Adjust batch page size (default: 10).
- `concurrency <n>` — Adjust active worker threads.
- `buffer` — List buffered pages with their chapter numbers.
- `manual` — Copy buffered pages to clipboard for external translation.
- `paste` — Ingest translated JSON from clipboard.
- `cancel` — Cancel manual mode or active batch and recover pages to buffer.
- `flush` — Dispatch any partially filled page buffer immediately.
- `retry` — Re-dispatch all quarantined failed batches.
- `quit` — Gracefully drain and shut down workers.

### Goal progress

Progress bars count from the first chapter the scraper sends after the program starts, and both ends are included. For example, if the session starts at chapter 10 and you run `goal 30`, scraping chapters 10–20 shows `11/21` on the Scraped bar. Each chapter is counted once, even when it is retried or re-scraped.

`goal +50` set before the first page arrives covers 50 chapters starting with the first one. Set later, it means the 50 chapters after the current one.

### Logs

Each finished batch logs one chapter summary, such as `Batch #4: Saved 10 chapters (Chapters 10–19)`, and one character summary, such as `Modified 2 characters, added 1 character, associated 3 characters`.

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
