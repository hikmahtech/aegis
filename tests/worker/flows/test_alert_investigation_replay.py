"""AlertInvestigationFlow replays its own histories (#500/#501).

A run can wait up to 48 hours on its Gate-2 card, so some are always in
flight when the worker is redeployed, and the worker replays each one's
history through the code it now runs. A single command issued differently
from the recorded history wedges the run.

The scenarios below walk the command sequences the card paths take: a verdict
with nothing to decide, with and without proposed commands; a repeat
automatic restart; and a first restart.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from aegis_worker.flows.alert_investigation import AlertInvestigationFlow
from temporalio import workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

from tests.worker.flows import _alert_flow_harness as h

with workflow.unsafe.imports_passed_through():
    from aegis_worker.flows.interaction import InteractionFlowInput, InteractionResult


@workflow.defn(name="InteractionFlow", sandboxed=False)
class OpenCard:
    """A Gate-2 card nobody has answered yet."""

    @workflow.signal
    async def submit_response(self, response: dict) -> None:
        return None

    @workflow.run
    async def run(self, input: InteractionFlowInput) -> InteractionResult:
        h.S.cards.append(input)
        await workflow.wait_condition(lambda: False)
        raise AssertionError("unreachable")


async def _record(flow_cls: type, alert: dict, *, card_cls: type = h.FakeInteractionFlow):
    """Run `alert` through `flow_cls` and return its history. With an open
    card the history is cut where the run waits on it, which is where every
    run in flight across a deploy is."""
    tq = h.task_queue()
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(env.client, task_queue=tq, workflows=[flow_cls, card_cls], activities=h.STUBS),
    ):
        handle = await env.client.start_workflow(
            "AlertInvestigationFlow", alert, id=f"replay-{uuid.uuid4().hex[:8]}", task_queue=tq
        )
        if card_cls is OpenCard:
            for _ in range(400):
                history = await handle.fetch_history()
                if any(e.HasField("child_workflow_execution_started_event_attributes") for e in history.events):
                    break
                await asyncio.sleep(0.05)
            else:
                raise AssertionError("the card never opened")
            await handle.terminate("recorded")
            return history
        await handle.result()
        return await handle.fetch_history()


async def _replays(history) -> None:
    # Raises on any command the new flow issues differently from the history.
    await Replayer(workflows=[AlertInvestigationFlow]).replay_workflow(history)


@pytest.mark.parametrize(
    "scenario",
    ["no_card", "no_card_with_commands", "restart_repeat", "first_restart"],
)
async def test_the_new_flow_replays_its_own_histories(scenario):
    h.reset()
    if scenario == "no_card":
        alert = h.app_alert()
    elif scenario == "no_card_with_commands":
        h.S.run_investigation = {
            **h.S.run_investigation,
            "output": "Unclear.\n\nPROPOSED_COMMANDS:\n- docker service ps shop_web\n",
        }
        h.S.verdict = {**h.S.verdict, "status": "inconclusive"}
        alert = h.app_alert(
            title="Host out of memory", source="alertmanager", labels={"alertname": "HostOutOfMemory"}
        )
    else:
        alert = h.service_down_alert()
        if scenario == "restart_repeat":
            h.S.restart_history = {
                "repeat": True,
                "window_minutes": 60,
                "service": "shop_web",
                "restarted_at": "2026-09-11T12:31:15+00:00",
                "minutes_ago": 10.0,
                "command": "docker service update --force shop_web",
                "recovered": True,
                "diagnostics_then": [],
                "diagnostics_now": [],
                "new_tasks": [],
            }
        else:
            h.S.remediation = {
                "attempted": True,
                "recovered": True,
                "service": "shop_web",
                "command": "docker service update --force shop_web",
                "output": "",
                "reason": "recovered",
                "diagnostics": [],
            }
    history = await _record(AlertInvestigationFlow, alert)
    await _replays(history)
