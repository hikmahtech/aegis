"""InteractionFlow now dispatches a Telegram card before waiting."""

from __future__ import annotations

import pytest
from temporalio import activity, workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

with workflow.unsafe.imports_passed_through():
    from aegis_worker.activities.interactions import (
        ApplyTimeoutInput,
        InsertInteractionInput,
        InsertInteractionResult,
        ResolveInteractionInput,
        ResolveInteractionResult,
    )
    from aegis_worker.flows.interaction import InteractionFlow, InteractionFlowInput


_send_card_calls: list[tuple] = []
_update_msg_calls: list[tuple] = []
_update_ref_calls: list[tuple] = []


@activity.defn(name="update_interaction_delivery_ref")
async def stub_update_ref(interaction_id: str, delivery_ref: dict) -> None:
    _update_ref_calls.append((interaction_id, delivery_ref))
    return None


@activity.defn(name="insert_interaction")
async def stub_insert(inp: InsertInteractionInput) -> InsertInteractionResult:
    return InsertInteractionResult(interaction_id="ia-test-1")


@activity.defn(name="send_interaction_card")
async def stub_send_card(
    interaction_id: str,
    agent_id: str,
    kind: str,
    prompt: str,
    options: dict | None,
    allow_hint: bool = False,
) -> dict:
    _send_card_calls.append((interaction_id, agent_id, kind, prompt, options))
    return {"ok": True, "message_id": 9999}


@activity.defn(name="update_interaction_message_id")
async def stub_update(interaction_id: str, telegram_message_id: int) -> None:
    _update_msg_calls.append((interaction_id, telegram_message_id))
    return None


@activity.defn(name="resolve_interaction")
async def stub_resolve(inp: ResolveInteractionInput) -> ResolveInteractionResult:
    return ResolveInteractionResult(already_resolved=False)


@activity.defn(name="apply_interaction_timeout")
async def stub_timeout(inp: ApplyTimeoutInput) -> None:
    return None


@pytest.mark.asyncio
async def test_interaction_flow_sends_card_then_waits():
    _send_card_calls.clear()
    _update_msg_calls.clear()
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue="aegis-test",
            workflows=[InteractionFlow],
            activities=[stub_insert, stub_send_card, stub_update, stub_resolve, stub_timeout],
        ),
    ):
        handle = await env.client.start_workflow(
            InteractionFlow.run,
            InteractionFlowInput(
                agent_id="sebas",
                kind="approval",
                origin="test",
                prompt="Reply?",
                options=None,
                timeout_seconds=3600,
                timeout_policy="archive",
            ),
            id="ia-flow-1",
            task_queue="aegis-test",
        )
        await handle.signal(InteractionFlow.submit_response, {"value": "approve"})
        result = await handle.result()
        assert result.status == "resolved"
        assert result.response == {"value": "approve"}
        # The card was dispatched with the right args
        assert len(_send_card_calls) == 1
        call = _send_card_calls[0]
        assert call[0] == "ia-test-1"  # interaction_id
        assert call[1] == "sebas"  # agent_id
        assert call[2] == "approval"  # kind
        # And the message_id was stored back on the row
        assert _update_msg_calls == [("ia-test-1", 9999)]


@pytest.mark.asyncio
async def test_interaction_flow_records_slack_delivery_ref():
    """A Slack card returns a channel-neutral delivery_ref and NO numeric
    message_id — the flow must persist the delivery_ref (via
    update_interaction_delivery_ref) so the delivery watchdog sees the card as
    delivered. Previously only message_id was recorded, so every Slack card
    looked permanently undelivered."""
    _send_card_calls.clear()
    _update_msg_calls.clear()
    _update_ref_calls.clear()
    ref = {"adapter": "slack", "channel": "C1", "ts": "1.1"}

    @activity.defn(name="send_interaction_card")
    async def slack_send_card(
        interaction_id: str,
        agent_id: str,
        kind: str,
        prompt: str,
        options: dict | None,
        allow_hint: bool = False,
    ) -> dict:
        _send_card_calls.append((interaction_id,))
        return {"ok": True, "delivery_ref": ref}

    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue="aegis-test",
            workflows=[InteractionFlow],
            activities=[
                stub_insert,
                slack_send_card,
                stub_update,
                stub_update_ref,
                stub_resolve,
                stub_timeout,
            ],
        ),
    ):
        handle = await env.client.start_workflow(
            InteractionFlow.run,
            InteractionFlowInput(
                agent_id="pandoras-actor",
                kind="choice",
                origin="test",
                prompt="Which repo?",
                options={"a": "x"},
                timeout_seconds=3600,
                timeout_policy="archive",
            ),
            id="ia-flow-slack",
            task_queue="aegis-test",
        )
        await handle.signal(InteractionFlow.submit_response, {"value": "a"})
        result = await handle.result()
        assert result.status == "resolved"
        # delivery_ref persisted; the numeric message_id path was NOT taken
        assert _update_ref_calls == [("ia-test-1", ref)]
        assert _update_msg_calls == []


@pytest.mark.asyncio
async def test_interaction_flow_tolerates_card_failure():
    """If send_interaction_card raises, flow still completes (don't drop interaction)."""
    _send_card_calls.clear()
    _update_msg_calls.clear()
    fail_count = []

    @activity.defn(name="send_interaction_card")
    async def failing_send_card(
        interaction_id: str,
        agent_id: str,
        kind: str,
        prompt: str,
        options: dict | None,
        allow_hint: bool = False,
    ) -> dict:
        fail_count.append(1)
        raise RuntimeError("telegram service down")

    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue="aegis-test",
            workflows=[InteractionFlow],
            activities=[stub_insert, failing_send_card, stub_update, stub_resolve, stub_timeout],
        ),
    ):
        handle = await env.client.start_workflow(
            InteractionFlow.run,
            InteractionFlowInput(
                agent_id="sebas",
                kind="approval",
                origin="test",
                prompt="Reply?",
                options=None,
                timeout_seconds=3600,
                timeout_policy="archive",
            ),
            id="ia-flow-2",
            task_queue="aegis-test",
        )
        await handle.signal(InteractionFlow.submit_response, {"value": "approve"})
        result = await handle.result()
        assert result.status == "resolved"
        # Prove the card path was actually exercised (not skipped). The
        # best-effort retry policy is 2 attempts, so we expect 2 failures
        # before the workflow gives up and proceeds.
        assert len(fail_count) == 2
        # And that update was NOT called when card failed
        assert _update_msg_calls == []
