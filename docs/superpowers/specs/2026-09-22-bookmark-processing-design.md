# Stateful bookmark-to-RAG script

## Destination

Deliver one executable `process_bookmarks.py` script and a small runnable check for its processing and resume behavior. Run it with `uv run process_bookmarks.py`; no package scaffolding or Wayfinder map is needed.

## Interface and dependencies

- PEP 723 inline metadata declares Python 3.11+ and `aiohttp`, `beautifulsoup4`, and `tldextract`.
- Defaults: input `bookmarks.json`, output `rag_bookmarks_processed.json`, state `checkpoint.json`, batch size 50, concurrency 10, and a five-second total timeout per request.
- Command-line options configure file paths, batch size, and concurrency. Relative paths resolve from the working directory.
- Standard-library logging reports batch progress, failed URLs, result counts, and the final checkpoint.

## Selection and checkpoint semantics

1. Load the input array, validate bookmark records, and sort immediately by parsed, timezone-aware `time` values, oldest first. Reject malformed timestamps or required fields before scraping or changing persisted data.
2. Load an optional checkpoint containing a `time` timestamp. Only bookmarks strictly newer than that timestamp are eligible.
3. Select the oldest eligible batch, extending its boundary to include every bookmark with the same final timestamp.
4. Process this batch only, preserving chronological order in successful output despite concurrent requests.
5. Save successful results before advancing the checkpoint to the latest successful bookmark's original `time` value.
6. If all requests fail, leave the checkpoint unchanged. If nothing is eligible, exit without changing data.

The checkpoint intentionally tracks success, not attempts. A failed bookmark older than or equal to a later success is excluded on future runs. Trailing failures remain eligible; an entirely failing final batch can be retried indefinitely. These consequences were explicitly accepted.

## Fetching and transformation

- Validate HTTP/HTTPS URLs and log invalid or unavailable URLs as omissions rather than output documents.
- Share an `aiohttp.ClientSession`, limit concurrent processing, follow redirects, send a realistic browser User-Agent, and enforce the request timeout.
- Omit HTTP errors, DNS failures, connection failures, timeouts, and unsupported non-HTML responses. A successful HTML page with no readable text may still produce a document from the bookmark's own content.
- Run BeautifulSoup parsing with `asyncio.to_thread` so CPU-bound parsing does not block the event loop.
- Remove `script`, `style`, `nav`, and `footer` elements. Extract readable text in document order, prioritizing `p`, `h1`, `h2`, `h3`, and `li`, avoiding repeated text from nested selected elements. Fall back to body text when none of those elements provide text. Normalize whitespace and retain at most 3,000 characters.
- Remove Markdown fence-wrapper lines from `extended`, preserving the notes they surround.
- Split `tags` on whitespace. Derive `date_bookmarked` from the parsed bookmark timestamp as `YYYY-MM-DD`.
- Derive the root domain from the original URL using `tldextract` with its bundled public-suffix data, without an extra network fetch. For IP addresses or hosts without a registrable domain, retain the hostname.
- Use the bookmark's nonempty `hash` as the document ID; otherwise use a deterministic digest of its URL and timestamp.

## Output contract

The output file is a JSON array. Every document has this shape:

```json
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
```

`page_content` is a single Markdown string. Metadata is flat, with the requested list-valued `tags` field and no nested dictionaries.

## Persistence and recovery

- A missing output file starts an empty array; a missing checkpoint starts with all bookmarks eligible.
- Validate existing output and checkpoint data. Malformed persisted data fails visibly and is never silently reset or overwritten.
- Merge successes into existing output, deduplicating by document ID. Previously saved batch records remain recoverable if a prior run saved output but stopped before saving its checkpoint.
- Write each JSON file through a temporary file in its own directory and atomically replace the target. Flush the temporary file before replacement.
- Output replacement must succeed before checkpoint replacement. A checkpoint write failure leaves valid output that can be deduplicated on rerun.
- Prevent simultaneous invocations from modifying the same state/output using a simple exclusive run lock on the target Linux environment.
- Fatal validation or filesystem failures log a clear error and exit nonzero. Individual unavailable pages are logged and omitted while other pages continue.

## Verification

Use a small standard-library test module and a local HTTP server; checks must not depend on public websites. Cover:

- Unsorted input, timezone-aware comparison, one-batch selection, and timestamp ties.
- Successful and failing URLs, including an entirely failed batch with unchanged checkpoint.
- Literal last-successful resume semantics and deduplication after interrupted checkpoint persistence.
- Root-domain extraction, cleaned fenced notes, tags, Markdown shape, HTML noise removal, and the text limit.
- Malformed persisted data leaves files intact.

Also check the executable's help command and run the local checks through `uv` using the script's declared dependencies.

## Scope

This tool fetches HTML once per attempt. Browser rendering, video transcription, PDF extraction, embeddings, vector storage, and retry scheduling are outside this effort.
