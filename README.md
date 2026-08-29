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

3. Select your target book from `../TranslateBook/` and choose your preferred model from the live model picker.
4. Install `bookContent.js` in Tampermonkey and navigate to your book page on 69shuba.

## Subcommands

- `python translate.py run` (default) — Starts the ingest server and interactive REPL.
- `python translate.py models` — Lists models available on your configured endpoint.
- `python translate.py books` — Lists books in your library directory.
- `python translate.py check` — Tests endpoint connectivity and runs a lightweight ping completion.
- `python translate.py retry -b <BookName>` — Re-queues failed translation batches from `_failed/`.
- `python translate.py prompt -b <BookName>` — Inspects the generated translation prompt.

## Interactive REPL Commands

While running, type `/help` in the console to view all available commands:
- `/status` — View current pipeline status, queue depth, and metrics.
- `/model [id]` — Switch model on the fly or reopen the interactive model picker.
- `/endpoint <url>` — Change the target API base URL.
- `/batch <n>` — Adjust batch page size (default: 10).
- `/concurrency <n>` — Adjust active worker threads.
- `/manual` — Copy buffered pages to clipboard for external translation.
- `/paste` — Ingest translated JSON from clipboard.
- `/cancel` — Cancel manual mode or active batch and recover pages to buffer.
- `/flush` — Dispatch any partially filled page buffer immediately.
- `/retry` — Re-dispatch all quarantined failed batches.
- `/quit` — Gracefully drain and shut down workers.
