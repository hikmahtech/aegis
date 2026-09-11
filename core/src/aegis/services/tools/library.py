"""Raphael's library tools (#510): the Calibre book library, read-only.

Four tools over `services/library.py`, the implementation `ResearchFlow` and
`CalibreSyncFlow` share. None of them stores anything: a book's text is read
on demand, bounded, and returned with a citation. A failure to reach
calibre-web is recorded as connector health and returned as an error line.
"""

from __future__ import annotations

import json
from typing import Annotated

import asyncpg
import structlog
from pydantic import Field

from aegis.connectors.calibre import CalibreError
from aegis.services import library
from aegis.services.connector_health import record_connector_health
from aegis.services.tools.base import ToolContext
from aegis.services.tools.registry import aegis_tool

logger = structlog.get_logger()


def _unavailable(reason: str) -> str:
    if reason == "not_configured":
        return json.dumps({"error": library.NOT_CONFIGURED})
    return json.dumps({"error": f"the Calibre library cannot be used: {reason}"})


async def _failed(pool: asyncpg.Pool, ctx: ToolContext, exc: Exception) -> str:
    await record_connector_health(pool, ctx.settings, "calibre", ok=False, error=str(exc))
    logger.warning("library_tool_failed", error=str(exc)[:200])
    return json.dumps({"error": f"the Calibre library could not be read: {str(exc)[:300]}"})


@aegis_tool
async def _exec_library_search(
    pool: asyncpg.Pool,
    ctx: ToolContext,
    *,
    query: str = "",
    author: str = "",
    tag: str = "",
    limit: Annotated[int, Field(ge=1, le=25)] = 10,
) -> str:
    """Search the Calibre book library by title, author, tag or subject. Returns each book's id (for library_book and library_read), title, authors, tags, formats and a short description.

    Args:
        query: Words from a title, an author, a tag or the subject. Blank lists the newest books.
        author: Only books by an author whose name contains this.
        tag: Only books with a tag containing this, e.g. machine-learning.
        limit: How many books (1-25).
    """
    conn, reason = library.connector_or_reason(ctx.settings)
    if conn is None:
        return _unavailable(reason)
    try:
        books = await library.search_books(conn, query, author=author, tag=tag, limit=limit)
    except CalibreError as exc:
        return await _failed(pool, ctx, exc)
    await record_connector_health(pool, ctx.settings, "calibre", ok=True)
    return json.dumps({"query": query, "books": books})


@aegis_tool
async def _exec_library_book(pool: asyncpg.Pool, ctx: ToolContext, *, book_id: int) -> str:
    """One book from the Calibre library: its metadata, full description and formats, and for an EPUB its table of contents.

    Args:
        book_id: The book's id, from library_search or library_suggest.
    """
    conn, reason = library.connector_or_reason(ctx.settings)
    if conn is None:
        return _unavailable(reason)
    try:
        details = await library.book_details(conn, book_id)
    except CalibreError as exc:
        return await _failed(pool, ctx, exc)
    await record_connector_health(pool, ctx.settings, "calibre", ok=True)
    return json.dumps(details)


@aegis_tool
async def _exec_library_read(
    pool: asyncpg.Pool,
    ctx: ToolContext,
    *,
    book_id: int,
    section: str = "",
    pages: str = "",
    query: str = "",
    max_chars: Annotated[int, Field(ge=1000, le=40000)] = library.READ_CHARS,
) -> str:
    """Read from a book in the Calibre library. Give a query to get the passages that best match it, a section (chapter number or title) for an EPUB, or pages (e.g. 12-18) for a PDF; with none of these it returns the opening. Every result names the book and chapter or pages to cite. Nothing is saved.

    Args:
        book_id: The book's id, from library_search or library_suggest.
        section: EPUB only: a chapter number or part of its title.
        pages: PDF only: a page or a range, e.g. 12-18 (at most 30 pages).
        query: Return the passages that best match this instead of a whole section.
        max_chars: The most characters of text to return (1000-40000).
    """
    conn, reason = library.connector_or_reason(ctx.settings)
    if conn is None:
        return _unavailable(reason)
    try:
        result = await library.read_book(
            conn, book_id, section=section, pages=pages, query=query, max_chars=max_chars
        )
    except CalibreError as exc:
        return await _failed(pool, ctx, exc)
    await record_connector_health(pool, ctx.settings, "calibre", ok=True)
    return json.dumps(result)


@aegis_tool
async def _exec_library_suggest(
    pool: asyncpg.Pool,
    ctx: ToolContext,
    *,
    topic: str,
    limit: Annotated[int, Field(ge=1, le=15)] = 5,
) -> str:
    """Suggest books from the Calibre library for a topic, best match first.

    Args:
        topic: What the books should be about.
        limit: How many books (1-15).
    """
    topic = (topic or "").strip()
    if not topic:
        return json.dumps({"error": "topic is required"})
    conn, reason = library.connector_or_reason(ctx.settings)
    try:
        result = await library.suggest_books(ctx.knowledge_connector, conn, topic, limit=limit)
    except CalibreError as exc:
        return await _failed(pool, ctx, exc)
    if not result["books"] and conn is None:
        return _unavailable(reason)
    return json.dumps(result)
