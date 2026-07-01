"""InteractionFlow — man-in-the-middle handoff to a human.

Creates an `interactions` row, awaits a `submit_response` signal or a
timeout. On timeout, applies one of two policies: `archive` (row becomes
`archived`, flow returns status `archived`) or `hold` (the timeout is
ignored and the flow blocks until a signal arrives).

The historical `auto_reject` / `auto_approve` policies were removed
2026-05-28 after a DB sweep found zero callers in 30 days. If you need
"timeout = soft-reject", spawn the flow with `archive` and have the
parent treat the `archived` status as "no answer".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    from aegis_worker.activities.interactions import (
        ApplyTimeoutInput,
        InsertInteractionInput,
        InsertInteractionResult,
        ResolveInteractionInput,
    )
    from aegis_worker.shared.retry import ACT_RETRY


@dataclass
class InteractionFlowInput:
    agent_id: str
    kind: str
    origin: str
    prompt: str
    options: dict[str, Any] | None = None
    timeout_seconds: int = 86400
    timeout_policy: str = "archive"
    allow_hint: bool = False
    # Phase 4: optional fire-and-forget post-resolution hook. After
    # resolve_interaction succeeds (signal or auto-policy), call this
    # activity with args=[interaction_id, response, metadata]. Lets
    # ClarifyFlow spawn this workflow ABANDONED + still dispatch a
    # follow-up action when the user picks an option. AlertInvestigation
    # and other parent-await callers leave these as None.
    metadata: dict[str, Any] | None = None
    post_resolve_activity: str | None = None


@dataclass
class InteractionResult:
    interaction_id: str
    status: str
    response: dict[str, Any] | None


# 2 attempts (was 1): used by both `update_interaction_message_id` (the
# best-effort bridge that lets the bot edit-in-place after a user clicks a
# callback) and `post_resolve_activity` (which often does Todoist label
# updates that hit transient 5xx). One retry is cheap insurance; the
# exception-swallow path in the flow still catches a persistent failure.
_BEST_EFFORT_RETRY = RetryPolicy(maximum_attempts=2)
_ACT_TIMEOUT = timedelta(seconds=30)


@workflow.defn(name="InteractionFlow")
class InteractionFlow:
    def __init__(self) -> None:
        self._response: dict[str, Any] | None = None
        self._resolved: bool = False

    @workflow.signal
    async def submit_response(self, response: dict[str, Any]) -> None:
        if self._resolved:
            return
        self._response = response
        self._resolved = True

    @workflow.run
    async def run(self, input: InteractionFlowInput) -> InteractionResult:
        # Compute the wall-clock deadline so the DB carries a real
        # `timeout_at` for external sweeps (e.g. dashboards / scheduled
        # cleanup). `hold` policy intentionally stores NULL — there is no
        # deadline; the flow waits indefinitely.
        timeout_at = (
            workflow.now() + timedelta(seconds=input.timeout_seconds)
            if input.timeout_policy != "hold"
            else None
        )
        inserted: InsertInteractionResult = await workflow.execute_activity(
            "insert_interaction",
            InsertInteractionInput(
                flow_run_id=workflow.info().workflow_id,
                agent_id=input.agent_id,
                kind=input.kind,
                origin=input.origin,
                prompt=input.prompt,
                options=input.options,
                timeout_policy=input.timeout_policy,
                timeout_at=timeout_at,
                metadata=input.metadata,
            ),
            result_type=InsertInteractionResult,
            retry_policy=ACT_RETRY,
            start_to_close_timeout=_ACT_TIMEOUT,
        )
        interaction_id = inserted.interaction_id

        # Dispatch the interaction card (best-effort — log if it fails, don't
        # drop the interaction). The active comms adapter (Slack) routes by the
        # agent's channel, so no chat/topic override is threaded here.
        try:
            card_result = await workflow.execute_activity(
                "send_interaction_card",
                args=[
                    interaction_id,
                    input.agent_id,
                    input.kind,
                    input.prompt,
                    input.options,
                    input.allow_hint,
                ],
                retry_policy=_BEST_EFFORT_RETRY,
                start_to_close_timeout=_ACT_TIMEOUT,
            )
            # Record how the card was delivered so the DeliveryWatchdog knows it
            # landed. Telegram returns a numeric message_id; Slack (and any
            # channel-neutral adapter) returns a delivery_ref {adapter,channel,ts}
            # with no message_id — persist whichever is present.
            if isinstance(card_result, dict):
                if card_result.get("message_id"):
                    await workflow.execute_activity(
                        "update_interaction_message_id",
                        args=[interaction_id, int(card_result["message_id"])],
                        retry_policy=_BEST_EFFORT_RETRY,
                        start_to_close_timeout=_ACT_TIMEOUT,
                    )
                elif card_result.get("delivery_ref"):
                    await workflow.execute_activity(
                        "update_interaction_delivery_ref",
                        args=[interaction_id, card_result["delivery_ref"]],
                        retry_policy=_BEST_EFFORT_RETRY,
                        start_to_close_timeout=_ACT_TIMEOUT,
                    )
        except Exception as exc:
            workflow.logger.warning("interaction_card_dispatch_failed: %s", str(exc)[:200])

        if input.timeout_policy == "hold":
            await workflow.wait_condition(lambda: self._resolved)
        else:
            try:
                await workflow.wait_condition(
                    lambda: self._resolved,
                    timeout=timedelta(seconds=input.timeout_seconds),
                )
            except TimeoutError:
                await workflow.execute_activity(
                    "apply_interaction_timeout",
                    ApplyTimeoutInput(interaction_id=interaction_id, policy=input.timeout_policy),
                    retry_policy=ACT_RETRY,
                    start_to_close_timeout=_ACT_TIMEOUT,
                )
                if input.timeout_policy == "archive":
                    return InteractionResult(
                        interaction_id=interaction_id, status="archived", response=None
                    )
                raise ApplicationError(f"unknown timeout_policy: {input.timeout_policy}") from None

        await workflow.execute_activity(
            "resolve_interaction",
            ResolveInteractionInput(interaction_id=interaction_id, response=self._response or {}),
            retry_policy=ACT_RETRY,
            start_to_close_timeout=_ACT_TIMEOUT,
        )
        # Phase 4: fire post-resolve hook. Best-effort — one attempt; on
        # failure the interaction is already resolved so user sees nothing
        # happens. Logged for follow-up.
        if input.post_resolve_activity:
            try:
                await workflow.execute_activity(
                    input.post_resolve_activity,
                    args=[interaction_id, self._response or {}, input.metadata or {}],
                    retry_policy=_BEST_EFFORT_RETRY,
                    start_to_close_timeout=_ACT_TIMEOUT,
                )
            except Exception as exc:  # noqa: BLE001
                workflow.logger.warning(
                    "interaction_post_resolve_failed activity=%s id=%s err=%s",
                    input.post_resolve_activity,
                    interaction_id,
                    str(exc)[:200],
                )
        return InteractionResult(
            interaction_id=interaction_id,
            status="resolved",
            response=self._response,
        )
