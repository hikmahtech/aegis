"""kimi→claude fallback test for AlertInvestigationFlow.

A non-org (kimi) investigation that does not succeed gets ONE retry with the
claude CLI (personal login) as a second activity, before degrading to the
LLM-only `investigate()` fallback. The flow gates the retry on
`inv_result["engine"] == "kimi"` so org-routed claude runs are never retried.

Mirrors the WorkflowEnvironment + Worker + gate-2-signal pattern of
test_alert_verdict_consistency.py (Gate-2 fires for every non-Jira, non-resolved
investigation, so the flow must be signalled to complete).
"""

from __future__ import annotations

import asyncio
import re

import pytest
from temporalio import activity, workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

with workflow.unsafe.imports_passed_through():
    from aegis_worker.flows.alert_investigation import AlertInvestigationFlow
    from aegis_worker.flows.interaction import InteractionFlow

from tests.worker.flows.test_alert_investigation_gates import ALL_STUBS, _calls, _reset

_state: dict = {}


@activity.defn(name="run_investigation")
async def stub_run_investigation(
    alert: dict, resources: list[dict], runbook: str, engine_override: str = "", allow_fix: bool = True
) -> dict:
    _calls.setdefault("engine_overrides", []).append(engine_override)
    return _state["claude_result"] if engine_override == "claude" else _state["kimi_result"]


@activity.defn(name="assess_investigation")
async def stub_assess_investigation(alert: dict, investigation_output: str) -> dict:
    _calls.setdefault("assess_outputs", []).append(investigation_output)
    return {"status": "actionable", "root_cause": "rc", "suggested_fix": "fix", "confidence": 0.8}


@activity.defn(name="record_verdict_to_kg")
async def stub_record_verdict_to_kg(alert: dict, verdict: dict, investigation_output: str) -> None:
    return None


@activity.defn(name="investigate")
async def stub_investigate(alert: dict, system_prompt: str) -> dict:
    _calls.setdefault("investigate_called", []).append(True)
    return {"investigation": "narrative", "actionable": True, "auto_fixable": False}


def _build_stubs() -> list:
    overridden = {"run_investigation", "assess_investigation", "investigate", "record_verdict_to_kg"}
    base = [s for s in ALL_STUBS if activity._Definition.must_from_callable(s).name not in overridden]
    return base + [
        stub_run_investigation,
        stub_assess_investigation,
        stub_record_verdict_to_kg,
        stub_investigate,
    ]


_FINGERPRINT = "alertmanager:claude-fallback"


def _alert() -> dict:
    return {
        "title": "Dagster Pipeline Failed: materialize_price_statistics",
        "fingerprint": _FINGERPRINT,
        "severity": "critical",
        "source": "alertmanager",
        "service": "",
        "description": "ClickHouse OOM",
        "labels": {"alertname": "Dagster Pipeline Failure"},
        "raw_payload": {},
        "requires_approval": False,
        "todoist_task_id": "task-track-1",
    }


async def _run_to_completion(wf_id: str) -> dict:
    """Start the flow, ack Gate-2 once it opens, return the workflow result."""
    async with (
        await WorkflowEnvironment.start_local() as env,
        Worker(
            env.client,
            task_queue="tq-claude-fb",
            workflows=[AlertInvestigationFlow, InteractionFlow],
            activities=_build_stubs(),
        ),
    ):
        handle = await env.client.start_workflow(
            AlertInvestigationFlow.run, _alert(), id=wf_id, task_queue="tq-claude-fb"
        )
        for _ in range(200):
            await asyncio.sleep(0.05)
            if _calls.get("insert_ia"):
                break
        assert _calls.get("insert_ia"), "Gate 2 child never started"
        safe_fp = re.sub(r"[^a-zA-Z0-9._\-]", "-", _FINGERPRINT)[:60]
        gate2 = env.client.get_workflow_handle(f"gate2-{safe_fp}-{wf_id}")
        await gate2.signal(InteractionFlow.submit_response, {"value": "ack"})
        return await asyncio.wait_for(handle.result(), timeout=15.0)


@pytest.mark.asyncio
async def test_kimi_failure_retries_claude_and_uses_its_output():
    """kimi times out (engine=kimi) → flow retries with engine_override='claude';
    the succeeding claude result is what feeds assess (not kimi's output, and the
    LLM fallback never runs)."""
    _reset(muted=False)
    _state.clear()
    _state["kimi_result"] = {
        "status": "timed_out",
        "output": "kimi never finished",
        "session_id": "",
        "branch": "",
        "branches": {},
        "engine": "kimi",
    }
    _state["claude_result"] = {
        "status": "succeeded",
        "output": "claude diagnosed the root cause",
        "session_id": "sess-claude",
        "branch": "",
        "branches": {},
        "engine": "claude",
    }

    result = await _run_to_completion("claude-fallback-success")

    assert _calls.get("engine_overrides") == ["", "claude"], _calls.get("engine_overrides")
    assert _calls.get("assess_outputs") == ["claude diagnosed the root cause"]
    assert not _calls.get("investigate_called")
    assert result["status"] == "logged", result["status"]


@pytest.mark.asyncio
async def test_kimi_failure_then_claude_failure_degrades_to_llm():
    """Both coding-CLI attempts fail → the original (kimi) result stands and the
    flow falls through to the LLM-only investigate()."""
    _reset(muted=False)
    _state.clear()
    _state["kimi_result"] = {
        "status": "failed",
        "output": "repo missing",
        "session_id": "",
        "branch": "",
        "branches": {},
        "engine": "kimi",
    }
    _state["claude_result"] = dict(_state["kimi_result"], engine="claude")

    result = await _run_to_completion("claude-fallback-then-llm")

    assert _calls.get("engine_overrides") == ["", "claude"]
    assert _calls.get("investigate_called"), "LLM fallback must run after both CLIs fail"
    assert result["status"] == "logged", result["status"]
