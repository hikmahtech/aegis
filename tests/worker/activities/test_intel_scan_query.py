"""The intel scans' searxng query is the row's `query_template`, the source's
built-in query when the row sets none."""

from __future__ import annotations

import pytest
import respx
from aegis_worker.activities.intel_scan import (
    DEFAULT_QUERY_TEMPLATES,
    IntelScanActivities,
    SearchSourceInput,
    render_query,
)
from httpx import Response
from temporalio.testing import ActivityEnvironment


def test_render_query_fills_the_template_or_falls_back_to_the_built_in_one():
    assert render_query("", "hn", "rust") == "site:news.ycombinator.com rust"
    assert render_query("", "news", "rust") == "rust"
    assert render_query("", "finance", "rust") == (
        "rust site:ft.com OR site:reuters.com OR site:bloomberg.com"
    )
    assert render_query("", "unknown-source", "rust") == "rust"
    assert render_query("site:lobste.rs {topic}", "hn", "rust") == "site:lobste.rs rust"
    assert render_query("site:lobste.rs", "hn", "rust") == "site:lobste.rs rust", "no {topic}: appended"
    assert set(DEFAULT_QUERY_TEMPLATES) == {"hn", "news", "finance"}


def test_build_query_keeps_the_news_categories_and_hn_site_search():
    intel = IntelScanActivities(searxng_url="http://searxng:8080")
    assert intel._build_query("hn", "rust") == {"q": "site:news.ycombinator.com rust", "format": "json"}
    assert intel._build_query("finance", "rust") == {
        "q": "rust site:ft.com OR site:reuters.com OR site:bloomberg.com",
        "categories": "news",
        "format": "json",
    }
    assert intel._build_query("news", "rust", "{topic} site:bbc.co.uk") == {
        "q": "rust site:bbc.co.uk",
        "categories": "news",
        "format": "json",
    }


@pytest.mark.asyncio
@respx.mock
async def test_the_template_on_the_input_reaches_searxng():
    route = respx.get("http://searxng:8080/search").mock(
        return_value=Response(200, json={"results": []})
    )
    intel = IntelScanActivities(searxng_url="http://searxng:8080")
    await ActivityEnvironment().run(
        intel.search_source,
        SearchSourceInput(source="hn", topics=["rust"], query_template="site:lobste.rs {topic}"),
    )
    assert route.calls.last.request.url.params["q"] == "site:lobste.rs rust"
