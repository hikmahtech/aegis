"""AlertInvestigationFlow and the problem hub (PR 3b).

The flow no longer owns an alert's identity. These pin the seams: a caller
that hands over `problem_id` is trusted, a caller that does not gets the hub's
verdict, the verification re-check and the Gate-2 race ask the hub, `Mute 24h`
mutes the problem, and every step lands on the problem's timeline.
"""

from __future__ import annotations

import asyncio

import pytest
from aegis_worker.flows.alert_investigation import AlertInvestigationFlow
from aegis_worker.flows.interaction import InteractionFlow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from tests.worker.flows.test_alert_investigation_gates import (
    _HUB,
    ALL_STUBS,
    _drive_to_gate2,
    _make_alert,
    _reset,
)


async def _run(alert: dict) -> dict:
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue="tq-hub",
            workflows=[AlertInvestigationFlow, InteractionFlow],
            activities=ALL_STUBS,
        ),
    ):
        return await env.client.execute_workflow(
            AlertInvestigationFlow.run, alert, id="hub-flow-test", task_queue="tq-hub"
        )


@pytest.mark.asyncio
async def test_hub_says_no_investigation_so_the_flow_stops():
    """A repeat occurrence: the hub attaches it and the flow returns before
    any ping, delay or investigation."""
    _reset()
    _HUB["investigate"] = False
    result = await _run(_make_alert())
    assert result["status"] == "skipped_by_hub"
    assert result["hub_action"] == "created"
    assert _HUB["ingest"] == [(_make_alert()["fingerprint"], False)]
    assert _HUB["record"] == []


@pytest.mark.asyncio
async def test_caller_supplied_problem_is_trusted_and_self_resolve_asks_the_hub():
    """A producer that already ingested the alert passes `problem_id` and the
    hub's task: the flow ingests nothing, waits the class's verification
    delay, and ends when the hub reports the problem resolved."""
    _reset()
    _HUB["resolved"] = True
    alert = _make_alert(problem_id="prob-given", todoist_task_id="task-given")
    result = await _run(alert)
    assert result["status"] == "self_resolved"
    assert result["problem_id"] == "prob-given"
    assert result["todoist_task_id"] == "task-given"
    assert _HUB["ingest"] == []
    assert _HUB["status"] == ["prob-given"]
    assert [(r["status"], r["external_id"].split(":")[-1]) for r in _HUB["record"]] == [
        ("resolved", "self_resolved")
    ]
    assert _HUB["record"][0]["posted"] is True


@pytest.mark.asyncio
async def test_gate2_mute_24h_mutes_the_problem():
    _reset()
    async with (
        await WorkflowEnvironment.start_local() as env,
        Worker(
            env.client,
            task_queue="tq-gates",
            workflows=[AlertInvestigationFlow, InteractionFlow],
            activities=ALL_STUBS,
        ),
    ):
        wf_id = "gate2-mute-problem-test"
        handle = await env.client.start_workflow(
            AlertInvestigationFlow.run, _make_alert(), id=wf_id, task_queue="tq-gates"
        )
        gate2_handle = await _drive_to_gate2(env, handle, wf_id)
        await gate2_handle.signal(InteractionFlow.submit_response, {"value": "mute_24h"})
        result = await asyncio.wait_for(handle.result(), timeout=15.0)

    assert result["status"] != "gate2_discarded"
    assert _HUB["mute"] == [("prob-1", 24)]


@pytest.mark.asyncio
async def test_every_step_lands_on_the_problem_timeline():
    """investigating → waiting_human (card) → waiting_human (final), each
    keyed on the workflow id and step so a replay records once."""
    _reset()
    async with (
        await WorkflowEnvironment.start_local() as env,
        Worker(
            env.client,
            task_queue="tq-gates",
            workflows=[AlertInvestigationFlow, InteractionFlow],
            activities=ALL_STUBS,
        ),
    ):
        wf_id = "timeline-test"
        handle = await env.client.start_workflow(
            AlertInvestigationFlow.run, _make_alert(), id=wf_id, task_queue="tq-gates"
        )
        gate2_handle = await _drive_to_gate2(env, handle, wf_id)
        await gate2_handle.signal(InteractionFlow.submit_response, {"value": "discard"})
        result = await asyncio.wait_for(handle.result(), timeout=15.0)

    assert result["status"] == "gate2_discarded"
    steps = [(r["status"], r["external_id"]) for r in _HUB["record"]]
    assert steps == [
        ("investigating", f"{wf_id}:investigating"),
        ("waiting_human", f"{wf_id}:gate2"),
        ("waiting_human", f"{wf_id}:discard"),
    ]
    assert all(r["problem_id"] == "prob-1" for r in _HUB["record"])
