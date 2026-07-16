"""Alert gates — mute short-circuit + Gate 2 post-verdict InteractionFlow.

Covers the pre-existing-mute short-circuit and the Gate 2 post-verdict
decision card (open_all_prs / skip_pr / discard / ack), plus alertmanager/
kimi routing into Gate 2. There is no Gate 1 approval step — the flow
investigates without pre-approval.

Uses start_local() so we can signal the child InteractionFlow at the right
moment (same pattern as test_gmail_ingest.py::test_auth_expired_pauses_via_interaction_flow).
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


def _reset(**overrides):
    _calls.clear()
    _state.clear()
    _state.update(
        {
            "muted": False,
            "check_dedup_result": {"is_duplicate": False},
            "delay_result": {"delay_seconds": 0, "reason": "test"},
            "resolved_check_result": {"resolved": False},
            "resource_result": {
                "resource_id": "res-1",
                "resource_title": "aegis",
                "resource_path": "aegis",
                "github_repo": "youruser/aegis",
                "confidence": 0.9,
                "source": "knowledge",
                "resources": [
                    {
                        "resource_id": "res-1",
                        "resource_title": "aegis",
                        "resource_path": "aegis",
                        "github_repo": "youruser/aegis",
                        "confidence": 0.9,
                    }
                ],
            },
            "knowledge_result": "",
            "investigate_result": {
                "investigation": "Root cause: test",
                "actionable": True,
                "auto_fixable": False,
            },
            "assess_result": {
                "status": "actionable",
                "root_cause": "test root cause",
                "suggested_fix": "fix it",
                "confidence": 0.8,
            },
        }
    )
    _state.update(overrides)


# ---------------------------------------------------------------------------
# Stub activities  (shared across all tests in this module)
# ---------------------------------------------------------------------------


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
    # No existing open task bound to this signature → investigation proceeds.
    return _state.get("open_task_for_signature")


@activity.defn(name="record_signature_new_task")
async def stub_record_signature_new_task(signature: str, task_id: str) -> None:
    return None


@activity.defn(name="record_signature_recurrence")
async def stub_record_signature_recurrence(signature: str) -> None:
    return None


@activity.defn(name="get_verification_delay")
async def stub_get_verification_delay(alert: dict) -> dict:
    _calls.setdefault("delay_called", []).append(True)
    return _state["delay_result"]


@activity.defn(name="check_alert_resolved")
async def stub_check_alert_resolved(fingerprint: str, window_minutes: int) -> dict:
    return _state["resolved_check_result"]


@activity.defn(name="resolve_alert_resource")
async def stub_resolve_alert_resource(alert: dict) -> dict:
    _calls.setdefault("resource_called", []).append(True)
    return _state["resource_result"]


@activity.defn(name="resolve_infra_resource")
async def stub_resolve_infra_resource(alert: dict) -> dict:
    # Infra alerts (NodeDown / DockerServiceDown / Dagster, etc.) skip the LLM
    # repo-match and resolve straight to the infra-gitops resource. Mirror the
    # alert-resource stub so the configured _state["resource_result"] still
    # drives run_investigation / Gate-2.
    _calls.setdefault("infra_resource_called", []).append(True)
    return _state["resource_result"]


@activity.defn(name="score_resource_relevance")
async def stub_score_resource_relevance(alert: dict, resolved_resource_id: str) -> dict:
    # Gate-0: these gate tests exercise the confident path (no repo-confirm
    # interruption), so always report confident.
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
    return _state.get("run_investigation_result", {
        "status": "succeeded",
        "output": "test output",
        "session_id": "sess-1",
        "branch": "aegis-fix/test",
        "branches": {"aegis": "aegis-fix/test"},
    })


@activity.defn(name="assess_investigation")
async def stub_assess_investigation(alert: dict, investigation_output: str) -> dict:
    _calls.setdefault("assess_called", []).append(True)
    if _state.get("assess_raises"):
        # Simulate the verdict LLM (qwen3:14b) hanging past its StartToClose
        # ceiling. non_retryable so the test doesn't wait out the retry backoff.
        from temporalio.exceptions import ApplicationError

        raise ApplicationError("simulated assess StartToClose timeout", non_retryable=True)
    return _state["assess_result"]


@activity.defn(name="log_alert")
async def stub_log_alert(alert: dict) -> None:
    _calls.setdefault("log_alert", []).append(alert)


@activity.defn(name="send_system_event")
async def stub_send_system_event(msg: str) -> None:
    pass


@activity.defn(name="send_message")
async def stub_send_message(
    agent_id: str, msg: str, chat_id: int, reply_markup: dict | None = None
) -> None:
    pass


@activity.defn(name="accumulate_digest_item")
async def stub_accumulate_digest(item: dict) -> None:
    pass


# --- InteractionFlow activities ---


@activity.defn(name="insert_interaction")
async def stub_insert_interaction(inp: InsertInteractionInput) -> InsertInteractionResult:
    _calls.setdefault("insert_ia", []).append((inp.kind, inp.origin))
    return InsertInteractionResult(interaction_id="ia-gate1-test")


@activity.defn(name="send_interaction_card")
async def stub_send_card(
    interaction_id: str,
    agent_id: str,
    kind: str,
    prompt: str,
    options,
    allow_hint: bool = False,
) -> dict:
    return {"ok": True, "message_id": 42}


@activity.defn(name="resolve_interaction")
async def stub_resolve(inp: ResolveInteractionInput) -> ResolveInteractionResult:
    return ResolveInteractionResult(already_resolved=False)


@activity.defn(name="apply_interaction_timeout")
async def stub_timeout(inp: ApplyTimeoutInput) -> None:
    return None


@activity.defn(name="stage_pending_pr")
async def stub_stage_pending_pr(inp) -> str:
    _calls.setdefault("stage_pending_pr", []).append(inp)
    return "pr-uuid-stub"


@activity.defn(name="create_github_pr")
async def stub_create_github_pr(inp) -> dict:
    _calls.setdefault("create_github_pr", []).append(inp)
    return {"pr_url": "https://github.com/test/repo/pull/1", "status": "opened", "error": ""}


@activity.defn(name="resolve_agents")
async def stub_resolve_agents(tags):
    # Seed mapping: infra → pandoras-actor, so behavior is unchanged. Imported
    # by the verdict/claude-fallback tests via ALL_STUBS.
    return {t: {"infra": "pandoras-actor"}.get(t) for t in tags}


@activity.defn(name="get_alert_routing_config")
async def stub_get_alert_routing_config() -> dict:
    return {"infra_cluster": ""}


ALL_STUBS = [
    stub_resolve_agents,
    stub_get_alert_routing_config,
    stub_check_alert_mute,
    stub_write_alert_mute,
    stub_check_dedup,
    stub_find_open_task_for_signature,
    stub_record_signature_new_task,
    stub_record_signature_recurrence,
    stub_get_verification_delay,
    stub_check_alert_resolved,
    stub_resolve_alert_resource,
    stub_resolve_infra_resource,
    stub_score_resource_relevance,
    stub_gather_alert_knowledge,
    stub_investigate,
    stub_run_investigation,
    stub_assess_investigation,
    stub_log_alert,
    stub_send_system_event,
    stub_send_message,
    stub_accumulate_digest,
    stub_insert_interaction,
    stub_send_card,
    stub_resolve,
    stub_timeout,
    stub_stage_pending_pr,
    stub_create_github_pr,
]


def _make_alert(**overrides) -> dict:
    base = {
        "title": "GitHub workflow failed: CI on youruser/aegis",
        "fingerprint": "github:workflow_run:youruser/aegis:abc123",
        "severity": "error",
        "source": "github",
        "service": "youruser/aegis",
        "description": "workflow_run failure on main",
        "labels": {"event": "workflow_run", "workflow": "CI", "branch": "main"},
        "raw_payload": {},
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Child workflow ID helpers
# ---------------------------------------------------------------------------

_FINGERPRINT = "github:workflow_run:youruser/aegis:abc123"
_SAFE_FINGERPRINT = re.sub(r"[^a-zA-Z0-9._\-]", "-", _FINGERPRINT)[:60]
_GATE2_CHILD_PREFIX = f"gate2-{_SAFE_FINGERPRINT}-"


# ---------------------------------------------------------------------------
# Test 1: pre-existing mute → short-circuit before investigation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_existing_mute_short_circuits():
    """check_alert_mute=True → status=muted; no InteractionFlow spawned at all."""
    _reset(muted=True)

    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue="tq-gates",
            workflows=[AlertInvestigationFlow, InteractionFlow],
            activities=ALL_STUBS,
        ),
    ):
        result = await env.client.execute_workflow(
            AlertInvestigationFlow.run,
            _make_alert(),
            id="gate-mute-existing",
            task_queue="tq-gates",
        )

    assert result["status"] == "muted"
    # No InteractionFlow (Gate 2) was ever invoked
    assert not _calls.get("insert_ia"), (
        f"insert_interaction should not have been called, got: {_calls.get('insert_ia')}"
    )
    # Investigation never ran
    assert not _calls.get("delay_called")
    assert not _calls.get("investigate_called")


# ---------------------------------------------------------------------------
# Gate 2 helpers
# ---------------------------------------------------------------------------


async def _drive_to_gate2(env, handle, workflow_id: str):
    """Wait until Gate 2's InteractionFlow child has started. There is no Gate 1
    approval step — the flow reaches Gate 2 on its own after the (stubbed)
    investigation, so the FIRST insert_ia call is Gate 2. Returns its handle."""
    for _ in range(200):
        await asyncio.sleep(0.05)
        if _calls.get("insert_ia"):
            break
    assert _calls.get("insert_ia"), "Gate 2 child never started"

    gate2_id = _GATE2_CHILD_PREFIX + workflow_id
    return env.client.get_workflow_handle(gate2_id)


# ---------------------------------------------------------------------------
# Test 5: Gate 2 value=open_pr → stage_pending_pr called
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gate2_open_all_prs_stages_pending_pr():
    """Gate 2 value=open_all_prs → stage_pending_pr called with the alert's repo."""
    _reset(muted=False)

    async with (
        await WorkflowEnvironment.start_local() as env,
        Worker(
            env.client,
            task_queue="tq-gates",
            workflows=[AlertInvestigationFlow, InteractionFlow],
            activities=ALL_STUBS,
        ),
    ):
        wf_id = "gate2-open-pr-test"
        handle = await env.client.start_workflow(
            AlertInvestigationFlow.run,
            _make_alert(),
            id=wf_id,
            task_queue="tq-gates",
        )

        gate2_handle = await _drive_to_gate2(env, handle, wf_id)
        assert len(_calls.get("insert_ia", [])) >= 1, "Gate 2 child never started"

        await gate2_handle.signal(InteractionFlow.submit_response, {"value": "open_all_prs"})

        result = await asyncio.wait_for(handle.result(), timeout=15.0)

    assert result["status"] not in ("gate2_discarded",)
    staged = _calls.get("stage_pending_pr", [])
    assert staged, "stage_pending_pr was never called"
    call = staged[0]
    # Temporal may deserialise the dataclass as a dict on the wire
    repo = call["repo"] if isinstance(call, dict) else call.repo
    assert repo == "youruser/aegis"


# ---------------------------------------------------------------------------
# Test 6: Gate 2 value=skip_pr → task created, no pending_pr row
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gate2_skip_pr_creates_task_without_pending_pr():
    """Gate 2 value=skip_pr (no matching handler) → flow reaches task creation
    via fall-through; stage_pending_pr NOT called. `skip_pr` is not an emitted
    option post-Gate-2-redesign, but the stored response shape is kept as the
    canonical "no-op" signal to test the fall-through branch."""
    _reset(
        muted=False,
        assess_result={
            "status": "actionable",
            "root_cause": "test",
            "suggested_fix": "fix it",
            "confidence": 0.9,
        },
    )

    async with (
        await WorkflowEnvironment.start_local() as env,
        Worker(
            env.client,
            task_queue="tq-gates",
            workflows=[AlertInvestigationFlow, InteractionFlow],
            activities=ALL_STUBS,
        ),
    ):
        wf_id = "gate2-skip-pr-test"
        handle = await env.client.start_workflow(
            AlertInvestigationFlow.run,
            _make_alert(),
            id=wf_id,
            task_queue="tq-gates",
        )

        gate2_handle = await _drive_to_gate2(env, handle, wf_id)
        assert len(_calls.get("insert_ia", [])) >= 1, "Gate 2 child never started"

        await gate2_handle.signal(InteractionFlow.submit_response, {"value": "skip_pr"})

        result = await asyncio.wait_for(handle.result(), timeout=15.0)

    assert result["status"] != "gate2_discarded"
    assert not _calls.get("stage_pending_pr"), (
        f"stage_pending_pr should NOT be called on skip_pr, got: {_calls.get('stage_pending_pr')}"
    )


# ---------------------------------------------------------------------------
# Test 7: Gate 2 value=discard → returns gate2_discarded
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gate2_discard_returns_without_task_or_pr():
    """Gate 2 value=discard → status=gate2_discarded; no task, no pending_pr."""
    _reset(muted=False)

    async with (
        await WorkflowEnvironment.start_local() as env,
        Worker(
            env.client,
            task_queue="tq-gates",
            workflows=[AlertInvestigationFlow, InteractionFlow],
            activities=ALL_STUBS,
        ),
    ):
        wf_id = "gate2-discard-test"
        handle = await env.client.start_workflow(
            AlertInvestigationFlow.run,
            _make_alert(),
            id=wf_id,
            task_queue="tq-gates",
        )

        gate2_handle = await _drive_to_gate2(env, handle, wf_id)
        assert len(_calls.get("insert_ia", [])) >= 1, "Gate 2 child never started"

        await gate2_handle.signal(InteractionFlow.submit_response, {"value": "discard"})

        result = await asyncio.wait_for(handle.result(), timeout=15.0)

    assert result["status"] == "gate2_discarded"
    assert not _calls.get("stage_pending_pr"), "stage_pending_pr should NOT be called on discard"


# ---------------------------------------------------------------------------
# Test 7b: Gate 2 value=discard → audit_log gets written so re-fires dedup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gate2_discard_logs_to_audit_log():
    """Gate 2 value=discard must still call log_alert before returning. Without
    this, a re-fire of the same alert would not be caught by step-2 dedup
    (check_dedup reads audit_log) and would spawn a duplicate investigation
    even though the user already explicitly discarded the proposed fix."""
    _reset(muted=False)

    async with (
        await WorkflowEnvironment.start_local() as env,
        Worker(
            env.client,
            task_queue="tq-gates",
            workflows=[AlertInvestigationFlow, InteractionFlow],
            activities=ALL_STUBS,
        ),
    ):
        wf_id = "gate2-discard-audit-test"
        handle = await env.client.start_workflow(
            AlertInvestigationFlow.run,
            _make_alert(),
            id=wf_id,
            task_queue="tq-gates",
        )

        gate2_handle = await _drive_to_gate2(env, handle, wf_id)
        assert len(_calls.get("insert_ia", [])) >= 1, "Gate 2 child never started"

        await gate2_handle.signal(InteractionFlow.submit_response, {"value": "discard"})

        result = await asyncio.wait_for(handle.result(), timeout=15.0)

    assert result["status"] == "gate2_discarded"
    logged = _calls.get("log_alert", [])
    assert logged, "log_alert must be called on the discard branch (audit dedup)"


# ---------------------------------------------------------------------------
# Test 8: Alertmanager alert + kimi branches → Gate 2 fires (non-requires_approval)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_alertmanager_kimi_triggers_gate2():
    """Non-requires_approval alert with kimi branches → Gate 2 fires for all sources."""
    _reset(muted=False)
    _state["run_investigation_result"] = {
        "status": "succeeded",
        "output": "Kimi found OOM in ClickHouse config",
        "session_id": "sess-kimi",
        "branch": "aegis-fix/abcdef",
        "branches": {"infra-gitops": "aegis-fix/abcdef"},
    }
    # Nested workspace path; kimi's BRANCH: lines key by the repo BASENAME —
    # the flow must bridge the two when staging PRs.
    _state["resource_result"] = {
        "resource_id": "res-homelab",
        "resource_title": "Homelab GitOps",
        "resource_path": "infrastructure/infra-gitops",
        "github_repo": "example/infra-gitops",
        "confidence": 0.9,
        "source": "llm",
        "resources": [
            {
                "resource_id": "res-homelab",
                "resource_title": "Homelab GitOps",
                "resource_path": "infrastructure/infra-gitops",
                "github_repo": "example/infra-gitops",
                "confidence": 0.9,
            }
        ],
    }

    alertmanager_alert = {
        "title": "Dagster Pipeline Failed: materialize_price_statistics",
        "fingerprint": "alertmanager:abcdef1234",
        "severity": "critical",
        "source": "alertmanager",
        "service": "",
        "description": "ClickHouse OOM",
        "labels": {"alertname": "Dagster Pipeline Failure"},
        "raw_payload": {},
        "requires_approval": False,
    }

    async with (
        await WorkflowEnvironment.start_local() as env,
        Worker(
            env.client,
            task_queue="tq-gates",
            workflows=[AlertInvestigationFlow, InteractionFlow],
            activities=ALL_STUBS,
        ),
    ):
        wf_id = "alertmanager-gate2-test"
        handle = await env.client.start_workflow(
            AlertInvestigationFlow.run,
            alertmanager_alert,
            id=wf_id,
            task_queue="tq-gates",
        )

        # No Gate 1 for alertmanager — wait directly for Gate 2
        gate2_id = f"gate2-alertmanager-abcdef1234-{wf_id}"
        gate2_handle = env.client.get_workflow_handle(gate2_id)

        # Wait for Gate 2 child to start
        for _ in range(200):
            await asyncio.sleep(0.05)
            if _calls.get("insert_ia"):
                break
        assert _calls.get("insert_ia"), "Gate 2 child never started for Alertmanager alert"

        await gate2_handle.signal(InteractionFlow.submit_response, {"value": "open_all_prs"})

        result = await asyncio.wait_for(handle.result(), timeout=15.0)

    assert result["status"] != "gate2_discarded"
    staged = _calls.get("stage_pending_pr", [])
    assert staged, "stage_pending_pr was never called"
    created = _calls.get("create_github_pr", [])
    assert created, "create_github_pr was never called"
    # The PR push must target the resource's nested workspace checkout.
    call = created[0]
    repo_path = call["repo_path"] if isinstance(call, dict) else call.repo_path
    assert repo_path == "infrastructure/infra-gitops"


# ---------------------------------------------------------------------------
# Test 9: Alertmanager + kimi no branches → Gate 2 does NOT fire
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gate2_mute_24h_writes_alert_mute():
    """Gate 2 `mute_24h` choice triggers write_alert_mute and falls through
    to the verdict-comment path (status='logged'). Replaces the loop of
    "comments after comments" with an explicit user-driven mute."""
    _reset(muted=False)

    async with (
        await WorkflowEnvironment.start_local() as env,
        Worker(
            env.client,
            task_queue="tq-gates",
            workflows=[AlertInvestigationFlow, InteractionFlow],
            activities=ALL_STUBS,
        ),
    ):
        wf_id = "gate2-mute-24h-test"
        handle = await env.client.start_workflow(
            AlertInvestigationFlow.run,
            _make_alert(),
            id=wf_id,
            task_queue="tq-gates",
        )

        gate2_handle = await _drive_to_gate2(env, handle, wf_id)
        await gate2_handle.signal(InteractionFlow.submit_response, {"value": "mute_24h"})

        result = await asyncio.wait_for(handle.result(), timeout=15.0)

    assert result["status"] != "gate2_discarded"
    mute_calls = _calls.get("mute_write", [])
    assert mute_calls, "write_alert_mute should have been called on mute_24h"
    # ttl_seconds == 24h
    args = mute_calls[0]
    ttl = args["ttl_seconds"] if isinstance(args, dict) else args.ttl_seconds
    assert ttl == 86400
    assert not _calls.get("stage_pending_pr"), "PRs must NOT be opened on mute"


@pytest.mark.asyncio
async def test_gate2_ack_logs_acknowledgement_without_pr():
    """Gate 2 `ack` choice falls through to the normal verdict-comment +
    chat-info path. No PRs created, no mute written."""
    _reset(muted=False)

    async with (
        await WorkflowEnvironment.start_local() as env,
        Worker(
            env.client,
            task_queue="tq-gates",
            workflows=[AlertInvestigationFlow, InteractionFlow],
            activities=ALL_STUBS,
        ),
    ):
        wf_id = "gate2-ack-test"
        handle = await env.client.start_workflow(
            AlertInvestigationFlow.run,
            _make_alert(),
            id=wf_id,
            task_queue="tq-gates",
        )

        gate2_handle = await _drive_to_gate2(env, handle, wf_id)
        await gate2_handle.signal(InteractionFlow.submit_response, {"value": "ack"})

        result = await asyncio.wait_for(handle.result(), timeout=15.0)

    assert result["status"] != "gate2_discarded"
    assert not _calls.get("mute_write"), "Ack should NOT trigger a mute"
    assert not _calls.get("stage_pending_pr"), "Ack should NOT open PRs"


@pytest.mark.asyncio
async def test_alertmanager_kimi_no_branches_still_fires_gate2_for_decision():
    """When Kimi returns empty branches (no code fix), Gate 2 still fires —
    it just omits the `open_all_prs` option and offers Mute / Acknowledge
    so the user can dispose of the alert from chat.

    Updated 2026-05-22 — pre-fix the gate skipped this case entirely, which
    is what triggered the "comments after comments, no chat approval
    prompt" feedback from the user.
    """
    _reset(muted=False)
    _state["run_investigation_result"] = {
        "status": "succeeded",
        "output": "No code fix — purely infra memory issue",
        "session_id": "sess-kimi",
        "branch": "",
        "branches": {},
    }

    alertmanager_alert = {
        "title": "Dagster Pipeline Failed",
        "fingerprint": "alertmanager:nobranch",
        "severity": "critical",
        "source": "alertmanager",
        "service": "",
        "description": "No code fix needed",
        "labels": {},
        "raw_payload": {},
        "requires_approval": False,
    }

    async with (
        await WorkflowEnvironment.start_local() as env,
        Worker(
            env.client,
            task_queue="tq-gates",
            workflows=[AlertInvestigationFlow, InteractionFlow],
            activities=ALL_STUBS,
        ),
    ):
        wf_id = "alertmanager-no-branch-test"
        handle = await env.client.start_workflow(
            AlertInvestigationFlow.run,
            alertmanager_alert,
            id=wf_id,
            task_queue="tq-gates",
        )

        # requires_approval=False so Gate-1 is skipped; the FIRST insert_ia
        # call is Gate-2 directly. Gate-2 child id is built from the alert's
        # fingerprint, NOT the canned _SAFE_FINGERPRINT.
        for _ in range(200):
            await asyncio.sleep(0.05)
            if _calls.get("insert_ia"):
                break
        assert _calls.get("insert_ia"), "Gate 2 child never started"

        safe_fp = re.sub(r"[^a-zA-Z0-9._\-]", "-", alertmanager_alert["fingerprint"])[:60]
        gate2_id = f"gate2-{safe_fp}-{wf_id}"
        gate2_handle = env.client.get_workflow_handle(gate2_id)
        await gate2_handle.signal(InteractionFlow.submit_response, {"value": "ack"})

        result = await asyncio.wait_for(handle.result(), timeout=10.0)

    assert result["status"] == "logged"
    assert not _calls.get("stage_pending_pr")
    assert not _calls.get("create_github_pr")


# ---------------------------------------------------------------------------
# Test 10: Kimi failure → flow falls back to LLM-only investigation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kimi_failure_falls_back_to_llm_investigate():
    """When run_investigation returns non-succeeded (e.g. missing repo dir,
    no clone_url), the flow must call investigate() instead of feeding the
    error string to Haiku as the investigation output."""
    _reset(muted=False)
    _state["run_investigation_result"] = {
        "status": "failed",
        "output": "Repo directory does not exist on remote: /home/user/Workspace/trading-system-pipeline",
        "session_id": "",
        "branch": "",
        "branches": {},
    }
    _state["investigate_result"] = {
        "investigation": "LLM-only narrative based on the alert",
        "actionable": True,
        "auto_fixable": False,
    }

    alertmanager_alert = {
        "title": "Dagster Pipeline Failed: equity_ml_daily_inference",
        "fingerprint": "alertmanager:kimi-fail",
        "severity": "critical",
        "source": "alertmanager",
        "service": "",
        "description": "Failure",
        "labels": {},
        "raw_payload": {},
        "requires_approval": False,
    }

    async with (
        await WorkflowEnvironment.start_local() as env,
        Worker(
            env.client,
            task_queue="tq-gates",
            workflows=[AlertInvestigationFlow, InteractionFlow],
            activities=ALL_STUBS,
        ),
    ):
        wf_id = "alertmanager-kimi-failure-test"
        handle = await env.client.start_workflow(
            AlertInvestigationFlow.run,
            alertmanager_alert,
            id=wf_id,
            task_queue="tq-gates",
        )

        # Post-2026-05-22: Gate 2 fires even without branches (Mute/Ack
        # options). Signal `ack` to complete the gate and let the flow
        # finish without taking any action.
        for _ in range(200):
            await asyncio.sleep(0.05)
            if _calls.get("insert_ia"):
                break
        assert _calls.get("insert_ia"), "Gate 2 child never started"
        safe_fp = re.sub(r"[^a-zA-Z0-9._\-]", "-", alertmanager_alert["fingerprint"])[:60]
        gate2_handle = env.client.get_workflow_handle(f"gate2-{safe_fp}-{wf_id}")
        await gate2_handle.signal(InteractionFlow.submit_response, {"value": "ack"})

        result = await asyncio.wait_for(handle.result(), timeout=10.0)

    assert _calls.get("run_investigation_called"), "kimi attempt must have happened"
    assert _calls.get("investigate_called"), "LLM fallback must run when kimi fails"
    # The error string from kimi must not leak into the final summary
    inv = result.get("investigation", "")
    assert "Repo directory does not exist" not in inv
    assert "LLM-only narrative" in inv
    # No PRs (no kimi branches → no open_all_prs option to pick)
    assert not _calls.get("stage_pending_pr")
    assert not _calls.get("create_github_pr")


# ---------------------------------------------------------------------------
# Test 11: Haiku verdict=inconclusive → final_status=inconclusive, no Gate 2
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inconclusive_verdict_sets_inconclusive_status():
    """When the kimi run reported insufficient evidence and Haiku returns
    `inconclusive`, the final status must surface that honestly instead of
    falling into the actionable bucket."""
    _reset(muted=False)
    _state["run_investigation_result"] = {
        "status": "succeeded",
        "output": (
            "Tried `kubectl logs aegis_knowledge` but got `error: forbidden`.\n"
            "STATUS: insufficient_evidence: no log access"
        ),
        "session_id": "sess-inc",
        "branch": "",
        "branches": {},
    }
    _state["assess_result"] = {
        "status": "inconclusive",
        "root_cause": "",
        "suggested_fix": "",
        "confidence": 0.2,
    }

    alertmanager_alert = {
        "title": "Service aegis_knowledge has fewer tasks than desired",
        "fingerprint": "alertmanager:inconclusive",
        "severity": "critical",
        "source": "alertmanager",
        "service": "",
        "description": "Service down",
        "labels": {},
        "raw_payload": {},
        "requires_approval": False,
    }

    async with (
        await WorkflowEnvironment.start_local() as env,
        Worker(
            env.client,
            task_queue="tq-gates",
            workflows=[AlertInvestigationFlow, InteractionFlow],
            activities=ALL_STUBS,
        ),
    ):
        wf_id = "alertmanager-inconclusive-test"
        handle = await env.client.start_workflow(
            AlertInvestigationFlow.run,
            alertmanager_alert,
            id=wf_id,
            task_queue="tq-gates",
        )

        # Post-2026-05-22: inconclusive verdicts still fire Gate 2 so the
        # user can Mute/Ack via chat. Send `ack` to let the flow
        # finish; final_status must remain `inconclusive`.
        for _ in range(200):
            await asyncio.sleep(0.05)
            if _calls.get("insert_ia"):
                break
        assert _calls.get("insert_ia"), "Gate 2 child never started"
        safe_fp = re.sub(r"[^a-zA-Z0-9._\-]", "-", alertmanager_alert["fingerprint"])[:60]
        gate2_handle = env.client.get_workflow_handle(f"gate2-{safe_fp}-{wf_id}")
        await gate2_handle.signal(InteractionFlow.submit_response, {"value": "ack"})

        result = await asyncio.wait_for(handle.result(), timeout=10.0)

    assert result["status"] == "inconclusive"
    assert not _calls.get("stage_pending_pr")
    assert not _calls.get("create_github_pr")


# ---------------------------------------------------------------------------
# Test 12: assess_investigation raises (verdict LLM hangs) → graceful degrade
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assess_timeout_degrades_to_inconclusive():
    """When assess_investigation raises (the qwen3:14b verdict call hangs past
    its StartToClose ceiling and exhausts retries), the flow must NOT fail the
    whole investigation. It degrades to an `inconclusive` verdict that carries
    investigate()'s already-successful output, so the user still gets a Gate-2
    card to act on.

    Regression for sentry:7510268390 (root-caused 2026-05-30): assess timed
    out 3× and the workflow failed with a bare "Activity task timed out",
    leaving the user with only the "investigation has begun" note.
    """
    _reset(muted=False, assess_raises=True)
    _state["run_investigation_result"] = {
        "status": "succeeded",
        "output": "Investigation found the exec_info typo in notifications/__init__.py",
        "session_id": "sess-x",
        "branch": "",
        "branches": {},
    }

    alertmanager_alert = {
        "title": "TypeError: Logger._log() got an unexpected keyword argument 'exec_info'",
        "fingerprint": "alertmanager:assess-timeout",
        "severity": "error",
        "source": "alertmanager",
        "service": "",
        "description": "verdict LLM hangs past the StartToClose ceiling",
        "labels": {},
        "raw_payload": {},
        "requires_approval": False,
    }

    async with (
        await WorkflowEnvironment.start_local() as env,
        Worker(
            env.client,
            task_queue="tq-gates",
            workflows=[AlertInvestigationFlow, InteractionFlow],
            activities=ALL_STUBS,
        ),
    ):
        wf_id = "assess-timeout-test"
        handle = await env.client.start_workflow(
            AlertInvestigationFlow.run,
            alertmanager_alert,
            id=wf_id,
            task_queue="tq-gates",
        )

        # Gate 2 must still fire on the degraded inconclusive verdict.
        for _ in range(200):
            await asyncio.sleep(0.05)
            if _calls.get("insert_ia"):
                break
        assert _calls.get("insert_ia"), "Gate 2 child never started after assess degraded"
        safe_fp = re.sub(r"[^a-zA-Z0-9._\-]", "-", alertmanager_alert["fingerprint"])[:60]
        gate2_handle = env.client.get_workflow_handle(f"gate2-{safe_fp}-{wf_id}")
        await gate2_handle.signal(InteractionFlow.submit_response, {"value": "ack"})

        result = await asyncio.wait_for(handle.result(), timeout=10.0)

    # The workflow COMPLETED (did not fail with "Activity task timed out") and
    # surfaced an honest inconclusive verdict.
    assert result["status"] == "inconclusive"
    assert _calls.get("assess_called"), "assess_investigation must have been attempted"
    assert not _calls.get("stage_pending_pr")
    assert not _calls.get("create_github_pr")
