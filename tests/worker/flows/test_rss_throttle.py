"""A per-feed cap must throttle a burst, never drop it.

arxiv cs.AI publishes 270-750 entries in ONE run and every entry is fully
fetched and chunked, which is how PDFs became 94.7% of the knowledge corpus
and crowded every other source out of retrieval.

The dangerous way to cap is to take the NEWEST N: the cursor then advances
past the rest and they are excluded from every later poll — the same silent
permanent loss the cursor logic exists to prevent. So the cap takes the
OLDEST N and leaves the remainder above the cursor.
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
BURST = 10
CAP = 3
_seen: dict[str, list] = {"content": [], "cursor": []}


def _entry(i: int) -> dict:
    return {
        "id": f"{FEED}#{i}",
        "title": f"paper {i}",
        "link": f"{FEED}/{i}",
        "summary": "s",
        "published": f"2026-04-18T{i:02d}:00:00",
    }


def _stubs(cap):
    @activity.defn(name="list_active_channels")
    async def list_channels(kind: str) -> list[dict]:
        config = {"last_cursor": None, "agent_id": "raphael"}
        if cap is not None:
            config["max_entries_per_run"] = cap
        return [{"id": "c1", "kind": "rss", "identifier": FEED, "config": config, "active": True}]

    @activity.defn(name="fetch_feed")
    async def fetch(inp: FetchFeedInput) -> FetchFeedResult:
        # Newest first, as a real feed serves them.
        entries = [_entry(i) for i in range(BURST, 0, -1)]
        return FetchFeedResult(entries=entries, latest_published=f"2026-04-18T{BURST:02d}:00:00")

    @activity.defn(name="ingest_idempotency_claim")
    async def claim(source_type: str, external_id: str) -> bool:
        return True

    @activity.defn(name="ingest_idempotency_release")
    async def release(source_type: str, external_id: str) -> bool:
        return True

    @activity.defn(name="process_content")
    async def content(url, title, category, fallback_text="", extra_tags=None) -> dict:
        _seen["content"].append(url)
        return {"status": "ok"}

    @activity.defn(name="update_channel_config_key")
    async def cursor(kind, identifier, key, value) -> None:
        _seen["cursor"].append(value)

    return [list_channels, fetch, claim, release, content, cursor]


async def _run(cap, wf_id: str) -> dict:
    for v in _seen.values():
        v.clear()
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(env.client, task_queue="tq", workflows=[RssIngestFlow], activities=_stubs(cap)),
    ):
        return await env.client.execute_workflow(
            RssIngestFlow.run, RssIngestInput(), id=wf_id, task_queue="tq"
        )


@pytest.mark.asyncio
async def test_cap_limits_a_burst_to_the_oldest_n():
    result = await _run(CAP, "rss-throttle-1")

    assert len(_seen["content"]) == CAP
    # entries 1,2,3 — the OLDEST. Taking 8,9,10 would strand 1-7 behind the
    # cursor forever, which is the bug this shape avoids.
    assert sorted(_seen["content"]) == [f"{FEED}/1", f"{FEED}/2", f"{FEED}/3"]
    assert result["feeds"][0]["entries"] == CAP
    assert result["feeds"][0]["available"] == BURST
    assert result["feeds"][0]["backlog"] == BURST - CAP


@pytest.mark.asyncio
async def test_the_cursor_stops_at_the_last_entry_taken():
    """The no-loss property: the untaken entries are NEWER than the cursor,
    so the next poll still sees them."""
    await _run(CAP, "rss-throttle-2")

    assert _seen["cursor"] == ["2026-04-18T03:00:00"]


@pytest.mark.asyncio
async def test_no_cap_configured_processes_everything():
    result = await _run(None, "rss-throttle-3")

    assert len(_seen["content"]) == BURST
    assert result["feeds"][0]["entries"] == BURST
    assert "backlog" not in result["feeds"][0]


@pytest.mark.asyncio
async def test_a_garbage_cap_is_ignored_rather_than_stopping_the_feed():
    """A bad config value must not be able to halt ingestion — the feed keeps
    running unthrottled and the operator gets a warning."""
    result = await _run("thirty", "rss-throttle-4")

    assert len(_seen["content"]) == BURST
    assert result["feeds"][0]["entries"] == BURST
