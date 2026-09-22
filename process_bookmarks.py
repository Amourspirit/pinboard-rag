#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["aiohttp>=3.10,<4", "beautifulsoup4>=4.12,<5", "tldextract>=5,<6"]
# ///
"""Convert one chronological batch of saved bookmarks to RAG documents."""

import argparse
import asyncio
from contextlib import contextmanager, ExitStack
from datetime import date, datetime, timezone
import fcntl
import hashlib
from itertools import combinations
import json
import logging
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit

import aiohttp
from bs4 import BeautifulSoup
import tldextract


DOMAIN_EXTRACTOR = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None)
REQUEST_TIMEOUT = 5
LOGGER = logging.getLogger(__name__)
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)


def parse_time(value: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError("Bookmark time must be an ISO timestamp string")
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError(f"Timestamp needs a timezone: {value!r}")
    return result


def validate_bookmarks(value: object) -> list[dict]:
    if not isinstance(value, list):
        raise ValueError("Input must be a JSON array of bookmarks")
    records = []
    for index, record in enumerate(value):
        if not isinstance(record, dict):
            raise ValueError(f"Bookmark {index} must be an object")
        record = record.copy()
        for field in ("href", "time", "description", "extended", "tags", "hash"):
            if field not in ("href", "time"):
                record.setdefault(field, "")
            if not isinstance(record.get(field), str):
                raise ValueError(f"Bookmark {index}: {field} must be a string")
        parse_time(record["time"])
        records.append(record)
    return sorted(records, key=lambda b: parse_time(b["time"]))


def select_batch(bookmarks: list[dict], checkpoint: str | None, size: int) -> list[dict]:
    if size <= 0:
        raise ValueError("Batch size must be positive")
    cutoff = parse_time(checkpoint) if checkpoint is not None else None
    eligible = [b for b in bookmarks if cutoff is None or parse_time(b["time"]) > cutoff]
    if len(eligible) <= size:
        return eligible
    boundary = parse_time(eligible[size - 1]["time"])
    return [b for b in eligible if parse_time(b["time"]) <= boundary]


def bookmark_id(bookmark: dict) -> str:
    if bookmark.get("hash", "").strip():
        return bookmark["hash"]
    timestamp = parse_time(bookmark["time"]).astimezone(timezone.utc).isoformat()
    return hashlib.sha256(f"{bookmark['href']}\n{timestamp}".encode()).hexdigest()


def extract_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for element in soup(["script", "style", "nav", "footer"]):
        element.decompose()
    tags = ("p", "h1", "h2", "h3", "li")
    parts = [element.get_text(" ", strip=True) for element in soup(tags)
             if element.find_parent(tags) is None]
    text = " ".join(parts).strip() or (soup.body or soup).get_text(" ", strip=True)
    return " ".join(text.split())[:3000]


def make_document(bookmark: dict, text: str) -> dict:
    notes = bookmark.get("extended", "").strip()
    lines = notes.splitlines()
    if len(lines) >= 2:
        fence = re.fullmatch(r"(`{3,}|~{3,})[^`~]*", lines[0].strip())
        if fence and re.fullmatch(re.escape(fence[1][0]) + "{" + str(len(fence[1])) + ",}", lines[-1].strip()):
            notes = "\n".join(lines[1:-1]).strip()
    tags = bookmark.get("tags", "").split()
    hostname = urlsplit(bookmark["href"]).hostname or ""
    domain = DOMAIN_EXTRACTOR(hostname).top_domain_under_public_suffix or hostname
    title = bookmark.get("description", "")
    return {
        "id": bookmark_id(bookmark),
        "page_content": (
            f"# {title}\n\n**Bookmark Notes:**\n\n{notes}"
            f"\n\n## Scraped Page Content\n\n{text}"
            f"\n\n## Tags\n{' '.join(tags)}"
        ),
        "metadata": {
            "url": bookmark["href"], "domain": domain,
            "date_bookmarked": parse_time(bookmark["time"]).date().isoformat(),
            "tags": tags,
        },
    }


def load_json(path: Path, default: object) -> object:
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return default


def validate_output(value: object) -> list[dict]:
    if not isinstance(value, list):
        raise ValueError("Existing output must be a JSON array")
    for index, document in enumerate(value):
        if (not isinstance(document, dict)
                or not isinstance(document.get("id"), str) or not document["id"].strip()
                or not isinstance(document.get("page_content"), str)
                or not isinstance(document.get("metadata"), dict)):
            raise ValueError(f"Invalid existing document at index {index}")
        metadata = document["metadata"]
        if (set(metadata) != {"url", "domain", "date_bookmarked", "tags"}
                or any(not isinstance(metadata.get(k), str) for k in ("url", "domain", "date_bookmarked"))
                or not isinstance(metadata.get("tags"), list)
                or any(not isinstance(tag, str) for tag in metadata["tags"])):
            raise ValueError(f"Invalid existing metadata at index {index}")
        date.fromisoformat(metadata["date_bookmarked"])
    return value


def atomic_write(path: Path, value: object) -> None:
    """Replace only after a complete, flushed write; leave the old file on failure."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.name}.", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        # Persist the rename before another file can depend on this write.
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


async def fetch_document(session: aiohttp.ClientSession, bookmark: dict) -> dict | None:
    url = bookmark["href"]
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("expected an HTTP/HTTPS URL with a hostname")
        async with session.get(url) as response:
            response.raise_for_status()
            if not 200 <= response.status < 300:
                raise ValueError(f"HTTP {response.status}")
            if response.content_type not in ("text/html", "application/xhtml+xml"):
                raise ValueError(f"unsupported content type: {response.content_type}")
            html = await response.text(errors="replace")
        text = await asyncio.to_thread(extract_text, html)
        return make_document(bookmark, text)
    except (aiohttp.ClientError, TimeoutError, ValueError, LookupError) as exc:
        LOGGER.warning("Skipped %s: %s", url, str(exc) or type(exc).__name__)
        return None


async def run_batch(input_path: Path, output_path: Path, checkpoint_path: Path,
                    batch_size: int, concurrency: int) -> int:
    if batch_size <= 0 or concurrency <= 0:
        raise ValueError("Batch size and concurrency must be positive")
    with input_path.open(encoding="utf-8") as handle:
        bookmarks = validate_bookmarks(json.load(handle))
    existing = validate_output(load_json(output_path, []))
    missing = object()
    state = load_json(checkpoint_path, missing)
    checkpoint = None
    if state is not missing:
        if not isinstance(state, dict) or "time" not in state:
            raise ValueError("Checkpoint must be an object with a time timestamp")
        parse_time(state["time"])
        checkpoint = state["time"]
    batch = select_batch(bookmarks, checkpoint, batch_size)
    LOGGER.info("Loaded %d bookmarks; batch contains %d; checkpoint: %s",
                len(bookmarks), len(batch), checkpoint or "none")
    if not batch:
        LOGGER.info("Nothing eligible; checkpoint unchanged: %s", checkpoint or "none")
        return 0

    saved = {document["id"]: document for document in existing}
    semaphore = asyncio.Semaphore(concurrency)
    completed = reused = 0
    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
        connector=aiohttp.TCPConnector(limit=concurrency),
        headers={"User-Agent": USER_AGENT},
    ) as session:
        async def process(bookmark):
            nonlocal completed, reused
            async with semaphore:
                document = saved.get(bookmark_id(bookmark))
                if document is not None:
                    reused += 1
                else:
                    document = await fetch_document(session, bookmark)
                completed += 1
                LOGGER.info("[%d/%d] %s %s", completed, len(batch),
                            "Ready" if document is not None else "Omitted", bookmark["href"])
                return document

        results = await asyncio.gather(*(process(bookmark) for bookmark in batch))

    new_documents = []
    successful = []
    for bookmark, document in zip(batch, results):
        if document is None:
            continue
        successful.append(bookmark)
        if document["id"] not in saved:
            saved[document["id"]] = document
            new_documents.append(document)
    if successful:
        if new_documents:
            atomic_write(output_path, existing + new_documents)
        checkpoint = successful[-1]["time"]
        atomic_write(checkpoint_path, {"time": checkpoint})
    LOGGER.info("Appended %d; reused %d; omitted %d; checkpoint: %s",
                len(new_documents), reused, len(batch) - len(successful), checkpoint or "none")
    return len(new_documents)


@contextmanager
def run_lock(output_path: Path, checkpoint_path: Path):
    """Hold stable sibling lock files; never unlink a live lock inode."""
    with ExitStack() as stack:
        for path in sorted({output_path.resolve(), checkpoint_path.resolve()}):
            lock_path = path.with_name(path.name + ".lock")
            handle = stack.enter_context(lock_path.open("a"))
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise BlockingIOError(f"Another run owns {path}") from exc
        yield


def positive_integer(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=Path("bookmarks.json"))
    parser.add_argument("--output", type=Path, default=Path("rag_bookmarks_processed.json"))
    parser.add_argument("--checkpoint", type=Path, default=Path("checkpoint.json"))
    parser.add_argument("--batch-size", type=positive_integer, default=50)
    parser.add_argument("--concurrency", type=positive_integer, default=10)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        source, output, checkpoint = [path.resolve() for path in (args.input, args.output, args.checkpoint)]
        paths = [source, output, checkpoint,
                 output.with_name(output.name + ".lock"), checkpoint.with_name(checkpoint.name + ".lock")]
        for left, right in combinations(paths, 2):
            if left == right or (left.exists() and right.exists() and left.samefile(right)):
                raise ValueError(f"Input, output, checkpoint, and lock paths must be distinct: {left}, {right}")
        with run_lock(output, checkpoint):
            asyncio.run(run_batch(source, output, checkpoint, args.batch_size, args.concurrency))
        return 0
    except (OSError, ValueError) as exc:
        LOGGER.error("Processing failed: %s", exc)
        return 1
    except KeyboardInterrupt:
        LOGGER.info("Interrupted")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
