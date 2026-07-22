"""CalendarIngestFlow tests."""

from __future__ import annotations

import pytest
from temporalio import activity, workflow
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

with workflow.unsafe.imports_passed_through():
    from aegis_worker.activities.calendar import FetchEventsInput, FetchEventsResult
    from aegis_worker.activities.interactions import (
        ApplyTimeoutInput,
        InsertInteractionInput,
        InsertInteractionResult,
        ResolveInteractionInput,
        ResolveInteractionResult,
    )
    from aegis_worker.flows.calendar_ingest import CalendarIngestFlow, CalendarIngestInput
    from aegis_worker.flows.interaction import InteractionFlow


_calls: dict[str, list] = {
    "list": [],
    "fetch": [],
    "to_content": [],
    "ingest": [],
    "cursor": [],
}


def _reset() -> None:
    for v in _calls.values():
        v.clear()


@activity.defn(name="list_active_channels")
async def stub_list(kind: str) -> list[dict]:
    _calls["list"].append(kind)
    return [
        {
            "id": "ch-1",
            "kind": "email",
            "identifier": "a@b.com",
            "config": {"label": "sebas", "calendar_last_cursor_ts": None},
            "active": True,
        },
        {
            "id": "ch-2",
            "kind": "email",
            "identifier": "c@d.com",
            "config": {"label": "personal", "calendar_last_cursor_ts": None},
            "active": True,
        },
    ]


@activity.defn(name="fetch_events")
async def stub_fetch(inp: FetchEventsInput) -> FetchEventsResult:
    _calls["fetch"].append(inp.account_label)
    return FetchEventsResult(
        events=[
            {"id": f"e1-{inp.account_label}", "summary": "Standup"},
            {"id": f"e2-{inp.account_label}", "summary": "Lunch"},
        ],
        latest_updated_ts="2026-04-18T12:00:00Z",
    )


@activity.defn(name="events_to_content")
async def stub_to_content(events: list[dict]) -> list[dict]:
    _calls["to_content"].append(len(events))
    return [
        {
            "url": f"calendar://{e['id']}",
            "title": e.get("summary", "Untitled"),
            "source_type": "calendar",
            "raw_text": e.get("summary", ""),
            "summary": "",
            "tags": ["calendar", "event"],
        }
        for e in events
    ]


@activity.defn(name="ingest_content")
async def stub_ingest(content: dict) -> dict:
    _calls["ingest"].append(1)
    return {"status": "ok", "content_id": "test"}



@activity.defn(name="update_channel_config_key")
async def stub_cursor(kind: str, identifier: str, key: str, value: str) -> None:
    _calls["cursor"].append((identifier, key, value))


ALL_STUBS = [stub_list, stub_fetch, stub_to_content, stub_ingest, stub_cursor]


@pytest.mark.asyncio
async def test_calendar_ingest_2_accounts():
    _reset()
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue="tq",
            workflows=[CalendarIngestFlow],
            activities=ALL_STUBS,
        ),
    ):
        result = await env.client.execute_workflow(
            CalendarIngestFlow.run,
            CalendarIngestInput(agent_id="sebas"),
            id="cal-1",
            task_queue="tq",
        )

    assert result["events"] == 4  # 2 per account x 2 accounts
    assert result["content"] == 4
    assert result["ingested"] == 4
    assert result["errors"] == 0
    assert len(_calls["fetch"]) == 2
    assert len(_calls["ingest"]) == 4  # one ingest call per content item
    assert len(_calls["cursor"]) == 2
    assert all(c[1] == "calendar_last_cursor_ts" for c in _calls["cursor"])


@pytest.mark.asyncio
async def test_fetch_failure_skips_account():
    _reset()

    @activity.defn(name="fetch_events")
    async def failing_fetch(inp: FetchEventsInput) -> FetchEventsResult:
        if inp.account_label == "personal":
            raise RuntimeError("boom")
        return FetchEventsResult(
            events=[{"id": "e1", "summary": "A"}],
            latest_updated_ts="2026-04-18T12:00:00Z",
        )

    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue="tq",
            workflows=[CalendarIngestFlow],
            activities=[stub_list, failing_fetch, stub_to_content, stub_ingest, stub_cursor],
        ),
    ):
        result = await env.client.execute_workflow(
            CalendarIngestFlow.run,
            CalendarIngestInput(),
            id="cal-2",
            task_queue="tq",
        )

    assert result["errors"] == 1  # personal failed
    assert result["events"] == 1  # only sebas' 1 event
    assert result["ingested"] == 1
    # Labeled so a soft-failing account is visible instead of hiding behind
    # an unlabeled `errors: 1` (issue #120).
    assert result["errors_by_account"] == {"personal": 1}


@pytest.mark.asyncio
async def test_no_channels_returns_zeros():
    _reset()

    @activity.defn(name="list_active_channels")
    async def empty_list(kind: str) -> list[dict]:
        return []

    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue="tq",
            workflows=[CalendarIngestFlow],
            activities=[empty_list, stub_fetch, stub_to_content, stub_ingest, stub_cursor],
        ),
    ):
        result = await env.client.execute_workflow(
            CalendarIngestFlow.run,
            CalendarIngestInput(),
            id="cal-3",
            task_queue="tq",
        )

    assert result == {"events": 0, "content": 0, "ingested": 0, "errors": 0}
    assert _calls["fetch"] == []


_cal_auth_count: list[int] = [0]


@pytest.mark.asyncio
async def test_calendar_auth_expired_spawns_interaction_flow_and_retries():
    """First fetch_events raises gmail_auth_expired → InteractionFlow(ack)
    spawned → resolved → fetch retried once.

    Calendar reuses the Gmail OAuth token, so the same reauth UX applies.
    Regression lock for the prior catch-all `except Exception` that swallowed
    auth errors silently — calendar accounts would just stop syncing.
    """
    import asyncio

    _reset()
    _cal_auth_count[0] = 0
    insert_ia_log: list[dict] = []

    @activity.defn(name="list_active_channels")
    async def single_channel(kind: str) -> list[dict]:
        return [
            {
                "id": "ch-1",
                "kind": "email",
                "identifier": "a@b.com",
                "config": {"label": "sebas", "calendar_last_cursor_ts": None},
                "active": True,
            }
        ]

    @activity.defn(name="fetch_events")
    async def fetch_auth_then_ok(inp: FetchEventsInput) -> FetchEventsResult:
        _cal_auth_count[0] += 1
        if _cal_auth_count[0] == 1:
            raise ApplicationError(
                "gmail_auth_expired:sebas",
                "sebas",
                "http://reauth",
                non_retryable=True,
            )
        return FetchEventsResult(events=[], latest_updated_ts=None)

    @activity.defn(name="insert_interaction")
    async def insert_ia(inp: InsertInteractionInput) -> InsertInteractionResult:
        insert_ia_log.append({"kind": inp.kind, "origin": inp.origin, "options": inp.options})
        return InsertInteractionResult(interaction_id="ia-cal-1")

    @activity.defn(name="send_interaction_card")
    async def send_card(
        interaction_id: str,
        agent_id: str,
        kind: str,
        prompt: str,
        options,
        allow_hint: bool = False,
    ) -> dict:
        return {"ok": True, "message_id": 7}

    @activity.defn(name="resolve_interaction")
    async def resolve(inp: ResolveInteractionInput) -> ResolveInteractionResult:
        return ResolveInteractionResult(already_resolved=False)

    @activity.defn(name="apply_interaction_timeout")
    async def timeout(inp: ApplyTimeoutInput) -> None:
        return None

    async with (
        await WorkflowEnvironment.start_local() as env,
        Worker(
            env.client,
            task_queue="tq",
            workflows=[CalendarIngestFlow, InteractionFlow],
            activities=[
                single_channel,
                fetch_auth_then_ok,
                stub_to_content,
                stub_ingest,
                stub_cursor,
                insert_ia,
                send_card,
                resolve,
                timeout,
            ],
        ),
    ):
        handle = await env.client.start_workflow(
            CalendarIngestFlow.run,
            CalendarIngestInput(
                agent_id="sebas",
                aegis_ui_url="https://x",
            ),
            id="cal-auth-1",
            task_queue="tq",
        )

        child_id = "calendar-reauth-sebas-cal-auth-1"
        child_handle = env.client.get_workflow_handle(child_id)

        # Poll until insert_interaction fires (child workflow has spawned)
        for _ in range(100):
            await asyncio.sleep(0.05)
            if insert_ia_log:
                break

        assert insert_ia_log, "insert_interaction was never called for calendar reauth child"
        assert insert_ia_log[0]["kind"] == "ack"
        assert insert_ia_log[0]["origin"] == "calendar_reauth"
        opts = insert_ia_log[0]["options"] or {}
        assert "url" in opts and "{interaction_id}" in opts["url"]
        assert "reauth/sebas/initiate" in opts["url"]

        # Resolve the reauth interaction so the parent retries the fetch
        await child_handle.signal(InteractionFlow.submit_response, {"value": "ack"})

        result = await asyncio.wait_for(handle.result(), timeout=10.0)

    # Account did NOT count as an error (reauth succeeded) and fetch ran twice.
    assert result["errors"] == 0
    assert _cal_auth_count[0] == 2


@pytest.mark.asyncio
async def test_empty_events_skips_content_and_ingest():
    """Account with no events should not call events_to_content or ingest_content."""
    _reset()

    @activity.defn(name="fetch_events")
    async def empty_fetch(inp) -> FetchEventsResult:
        return FetchEventsResult(events=[], latest_updated_ts=None)

    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue="tq",
            workflows=[CalendarIngestFlow],
            activities=[stub_list, empty_fetch, stub_to_content, stub_ingest, stub_cursor],
        ),
    ):
        result = await env.client.execute_workflow(
            CalendarIngestFlow.run,
            CalendarIngestInput(),
            id="cal-4",
            task_queue="tq",
        )

    assert result["events"] == 0
    assert result["content"] == 0
    assert result["ingested"] == 0
    assert _calls["to_content"] == []
    assert _calls["ingest"] == []
    assert _calls["cursor"] == []  # no cursor update when no events
