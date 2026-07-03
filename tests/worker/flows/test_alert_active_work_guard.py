"""Active-work guard flow tests for AlertInvestigationFlow.

The guard sits right after Gate-0 (Step 4.4) and before the start-comment
(Step 4.5). For a confidently-resolved repo it:
  • calls check_active_work(alert, repo),
  • if active → emits a ⏸ FYI, posts a track-task note, accumulates a
    `skipped_active_work` digest item, and returns status="skipped_active_work"
    WITHOUT ever running the investigation,
  • if inactive → falls through to the normal investigation path.

These tests drive the CONFIDENT Gate-0 path (score_resource_relevance returns
confident=True so no repo-confirm card is shown) and toggle check_active_work's
return to exercise both branches. check_active_work is stubbed BY NAME — no DB
needed.

Harness mirrors tests/worker/flows/test_alert_investigation_gates.py:
WorkflowEnvironment.start_time_skipping() + stub-activities-by-name + the real
InteractionFlow registered alongside AlertInvestigationFlow.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from aegis_worker.activities.interactions import (
    ApplyTimeoutInput,
    InsertInteractionInput,
    InsertInteractionResult,
    ResolveInteractionInput,
    ResolveInteractionResult,
)
from aegis_worker.flows.alert_investigation import AlertInvestigationFlow
from aegis_worker.flows.interaction import InteractionFlow
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

# ---------------------------------------------------------------------------
# Mutable test state
# ---------------------------------------------------------------------------

_state: dict = {}


def _reset(**overrides):
    _state.clear()
    _state.update(
        {
            "active_work_result": {"active": False, "reasons": []},
            "run_investigation_result": {
                "status": "succeeded",
                "output": "Root cause found. STATUS: investigated",
                "session_id": "sess-1",
                "branch": "",
                "branches": {},
            },
            "assess_result": {
                "status": "actionable",
                "root_cause": "rc",
                "suggested_fix": "fix",
                "confidence": 0.8,
            },
            "run_investigation_called": False,
            "check_active_work_called": False,
            "digest_items": [],
            "system_events": [],
        }
    )
    _state.update(overrides)


# ---------------------------------------------------------------------------
# Stub activities — resolve returns a single (confident) repo so the guard runs.
# ---------------------------------------------------------------------------


@activity.defn(name="check_dedup")
async def stub_check_dedup(fingerprint: str, hours: int) -> dict:
    return {"is_duplicate": False}


@activity.defn(name="find_open_task_for_signature")
async def stub_find_open_task_for_signature(signature: str) -> str | None:
    return None


@activity.defn(name="record_signature_new_task")
async def stub_record_signature_new_task(signature: str, task_id: str) -> None:
    return None


@activity.defn(name="get_verification_delay")
async def stub_get_verification_delay(alert: dict) -> dict:
    return {"delay_seconds": 0, "reason": "immediate"}


@activity.defn(name="check_alert_resolved")
async def stub_check_alert_resolved(fingerprint: str, window_minutes: int) -> dict:
    return {"resolved": False}


@activity.defn(name="resolve_alert_resource")
async def stub_resolve_alert_resource(alert: dict) -> dict:
    return {
        "resource_id": "RID_AEGIS",
        "resource_title": "aegis",
        "resource_path": "aegis",
        "github_repo": "youruser/aegis",
        "confidence": 0.9,
        "source": "service_match",
        "resources": [
            {
                "resource_id": "RID_AEGIS",
                "resource_title": "aegis",
                "resource_path": "aegis",
                "github_repo": "youruser/aegis",
                "confidence": 0.9,
            }
        ],
    }


@activity.defn(name="score_resource_relevance")
async def stub_score_resource_relevance(alert: dict, resolved_resource_id: str) -> dict:
    # Confident path: no Gate-0 card, guard runs against the resolved repo.
    return {"confident": True, "resolved_resource_id": resolved_resource_id, "candidates": []}


# ── the active-work check, stubbed BY NAME ───────────────────────────────────


@activity.defn(name="check_active_work")
async def stub_check_active_work(alert: dict, repo: str) -> dict:
    _state["check_active_work_called"] = True
    _state["checked_repo"] = repo
    return _state["active_work_result"]


@activity.defn(name="reresolve_with_hint")
async def stub_reresolve_with_hint(alert: dict, hint: str) -> dict:
    # Not exercised in the confident path, but register it so the worker boots.
    return {"confident": False, "candidates": []}


@activity.defn(name="gather_alert_knowledge")
async def stub_gather_alert_knowledge(title: str, project: str, alert_name: str = "") -> str:
    return ""


@activity.defn(name="run_investigation")
async def stub_run_investigation(alert: dict, resources: list[dict], runbook: str, *_a) -> dict:
    _state["run_investigation_called"] = True
    _state["run_investigation_resources"] = resources
    return _state["run_investigation_result"]


@activity.defn(name="investigate")
async def stub_investigate(alert: dict, system_prompt: str) -> dict:
    if _state.get("investigate_raises"):
        raise RuntimeError("stub: investigate forced to fail")
    return {"investigation": "", "actionable": True, "auto_fixable": False}


@activity.defn(name="assess_investigation")
async def stub_assess_investigation(alert: dict, investigation_output: str) -> dict:
    return _state["assess_result"]


@activity.defn(name="record_verdict_to_kg")
async def stub_record_verdict_to_kg(alert: dict, verdict: dict, output: str) -> dict:
    return {"ingested": False}


@activity.defn(name="log_alert")
async def stub_log_alert(alert: dict) -> None:
    pass


@activity.defn(name="send_system_event")
async def stub_send_system_event(msg: str) -> None:
    _state.setdefault("system_events", []).append(msg)


@activity.defn(name="send_message")
async def stub_send_message(
    agent_id: str, msg: str, chat_id: int, reply_markup: dict | None = None
) -> dict:
    return {"ok": True}


@activity.defn(name="check_alert_mute")
async def stub_check_alert_mute(_inp) -> bool:
    return False


@activity.defn(name="write_alert_mute")
async def stub_write_alert_mute(_inp) -> None:
    pass


@activity.defn(name="accumulate_digest_item")
async def stub_accumulate_digest_item(payload: dict) -> None:
    _state.setdefault("digest_items", []).append(payload)


@activity.defn(name="capture_to_inbox")
async def stub_capture_to_inbox(
    source_tag: str,
    external_id: str,
    title: str,
    description: str | None = None,
    extra_labels: list[str] | None = None,
) -> str | None:
    return "real-captured-1"


@activity.defn(name="post_task_note")
async def stub_post_task_note(
    task_id: str,
    content: str,
    file_attachment: dict | None = None,
    workflow_id: str = "",
    run_id: str = "",
) -> dict:
    _state.setdefault("posted_notes", []).append({"task_id": task_id, "content": content})
    return {"ok": True, "error": None}


@activity.defn(name="upload_kimi_log")
async def stub_upload_kimi_log(output_file: str, filename_hint: str, host: str = "") -> dict:
    return {"ok": False, "file_attachment": None, "file_name": "", "error": "skip"}


# ── InteractionFlow activity stubs (in-memory; real flow handles signals) ────


@activity.defn(name="insert_interaction")
async def stub_insert_interaction(inp: InsertInteractionInput) -> InsertInteractionResult:
    _state.setdefault("insert_ia", []).append((inp.kind, inp.origin))
    return InsertInteractionResult(interaction_id="ia-guard-test")


@activity.defn(name="send_interaction_card")
async def stub_send_card(
    interaction_id: str,
    agent_id: str,
    kind: str,
    prompt: str,
    options,
    allow_hint: bool = False,
) -> dict:
    return {"ok": True, "message_id": 1}


@activity.defn(name="update_interaction_message_id")
async def stub_update_msg(interaction_id: str, telegram_message_id: int) -> None:
    pass


@activity.defn(name="resolve_interaction")
async def stub_resolve_interaction(inp: ResolveInteractionInput) -> ResolveInteractionResult:
    return ResolveInteractionResult(already_resolved=False)


@activity.defn(name="apply_interaction_timeout")
async def stub_apply_timeout(inp: ApplyTimeoutInput) -> None:
    return None


@activity.defn(name="stage_pending_pr")
async def stub_stage_pending_pr(inp) -> str:
    return "pr-uuid-stub"


@activity.defn(name="create_github_pr")
async def stub_create_github_pr(inp) -> dict:
    return {"pr_url": "", "status": "opened", "error": ""}


ALL_ACTIVITIES = [
    stub_check_alert_mute,
    stub_write_alert_mute,
    stub_check_dedup,
    stub_find_open_task_for_signature,
    stub_record_signature_new_task,
    stub_get_verification_delay,
    stub_check_alert_resolved,
    stub_resolve_alert_resource,
    stub_score_resource_relevance,
    stub_check_active_work,
    stub_reresolve_with_hint,
    stub_gather_alert_knowledge,
    stub_run_investigation,
    stub_investigate,
    stub_assess_investigation,
    stub_record_verdict_to_kg,
    stub_log_alert,
    stub_send_system_event,
    stub_send_message,
    stub_accumulate_digest_item,
    stub_capture_to_inbox,
    stub_post_task_note,
    stub_upload_kimi_log,
    stub_insert_interaction,
    stub_send_card,
    stub_update_msg,
    stub_resolve_interaction,
    stub_apply_timeout,
    stub_stage_pending_pr,
    stub_create_github_pr,
]


def _make_alert(**overrides) -> dict:
    base = {
        "title": "aegis worker crash loop",
        "fingerprint": "fp-guard-1",
        "severity": "error",
        "source": "todoist-chat",
        "service": "aegis",
        "description": "worker container restarting",
        "labels": {},
        "raw_payload": {},
        "todoist_task_id": "TRACK_TASK_1",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_confident_active_skips_investigation():
    """confident Gate-0 + check_active_work returns active=True →
    status='skipped_active_work', investigation NEVER runs, ⏸ FYI + audit fired,
    and the in-flight row is marked then cleared."""
    _reset(
        active_work_result={"active": True, "reasons": ["open PR #1 (alice)"]},
    )

    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue="tq-guard",
            workflows=[AlertInvestigationFlow, InteractionFlow],
            activities=ALL_ACTIVITIES,
        ),
    ):
        result = await env.client.execute_workflow(
            AlertInvestigationFlow.run,
            _make_alert(),
            id=f"alert-guard-active-{uuid4().hex[:8]}",
            task_queue="tq-guard",
        )

    assert result["status"] == "skipped_active_work"
    assert result["resolved_repo"] == "youruser/aegis"
    assert "open PR #1 (alice)" in result["active_work_reasons"]

    # Investigation must NOT have run — the human is already on it.
    assert _state["run_investigation_called"] is False

    # Guard wiring: checked against the resolved repo.
    assert _state["check_active_work_called"] is True
    assert _state["checked_repo"] == "youruser/aegis"

    # Operator-visible FYI + digest audit.
    assert any("under active work" in e for e in _state["system_events"])
    assert any(d.get("type") == "skipped_active_work" for d in _state["digest_items"])


@pytest.mark.asyncio
async def test_confident_inactive_proceeds_to_investigation():
    """confident Gate-0 + check_active_work returns active=False → guard is a
    no-op and the flow proceeds into the investigation path, reaching the
    clear-on-success at the terminal return.

    Uses a `todoist-jira` source so Gate-2 is skipped (scoping-only by
    contract) and the flow runs straight to its terminal return under
    start_time_skipping — no signal plumbing needed to exercise the happy
    path + clear-on-success."""
    _reset(active_work_result={"active": False, "reasons": []})

    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue="tq-guard",
            workflows=[AlertInvestigationFlow, InteractionFlow],
            activities=ALL_ACTIVITIES,
        ),
    ):
        result = await env.client.execute_workflow(
            AlertInvestigationFlow.run,
            _make_alert(source="todoist-jira"),
            id=f"alert-guard-inactive-{uuid4().hex[:8]}",
            task_queue="tq-guard",
        )

    # Guard ran but did not short-circuit.
    assert _state["check_active_work_called"] is True
    assert result["status"] != "skipped_active_work"

    # Investigation proceeded against the resolved repo.
    assert _state["run_investigation_called"] is True
    resources = _state["run_investigation_resources"]
    assert resources and resources[0]["github_repo"] == "youruser/aegis"

    assert not any(d.get("type") == "skipped_active_work" for d in _state["digest_items"])
