"""InteractionFlow escalation — re-dispatch card with mention until ack."""

from __future__ import annotations

import uuid

from temporalio import activity, workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

with workflow.unsafe.imports_passed_through():
    from aegis_worker.activities.interactions import (
        ApplyTimeoutInput,
        InsertInteractionInput,
        InsertInteractionResult,
        ResolveInteractionInput,
    )
    from aegis_worker.flows.interaction import InteractionFlow, InteractionFlowInput

_calls: dict = {}


def _reset():
    _calls.clear()
    _calls.update({"cards": [], "timeouts": 0, "resolved": 0})


@activity.defn(name="insert_interaction")
async def _insert(input: InsertInteractionInput) -> InsertInteractionResult:
    return InsertInteractionResult(interaction_id="int-1")


@activity.defn(name="send_interaction_card")
async def _card(
    interaction_id: str, agent_id: str, kind: str, prompt: str, options, allow_hint: bool = False
) -> dict:
    _calls["cards"].append(prompt)
    return {"ok": True, "delivery_ref": {"adapter": "web"}}


@activity.defn(name="update_interaction_delivery_ref")
async def _ref(interaction_id: str, delivery_ref: dict) -> None:
    return None


@activity.defn(name="resolve_interaction")
async def _resolve(input: ResolveInteractionInput):
    _calls["resolved"] += 1
    return None


@activity.defn(name="apply_interaction_timeout")
async def _timeout(input: ApplyTimeoutInput) -> None:
    _calls["timeouts"] += 1


_ACTS = [_insert, _card, _ref, _resolve, _timeout]


def _input(**esc) -> InteractionFlowInput:
    return InteractionFlowInput(
        agent_id="pandoras-actor",
        kind="choice",
        origin="test",
        prompt="original prompt",
        options={"a": "A"},
        timeout_seconds=3600,
        timeout_policy="archive",
        metadata={"escalation": esc} if esc else None,
    )


async def _start(input: InteractionFlowInput):
    env = await WorkflowEnvironment.start_time_skipping()
    worker = Worker(
        env.client,
        task_queue=f"esc-{uuid.uuid4()}",
        workflows=[InteractionFlow],
        activities=_ACTS,
    )
    return env, worker, input


async def test_no_escalation_metadata_keeps_single_card():
    _reset()
    env, worker, input = await _start(_input())
    async with env, worker:
        handle = await env.client.start_workflow(
            InteractionFlow.run, input, id=f"i-{uuid.uuid4()}", task_queue=worker.task_queue
        )
        await handle.signal(InteractionFlow.submit_response, {"value": "a"})
        result = await handle.result()
    assert result.status == "resolved"
    assert len(_calls["cards"]) == 1


async def test_escalation_redispatches_with_mention_until_ack():
    _reset()
    env, worker, input = await _start(
        _input(interval_minutes=3, mention_id="U042", max_repeats=10)
    )
    async with env, worker:
        handle = await env.client.start_workflow(
            InteractionFlow.run, input, id=f"i-{uuid.uuid4()}", task_queue=worker.task_queue
        )
        await env.sleep(60 * 7)  # two intervals pass unacked
        await handle.signal(InteractionFlow.submit_response, {"value": "a"})
        result = await handle.result()
    assert result.status == "resolved"
    assert len(_calls["cards"]) == 3  # 1 original + 2 escalations
    assert "<@U042>" in _calls["cards"][1]
    assert "original prompt" in _calls["cards"][1]


async def test_escalation_stops_at_max_repeats_then_times_out():
    _reset()
    env, worker, input = await _start(
        _input(interval_minutes=3, mention_id="U042", max_repeats=2)
    )
    input.timeout_seconds = 1200
    async with env, worker:
        result = await env.client.execute_workflow(
            InteractionFlow.run, input, id=f"i-{uuid.uuid4()}", task_queue=worker.task_queue
        )
    assert result.status == "archived"
    assert len(_calls["cards"]) == 3  # original + exactly max_repeats
    assert _calls["timeouts"] == 1
