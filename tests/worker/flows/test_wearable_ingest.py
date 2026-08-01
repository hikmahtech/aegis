"""WearableIngestFlow — channel fan-out and the cursor-advance rule (B7).

Activity stubs only: the HTTP and DB halves are covered in
tests/worker/activities/test_wearable.py. What is under test here is the
decision the flow makes — when a run is reported as unconfigured, and when the
channel cursor is allowed to move.
"""

from __future__ import annotations

import pytest
from temporalio import activity, workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

with workflow.unsafe.imports_passed_through():
    from aegis_worker.activities.wearable import (
        PollWearableInput,
        PollWearableResult,
        RecordWearableInput,
        RecordWearableResult,
    )
    from aegis_worker.flows.wearable_ingest import WearableIngestFlow, WearableIngestInput


_calls: dict[str, list] = {"list": [], "poll": [], "write": [], "cursor": []}


def _reset() -> None:
    for v in _calls.values():
        v.clear()


def _channel(identifier="oura", cursor=None) -> dict:
    return {
        "id": "ch-w1",
        "kind": "wearable",
        "identifier": identifier,
        "config": {"last_cursor": cursor, "agent_id": "sebas"},
        "active": True,
    }


@activity.defn(name="list_active_channels")
async def stub_list(kind: str) -> list[dict]:
    _calls["list"].append(kind)
    return [_channel(cursor="2026-07-28")]


@activity.defn(name="list_active_channels")
async def stub_list_empty(kind: str) -> list[dict]:
    _calls["list"].append(kind)
    return []


@activity.defn(name="poll_wearable")
async def stub_poll(inp: PollWearableInput) -> PollWearableResult:
    _calls["poll"].append((inp.vendor, inp.since_cursor))
    return PollWearableResult(
        status="ok",
        errors=0,
        records=[
            {
                "external_id": "sleep-30",
                "metric": "sleep_score",
                "value": 78.0,
                "day": "2026-07-30",
                "observed_at": "2026-07-30T07:30:00+00:00",
                "metadata": {},
            }
        ],
    )


@activity.defn(name="record_wearable_observations")
async def stub_write(inp: RecordWearableInput) -> RecordWearableResult:
    _calls["write"].append((inp.source, len(inp.records)))
    return RecordWearableResult(written=1, duplicates=0, failed=0, latest_resolved_day="2026-07-30")


@activity.defn(name="update_channel_config_key")
async def stub_cursor(kind, identifier, key, value) -> None:
    _calls["cursor"].append((kind, identifier, key, value))


async def _run(activities: list, wf_id: str) -> dict:
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue="tq",
            workflows=[WearableIngestFlow],
            activities=activities,
        ),
    ):
        return await env.client.execute_workflow(
            WearableIngestFlow.run,
            WearableIngestInput(),
            id=wf_id,
            task_queue="tq",
        )


@pytest.mark.asyncio
async def test_no_active_channel_reports_no_channel_and_never_polls():
    """An unconfigured install must READ as unconfigured. `no_channel` in
    result_summary is the difference between "nobody turned it on" and "the
    vendor had nothing", which is exactly the distinction the owner_emails
    silent no-op lacked."""
    _reset()
    result = await _run([stub_list_empty, stub_poll, stub_write, stub_cursor], "wear-none")
    assert result["status"] == "no_channel"
    assert result["vendors"] == 0
    assert result["per_vendor"] == []
    assert _calls["poll"] == [], "polled the vendor with no channel configured"


@pytest.mark.asyncio
async def test_happy_path_writes_and_advances_the_cursor():
    _reset()
    result = await _run([stub_list, stub_poll, stub_write, stub_cursor], "wear-ok")
    assert _calls["list"] == ["wearable"]
    # The channel's stored cursor is what gets polled from.
    assert _calls["poll"] == [("oura", "2026-07-28")]
    assert _calls["write"] == [("oura", 1)]
    assert result["written"] == 1 and result["status"] == "ok"
    assert _calls["cursor"] == [("wearable", "oura", "last_cursor", "2026-07-30")]


@pytest.mark.asyncio
async def test_token_missing_is_reported_and_leaves_the_cursor_alone():
    _reset()

    @activity.defn(name="poll_wearable")
    async def unconfigured(inp: PollWearableInput) -> PollWearableResult:
        return PollWearableResult(status="token_missing", detail="oura_api_token is not configured")

    result = await _run([stub_list, unconfigured, stub_write, stub_cursor], "wear-token")
    assert result["skipped"] == 1
    assert result["per_vendor"][0]["status"] == "token_missing"
    assert "oura_api_token" in result["per_vendor"][0]["detail"]
    assert _calls["write"] == []
    assert _calls["cursor"] == []


@pytest.mark.asyncio
async def test_fetch_failed_is_a_skipped_vendor_not_a_failed_run():
    _reset()

    @activity.defn(name="poll_wearable")
    async def broken(inp: PollWearableInput) -> PollWearableResult:
        return PollWearableResult(status="fetch_failed", errors=3, detail="daily_sleep: HTTPError")

    result = await _run([stub_list, broken, stub_write, stub_cursor], "wear-fetchfail")
    assert result["skipped"] == 1
    assert result["per_vendor"][0]["status"] == "fetch_failed"
    assert _calls["cursor"] == []


@pytest.mark.asyncio
async def test_a_partially_failed_poll_does_not_advance_the_cursor():
    """One endpoint 429'd, the rest returned data that wrote fine. Advancing
    the cursor here would step over the days the dead endpoint was carrying and
    lose them permanently — so the write happens and the cursor does not."""
    _reset()

    @activity.defn(name="poll_wearable")
    async def partial(inp: PollWearableInput) -> PollWearableResult:
        base = await stub_poll(inp)
        return PollWearableResult(status="ok", errors=1, records=base.records, detail="daily_sleep")

    result = await _run([stub_list, partial, stub_write, stub_cursor], "wear-partial")
    assert result["written"] == 1
    assert result["per_vendor"][0]["endpoint_errors"] == 1
    assert result["per_vendor"][0]["cursor"] is None
    assert _calls["cursor"] == [], "cursor advanced past an endpoint we never read"


@pytest.mark.asyncio
async def test_unresolved_write_day_does_not_advance_the_cursor():
    _reset()

    @activity.defn(name="record_wearable_observations")
    async def all_failed(inp: RecordWearableInput) -> RecordWearableResult:
        return RecordWearableResult(written=0, duplicates=0, failed=1, latest_resolved_day=None)

    result = await _run([stub_list, stub_poll, all_failed, stub_cursor], "wear-writefail")
    assert result["failed"] == 1
    assert _calls["cursor"] == []


@pytest.mark.asyncio
async def test_a_crashing_poll_activity_skips_the_vendor_instead_of_failing_the_run():
    _reset()

    @activity.defn(name="poll_wearable")
    async def exploding(inp: PollWearableInput) -> PollWearableResult:
        raise RuntimeError("vendor DNS gone")

    result = await _run([stub_list, exploding, stub_write, stub_cursor], "wear-crash")
    assert result["skipped"] == 1
    assert result["per_vendor"][0]["status"] == "poll_failed"
    assert _calls["cursor"] == []


@pytest.mark.asyncio
async def test_multiple_vendors_each_get_their_own_cursor():
    _reset()

    @activity.defn(name="list_active_channels")
    async def two_vendors(kind: str) -> list[dict]:
        return [_channel("oura", "2026-07-28"), _channel("whoop", None)]

    @activity.defn(name="record_wearable_observations")
    async def per_source(inp: RecordWearableInput) -> RecordWearableResult:
        day = "2026-07-30" if inp.source == "oura" else "2026-07-31"
        return RecordWearableResult(written=1, latest_resolved_day=day)

    result = await _run([two_vendors, stub_poll, per_source, stub_cursor], "wear-two")
    assert result["vendors"] == 2
    assert _calls["cursor"] == [
        ("wearable", "oura", "last_cursor", "2026-07-30"),
        ("wearable", "whoop", "last_cursor", "2026-07-31"),
    ]


def test_the_seed_row_maps_through_the_registry_to_this_flow():
    """`registry.check_registration` already refuses to boot a half-wired flow;
    this pins the other half — that the shipped seed row's `config` actually
    reaches the flow's input, rather than being a key nothing reads."""
    from aegis_worker.registry import activity_type_map, seed_workflow_types

    seed = seed_workflow_types("config/seed")
    assert seed is not None
    assert seed.get("WearableIngestFlow") == ["wearable-ingest-6h"]

    builder = activity_type_map()["WearableIngestFlow"]
    flow_cls, config = builder(
        {"agent_id": "sebas", "config": {"lookback_days": 3}, "_settings": {}}
    )
    assert flow_cls is WearableIngestFlow
    assert config == WearableIngestInput(agent_id="sebas", lookback_days=3)
