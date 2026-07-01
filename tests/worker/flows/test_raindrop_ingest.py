"""RaindropIngestFlow tests."""

from __future__ import annotations

import pytest
from temporalio import activity, workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

with workflow.unsafe.imports_passed_through():
    from aegis_worker.activities.raindrop import (
        PollBookmarksInput,
        PollBookmarksResult,
    )
    from aegis_worker.flows.raindrop_ingest import (
        RaindropIngestFlow,
        RaindropIngestInput,
    )


_calls: dict[str, list] = {
    "list": [],
    "poll": [],
    "idem": [],
    "content": [],
    "ingest": [],
    "cursor": [],
    "capture": [],
}


@activity.defn(name="list_active_channels")
async def stub_list(kind: str) -> list[dict]:
    _calls["list"].append(kind)
    return [
        {
            "id": "ch-1",
            "kind": "raindrop",
            "identifier": "default",
            "config": {"last_cursor": None, "agent_id": "raphael"},
            "active": True,
        }
    ]


@activity.defn(name="poll_bookmarks")
async def stub_poll(inp: PollBookmarksInput) -> PollBookmarksResult:
    _calls["poll"].append(inp.since_cursor)
    return PollBookmarksResult(
        bookmarks=[
            {
                "id": "1",
                "link": "https://x.com/a",
                "title": "A",
                "excerpt": "a ex",
                "tags": ["rust"],
                "created": "2026-04-18T10:00:00Z",
            },
            {
                "id": "2",
                "link": "https://x.com/b",
                "title": "B",
                "excerpt": "b ex",
                "tags": [],
                "created": "2026-04-18T11:00:00Z",
            },
        ],
        latest_created="2026-04-18T11:00:00Z",
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
    _calls["content"].append(url)
    return {"status": "ok", "job_id": "job-123", "content_id": "c-456"}


@activity.defn(name="ingest_claims")
async def stub_ingest(claims: list[dict]) -> dict:
    _calls["ingest"].append([c["source_ref"] for c in claims])
    return {"ingested": len(claims), "skipped": 0}


@activity.defn(name="update_channel_config_key")
async def stub_cursor(kind: str, identifier: str, key: str, value: str) -> None:
    _calls["cursor"].append((kind, identifier, key, value))


@activity.defn(name="capture_to_inbox")
async def stub_capture(
    source_tag: str, external_id: str, title: str, description: str | None = None
) -> str | None:
    _calls["capture"].append((source_tag, external_id, title))
    return f"task-{external_id}"


ALL_STUBS = [
    stub_list,
    stub_poll,
    stub_idem,
    stub_content,
    stub_ingest,
    stub_cursor,
    stub_capture,
]


def _reset() -> None:
    for v in _calls.values():
        v.clear()


@pytest.mark.asyncio
async def test_raindrop_fetches_extracts_ingests_and_advances():
    _reset()
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue="tq",
            workflows=[RaindropIngestFlow],
            activities=ALL_STUBS,
        ),
    ):
        result = await env.client.execute_workflow(
            RaindropIngestFlow.run,
            RaindropIngestInput(agent_id="raphael"),
            id="r-1",
            task_queue="tq",
        )
    assert result["bookmarks"] == 2
    assert result["ingested"] == 2
    # Real Todoist ids returned by stub_capture (no `item-` prefix) → committed.
    assert result["todoist_committed"] == 2
    assert result["outbox_staged"] == 0
    assert result["capture_failed"] == 0
    assert len(_calls["content"]) == 2
    # ingest_claims is no longer called from raindrop_ingest — the broken metadata
    # claim was redundant with process_content's /api/content call.
    assert len(_calls["ingest"]) == 0
    assert _calls["cursor"] == [("raindrop", "default", "last_cursor", "2026-04-18T11:00:00Z")]
    # Phase 5: every new Raindrop becomes an Inbox capture tagged #research,
    # which ClarifyFlow then auto-routes to 🔖 Reference via skip_inbox.
    assert len(_calls["capture"]) == 2
    assert _calls["capture"][0][0] == "#research"
    assert _calls["capture"][0][1] == "raindrop-1"
    assert _calls["capture"][0][2] == "A"
    assert _calls["capture"][1][1] == "raindrop-2"


@pytest.mark.asyncio
async def test_raindrop_outbox_staged_counts_separately_from_committed():
    """When capture_to_inbox hits a retryable Todoist failure, the helper
    returns the outbox temp_id (`item-…`). The flow surfaces that as
    `outbox_staged` so a "stuck" state is visible in result_summary instead
    of the previous `ingested=N, todoist_committed=0` invisible drift."""
    _reset()

    @activity.defn(name="capture_to_inbox")
    async def outbox_capture(
        source_tag: str, external_id: str, title: str, description: str | None = None
    ) -> str | None:
        return f"item-{external_id}-temp"

    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue="tq",
            workflows=[RaindropIngestFlow],
            activities=[
                stub_list,
                stub_poll,
                stub_idem,
                stub_content,
                stub_ingest,
                stub_cursor,
                outbox_capture,
            ],
        ),
    ):
        result = await env.client.execute_workflow(
            RaindropIngestFlow.run,
            RaindropIngestInput(),
            id="r-outbox",
            task_queue="tq",
        )
    assert result["bookmarks"] == 2
    assert result["ingested"] == 2
    assert result["todoist_committed"] == 0
    assert result["outbox_staged"] == 2
    assert result["capture_failed"] == 0


@pytest.mark.asyncio
async def test_raindrop_no_channel():
    _reset()

    @activity.defn(name="list_active_channels")
    async def empty_list(kind: str) -> list[dict]:
        return []

    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue="tq",
            workflows=[RaindropIngestFlow],
            activities=[
                empty_list,
                stub_poll,
                stub_idem,
                stub_content,
                stub_ingest,
                stub_cursor,
            ],
        ),
    ):
        result = await env.client.execute_workflow(
            RaindropIngestFlow.run,
            RaindropIngestInput(),
            id="r-2",
            task_queue="tq",
        )
    assert result["bookmarks"] == 0
    assert result["no_channel"] is True


@pytest.mark.asyncio
async def test_raindrop_content_failure_still_advances():
    """A process_content failure must not block the cursor — bookmarks still
    count toward ``ingested`` so the per-feed cursor advances on the next run.
    """
    _reset()

    @activity.defn(name="process_content")
    async def failing_content(
        url: str,
        title: str,
        category: str,
        fallback_text: str = "",
        extra_tags: list[str] | None = None,
    ) -> dict:
        raise RuntimeError("fetch timeout")

    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue="tq",
            workflows=[RaindropIngestFlow],
            activities=[
                stub_list,
                stub_poll,
                stub_idem,
                failing_content,
                stub_ingest,
                stub_cursor,
            ],
        ),
    ):
        result = await env.client.execute_workflow(
            RaindropIngestFlow.run,
            RaindropIngestInput(),
            id="r-3",
            task_queue="tq",
        )
    assert result["bookmarks"] == 2
    assert result["ingested"] == 2
    # capture_to_inbox isn't registered in this test's stub set, so the
    # activity call raises and ref stays None → capture_failed.
    assert result["todoist_committed"] == 0
    assert result["outbox_staged"] == 0
    assert result["capture_failed"] == 2
