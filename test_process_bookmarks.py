# /// script
# requires-python = ">=3.11"
# dependencies = ["aiohttp>=3.10,<4", "beautifulsoup4>=4.12,<5", "tldextract>=5,<6"]
# ///
"""Local regression checks: uv run test_process_bookmarks.py -v."""

import asyncio
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import aiohttp
from aiohttp import web

import process_bookmarks as app


def bookmark(key, time, href="https://docs.example.co.uk/page"):
    return dict(hash=key, time=time, href=href, description="Title",
                extended="~~~md\nUseful notes\n~~~", tags="one  two")


class BookmarkChecks(unittest.TestCase):
    def test_lock_releases_and_blocks_shared_destinations(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output.json"
            state = Path(directory) / "state.json"
            with app.run_lock(output, state):
                for other_output, other_state in ((output, state), (output, state.with_name("other.json")),
                                                   (output.with_name("other.json"), state)):
                    with self.assertRaises(BlockingIOError):
                        with app.run_lock(other_output, other_state):
                            self.fail("second writer acquired the lock")
            with app.run_lock(output, state):
                pass

    def test_cli_validation_and_empty_run(self):
        with patch("sys.argv", ["process_bookmarks.py", "--help"]), redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as stopped:
                app.main()
            self.assertEqual(stopped.exception.code, 0)
        for flag in ("--batch-size", "--concurrency"):
            with patch("sys.argv", ["process_bookmarks.py", flag, "0"]), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as stopped:
                    app.main()
                self.assertEqual(stopped.exception.code, 2)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, output, state = [root / n for n in ("input.json", "output.json", "state.json")]
            source.write_text("[]")
            args = ["process_bookmarks.py", "--input", str(source), "--output", str(output), "--checkpoint", str(state)]
            with patch("sys.argv", args):
                self.assertEqual(app.main(), 0)
            self.assertFalse(output.exists())
            self.assertFalse(state.exists())
            os.link(source, output)
            with patch("sys.argv", args), self.assertLogs(app.LOGGER, level="ERROR"):
                self.assertEqual(app.main(), 1)
            self.assertEqual(source.read_text(), "[]")
            output.unlink()
            source.write_text("invalid")
            with patch("sys.argv", args), self.assertLogs(app.LOGGER, level="ERROR"):
                self.assertEqual(app.main(), 1)
            self.assertFalse(output.exists())

    def test_selection_and_document(self):
        old = bookmark("old", "2026-09-01T00:00:00Z")
        a = bookmark("a", "2026-09-02T00:00:00Z")
        b = bookmark("b", "2026-09-02T01:00:00+01:00")
        later = bookmark("later", "2026-09-03T00:00:00Z")
        records = app.validate_bookmarks([later, b, old, a])
        self.assertEqual(records[0], old)
        self.assertEqual({r["hash"] for r in app.select_batch(records, old["time"], 1)}, {"a", "b"})
        self.assertEqual(app.select_batch(records, a["time"], 1), [later])
        self.assertEqual(app.select_batch(records, later["time"], 50), [])
        text = app.extract_text(
            "<nav>noise</nav><script>bad()</script><style>bad{}</style>"
            "<h1>Heading</h1><li><p>Unique</p></li><footer>noise</footer>"
        )
        doc = app.make_document(a, text)
        self.assertEqual(doc["metadata"], dict(
            url=a["href"], domain="example.co.uk",
            date_bookmarked="2026-09-02", tags=["one", "two"]))
        self.assertEqual(doc["id"], "a")
        self.assertEqual(doc["page_content"],
                         "# Title\n\n**Bookmark Notes:**\n\nUseful notes"
                         "\n\n## Scraped Page Content\n\nHeading Unique\n\n## Tags\none two")
        self.assertEqual(len(app.extract_text("<p>" + "x" * 4000 + "</p>")), 3000)
        self.assertEqual(app.extract_text("<body>Plain text</body>"), "Plain text")
        self.assertEqual(app.extract_text("<body></body>"), "")
        for invalid in ({}, [dict(a, time="invalid")], [dict(a, time="2026-09-02")],
                        [dict(a, tags=[])], [dict(a, href=None)]):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                app.validate_bookmarks(invalid)
        plain = bookmark("", a["time"], "http://127.0.0.1/page")
        self.assertTrue(app.bookmark_id(plain))
        self.assertEqual(app.bookmark_id(plain), app.bookmark_id(dict(plain, time=b["time"])))
        self.assertNotEqual(app.bookmark_id(plain), app.bookmark_id(dict(plain, href="http://localhost/")))
        for host in ("127.0.0.1", "localhost", "[::1]"):
            self.assertEqual(app.make_document(dict(plain, href=f"http://{host}/"), "")["metadata"]["domain"], host.strip("[]"))
        fenced = dict(plain, extended="```md\nNotes\n```python\nx = 1\n```\n```")
        self.assertIn("Notes\n```python\nx = 1\n```", app.make_document(fenced, "")["page_content"])
        minimal = app.validate_bookmarks([{"href": a["href"], "time": a["time"]}])[0]
        self.assertEqual(app.make_document(minimal, "")["metadata"]["tags"], [])


class PipelineChecks(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.input, self.output, self.state = [root / name for name in ("input.json", "output.json", "state.json")]
        self.requests = []
        self.active = self.peak = 0

        async def handler(request):
            self.requests.append(request.path)
            if request.path == "/dead":
                raise web.HTTPNotFound()
            if request.path == "/redirect":
                raise web.HTTPFound("/ok")
            if request.path == "/pdf":
                return web.Response(body=b"%PDF", content_type="application/pdf")
            self.active += 1
            self.peak = max(self.active, self.peak)
            try:
                await asyncio.sleep(0.1 if request.path == "/slow" else 0.005)
                return web.Response(text="<nav>Noise</nav><h1>Heading</h1><p>Page</p>", content_type="text/html")
            finally:
                self.active -= 1

        server = web.Application()
        server.router.add_get("/{path}", handler)
        runner = web.AppRunner(server)
        await runner.setup()
        self.addAsyncCleanup(runner.cleanup)
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        self.base = f"http://127.0.0.1:{runner.addresses[0][1]}"

    async def run_batch(self, records=None, size=50, concurrency=2):
        if records is not None:
            self.input.write_text(json.dumps(records), encoding="utf-8")
        return await app.run_batch(self.input, self.output, self.state, size, concurrency)

    async def test_resume_and_failed_tail(self):
        records = [
            bookmark("dead-first", "2026-09-01T00:00:00Z", self.base + "/dead"),
            bookmark("ok", "2026-09-02T00:00:00Z", self.base + "/ok"),
            bookmark("tie", "2026-09-02T00:00:00Z", self.base + "/ok"),
            bookmark("dead-last", "2026-09-03T00:00:00Z", self.base + "/dead"),
        ]
        self.assertEqual(await self.run_batch(list(reversed(records)), size=2), 2)
        self.assertEqual([d["id"] for d in json.loads(self.output.read_text())], ["tie", "ok"])
        self.assertEqual(json.loads(self.state.read_text()), {"time": records[1]["time"]})
        before = (self.output.read_bytes(), self.state.read_bytes())
        self.requests.clear()
        self.assertEqual(await self.run_batch(size=2), 0)
        self.assertEqual(self.requests, ["/dead"])
        self.assertEqual((self.output.read_bytes(), self.state.read_bytes()), before)

    async def test_checkpoint_write_failure_recovers_without_refetching(self):
        record = bookmark("ok", "2026-09-02T00:00:00Z", self.base + "/ok")
        real_write = app.atomic_write

        def fail_state(path, value):
            if path == self.state:
                raise OSError("simulated checkpoint failure")
            return real_write(path, value)

        with patch.object(app, "atomic_write", side_effect=fail_state):
            with self.assertRaises(OSError):
                await self.run_batch([record])
        self.assertFalse(self.state.exists())
        self.assertEqual(len(json.loads(self.output.read_text())), 1)
        self.requests.clear()
        self.assertEqual(await self.run_batch(), 0)
        self.assertEqual(self.requests, [])
        self.assertEqual(len(json.loads(self.output.read_text())), 1)
        self.assertEqual(json.loads(self.state.read_text()), {"time": record["time"]})
        before = (self.output.read_bytes(), self.state.read_bytes())
        self.assertEqual(await self.run_batch(), 0)
        self.assertEqual((self.output.read_bytes(), self.state.read_bytes()), before)

    async def test_unavailable_urls_are_omitted(self):
        records = [bookmark(path, "2026-09-02T00:00:00Z", self.base + "/" + path)
                   for path in ("dead", "pdf", "redirect")]
        records += [bookmark("invalid", records[0]["time"], "ftp://example.com/")]
        self.assertEqual(await self.run_batch(records), 1)
        documents = json.loads(self.output.read_text())
        self.assertEqual([d["id"] for d in documents], ["redirect"])
        self.assertEqual(documents[0]["metadata"]["url"], self.base + "/redirect")
        before = (self.output.read_bytes(), self.state.read_bytes())
        with patch.object(app, "REQUEST_TIMEOUT", 0.02):
            self.assertEqual(await self.run_batch([
                bookmark("slow", "2026-09-03T00:00:00Z", self.base + "/slow")]), 0)
        self.assertEqual((self.output.read_bytes(), self.state.read_bytes()), before)
        async with aiohttp.ClientSession() as session:
            with patch.object(session, "get", side_effect=aiohttp.ClientConnectionError("offline")):
                self.assertIsNone(await app.fetch_document(session, records[0]))

    async def test_all_failed_initial_batch_creates_no_data(self):
        self.assertEqual(await self.run_batch([bookmark("dead", "2026-09-02T00:00:00Z", self.base + "/dead")]), 0)
        self.assertFalse(self.output.exists())
        self.assertFalse(self.state.exists())

    async def test_concurrency_and_duplicate_ids(self):
        records = [bookmark(str(n), "2026-09-02T00:00:00Z", self.base + "/ok") for n in range(3)]
        self.assertEqual(await self.run_batch(records + [records[0]], concurrency=2), 3)
        self.assertEqual(self.peak, 2)
        self.assertEqual([d["id"] for d in json.loads(self.output.read_text())], ["0", "1", "2"])

    async def test_corruption_preserves_files(self):
        record = bookmark("a", "2026-09-02T00:00:00Z", self.base + "/ok")
        for output, state in (("not json", '{"time":"2026-09-01T00:00:00Z"}'),
                              ("{}", '{"time":"2026-09-01T00:00:00Z"}'),
                              ("[]", '{"time":"bad"}'), ("[]", "null"),
                              ('[{"id":"a"}]', '{"time":"2026-09-01T00:00:00Z"}')):
            with self.subTest(output=output, state=state):
                self.output.write_text(output)
                self.state.write_text(state)
                before = (self.output.read_bytes(), self.state.read_bytes())
                with self.assertRaises(ValueError):
                    await self.run_batch([record])
                self.assertEqual((self.output.read_bytes(), self.state.read_bytes()), before)
                self.assertEqual(self.requests, [])

    async def test_output_failure_does_not_advance_checkpoint(self):
        record = bookmark("a", "2026-09-02T00:00:00Z", self.base + "/ok")
        with patch.object(app, "atomic_write", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                await self.run_batch([record])
        self.assertFalse(self.state.exists())
        self.output.write_text("[]")
        with patch.object(app.os, "replace", side_effect=OSError("replace failed")):
            with self.assertRaises(OSError):
                app.atomic_write(self.output, [{"new": "data"}])
        self.assertEqual(self.output.read_text(), "[]")
        self.assertEqual({p.name for p in self.output.parent.iterdir()}, {"input.json", "output.json"})


if __name__ == "__main__":
    unittest.main()
