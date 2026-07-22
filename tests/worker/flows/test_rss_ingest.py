"""RssIngestFlow tests."""

from __future__ import annotations

import logging

import pytest
from temporalio import activity, workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

with workflow.unsafe.imports_passed_through():
    from aegis_worker.activities.rss import FetchFeedInput, FetchFeedResult
    from aegis_worker.flows.rss_ingest import RssIngestFlow, RssIngestInput


_calls: dict[str, list] = {
    "list": [],
    "fetch": [],
    "idem": [],
    "content": [],
    "ingest": [],
    "cursor": [],
}


@activity.defn(name="list_active_channels")
async def stub_list(kind: str) -> list[dict]:
    _calls["list"].append(kind)
    return [
        {
            "id": "ch-1",
            "kind": "rss",
            "identifier": "https://hnrss.org/frontpage",
            "config": {"last_cursor": None, "agent_id": "raphael"},
            "active": True,
        },
        {
            "id": "ch-2",
            "kind": "rss",
            "identifier": "https://arxiv.org/rss/cs.AI",
            "config": {"last_cursor": None, "agent_id": "raphael"},
            "active": True,
        },
    ]


@activity.defn(name="fetch_feed")
async def stub_fetch(inp: FetchFeedInput) -> FetchFeedResult:
    _calls["fetch"].append(inp.url)
    return FetchFeedResult(
        entries=[
            {
                "id": f"{inp.url}#1",
                "title": "Entry A",
                "link": f"{inp.url}/a",
                "summary": "sa",
                "published": "2026-04-18T10:00:00",
            },
            {
                "id": f"{inp.url}#2",
                "title": "Entry B",
                "link": f"{inp.url}/b",
                "summary": "sb",
                "published": "2026-04-18T11:00:00",
            },
        ],
        latest_published="2026-04-18T11:00:00",
    )


@activity.defn(name="ingest_idempotency_claim")
async def stub_idem(source_type: str, external_id: str) -> bool:
    _calls["idem"].append((source_type, external_id))
    return True


@activity.defn(name="process_content")
async def stub_content(
    url: str,
    title: str,
    category: str,
    fallback_text: str = "",
    extra_tags: list[str] | None = None,
) -> dict:
    _calls["content"].append((url, category, fallback_text))
    return {"status": "ok"}


@activity.defn(name="ingest_claims")
async def stub_ingest(claims: list[dict]) -> dict:
    _calls["ingest"].append([c["source_ref"] for c in claims])
    return {"ingested": len(claims), "skipped": 0}


@activity.defn(name="update_channel_config_key")
async def stub_cursor(kind, identifier, key, value) -> None:
    _calls["cursor"].append((kind, identifier, key, value))


ALL_STUBS = [stub_list, stub_fetch, stub_idem, stub_content, stub_ingest, stub_cursor]


def _reset():
    for v in _calls.values():
        v.clear()


@pytest.mark.asyncio
async def test_rss_ingests_2_feeds():
    _reset()
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue="tq",
            workflows=[RssIngestFlow],
            activities=ALL_STUBS,
        ),
    ):
        result = await env.client.execute_workflow(
            RssIngestFlow.run,
            RssIngestInput(),
            id="rss-1",
            task_queue="tq",
        )
    assert result["entries"] == 4
    assert result["ingested"] == 4
    assert result["errors"] == 0
    assert len(_calls["fetch"]) == 2
    # ingest_claims is no longer called from rss_ingest — the broken metadata
    # claim was redundant with process_content's /api/content call.
    assert len(_calls["ingest"]) == 0
    assert len(_calls["cursor"]) == 2


@pytest.mark.asyncio
async def test_rss_fetch_failure_skips_feed():
    _reset()

    @activity.defn(name="fetch_feed")
    async def failing_fetch(inp: FetchFeedInput) -> FetchFeedResult:
        if "arxiv" in inp.url:
            raise RuntimeError("feed dead")
        return FetchFeedResult(
            entries=[
                {
                    "id": "x",
                    "title": "T",
                    "link": "https://x",
                    "summary": "",
                    "published": "2026-04-18T10:00:00",
                }
            ],
            latest_published="2026-04-18T10:00:00",
        )

    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue="tq",
            workflows=[RssIngestFlow],
            activities=[
                stub_list,
                failing_fetch,
                stub_idem,
                stub_content,
                stub_ingest,
                stub_cursor,
            ],
        ),
    ):
        result = await env.client.execute_workflow(
            RssIngestFlow.run,
            RssIngestInput(),
            id="rss-2",
            task_queue="tq",
        )
    assert result["errors"] == 1
    assert result["ingested"] == 1  # only the working feed's entry


@pytest.mark.asyncio
async def test_rss_cursor_does_not_advance_past_failed_entries():
    """Earlier code advanced the per-feed cursor to the highest entry
    `published` in the result, regardless of whether `process_content`
    succeeded — so a feed where 5/10 entries blew up silently dropped
    the failures. Cursor now advances only to the latest SUCCESSFUL
    entry's `published`, keeping failures inside the next-tick window.
    """
    _reset()

    failing_url_suffix = "/b"

    @activity.defn(name="process_content")
    async def selective_content(
        url: str,
        title: str,
        category: str,
        fallback_text: str = "",
        extra_tags: list[str] | None = None,
    ) -> dict:
        if url.endswith(failing_url_suffix):
            raise RuntimeError("downstream KS unavailable")
        return {"status": "ok"}

    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue="tq",
            workflows=[RssIngestFlow],
            activities=[
                stub_list,
                stub_fetch,
                stub_idem,
                selective_content,
                stub_ingest,
                stub_cursor,
            ],
        ),
    ):
        result = await env.client.execute_workflow(
            RssIngestFlow.run,
            RssIngestInput(),
            id="rss-failed-cursor",
            task_queue="tq",
        )
    # Both feeds processed; their later entry (link suffix `/b`)
    # fails for both feeds. Cursor advances only to the earlier
    # successful entry's `published` (`10:00:00`), NOT the latest
    # `11:00:00`.
    assert result["errors"] == 0  # fetch_feed didn't fail
    cursor_values = {call[3] for call in _calls["cursor"]}
    assert cursor_values == {"2026-04-18T10:00:00"}


@pytest.mark.asyncio
async def test_rss_dedup_skips_ingest():
    _reset()

    @activity.defn(name="ingest_idempotency_claim")
    async def always_dup(source_type: str, external_id: str) -> bool:
        return False  # everything is a dup

    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue="tq",
            workflows=[RssIngestFlow],
            activities=[stub_list, stub_fetch, always_dup, stub_content, stub_ingest, stub_cursor],
        ),
    ):
        result = await env.client.execute_workflow(
            RssIngestFlow.run,
            RssIngestInput(),
            id="rss-3",
            task_queue="tq",
        )
    assert result["ingested"] == 0  # all skipped as dups
    assert len(_calls["ingest"]) == 0
    # But cursor STILL advances (bookmark is read)
    assert len(_calls["cursor"]) == 2


@pytest.mark.asyncio
async def test_rss_logs_warning_for_stale_feed(caplog):
    """A feed whose last_cursor is far in the past must log a WARNING so
    dead feeds surface instead of being polled silently forever (issue #120).
    """
    _reset()

    @activity.defn(name="list_active_channels")
    async def stale_channel(kind: str) -> list[dict]:
        return [
            {
                "id": "ch-1",
                "kind": "rss",
                "identifier": "https://dead.example/feed",
                "config": {
                    "last_cursor": "2020-01-01T00:00:00+00:00",
                    "agent_id": "raphael",
                },
                "active": True,
            }
        ]

    caplog.set_level(logging.WARNING, logger="temporalio.workflow")

    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue="tq",
            workflows=[RssIngestFlow],
            activities=[
                stale_channel,
                stub_fetch,
                stub_idem,
                stub_content,
                stub_ingest,
                stub_cursor,
            ],
        ),
    ):
        await env.client.execute_workflow(
            RssIngestFlow.run,
            RssIngestInput(),
            id="rss-stale",
            task_queue="tq",
        )

    assert any(
        "rss_feed_stale" in rec.getMessage() and "dead.example" in rec.getMessage()
        for rec in caplog.records
    )


@pytest.mark.asyncio
async def test_rss_no_warning_for_fresh_feed(caplog):
    """A feed whose last_cursor is recent must NOT trigger the stale warning."""
    _reset()
    caplog.set_level(logging.WARNING, logger="temporalio.workflow")

    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue="tq",
            workflows=[RssIngestFlow],
            activities=ALL_STUBS,
        ),
    ):
        await env.client.execute_workflow(
            RssIngestFlow.run,
            RssIngestInput(),
            id="rss-fresh",
            task_queue="tq",
        )

    assert not any("rss_feed_stale" in rec.getMessage() for rec in caplog.records)
