"""ContentActivities.store_feed_abstract — an RSS entry as its title and summary only (#512)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from aegis_worker.activities.content import ContentActivities
from temporalio.testing import ActivityEnvironment

pytestmark = pytest.mark.asyncio


def _act(kc) -> ContentActivities:
    return ContentActivities(knowledge_connector=kc, db_pool=None, enabled=True)


async def test_it_stores_the_summary_as_an_abstract_row_and_fetches_nothing():
    kc = AsyncMock()
    kc.ingest_content = AsyncMock(return_value={"content_id": "cid-1", "status": "ok"})
    out = await ActivityEnvironment().run(
        _act(kc).store_feed_abstract,
        "https://arxiv.org/abs/2609.00001",
        "A paper",
        "<p>We show that <b>small</b> models &amp; big data win.</p>",
    )
    assert out == {"status": "ok", "content_id": "cid-1"}
    kwargs = kc.ingest_content.call_args.kwargs
    assert kwargs["source_type"] == "abstract"
    assert kwargs["url"] == "https://arxiv.org/abs/2609.00001"
    assert kwargs["summary"] == "We show that small models & big data win."
    assert kwargs["raw_text"].startswith("A paper\n\nWe show that small models")
    assert kwargs["raw_text"].endswith("https://arxiv.org/abs/2609.00001")
    assert kwargs["tags"] == ["rss", "abstract"]


async def test_too_little_text_is_empty_not_stored():
    kc = AsyncMock()
    out = await ActivityEnvironment().run(_act(kc).store_feed_abstract, "https://x", "Hi", "")
    assert out == {"status": "empty"}
    kc.ingest_content.assert_not_called()


async def test_a_store_failure_is_an_error_status():
    kc = AsyncMock()
    kc.ingest_content = AsyncMock(side_effect=RuntimeError("store down"))
    out = await ActivityEnvironment().run(
        _act(kc).store_feed_abstract, "https://x", "A title", "a long enough summary here"
    )
    assert out == {"status": "error"}


async def test_switched_off_extraction_stores_nothing():
    kc = AsyncMock()
    act = ContentActivities(knowledge_connector=kc, db_pool=None, enabled=False)
    out = await ActivityEnvironment().run(act.store_feed_abstract, "https://x", "T", "summary text")
    assert out == {"status": "disabled"}
