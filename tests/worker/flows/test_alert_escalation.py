"""AlertInvestigationFlow escalation — heads-up ping, Gate-2 escalation
metadata, and the self-resolve-during-gate race.

Escalating infra alerts (NodeDown / HeartbeatCollectFailed — Task 4 sets
``alert["escalate"] = True``) get:
  1. an immediate heads-up chat ping at flow start,
  2. a Gate-2 decision card spawned with escalation metadata
     (interval_minutes / mention_id / max_repeats) so InteractionFlow nags
     the owner until they ack, and
  3. that gate raced against ``check_alert_resolved`` every 3 min — if the
     underlying alert self-resolves while awaiting the human, the flow signals
     the card closed and returns ``self_resolved_during_gate``.

Harness copied from test_alert_investigation_gates.py, retargeted to the
escalating infra-alert path (Gate-0 skipped; resolve_infra_resource /
remediate_infra_service instead of resolve_alert_resource).
"""

from __future__ import annotations

import asyncio
import re

import pytest
from temporalio import activity, workflow
from temporalio.api.enums.v1 import EventType
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
    from aegis_worker.flows.alert_investigation import AlertInvestigationFlow
    from aegis_worker.flows.interaction import InteractionFlow


# ---------------------------------------------------------------------------
# Mutable test state
# ---------------------------------------------------------------------------

_calls: dict = {}
_state: dict = {}


def _esc_alert() -> dict:
    return {
        "title": "Swarm node noon down",
        "fingerprint": "aegis-heartbeat:NodeDown:noon",
        "severity": "critical",
        "source": "aegis-heartbeat",
        "labels": {"alertname": "NodeDown", "cluster": "homelab-swarm"},
        "escalate": True,
    }


def _reset(**overrides):
    _hub_reset()
    _calls.clear()
    _state.clear()
    _state.update(
        {
            # Infra-resource shape (resolve_infra_resource path). source=infra so
            # is_infra_alert-driven Gate-0 skip + infra investigation apply.
            "resource_result": {
                "resource_id": "res-homelab",
                "resource_title": "Homelab GitOps",
                "resource_path": "infrastructure/homelab-gitops",
                "github_repo": "youruser/homelab-gitops",
                "confidence": 0.9,
                "source": "infra",
                "resources": [
                    {
                        "resource_id": "res-homelab",
                        "resource_title": "Homelab GitOps",
                        "resource_path": "infrastructure/homelab-gitops",
                        "github_repo": "youruser/homelab-gitops",
                        "confidence": 0.9,
                    }
                ],
            },
            "routing": {"infra_cluster": "homelab-swarm", "slack_owner_member_id": "U042"},
            "knowledge_result": "",
            "run_investigation_result": {
                "status": "succeeded",
                "output": "Root cause: noon NIC flapped",
                "session_id": "sess-1",
                "branch": "aegis-fix/test",
                "branches": {"homelab-gitops": "aegis-fix/test"},
            },
            "investigate_result": {
                "investigation": "LLM-only narrative",
                "actionable": True,
                "auto_fixable": False,
            },
            "assess_result": {
                "status": "actionable",
                "root_cause": "noon NIC flapped",
                "suggested_fix": "check the switch port",
                "confidence": 0.8,
            },
        }
    )
    _state.update(overrides)


# ---------------------------------------------------------------------------
# Stub activities
# ---------------------------------------------------------------------------


@activity.defn(name="resolve_agents")
async def stub_resolve_agents(tags):
    return {t: {"infra": "pandoras-actor"}.get(t) for t in tags}


@activity.defn(name="get_alert_routing_config")
async def stub_get_alert_routing_config() -> dict:
    return _state["routing"]


@activity.defn(name="resolve_infra_resource")
async def stub_resolve_infra_resource(alert: dict) -> dict:
    _calls.setdefault("infra_resource_called", []).append(True)
    return _state["resource_result"]


@activity.defn(name="resolve_alert_resource")
async def stub_resolve_alert_resource(alert: dict) -> dict:
    _calls.setdefault("resource_called", []).append(True)
    return _state["resource_result"]


@activity.defn(name="remediate_infra_service")
async def stub_remediate_infra_service(alert: dict) -> dict:
    # NodeDown is not a remediable swarm-service class → no auto-restart kick.
    _calls.setdefault("remediate_called", []).append(True)
    return {"attempted": False}


@activity.defn(name="score_resource_relevance")
async def stub_score_resource_relevance(alert: dict, resolved_resource_id: str) -> dict:
    return {"confident": True, "resolved_resource_id": resolved_resource_id, "candidates": []}


@activity.defn(name="gather_alert_knowledge")
async def stub_gather_alert_knowledge(title: str, project: str, alert_name: str = "") -> str:
    return _state["knowledge_result"]


@activity.defn(name="investigate")
async def stub_investigate(alert: dict, system_prompt: str) -> dict:
    _calls.setdefault("investigate_called", []).append(True)
    return _state["investigate_result"]


@activity.defn(name="run_investigation")
async def stub_run_investigation(alert: dict, resources: list[dict], runbook: str, *_a) -> dict:
    _calls.setdefault("run_investigation_called", []).append(True)
    return _state["run_investigation_result"]


@activity.defn(name="assess_investigation")
async def stub_assess_investigation(alert: dict, investigation_output: str) -> dict:
    _calls.setdefault("assess_called", []).append(True)
    return _state["assess_result"]


@activity.defn(name="record_verdict_to_kg")
async def stub_record_verdict_to_kg(*args, **kwargs) -> None:
    return None


@activity.defn(name="post_task_note")
async def stub_post_task_note(*args, **kwargs) -> dict:
    _calls.setdefault("notes", []).append(args)
    return {}


@activity.defn(name="send_system_event")
async def stub_send_system_event(msg: str) -> None:
    pass


@activity.defn(name="send_message")
async def stub_send_message(
    agent_id: str, msg: str, chat_id: int, reply_markup: dict | None = None
) -> dict:
    _calls.setdefault("messages", []).append(msg)
    return {"ok": True}


@activity.defn(name="send_voice")
async def stub_send_voice(agent_id: str, text: str) -> dict:
    return {"ok": True}


@activity.defn(name="accumulate_digest_item")
async def stub_accumulate_digest(item: dict) -> None:
    pass


# --- InteractionFlow activities ---


@activity.defn(name="insert_interaction")
async def stub_insert_interaction(inp: InsertInteractionInput) -> InsertInteractionResult:
    _calls.setdefault("insert_inputs", []).append(inp)
    return InsertInteractionResult(interaction_id="ia-gate2-test")


@activity.defn(name="send_interaction_card")
async def stub_send_card(
    interaction_id: str,
    agent_id: str,
    kind: str,
    prompt: str,
    options,
    allow_hint: bool = False,
) -> dict:
    _calls.setdefault("cards", []).append(prompt)
    # Optional hold. Parking the gate child inside its first card dispatch parks
    # it BEFORE it arms its escalation-reminder timer, which is what lets the
    # self-resolve race test below de-alias that timer from the flow's own
    # recheck timer. Absent `card_gate` (every other test) this is a no-op.
    card_gate = _state.get("card_gate")
    if card_gate is not None:
        await card_gate.wait()
    return {"ok": True, "message_id": 42}


@activity.defn(name="resolve_interaction")
async def stub_resolve(inp: ResolveInteractionInput) -> ResolveInteractionResult:
    return ResolveInteractionResult(already_resolved=False)


@activity.defn(name="apply_interaction_timeout")
async def stub_timeout(inp: ApplyTimeoutInput) -> None:
    return None


# ── problem hub stubs (PR 3b) ───────────────────────────────────────────────
# The flow no longer owns an alert's identity: it asks the hub. These stand in
# for HubActivities; `_HUB["resolved"]` makes `problem_status` report the
# problem as resolved, `_HUB["investigate"]` is what `ingest_alert` answers.
_HUB: dict = {
    "ingest": [],
    "status": [],
    "record": [],
    "mute": [],
    "resolved": False,
    "investigate": True,
    "delay": 0,
    # a hub that cannot answer: `problem_status` raises
    "status_raises": False,
}


@activity.defn(name="ingest_alert")
async def stub_ingest_alert(alert: dict, resolved: bool = False) -> dict:
    _HUB["ingest"].append((alert.get("fingerprint"), resolved))
    return {
        "problem_id": "prob-1",
        "action": "created",
        "key": "k",
        "occurrences": 1,
        "suppressed": False,
        "investigate": _HUB["investigate"],
        "todoist_task_id": alert.get("todoist_task_id") or "task-hub-1",
    }


@activity.defn(name="problem_status")
async def stub_problem_status(problem_id: str) -> dict:
    _HUB["status"].append(problem_id)
    if _HUB["status_raises"]:
        raise RuntimeError("hub unreachable")
    return {
        "found": True,
        "status": "resolved" if _HUB["resolved"] else "open",
        "resolved": _HUB["resolved"],
        "occurrences": 1,
        "todoist_task_id": "task-hub-1",
    }


@activity.defn(name="record_investigation")
async def stub_record_investigation(inp: dict) -> dict:
    _HUB["record"].append(inp)
    return {"recorded": True, "status_changed": True}


@activity.defn(name="mute_problem")
async def stub_mute_problem(problem_id: str, hours: float, by: str = "") -> dict:
    _HUB["mute"].append((problem_id, hours))
    return {"muted_until": "2026-09-08T12:00:00+00:00"}


@activity.defn(name="verification_delay")
async def stub_verification_delay(alert: dict) -> dict:
    return {"delay_seconds": _HUB["delay"]}


def _hub_reset() -> None:
    for key in ("ingest", "status", "record", "mute"):
        _HUB[key].clear()
    _HUB["resolved"] = False
    _HUB["investigate"] = True
    _HUB["delay"] = 0
    _HUB["status_raises"] = False


ALL_STUBS = [
    stub_ingest_alert,
    stub_problem_status,
    stub_record_investigation,
    stub_mute_problem,
    stub_verification_delay,
    stub_resolve_agents,
    stub_get_alert_routing_config,
    stub_resolve_infra_resource,
    stub_resolve_alert_resource,
    stub_remediate_infra_service,
    stub_score_resource_relevance,
    stub_gather_alert_knowledge,
    stub_investigate,
    stub_run_investigation,
    stub_assess_investigation,
    stub_record_verdict_to_kg,
    stub_post_task_note,
    stub_send_system_event,
    stub_send_message,
    stub_send_voice,
    stub_accumulate_digest,
    stub_insert_interaction,
    stub_send_card,
    stub_resolve,
    stub_timeout,
]


_SAFE_FINGERPRINT = re.sub(r"[^a-zA-Z0-9._\-]", "-", "aegis-heartbeat:NodeDown:noon")[:60]


async def _wait_for_gate2(poll) -> None:
    """Poll until Gate-2's insert_interaction fired (the first + only insert on
    the infra path, since Gate-0 is skipped for infra alerts)."""
    for _ in range(400):
        if _calls.get("insert_inputs"):
            return
        await poll()
    raise AssertionError("Gate 2 child never started")


async def _wait_for_recheck_timer(handle) -> None:
    """Poll (in real time — no clock skipping) until the flow has armed the
    Gate-2 recheck timer, i.e. a TIMER_STARTED recorded after the gate child was
    started. Establishing that before skipping time is what makes the
    de-aliasing in the self-resolve race test below deterministic."""
    for _ in range(400):
        seen_child = False
        async for ev in handle.fetch_history_events():
            if ev.event_type == EventType.EVENT_TYPE_START_CHILD_WORKFLOW_EXECUTION_INITIATED:
                seen_child = True
            elif seen_child and ev.event_type == EventType.EVENT_TYPE_TIMER_STARTED:
                return
        await asyncio.sleep(0.05)
    raise AssertionError("flow never armed its Gate-2 recheck timer")


# ---------------------------------------------------------------------------
# Test 1: heads-up ping + Gate-2 escalation metadata
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_escalating_alert_sends_heads_up_and_escalation_metadata():
    """Escalating NodeDown alert fires an immediate heads-up chat ping and
    spawns Gate 2 carrying escalation metadata (3-min interval, owner mention
    from routing.slack_owner_member_id)."""
    _reset()

    async with (
        await WorkflowEnvironment.start_local() as env,
        Worker(
            env.client,
            task_queue="tq-esc",
            workflows=[AlertInvestigationFlow, InteractionFlow],
            activities=ALL_STUBS,
        ),
    ):
        wf_id = "esc-metadata-test"
        handle = await env.client.start_workflow(
            AlertInvestigationFlow.run,
            _esc_alert(),
            id=wf_id,
            task_queue="tq-esc",
        )

        await _wait_for_gate2(lambda: asyncio.sleep(0.05))

        gate2_id = f"gate2-{_SAFE_FINGERPRINT}-{wf_id}"
        gate2_handle = env.client.get_workflow_handle(gate2_id)
        await gate2_handle.signal(InteractionFlow.submit_response, {"value": "ack"})

        result = await asyncio.wait_for(handle.result(), timeout=15.0)

    assert result["status"] != "gate2_discarded"
    # Heads-up ping fired at start (before the Gate-2 verdict ping).
    assert any("noon" in m for m in _calls["messages"]), (
        f"heads-up ping never fired: {_calls.get('messages')}"
    )
    gate_insert = _calls["insert_inputs"][-1]
    assert gate_insert.metadata["escalation"]["interval_minutes"] == 3
    assert gate_insert.metadata["escalation"]["mention_id"] == "U042"
    assert gate_insert.metadata["escalation"]["max_repeats"] == 10


# ---------------------------------------------------------------------------
# Test 2: self-resolve race closes the gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gate2_self_resolve_race_closes_gate():
    """While the escalating Gate-2 card awaits a human, the alert self-resolves.
    The flow's 3-min race detects it via the hub (problem_status), signals the card
    closed, and returns self_resolved_during_gate — no human answer needed.

    De-aliased on purpose (aegis#190). The flow rechecks the alert every 180s and
    also hands InteractionFlow an escalation interval of 3 min — the same period,
    both armed at the same virtual instant when Gate 2 opens. Under the
    time-skipping test server "the same instant" is exact, so every recheck lands
    together with a reminder timer, and roughly one run in four the gate child's
    resulting activation (cancel the pending reminder timer + complete the
    workflow) is rejected by the test server's history builder with "invalid
    history builder state for action". Its workflow task then never completes,
    the parent waits on a child that can never finish, and the test wedges
    forever — which is how a leaked temporal-test-server got orphaned in the
    first place. So: park the gate child inside its first card dispatch, i.e.
    before it arms the reminder timer, skip 10s, and only then let it continue.
    The reminder is now due at t0+190 while the flow's recheck fires at t0+180,
    and the two never share an instant. Assertions are unchanged — this only
    controls *when* the two timers are armed, not what the flow is asked to do.
    """
    _reset()
    _state["card_gate"] = asyncio.Event()

    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue="tq-esc",
            workflows=[AlertInvestigationFlow, InteractionFlow],
            activities=ALL_STUBS,
        ),
    ):
        handle = await env.client.start_workflow(
            AlertInvestigationFlow.run,
            _esc_alert(),
            id="esc-self-resolve-test",
            task_queue="tq-esc",
        )

        # Let the flow reach Gate 2 (verification delay is 0 → fast). Poll in
        # real time: the child is held in send_interaction_card, so the clock
        # must not move until both sides are where we want them.
        await _wait_for_gate2(lambda: asyncio.sleep(0.05))
        await _wait_for_recheck_timer(handle)

        # Recheck timer armed at t0, reminder timer not armed yet → skip 10s
        # (well inside InteractionFlow's 30s activity start-to-close) and let the
        # child arm its reminder at t0+10.
        await env.sleep(10)
        _state["card_gate"].set()

        # The alert recovers while we await the human decision.
        _HUB["resolved"] = True
        await env.sleep(185)  # cross the t0+180 recheck, stop short of t0+190

        # Bounded: a wedged workflow must fail this test from INSIDE the
        # coroutine, so the `async with` unwinds and shuts the test server down.
        # Letting pytest-timeout fire instead abandons the context manager and
        # orphans the server (aegis#190).
        result = await asyncio.wait_for(handle.result(), timeout=30.0)

    assert result["status"] == "self_resolved_during_gate"


# ---------------------------------------------------------------------------
# Test 3: a raising recheck must not kill the pending gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gate2_recheck_exception_does_not_kill_gate():
    """A raising problem_status recheck (e.g. the activity exhausted its
    retries against a transient DB/API failure) must be treated as "not
    resolved yet" and the race loop keeps waiting — never propagate and kill
    the pending gate."""
    _reset()
    _HUB["status_raises"] = True

    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue="tq-esc",
            workflows=[AlertInvestigationFlow, InteractionFlow],
            activities=ALL_STUBS,
        ),
    ):
        handle = await env.client.start_workflow(
            AlertInvestigationFlow.run,
            _esc_alert(),
            id="esc-recheck-exception-test",
            task_queue="tq-esc",
        )

        await _wait_for_gate2(lambda: env.sleep(1))

        # Cross one 180s race tick while every recheck raises — the gate must
        # still be alive (not killed by a propagated exception) afterwards.
        await env.sleep(200)

        gate2_id = f"gate2-{_SAFE_FINGERPRINT}-esc-recheck-exception-test"
        gate2_handle = env.client.get_workflow_handle(gate2_id)
        await gate2_handle.signal(InteractionFlow.submit_response, {"value": "ack"})

        result = await asyncio.wait_for(handle.result(), timeout=15.0)

    assert result["status"] != "gate2_discarded"
    assert _HUB["status"], "recheck must have been attempted at least once"


# ---------------------------------------------------------------------------
# Test 4: escalating alert on a signature-dedup hit attaches and CONTINUES
# ---------------------------------------------------------------------------


