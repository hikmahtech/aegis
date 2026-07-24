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
    _calls.clear()
    _state.clear()
    _state.update(
        {
            "muted": False,
            "check_dedup_result": {"is_duplicate": False},
            "delay_result": {"delay_seconds": 0, "reason": "test"},
            "resolved_check_result": {"resolved": False},
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


@activity.defn(name="check_alert_mute")
async def stub_check_alert_mute(inp) -> bool:
    _calls.setdefault("mute_check", []).append(inp)
    return _state.get("muted", False)


@activity.defn(name="write_alert_mute")
async def stub_write_alert_mute(inp) -> None:
    _calls.setdefault("mute_write", []).append(inp)


@activity.defn(name="check_dedup")
async def stub_check_dedup(fingerprint: str, hours: int) -> dict:
    return _state["check_dedup_result"]


@activity.defn(name="find_open_task_for_signature")
async def stub_find_open_task_for_signature(signature: str) -> str | None:
    return _state.get("open_task_for_signature")


@activity.defn(name="record_signature_new_task")
async def stub_record_signature_new_task(signature: str, task_id: str) -> None:
    return None


@activity.defn(name="record_signature_recurrence")
async def stub_record_signature_recurrence(signature: str) -> None:
    return None


@activity.defn(name="capture_to_inbox")
async def stub_capture_to_inbox(
    project: str, external_id: str, title: str, description: str, labels: list[str]
) -> str:
    _calls.setdefault("capture", []).append(external_id)
    return "task-esc-1"


@activity.defn(name="get_verification_delay")
async def stub_get_verification_delay(alert: dict) -> dict:
    _calls.setdefault("delay_called", []).append(True)
    return _state["delay_result"]


@activity.defn(name="check_alert_resolved")
async def stub_check_alert_resolved(
    fingerprint: str, window_minutes: int, since_iso: str = ""
) -> dict:
    _calls.setdefault("resolved_checks", []).append((fingerprint, window_minutes, since_iso))
    if _state.get("resolved_check_raises"):
        raise RuntimeError("boom: resolved-check upstream failure")
    return _state["resolved_check_result"]


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


@activity.defn(name="check_active_work")
async def stub_check_active_work(alert: dict, repo: str) -> dict:
    return {"active": False, "reasons": []}


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


@activity.defn(name="log_alert")
async def stub_log_alert(alert: dict) -> None:
    _calls.setdefault("log_alert", []).append(alert)


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
    return {"ok": True, "message_id": 42}


@activity.defn(name="resolve_interaction")
async def stub_resolve(inp: ResolveInteractionInput) -> ResolveInteractionResult:
    return ResolveInteractionResult(already_resolved=False)


@activity.defn(name="apply_interaction_timeout")
async def stub_timeout(inp: ApplyTimeoutInput) -> None:
    return None


ALL_STUBS = [
    stub_resolve_agents,
    stub_get_alert_routing_config,
    stub_check_alert_mute,
    stub_write_alert_mute,
    stub_check_dedup,
    stub_find_open_task_for_signature,
    stub_record_signature_new_task,
    stub_record_signature_recurrence,
    stub_capture_to_inbox,
    stub_get_verification_delay,
    stub_check_alert_resolved,
    stub_resolve_infra_resource,
    stub_resolve_alert_resource,
    stub_remediate_infra_service,
    stub_check_active_work,
    stub_score_resource_relevance,
    stub_gather_alert_knowledge,
    stub_investigate,
    stub_run_investigation,
    stub_assess_investigation,
    stub_record_verdict_to_kg,
    stub_post_task_note,
    stub_log_alert,
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
    The flow's 3-min race detects it via check_alert_resolved, signals the card
    closed, and returns self_resolved_during_gate — no human answer needed."""
    _reset(resolved_check_result={"resolved": False})

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

        # Let the flow reach Gate 2 (verification delay is 0 → fast).
        await _wait_for_gate2(lambda: env.sleep(1))

        # The alert recovers while we await the human decision.
        _state["resolved_check_result"] = {"resolved": True}
        await env.sleep(200)  # cross one 180s race tick

        result = await handle.result()

    assert result["status"] == "self_resolved_during_gate"


# ---------------------------------------------------------------------------
# Test 3: a raising recheck must not kill the pending gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gate2_recheck_exception_does_not_kill_gate():
    """A raising check_alert_resolved recheck (e.g. the activity exhausted its
    retries against a transient DB/API failure) must be treated as "not
    resolved yet" and the race loop keeps waiting — never propagate and kill
    the pending gate."""
    _reset(resolved_check_raises=True)

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
    assert _calls["resolved_checks"], "recheck must have been attempted at least once"


# ---------------------------------------------------------------------------
# Test 4: escalating alert on a signature-dedup hit attaches and CONTINUES
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_escalating_signature_dedup_hit_continues_to_gate2():
    """An escalating alert whose signature already owns an open task must NOT
    early-exit as skipped_signature_dedup (the pre-fix behaviour that silently
    suppressed a real re-outage). It attaches to the existing task, skips the
    capture-new-task step, and CONTINUES the pipeline to the Gate-2 card."""
    _reset(open_task_for_signature="task-existing-1")

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
            id="esc-sigdedup-continue-test",
            task_queue="tq-esc",
        )

        await _wait_for_gate2(lambda: env.sleep(1))

        gate2_id = f"gate2-{_SAFE_FINGERPRINT}-esc-sigdedup-continue-test"
        gate2_handle = env.client.get_workflow_handle(gate2_id)
        await gate2_handle.signal(InteractionFlow.submit_response, {"value": "ack"})

        result = await asyncio.wait_for(handle.result(), timeout=15.0)

    # Did NOT early-exit; reached Gate-2 and attached to the existing task.
    assert result["status"] != "skipped_signature_dedup"
    assert result.get("todoist_task_id") == "task-existing-1"
    # No new capture task was created — it attached to the existing one.
    assert _calls.get("capture") is None
    # The Gate-2 decision card was actually presented.
    assert _calls.get("insert_inputs")
