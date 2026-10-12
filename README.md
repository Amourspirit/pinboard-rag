# Bookmark → RAG

Process one chronological batch of a Pinboard-style bookmark export into Markdown RAG documents. Requires [uv](https://docs.astral.sh/uv/) and Linux (uses `flock` for exclusive run ownership). Python 3.11+ and dependencies are selected by the script's PEP 723 metadata.

## make

The `Makefile` wraps the script with a few defaults. Only the input is required; `make help` prints the same list:

Values from `.env` are used when set; exported environment variables and command-line assignments take precedence.

```bash
make process <pinboard_export.json>
make process ~/bookmarks.json BATCH_SIZE=50
make help
```

| Variable | Default |
| --- | --- |
| `INPUT` | none (required) |
| `OUTPUT` | `storage/rag_bookmarks_processed.json` |
| `CHECKPOINT` | `storage/checkpoint.json` |
| `BATCH_SIZE` | `100` |
| `CONCURRENCY` | `10` |

## Running the script directly

Place your export in `bookmarks.json`, then run:

```bash
uv run process_bookmarks.py
```

Each run selects the oldest 50 eligible bookmarks, fetches up to 10 pages concurrently, appends successful documents to `rag_bookmarks_processed.json`, and saves `checkpoint.json`. Run the command again for the next batch.

All paths are relative to your working directory. Customize them and the batch limits:

```bash
uv run process_bookmarks.py --input bookmarks.json --output rag_bookmarks_processed.json --checkpoint checkpoint.json --batch-size 50 --concurrency 10
```

## Processing rules

- Input is a JSON array. Each bookmark requires string `href` and timezone-aware ISO `time` fields. `description`, `extended`, `tags`, and `hash` are optional strings.
- Sorting and resume comparisons use parsed timestamps, including timezone offsets. A batch includes **every bookmark sharing its final timestamp**, even if this exceeds the batch size.
- Requests use a browser User-Agent, follow redirects, and have a **five-second total timeout**. HTTP errors, invalid URLs, connection failures, timeouts, and non-HTML content are logged and omitted entirely.
- BeautifulSoup parsing runs in worker threads. Text prioritizes paragraphs, headings (`h1`–`h3`), and list items; scripts, styles, navigation, and footers are removed. Extracted text is capped at 3,000 characters.
- Surrounding Markdown fences are removed from notes; internal fenced examples remain. Tags split on whitespace. Root domains use bundled public-suffix data without an additional network request.
- IDs use the bookmark's `hash`, or a stable SHA-256 of its URL and normalized timestamp.

## Checkpoint and recovery

The checkpoint records **the timestamp of the latest successful bookmark whose document has been saved**, never the latest attempt:

```json
{"time": "2026-09-18T11:51:33Z"}
```

Bookmarks at or before that timestamp are excluded on subsequent runs. Consequently, failures before a later success are skipped permanently. Trailing failures remain eligible, and an entirely failing batch leaves the checkpoint unchanged; repeated runs may keep trying that batch. A first run with no successes creates no output or checkpoint.

Output is atomically replaced before the checkpoint is advanced. If checkpoint writing fails, rerunning reuses documents already saved, deduplicates IDs, and repairs the checkpoint without refetching those pages. Existing documents win ID collisions. Malformed input, output, or checkpoint data causes a nonzero exit without resetting your data.

Always use the same output/checkpoint pair together. Exclusive sibling `.lock` files prevent simultaneous runs from writing either destination. Those small files remain after exit; the OS releases the locks automatically. Parent directories must already exist. Keep backups of output and checkpoint together.

## Output

The output is a JSON array with a single Markdown content string and flat metadata per document:

```json
[
  {
    "id": "bookmark-hash-or-deterministic-fallback",
    "page_content": "# Title\n\n**Bookmark Notes:**\n\nNotes\n\n## Scraped Page Content\n\nPage text\n\n## Tags\ntag1 tag2",
    "metadata": {
      "url": "https://example.com/page",
      "domain": "example.com",
      "date_bookmarked": "2026-09-22",
      "tags": ["tag1", "tag2"]
    }
  }
]
```

This extracts server-returned HTML; it does not run JavaScript, transcribe videos, extract PDFs, or generate embeddings. An HTML page with no readable text can still produce a document from the bookmark's title, notes, and tags.

## Local checks

```bash
uv run test_process_bookmarks.py -v
```

Checks use temporary directories and a loopback HTTP server, covering batching, resume behavior, omissions, concurrency, formatting, corrupt data, interrupted saves, and run locks. No public sites or real bookmark exports are used.
