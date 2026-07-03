"""Verdict-consistency + failure-closure tests for AlertInvestigationFlow.

Two audit bugs:

Bug A — self-contradicting verdict. When kimi produced a fix branch but
assess_investigation returned `inconclusive`/`not_actionable`, the same run
posted BOTH "1 PR staged" AND "the evidence is too thin to call". The flow
must promote the verdict to `actionable` when branches exist, so the final
track-task comment + chat ping render a PR/actionable outcome and never
the "inconclusive"/"too thin" wording.

Bug B — silent strand on investigation failure. When kimi failed AND the LLM
fallback (`investigate`) also raised, the flow died after the Step-4.5
"investigation has begun" note with no closure, stranding the task. The flow
must now post a "couldn't complete" closure note and return a terminal status.

Mirrors the WorkflowEnvironment + Worker + stub pattern of
test_alert_investigation_gates.py, reusing its ALL_STUBS list (with a
post_task_note stub added so we can capture the comments).
"""

from __future__ import annotations

import asyncio
import re

import pytest
from temporalio import activity, workflow
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

with workflow.unsafe.imports_passed_through():
    from aegis_worker.flows.alert_investigation import AlertInvestigationFlow
    from aegis_worker.flows.interaction import InteractionFlow

from tests.worker.flows.test_alert_investigation_gates import (
    ALL_STUBS,
    _calls,
    _reset,
)

# ---------------------------------------------------------------------------
# Stubs specific to these tests (capture post_task_note + control investigate)
# ---------------------------------------------------------------------------


@activity.defn(name="post_task_note")
async def stub_post_task_note(
    task_id: str,
    content: str,
    file_attachment: dict | None = None,
    workflow_id: str = "",
    run_id: str = "",
) -> dict:
    _calls.setdefault("post_task_note", []).append(content)
    return {"ok": True}


@activity.defn(name="run_investigation")
async def stub_run_investigation_local(alert: dict, resources: list[dict], runbook: str, *_a) -> dict:
    _calls.setdefault("run_investigation_called", []).append(True)
    return _state_local.get("run_investigation_result")


@activity.defn(name="assess_investigation")
async def stub_assess_investigation_local(alert: dict, investigation_output: str) -> dict:
    _calls.setdefault("assess_called", []).append(True)
    return _state_local.get("assess_result")


@activity.defn(name="record_verdict_to_kg")
async def stub_record_verdict_to_kg(
    alert: dict, verdict: dict, investigation_output: str
) -> None:
    return None


@activity.defn(name="investigate")
async def stub_investigate_local(alert: dict, system_prompt: str) -> dict:
    _calls.setdefault("investigate_called", []).append(True)
    if _state_local.get("investigate_raises"):
        raise ApplicationError("simulated LLM fallback failure", non_retryable=True)
    return _state_local.get(
        "investigate_result",
        {"investigation": "narrative", "actionable": True, "auto_fixable": False},
    )


_state_local: dict = {}


def _build_stubs() -> list:
    """ALL_STUBS from the gates module, but with our capturing/overriding
    versions of post_task_note, run_investigation, assess_investigation and
    investigate swapped in (Temporal rejects two activities with the same
    registered name on one worker)."""
    overridden = {
        "run_investigation",
        "assess_investigation",
        "investigate",
    }
    base = [s for s in ALL_STUBS if activity._Definition.must_from_callable(s).name not in overridden]
    return base + [
        stub_post_task_note,
        stub_record_verdict_to_kg,
        stub_run_investigation_local,
        stub_assess_investigation_local,
        stub_investigate_local,
    ]


def _make_alertmanager_alert(**overrides) -> dict:
    base = {
        "title": "Dagster Pipeline Failed: materialize_price_statistics",
        "fingerprint": "alertmanager:consistency-test",
        "severity": "critical",
        "source": "alertmanager",
        "service": "",
        "description": "ClickHouse OOM",
        "labels": {"alertname": "Dagster Pipeline Failure"},
        "raw_payload": {},
        "requires_approval": False,
        # Caller-supplied track-task (clarify-APP shape): bypasses Step-2.7
        # signature dedup + capture_to_inbox, and gives the flow a real
        # (non-`item-`) task id so start/closure/final notes are posted.
        "todoist_task_id": "task-track-1",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Bug A: branches present + inconclusive verdict → rendered actionable
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_branches_with_inconclusive_verdict_renders_actionable():
    """kimi produced a fix branch BUT assess returned `inconclusive`. The final
    track-task note must NOT contain the "too thin"/"inconclusive" wording — it
    must render an actionable / PR outcome consistent with the staged fix."""
    _reset(muted=False)
    _state_local.clear()
    _state_local["run_investigation_result"] = {
        "status": "succeeded",
        "output": "Found the bug and committed a fix",
        "session_id": "sess-1",
        "branch": "aegis-fix/test",
        "branches": {"aegis": "aegis-fix/test"},
    }
    # The contradiction is in the verdict STATUS (inconclusive) vs the staged
    # fix, not in the root_cause text — keep root_cause a plausible diagnosis
    # so the assertions target the rendered head/status wording.
    _state_local["assess_result"] = {
        "status": "inconclusive",
        "root_cause": "null deref in price parser",
        "suggested_fix": "",
        "confidence": 0.1,
    }

    async with (
        await WorkflowEnvironment.start_local() as env,
        Worker(
            env.client,
            task_queue="tq-consistency",
            workflows=[AlertInvestigationFlow, InteractionFlow],
            activities=_build_stubs(),
        ),
    ):
        wf_id = "branches-inconclusive-test"
        handle = await env.client.start_workflow(
            AlertInvestigationFlow.run,
            _make_alertmanager_alert(),
            id=wf_id,
            task_queue="tq-consistency",
        )

        # Gate-2 fires (branches present). Signal `ack` so we go down the
        # normal verdict path (no PR opening) and still post the final note.
        for _ in range(200):
            await asyncio.sleep(0.05)
            if _calls.get("insert_ia"):
                break
        assert _calls.get("insert_ia"), "Gate 2 child never started"
        safe_fp = re.sub(
            r"[^a-zA-Z0-9._\-]", "-", _make_alertmanager_alert()["fingerprint"]
        )[:60]
        gate2_handle = env.client.get_workflow_handle(f"gate2-{safe_fp}-{wf_id}")
        await gate2_handle.signal(InteractionFlow.submit_response, {"value": "ack"})

        result = await asyncio.wait_for(handle.result(), timeout=15.0)

    # Reconciled to an actionable outcome, NOT inconclusive.
    assert result["status"] == "logged", result["status"]
    assert result["verdict"]["status"] == "actionable"

    notes = "\n".join(_calls.get("post_task_note", []))
    assert notes, "no track-task notes were posted"
    lowered = notes.lower()
    assert "inconclusive" not in lowered, f"contradicting 'inconclusive' wording leaked: {notes}"
    assert "too thin" not in lowered, f"contradicting 'too thin' wording leaked: {notes}"
    # Positively: the final verdict comment renders an actionable outcome.
    assert "actionable" in lowered, f"actionable head missing from notes: {notes}"


@pytest.mark.asyncio
async def test_branches_with_not_actionable_verdict_renders_actionable():
    """Same reconciliation for a `not_actionable` verdict when a fix branch
    exists — promoted to actionable."""
    _reset(muted=False)
    _state_local.clear()
    _state_local["run_investigation_result"] = {
        "status": "succeeded",
        "output": "Committed a fix",
        "session_id": "sess-2",
        "branch": "aegis-fix/x",
        "branches": {"aegis": "aegis-fix/x"},
    }
    _state_local["assess_result"] = {
        "status": "not_actionable",
        "root_cause": "no action needed",
        "suggested_fix": "",
        "confidence": 0.2,
    }

    async with (
        await WorkflowEnvironment.start_local() as env,
        Worker(
            env.client,
            task_queue="tq-consistency",
            workflows=[AlertInvestigationFlow, InteractionFlow],
            activities=_build_stubs(),
        ),
    ):
        wf_id = "branches-not-actionable-test"
        handle = await env.client.start_workflow(
            AlertInvestigationFlow.run,
            _make_alertmanager_alert(fingerprint="alertmanager:na-test"),
            id=wf_id,
            task_queue="tq-consistency",
        )

        for _ in range(200):
            await asyncio.sleep(0.05)
            if _calls.get("insert_ia"):
                break
        assert _calls.get("insert_ia"), "Gate 2 child never started"
        gate2_handle = env.client.get_workflow_handle(f"gate2-alertmanager-na-test-{wf_id}")
        await gate2_handle.signal(InteractionFlow.submit_response, {"value": "ack"})

        result = await asyncio.wait_for(handle.result(), timeout=15.0)

    assert result["status"] == "logged"
    assert result["verdict"]["status"] == "actionable"


# ---------------------------------------------------------------------------
# Bug B: kimi failed + LLM fallback also fails → closure note + terminal status
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_investigation_failure_posts_closure_note():
    """kimi returns `failed` (falls back to LLM) AND the LLM fallback itself
    raises → the flow must post a closure note to the track-task and return a
    terminal `investigation_failed` status, instead of dying silently after the
    Step-4.5 'investigation has begun' note."""
    _reset(muted=False)
    _state_local.clear()
    _state_local["run_investigation_result"] = {
        "status": "failed",
        "output": "Repo directory does not exist on remote",
        "session_id": "",
        "branch": "",
        "branches": {},
    }
    _state_local["investigate_raises"] = True

    async with (
        await WorkflowEnvironment.start_local() as env,
        Worker(
            env.client,
            task_queue="tq-consistency",
            workflows=[AlertInvestigationFlow, InteractionFlow],
            activities=_build_stubs(),
        ),
    ):
        wf_id = "investigation-failure-closure-test"
        result = await env.client.execute_workflow(
            AlertInvestigationFlow.run,
            _make_alertmanager_alert(fingerprint="alertmanager:fail-closure"),
            id=wf_id,
            task_queue="tq-consistency",
        )

    assert result["status"] == "investigation_failed", result["status"]
    assert _calls.get("run_investigation_called"), "kimi attempt must have happened"
    assert _calls.get("investigate_called"), "LLM fallback must have been attempted"

    notes = "\n".join(_calls.get("post_task_note", []))
    assert notes, "no track-task notes were posted"
    assert "couldn't complete" in notes.lower(), f"closure note missing: {notes}"
    # The verdict path never ran, so assess must not have been reached.
    assert not _calls.get("assess_called"), "assess should not run after fallback failure"
