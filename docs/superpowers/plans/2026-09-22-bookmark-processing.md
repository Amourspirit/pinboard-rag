# Bookmark Processing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver a runnable, stateful bookmark-to-RAG script with success-based checkpointing and local regression checks.

**Architecture:** One Python script owns validation, chronological batch selection, asynchronous fetching, transformation, and persistence. A separate standard-library unittest script uses temporary directories and a local HTTP server to check the real processing flow without public network access.

**Tech Stack:** Python 3.11+, uv/PEP 723, asyncio, aiohttp, BeautifulSoup, tldextract, standard-library unittest and Linux flock.

**Spec:** `docs/superpowers/specs/2026-09-22-bookmark-processing-design.md`

## Global Constraints

- PEP 723 inline metadata declares Python 3.11+ and `aiohttp`, `beautifulsoup4`, and `tldextract`.
- Defaults: input `bookmarks.json`, output `rag_bookmarks_processed.json`, state `checkpoint.json`, batch size 50, concurrency 10, and a five-second total timeout per request.
- Command-line options configure file paths, batch size, and concurrency. Relative paths resolve from the working directory.
- Standard-library logging reports batch progress, failed URLs, result counts, and the final checkpoint.
- The checkpoint intentionally tracks success, not attempts.
- If all requests fail, leave the checkpoint unchanged. If nothing is eligible, exit without changing data.
- Output replacement must succeed before checkpoint replacement.
- Prevent simultaneous invocations from modifying the same state/output using a simple exclusive run lock on the target Linux environment.
- No commits or additional dependencies unless the user explicitly requests them.

## Files and execution

- Create `process_bookmarks.py`: complete executable with inline dependencies, small functions, and CLI. No package scaffolding.
- Create `test_process_bookmarks.py`: executable unittest checks with the same three inline dependencies, temporary files, and a loopback HTTP server.
- Create `README.md`: commands, output shape, checkpoint semantics, and failure recovery.
- Create `.gitignore`: Python bytecode, generated data/checkpoints/locks, and local uv environment files.
- Existing design and glossary are authoritative; do not reinterpret last-successful as last-attempted.

Run checks using `uv run test_process_bookmarks.py -v`. Both scripts declare the same runtime dependencies so the checks import the actual application directly. Run targeted cases by appending `BookmarkChecks.test_name` instead of `-v`.

### Task 1: Validate, select, and transform bookmarks

**Files:** Create `process_bookmarks.py` and `test_process_bookmarks.py`.

**Interfaces:**

- `parse_time(value: str) -> datetime`: timezone-aware ISO timestamp; reject missing timezone.
- `validate_bookmarks(value: object) -> list[dict]`: validate fields and return records sorted by parsed time.
- `select_batch(bookmarks: list[dict], checkpoint: str | None, size: int) -> list[dict]`: strictly newer records, including final timestamp ties.
- `bookmark_id(bookmark: dict) -> str`: nonempty hash or SHA-256 of URL and normalized timestamp.
- `extract_text(html: str) -> str`: cleaned text limited to 3,000 characters.
- `make_document(bookmark: dict, text: str) -> dict`: exact requested RAG shape.

- [ ] **Write failing selection and transformation checks.** Start both script files with this metadata (the application can otherwise be empty for the red run):

```python
# /// script
# requires-python = ">=3.11"
# dependencies = ["aiohttp>=3.10,<4", "beautifulsoup4>=4.12,<5", "tldextract>=5,<6"]
# ///
```

Test module imports `unittest` and `process_bookmarks as app`, defines this helper and a `BookmarkChecks(unittest.TestCase)` class, and ends with `unittest.main()` guarded by `if __name__ == "__main__"`:

```python
def bookmark(key, time, href="https://docs.example.co.uk/page"):
    return dict(hash=key, time=time, href=href, description="Title",
                extended="~~~md\nUseful notes\n~~~", tags="one  two")

def test_selection_and_document(self):
    old = bookmark("old", "2026-09-01T00:00:00Z")
    a = bookmark("a", "2026-09-02T00:00:00Z")
    b = bookmark("b", "2026-09-02T01:00:00+01:00")
    later = bookmark("later", "2026-09-03T00:00:00Z")
    records = app.validate_bookmarks([later, b, old, a])
    batch = app.select_batch(records, old["time"], 1)
    self.assertEqual({item["hash"] for item in batch}, {"a", "b"})
    self.assertEqual(app.select_batch(records, a["time"], 1), [later])
    text = app.extract_text(
        "<nav>noise</nav><script>bad()</script><style>bad{}</style>"
        "<h1>Heading</h1><li><p>Unique</p></li><footer>noise</footer>"
    )
    doc = app.make_document(a, text)
    self.assertEqual(doc["metadata"], dict(
        url=a["href"], domain="example.co.uk",
        date_bookmarked="2026-09-02", tags=["one", "two"]))
    self.assertEqual(doc["id"], "a")
    self.assertIn("# Title\n\n**Bookmark Notes:**\n\nUseful notes", doc["page_content"])
    self.assertNotIn("~~~", doc["page_content"])
    self.assertNotIn("noise", text)
    self.assertNotIn("bad", text)
    self.assertEqual(text.count("Unique"), 1)
    self.assertEqual(len(app.extract_text("<p>" + "x" * 4000 + "</p>")), 3000)
    self.assertEqual(app.extract_text("<body>Plain text</body>"), "Plain text")
    self.assertEqual(app.extract_text("<body></body>"), "")
    for invalid in ({}, [dict(a, time="invalid")], [dict(a, time="2026-09-02")],
                    [dict(a, tags=[])], [dict(a, href=None)]):
        with self.assertRaises(ValueError):
            app.validate_bookmarks(invalid)
```

- [ ] **Run the check and confirm the missing application interface causes failure.**

```bash
uv run test_process_bookmarks.py BookmarkChecks.test_selection_and_document -v
```

- [ ] **Implement the interfaces.** Require an input list of objects with string `href` and timezone-aware `time`; missing `description`, `extended`, `tags`, and `hash` default to empty strings, but supplied non-string values fail validation. Sort immediately after validation, before checkpoint filtering. An invalid URL string is a per-bookmark omission at fetch time, not a corrupt input array. Use parsed instants for comparisons:

```python
eligible = [b for b in bookmarks
            if checkpoint is None or parse_time(b["time"]) > parse_time(checkpoint)]
if len(eligible) <= size:
    return eligible
boundary = parse_time(eligible[size - 1]["time"])
return [b for b in eligible if parse_time(b["time"]) <= boundary]
```

Create a module-level `tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None)`; use `top_domain_under_public_suffix` or the original parsed hostname. SHA-256 fallback identity uses URL plus the UTC-normalized timestamp. Remove only surrounding matching fence lines, retaining interior fenced examples. Remove unwanted HTML elements, then select `p`, `h1`, `h2`, `h3`, and `li`; take only selected elements without a selected ancestor so nested text is not repeated. Use body text as fallback and whitespace normalization before truncation. Compose Markdown using the spec's exact headings:

```python
page_content = (
    f"# {title}\n\n**Bookmark Notes:**\n\n{notes}"
    f"\n\n## Scraped Page Content\n\n{text}"
    f"\n\n## Tags\n{' '.join(tags)}"
)
```

- [ ] **Run the selection/transformation check again; require PASS.** Extend the same check with backtick wrappers, an interior code block, localhost/IP fallback, and stable fallback identity if those branches are not already exercised:

```python
plain = bookmark("", "2026-09-02T00:00:00Z", "http://127.0.0.1/page")
self.assertEqual(app.bookmark_id(plain), app.bookmark_id(dict(plain)))
self.assertTrue(app.bookmark_id(plain))
self.assertEqual(app.make_document(plain, "")["metadata"]["domain"], "127.0.0.1")
fenced = dict(plain, extended="```md\nNotes\n```python\nx = 1\n```\n```")
self.assertIn("Notes\n```python\nx = 1\n```", app.make_document(fenced, "")["page_content"])
```

### Task 2: Fetch one batch and persist recoverably

**Files:** Modify `process_bookmarks.py` and `test_process_bookmarks.py`.

**Interfaces:**

- Consumes all Task 1 functions.
- `atomic_write(path: Path, value: object) -> None`: same-directory temporary file, JSON dump, flush/fsync, `os.replace`, cleanup on error.
- `fetch_document(session: aiohttp.ClientSession, bookmark: dict) -> dict | None`: one request and transformation; expected URL/network/HTTP failures logged as omissions.
- `run_batch(input_path: Path, output_path: Path, checkpoint_path: Path, batch_size: int, concurrency: int) -> int` (async): returns count of newly appended documents after a complete persistence cycle.
- `REQUEST_TIMEOUT = 5`: total request timeout; patchable only by tests for fast timeout checks.

- [ ] **Add a failing local end-to-end check.** Use `unittest.IsolatedAsyncioTestCase` for `PipelineChecks`, `tempfile.TemporaryDirectory`, and `aiohttp.web.AppRunner`/`TCPSite` listening on `127.0.0.1` port 0. Register `/ok` as HTML and `/dead` as HTTP 404, tracking requests in a list. In async setup expose `self.base`, `self.input`, `self.output`, and `self.state`; clean up the runner and temporary directory in async teardown. Build the URLs from the bound ephemeral port. Test body:

```python
async def test_resume_and_failed_tail(self):
    records = [
        bookmark("dead-first", "2026-09-01T00:00:00Z", self.base + "/dead"),
        bookmark("ok", "2026-09-02T00:00:00Z", self.base + "/ok"),
        bookmark("tie", "2026-09-02T00:00:00Z", self.base + "/ok"),
        bookmark("dead-last", "2026-09-03T00:00:00Z", self.base + "/dead"),
    ]
    self.input.write_text(json.dumps(list(reversed(records))))
    args = (self.input, self.output, self.state, 2, 2)
    self.assertEqual(await app.run_batch(*args), 2)
    self.assertEqual([d["id"] for d in json.loads(self.output.read_text())], ["tie", "ok"])
    self.assertEqual(json.loads(self.state.read_text()), {"time": records[1]["time"]})
    before = (self.output.read_bytes(), self.state.read_bytes())
    self.requests.clear()
    self.assertEqual(await app.run_batch(*args), 0)
    self.assertEqual(self.requests, ["/dead"])
    self.assertEqual((self.output.read_bytes(), self.state.read_bytes()), before)

async def test_checkpoint_write_failure_recovers(self):
    from unittest.mock import patch
    record = bookmark("ok", "2026-09-02T00:00:00Z", self.base + "/ok")
    self.input.write_text(json.dumps([record]))
    real_write = app.atomic_write
    def fail_state(path, value):
        if path == self.state:
            raise OSError("simulated checkpoint failure")
        return real_write(path, value)
    args = (self.input, self.output, self.state, 50, 2)
    with patch.object(app, "atomic_write", side_effect=fail_state):
        with self.assertRaises(OSError):
            await app.run_batch(*args)
    self.assertFalse(self.state.exists())
    self.assertEqual(len(json.loads(self.output.read_text())), 1)
    self.requests.clear()
    self.assertEqual(await app.run_batch(*args), 0)
    self.assertEqual(self.requests, [])
    self.assertEqual(len(json.loads(self.output.read_text())), 1)
    self.assertEqual(json.loads(self.state.read_text()), {"time": record["time"]})
```

- [ ] **Run these checks and confirm failure because `run_batch` is absent.**

```bash
uv run test_process_bookmarks.py PipelineChecks -v
```

- [ ] **Implement fetching and persistence.** Load and validate every file before HTTP. Existing output must be a list of documents with nonempty string IDs, string content, and the specified flat metadata types; checkpoint must contain a valid aware `time`. Only `FileNotFoundError` yields defaults. Reject nonpositive batch size/concurrency. Use one session with `ClientTimeout(total=REQUEST_TIMEOUT)`, browser User-Agent, a connector concurrency limit, and an `asyncio.Semaphore` around fetching plus parsing. `raise_for_status()` omits HTTP errors; accept `text/html` and `application/xhtml+xml`, omit other declared content types. Parse using `await asyncio.to_thread(extract_text, html)`.

Use ordered `asyncio.gather` results, catching expected failures inside each fetch (not around the whole batch). A bookmark whose ID is already in validated output is already successfully saved: reuse it without refetching. This allows checkpoint recovery even if the original website has since disappeared. Preserve the existing document on duplicate ID and append only new IDs:

```python
saved = {document["id"] for document in existing}
new_documents = []
successful = []
for bookmark, document in zip(batch, results):
    if document is None:
        continue
    successful.append(bookmark)
    if document["id"] not in saved:
        saved.add(document["id"])
        new_documents.append(document)
if successful:
    if new_documents:
        atomic_write(output_path, existing + new_documents)
    atomic_write(checkpoint_path, {"time": successful[-1]["time"]})
return len(new_documents)
```

Emit per-bookmark completion logs and summary counts, including reused saved records and final checkpoint. Empty or entirely failing batches leave persisted files untouched. Filesystem errors propagate to the CLI rather than being treated as failed URLs.

- [ ] **Add failure-boundary checks and run the full test module.** Expand the existing local-server checks with `/slow` (sleep before sending headers), `/pdf`, and `/redirect`; patch `REQUEST_TIMEOUT` to `0.02` for the timeout case. Use an invalid `ftp://` URL and a mocked `aiohttp.ClientConnectionError` for transport failure. Check that only successful HTML is emitted and redirect metadata keeps the original bookmark URL. For corruption and output-write failure use:

```python
async def test_corruption_preserves_files(self):
    self.input.write_text(json.dumps([bookmark("a", "2026-09-02T00:00:00Z", self.base + "/ok")]))
    self.output.write_text("not json")
    self.state.write_text('{"time":"2026-09-01T00:00:00Z"}')
    before = (self.output.read_bytes(), self.state.read_bytes())
    with self.assertRaises(ValueError):
        await app.run_batch(self.input, self.output, self.state, 50, 2)
    self.assertEqual((self.output.read_bytes(), self.state.read_bytes()), before)
    self.assertEqual(self.requests, [])
```

Also run the corruption scenario with valid JSON of the wrong shape and an invalid checkpoint timestamp. Patch `atomic_write` to raise on output and assert checkpoint remains absent. Instrument the HTML handler with active/peak counters and a short await; a batch of three distinct IDs with concurrency two must have `peak == 2`. Add an already-exhausted checkpoint case asserting no requests or writes.

### Task 3: CLI, exclusive run ownership, and usage

**Files:** Modify both scripts; create `README.md` and `.gitignore`.

**Interfaces:**

- Consumes `run_batch` and all file validation from Task 2.
- `run_lock(output_path: Path, checkpoint_path: Path)` context manager: nonblocking exclusive locks for both destinations for the lifetime of the run.
- `main() -> int`: argparse, logging, path checks, lock acquisition, `asyncio.run`, process exit status.

- [ ] **Write failing lock and CLI checks.** Use `unittest.mock.patch("sys.argv", ...)` for help/invalid positive integers and a temporary-directory test for exclusive ownership:

```python
def test_lock_releases_after_failure(self):
    with tempfile.TemporaryDirectory() as directory:
        output = Path(directory) / "output.json"
        state = Path(directory) / "state.json"
        with app.run_lock(output, state):
            with self.assertRaises(BlockingIOError):
                with app.run_lock(output, state):
                    self.fail("second writer acquired the lock")
            with self.assertRaises(BlockingIOError):
                with app.run_lock(output, Path(directory) / "other-state.json"):
                    self.fail("shared output must still be locked")
        with app.run_lock(output, state):
            pass
```

- [ ] **Run the new checks; require failure from the absent lock/CLI interface.**

```bash
uv run test_process_bookmarks.py BookmarkChecks.test_lock_releases_after_failure -v
```

- [ ] **Implement the CLI and locks.** Use `argparse` options `--input`, `--output`, `--checkpoint`, `--batch-size`, and `--concurrency`. Reject overlapping resolved file paths before opening locks, including existing hard-link aliases via `os.path.samefile`. Hold both canonical destination locks in sorted order with `contextlib.ExitStack`, opening each sibling `.lock` file in append mode:

```python
handle = stack.enter_context(lock_path.open("a"))
fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
```

Keep lock files after exit; closing handles releases ownership, and deleting lock files introduces races. Give contention a clear error and nonzero exit. Catch validation, JSON, and filesystem failures in `main` with a concise logged error. Handle Ctrl-C with exit 130. The executable entry point is:

```python
if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Write usage documentation and ignores.** README includes the schema from the spec, explains that runs must share the same output/checkpoint pair, notes Linux locking, and explains the accepted failed-tail retry behavior. Document these exact commands:

```bash
uv run process_bookmarks.py
uv run process_bookmarks.py --input bookmarks.json --output rag_bookmarks_processed.json --checkpoint checkpoint.json --batch-size 50 --concurrency 10
uv run test_process_bookmarks.py -v
```

Use this `.gitignore` content:

```gitignore
__pycache__/
*.py[cod]
.venv/
bookmarks.json
rag_bookmarks_processed.json
checkpoint.json
*.json.lock
```

- [ ] **Run final verification and inspect the diff.** The first command must pass all local checks. Help must exit zero and show all five options. Compilation must succeed. Inspect generated files with `git diff --no-index /dev/null <file>` when still untracked (exit 1 means a diff exists), and inspect status for accidental data files.

```bash
uv run test_process_bookmarks.py -v
uv run process_bookmarks.py --help
uv run --python 3.11 python -m py_compile process_bookmarks.py test_process_bookmarks.py
git diff --check
git status --short
```

Do not exercise the user's real bookmark export or public URLs during verification. Report the run command and actual check results; do not claim unrun checks passed.

## Plan self-review

- Selection, aware timestamps, ties, transformations, and stable identity: Task 1.
- Async HTTP, concurrency, omissions, thread-offloaded parsing, ordered output, and progress logging: Task 2.
- Strict persistence validation, atomic output-before-checkpoint saving, and interrupted-write recovery: Task 2.
- Executable metadata and standard-library local checks: Tasks 1–3.
- CLI defaults, path ownership, Linux exclusive locks, usage, and final verification: Task 3.
- No implementation commits are authorized. No open design decisions require a map.

## Execution record

- [x] Task 1: validation, chronological selection including ties, and RAG transformations implemented and checked.
- [x] Task 2: concurrent fetching, atomic saves, and last-successful checkpoint recovery implemented and checked.
- [x] Task 3: CLI, exclusive run locks, README, and ignores implemented and checked.
- Verification: all 10 local checks pass on Python 3.13 and Python 3.11; `uv run process_bookmarks.py --help` and Python 3.11 compilation pass.
- Test adjustment: isolate the short-deadline timeout case from ordinary HTTP success checks, since a 50 ms deadline also expired on ordinary loopback requests in this environment.
- Work is uncommitted on `feat/bookmark-processing`; the repository had no initial commit from which to create a worktree.
