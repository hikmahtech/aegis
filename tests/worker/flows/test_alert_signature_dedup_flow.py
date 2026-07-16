"""AlertInvestigationFlow signature-dedup short-circuit.

Two tests:
  1. Sentry alert with signature → open task bound → flow returns status=
     "skipped_signature_dedup", posts recurrence note on existing task,
     bumps occurrence counter; capture_to_inbox + investigation never run.
  2. Non-sentry alert → build_alert_signature returns "" → dedup activities
     never called → flow proceeds normally to investigation.

Drives via WorkflowEnvironment + stubs because the goal is to verify wiring,
not the SQL. The activity-level SQL is covered in
tests/worker/test_alert_signature_activities.py.
"""

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
    from aegis_worker.flows.alert_investigation import AlertInvestigationFlow
    from aegis_worker.flows.interaction import InteractionFlow


_calls: dict = {}
_state: dict = {}


def _reset(**overrides):
    _calls.clear()
    _state.clear()
    _state.update(
        {
            "existing_task_id": None,  # signature dedup hit?
            "post_note_results": [],
        }
    )
    _state.update(overrides)


# --- Signature-dedup activities ---


@activity.defn(name="find_open_task_for_signature")
async def stub_find_open_task(signature: str) -> str | None:
    _calls.setdefault("find_open_task", []).append(signature)
    return _state.get("existing_task_id")


@activity.defn(name="record_signature_recurrence")
async def stub_record_recurrence(signature: str) -> None:
    _calls.setdefault("record_recurrence", []).append(signature)


@activity.defn(name="record_signature_new_task")
async def stub_record_new_task(signature: str, task_id: str) -> None:
    _calls.setdefault("record_new_task", []).append((signature, task_id))


@activity.defn(name="post_task_note")
async def stub_post_task_note(
    task_id: str,
    content: str,
    file_attachment: dict | None = None,
    workflow_id: str | None = None,
    run_id: str | None = None,
) -> dict:
    _calls.setdefault("post_task_note", []).append((task_id, content))
    return {"ok": True, "error": None}


@activity.defn(name="capture_to_inbox")
async def stub_capture_to_inbox(
    source_tag: str,
    external_id: str,
    title: str,
    description: str | None = None,
    extra_labels: list[str] | None = None,
) -> str | None:
    _calls.setdefault("capture_to_inbox", []).append(external_id)
    return _state.get("capture_returns", "new-task-id")


# --- Pre-investigation activities ---


@activity.defn(name="check_alert_mute")
async def stub_check_alert_mute(inp) -> bool:
    return False


@activity.defn(name="check_dedup")
async def stub_check_dedup(fingerprint: str, hours: int) -> dict:
    return {"is_duplicate": False}


@activity.defn(name="log_alert")
async def stub_log_alert(alert: dict) -> None:
    _calls.setdefault("log_alert", []).append(alert.get("fingerprint"))


@activity.defn(name="get_verification_delay")
async def stub_get_verification_delay(alert: dict) -> dict:
    _calls.setdefault("delay_called", []).append(True)
    return {"delay_seconds": 0, "reason": "test"}


@activity.defn(name="check_alert_resolved")
async def stub_check_alert_resolved(fingerprint: str, window_minutes: int) -> dict:
    return {"resolved": False}


@activity.defn(name="resolve_alert_resource")
async def stub_resolve_alert_resource(alert: dict) -> dict:
    _calls.setdefault("resource_called", []).append(True)
    return {
        "resource_id": "res-1",
        "resource_title": "aegis",
        "resource_path": None,  # forces llm-only investigate path (no kimi)
        "github_repo": "",
        "confidence": 0.9,
        "source": "knowledge",
        "resources": [],
    }


@activity.defn(name="gather_alert_knowledge")
async def stub_gather_alert_knowledge(title: str, project: str, alert_name: str = "") -> str:
    return ""


@activity.defn(name="investigate")
async def stub_investigate(alert: dict, system_prompt: str) -> dict:
    _calls.setdefault("investigate_called", []).append(True)
    return {"investigation": "test root cause", "actionable": True, "auto_fixable": False}


@activity.defn(name="assess_investigation")
async def stub_assess_investigation(alert: dict, investigation_output: str) -> dict:
    _calls.setdefault("assess_called", []).append(True)
    # status='resolved' lets Gate 2 skip (gate_skipped=True)
    return {
        "status": "resolved",
        "root_cause": "test",
        "suggested_fix": "n/a",
        "confidence": 0.9,
    }


@activity.defn(name="send_system_event")
async def stub_send_system_event(msg: str) -> None:
    _calls.setdefault("system_events", []).append(msg)


@activity.defn(name="send_message")
async def stub_send_message(
    agent_id: str, msg: str, chat_id: int, reply_markup: dict | None = None
) -> None:
    pass


@activity.defn(name="accumulate_digest_item")
async def stub_accumulate_digest(item: dict) -> None:
    pass


@activity.defn(name="record_verdict_to_kg")
async def stub_record_verdict_to_kg(*args, **kwargs) -> None:
    pass


# InteractionFlow activities (Gate 1/2 stubs — never actually fire when
# requires_approval=False + verdict_status='resolved')
@activity.defn(name="insert_interaction")
async def stub_insert_interaction(inp: InsertInteractionInput) -> InsertInteractionResult:
    _calls.setdefault("insert_ia", []).append((inp.kind, inp.origin))
    return InsertInteractionResult(interaction_id="ia-test")


@activity.defn(name="send_interaction_card")
async def stub_send_card(*args, **kwargs) -> dict:
    return {"ok": True, "message_id": 42}


@activity.defn(name="resolve_interaction")
async def stub_resolve(inp: ResolveInteractionInput) -> ResolveInteractionResult:
    return ResolveInteractionResult(already_resolved=False)


@activity.defn(name="apply_interaction_timeout")
async def stub_apply_timeout(inp: ApplyTimeoutInput) -> None:
    pass


@activity.defn(name="resolve_agents")
async def stub_resolve_agents(tags):
    # Seed mapping — infra → pandoras-actor (behavior unchanged).
    seed = {"finance": "maou", "infra": "pandoras-actor", "gtd": "sebas", "research": "raphael"}
    return {t: seed.get(t) for t in tags}


@activity.defn(name="get_alert_routing_config")
async def stub_get_alert_routing_config() -> dict:
    return {"infra_cluster": ""}


ALL_STUBS = [
    stub_resolve_agents,
    stub_get_alert_routing_config,
    stub_find_open_task,
    stub_record_recurrence,
    stub_record_new_task,
    stub_post_task_note,
    stub_capture_to_inbox,
    stub_check_alert_mute,
    stub_check_dedup,
    stub_log_alert,
    stub_get_verification_delay,
    stub_check_alert_resolved,
    stub_resolve_alert_resource,
    stub_gather_alert_knowledge,
    stub_investigate,
    stub_assess_investigation,
    stub_send_system_event,
    stub_send_message,
    stub_accumulate_digest,
    stub_record_verdict_to_kg,
    stub_insert_interaction,
    stub_send_card,
    stub_resolve,
    stub_apply_timeout,
]


def _make_sentry_alert(error_class: str = "IncompatiblePeer") -> dict:
    return {
        "title": "raise IncompatiblePeer(",
        "fingerprint": "sentry:7498728623",
        "severity": "error",
        "source": "sentry",
        "service": "acme-data",
        "description": "Incompatible ssh peer",
        "labels": {},
        "raw_payload": {"metadata": {"type": error_class, "value": "no host key"}},
        "requires_approval": False,
    }


# ---------------------------------------------------------------------------
# Test 1: signature hit → short-circuit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_signature_hit_attaches_to_existing_task_and_skips_investigation():
    """Open @pandora task is bound to the alert's signature →
    AlertInvestigationFlow posts a recurrence note on that task, bumps
    the occurrence counter, writes a fingerprint dedup audit row, and
    returns status=skipped_signature_dedup. capture_to_inbox + downstream
    investigation activities are never called."""
    _reset(existing_task_id="existing-task-xyz")

    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue="tq-sig",
            workflows=[AlertInvestigationFlow, InteractionFlow],
            activities=ALL_STUBS,
        ),
    ):
        result = await env.client.execute_workflow(
            AlertInvestigationFlow.run,
            _make_sentry_alert(),
            id="sig-hit-test",
            task_queue="tq-sig",
        )

    assert result["status"] == "skipped_signature_dedup"
    assert result["todoist_task_id"] == "existing-task-xyz"
    assert result["signature"] == "sentry-class:acme-data:IncompatiblePeer"

    # Signature dedup activities fired as expected
    assert _calls.get("find_open_task") == ["sentry-class:acme-data:IncompatiblePeer"]
    assert _calls.get("record_recurrence") == ["sentry-class:acme-data:IncompatiblePeer"]
    # Recurrence note landed on the existing task
    notes = _calls.get("post_task_note", [])
    assert notes, "post_task_note should have been called"
    target_id, content = notes[0]
    assert target_id == "existing-task-xyz"
    assert "Another occurrence" in content
    assert "sentry:7498728623" in content

    # Fingerprint dedup row written so re-fires of THIS issue short-circuit at step 2
    assert _calls.get("log_alert") == ["sentry:7498728623"]

    # No duplicate task creation, no investigation
    assert not _calls.get("capture_to_inbox"), "capture_to_inbox should be skipped"
    assert not _calls.get("record_new_task")
    assert not _calls.get("delay_called")
    assert not _calls.get("investigate_called")
    assert not _calls.get("assess_called")


# ---------------------------------------------------------------------------
# Test 2: signature miss → record new binding + continue
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_signature_miss_records_new_binding_and_continues():
    """No open task for this signature → flow proceeds, capture_to_inbox
    creates a fresh task, record_signature_new_task binds the signature to
    it. Investigation runs as normal. Verdict 'resolved' skips Gate 2."""
    _reset(existing_task_id=None, capture_returns="freshly-captured-task")

    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue="tq-sig",
            workflows=[AlertInvestigationFlow, InteractionFlow],
            activities=ALL_STUBS,
        ),
    ):
        result = await env.client.execute_workflow(
            AlertInvestigationFlow.run,
            _make_sentry_alert(),
            id="sig-miss-test",
            task_queue="tq-sig",
        )

    # Signature lookup happened, found nothing
    assert _calls.get("find_open_task") == ["sentry-class:acme-data:IncompatiblePeer"]
    # Recurrence NOT called (no hit)
    assert not _calls.get("record_recurrence")

    # Capture created a new task
    assert _calls.get("capture_to_inbox") == ["alert-sentry:7498728623"]

    # New binding recorded with the captured task id
    assert _calls.get("record_new_task") == [
        ("sentry-class:acme-data:IncompatiblePeer", "freshly-captured-task")
    ]

    # Investigation actually ran
    assert _calls.get("delay_called")
    assert _calls.get("investigate_called")
    assert _calls.get("assess_called")

    # Not the dedup branch
    assert result["status"] != "skipped_signature_dedup"


# ---------------------------------------------------------------------------
# Test 3: non-sentry alert → dedup layer skipped entirely
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_sentry_alert_skips_signature_layer():
    """A non-sentry alert produces signature="" → find_open_task /
    record_signature_* are NEVER called. The flow continues to capture +
    investigate without going through the new dedup gate."""
    _reset(capture_returns="gh-task-id")

    gh_alert = {
        "title": "GitHub workflow failed",
        "fingerprint": "github:run:1",
        "severity": "error",
        "source": "github",
        "service": "youruser/aegis",
        "description": "",
        "labels": {},
        "raw_payload": {"metadata": {"type": "Whatever"}},
        "requires_approval": False,
    }

    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue="tq-sig",
            workflows=[AlertInvestigationFlow, InteractionFlow],
            activities=ALL_STUBS,
        ),
    ):
        await env.client.execute_workflow(
            AlertInvestigationFlow.run,
            gh_alert,
            id="sig-non-sentry-test",
            task_queue="tq-sig",
        )

    assert not _calls.get("find_open_task")
    assert not _calls.get("record_recurrence")
    assert not _calls.get("record_new_task")
    # Capture + investigation still ran
    assert _calls.get("capture_to_inbox") == ["alert-github:run:1"]
    assert _calls.get("investigate_called")
