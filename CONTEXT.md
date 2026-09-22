# Bookmark Processing

Saved web bookmarks become documents suitable for retrieval-augmented generation (RAG).

## Language

**Successful bookmark**:
A bookmark whose page was fetched and transformed into a RAG document. An unavailable URL is not a successful bookmark.

**Bookmark checkpoint**:
The latest bookmarking timestamp among successfully processed bookmarks whose documents have been saved. Bookmarks at or before this timestamp are excluded from subsequent imports.
_Avoid_: Last-attempted timestamp

**Bookmark batch**:
The oldest eligible bookmarks selected for one execution. A batch includes every bookmark sharing its final timestamp, even when that exceeds the configured batch size.

**RAG document**:
A bookmark's title, cleaned notes, scraped text, and tags combined into Markdown, accompanied by an ID and flat metadata.
