"""RssIngestFlow must not step its cursor over a failed entry.

The cursor advanced to the MAX timestamp of every *resolved* entry in a batch.
A batch is not ordered by outcome, so when entry A (10:00) failed and entry B
(11:00) succeeded, the cursor jumped to 11:00 and `fetch_feed`'s
`published_iso <= since_cursor` filter excluded A from every later poll —
silently and permanently. Measured in prod: 553 of 3835 arXiv entries (14%)
lost over 14 days, in two large overnight batches.

The claim is the other half. A failed entry had already taken its
`ingest_idempotency` claim, so even a re-fetch was answered "not new" and
treated as a known dup — which is how one run logged 299 entries and 0
ingested.
"""

from __future__ import annotations

import pytest
from temporalio import activity, workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

with workflow.unsafe.imports_passed_through():
    from aegis_worker.activities.rss import FetchFeedInput, FetchFeedResult
    from aegis_worker.flows.rss_ingest import RssIngestFlow, RssIngestInput


FEED = "https://arxiv.org/rss/cs.AI"
_calls: dict[str, list] = {"cursor": [], "released": [], "content": []}


@activity.defn(name="list_active_channels")
async def stub_list(kind: str) -> list[dict]:
    return [
        {
            "id": "ch-1",
            "kind": "rss",
            "identifier": FEED,
            "config": {"last_cursor": None, "agent_id": "raphael"},
            "active": True,
        }
    ]


def _entry(n: int, hour: int) -> dict:
    return {
        "id": f"{FEED}#{n}",
        "title": f"Entry {n}",
        "link": f"{FEED}/{n}",
        "summary": "s",
        "published": f"2026-04-18T{hour:02d}:00:00",
    }


@activity.defn(name="fetch_feed")
async def stub_fetch(inp: FetchFeedInput) -> FetchFeedResult:
    # Deliberately NOT outcome-ordered: the older entry is the one that fails.
    return FetchFeedResult(
        entries=[_entry(1, 10), _entry(2, 11)],
        latest_published="2026-04-18T11:00:00",
    )


@activity.defn(name="ingest_idempotency_claim")
async def stub_idem(source_type: str, external_id: str) -> bool:
    return True


@activity.defn(name="ingest_idempotency_release")
async def stub_release(source_type: str, external_id: str) -> bool:
    _calls["released"].append(external_id)
    return True


@activity.defn(name="process_content")
async def stub_content_entry1_fails(
    url: str,
    title: str,
    category: str,
    fallback_text: str = "",
    extra_tags: list[str] | None = None,
) -> dict:
    _calls["content"].append(url)
    if url.endswith("/1"):
        raise RuntimeError("KS extraction timed out")
    return {"status": "ok"}


@activity.defn(name="process_content")
async def stub_content_entry2_fails(
    url: str,
    title: str,
    category: str,
    fallback_text: str = "",
    extra_tags: list[str] | None = None,
) -> dict:
    _calls["content"].append(url)
    if url.endswith("/2"):
        raise RuntimeError("KS extraction timed out")
    return {"status": "ok"}


@activity.defn(name="update_channel_config_key")
async def stub_cursor(kind, identifier, key, value) -> None:
    _calls["cursor"].append((identifier, key, value))


async def _run(content_stub, wf_id: str) -> dict:
    for v in _calls.values():
        v.clear()
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue="tq",
            workflows=[RssIngestFlow],
            activities=[stub_list, stub_fetch, stub_idem, stub_release, content_stub, stub_cursor],
        ),
    ):
        return await env.client.execute_workflow(
            RssIngestFlow.run, RssIngestInput(), id=wf_id, task_queue="tq"
        )


@pytest.mark.asyncio
async def test_cursor_is_held_when_the_older_entry_is_the_one_that_failed():
    """The exact data-loss shape. Entry 1 (10:00) fails, entry 2 (11:00)
    succeeds — advancing to 11:00 would exclude entry 1 forever."""
    result = await _run(stub_content_entry1_fails, "rss-loss-1")

    assert _calls["cursor"] == [], "cursor advanced past a failed entry"
    assert result["failed"] == 1
    assert result["ingested"] == 1, "a failed entry must not be counted as ingested"
    feed = result["feeds"][0]
    assert feed["failed"] == 1
    assert feed["cursor_held"] is True


@pytest.mark.asyncio
async def test_cursor_advances_only_up_to_the_oldest_failure():
    """Entry 1 (10:00) succeeds, entry 2 (11:00) fails — 10:00 is safe to keep,
    so the feed still makes progress instead of stalling on every hiccup."""
    result = await _run(stub_content_entry2_fails, "rss-loss-2")

    assert _calls["cursor"] == [(FEED, "last_cursor", "2026-04-18T10:00:00")]
    assert result["failed"] == 1
    assert result["ingested"] == 1


@pytest.mark.asyncio
async def test_a_failed_entry_gives_its_idempotency_claim_back():
    """Without this the retry the cursor logic protects can never happen: the
    next poll re-sees the entry, gets "not new", and skips it as a dup."""
    await _run(stub_content_entry1_fails, "rss-loss-3")

    assert _calls["released"] == [f"{FEED}#1"]
