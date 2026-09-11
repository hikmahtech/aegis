"""Raphael's library tools (#510) — library_search, library_book, library_read,
library_suggest."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aegis.connectors.calibre import CalibreError
from aegis.services import library
from aegis.services.chat import TOOL_EXECUTORS, ToolContext
from aegis.services.tools import library as tools_library

BOOK = {
    "id": 12,
    "title": "Hands-On Machine Learning",
    "authors": ["Aurélien Géron"],
    "tags": ["machine-learning"],
    "published": "2019-05-01",
    "description": "A practical book.",
    "formats": [{"format": "PDF", "href": "x", "size": 1}],
}


@pytest.fixture
def health(monkeypatch):
    mock = AsyncMock()
    monkeypatch.setattr(tools_library, "record_connector_health", mock)
    return mock


@pytest.fixture
def conn(monkeypatch):
    fake = AsyncMock()
    fake.search = AsyncMock(return_value=[BOOK])
    fake.catalog = AsyncMock(return_value=[BOOK])
    fake.get_book = AsyncMock(return_value=BOOK)
    monkeypatch.setattr(library, "connector_or_reason", lambda settings: (fake, ""))
    return fake


def _ctx(**kw) -> ToolContext:
    return ToolContext(agent_id="raphael", settings=SimpleNamespace(), **kw)


async def _call(name: str, args: dict, ctx: ToolContext | None = None) -> dict:
    return json.loads(await TOOL_EXECUTORS[name](AsyncMock(), args, ctx or _ctx()))


@pytest.mark.asyncio
async def test_not_configured_says_where_to_configure_it(monkeypatch):
    monkeypatch.setattr(library, "connector_or_reason", lambda settings: (None, "not_configured"))
    for name, args in (
        ("library_search", {"query": "ml"}),
        ("library_book", {"book_id": 12}),
        ("library_read", {"book_id": 12}),
        ("library_suggest", {"topic": "ml"}),
    ):
        out = await _call(name, args)
        assert out["error"] == library.NOT_CONFIGURED, name


@pytest.mark.asyncio
async def test_search_filters_by_author_and_tag(conn, health):
    out = await _call("library_search", {"query": "machine", "author": "géron"})
    assert [b["id"] for b in out["books"]] == [12]
    assert conn.search.await_args.args == ("machine",)
    none = await _call("library_search", {"query": "machine", "tag": "cooking"})
    assert none["books"] == []
    blank = await _call("library_search", {})
    assert [b["id"] for b in blank["books"]] == [12]
    conn.catalog.assert_awaited()
    assert health.await_args.kwargs["ok"] is True


@pytest.mark.asyncio
async def test_a_library_that_cannot_be_read_is_an_error_line_and_a_health_record(conn, health):
    conn.search = AsyncMock(side_effect=CalibreError("calibre-web is unreachable"))
    out = await _call("library_search", {"query": "ml"})
    assert "could not be read" in out["error"] and "unreachable" in out["error"]
    assert health.await_args.kwargs["ok"] is False
    assert health.await_args.args[2] == "calibre"


@pytest.mark.asyncio
async def test_read_passes_its_arguments_through(conn, health, monkeypatch):
    read = AsyncMock(return_value={"cite": "Hands-On Machine Learning, pp. 3-5", "text": "..."})
    monkeypatch.setattr(library, "read_book", read)
    out = await _call("library_read", {"book_id": 12, "pages": "3-5", "max_chars": 2000})
    assert out["cite"] == "Hands-On Machine Learning, pp. 3-5"
    assert read.await_args.args[1] == 12
    assert read.await_args.kwargs == {"section": "", "pages": "3-5", "query": "", "max_chars": 2000}


@pytest.mark.asyncio
async def test_suggest_needs_a_topic_and_uses_the_index(conn, health):
    assert (await _call("library_suggest", {"topic": "  "}))["error"] == "topic is required"
    kc = AsyncMock()
    kc.search = AsyncMock(
        return_value=[{"title": "HOML", "similarity": 0.8, "metadata": {"calibre_id": 12}}]
    )
    out = await _call("library_suggest", {"topic": "neural nets"}, _ctx(knowledge_connector=kc))
    assert out["via"] == "index" and out["books"][0]["id"] == 12


def test_the_four_tools_are_read_only_on_the_mcp_surface():
    from aegis.api.routes.mcp_server import _READ_ONLY_TOOLS

    assert {"library_search", "library_book", "library_read", "library_suggest"} <= _READ_ONLY_TOOLS
