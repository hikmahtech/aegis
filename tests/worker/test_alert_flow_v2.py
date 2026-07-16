"""Workflow-level tests for the rewritten AlertInvestigationFlow v2.

Tests the new pipeline: dedup → verification delay → resolve resource →
gather context → investigate → assess → create task.

Uses WorkflowEnvironment.start_time_skipping() with stub activities.
"""

from __future__ import annotations

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
            "check_dedup_result": {"is_duplicate": False},
            "delay_result": {"delay_seconds": 0, "reason": "test"},
            "resolved_check_result": {"resolved": False},
            "resource_result": {
                "resource_id": "res-1",
                "resource_title": "aegis",
                "resource_path": "aegis",
                "github_repo": "youruser/aegis",
                "confidence": 0.9,
                "source": "llm",
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
            "knowledge_result": "Check logs for OOM events",
            "run_investigation_result": {
                "status": "succeeded",
                "output": "Root cause: OOM. Fix: increase memory.",
                "session_id": "sess-1",
                "branch": "",
                "branches": {},
            },
            "investigate_result": {
                "investigation": "Root cause: OOM",
                "actionable": True,
                "auto_fixable": False,
            },
            "assess_result": {
                "status": "actionable",
                "root_cause": "OOM kill",
                "suggested_fix": "Increase memory",
                "confidence": 0.85,
            },
            # tracking
            "run_investigation_called": False,
            "investigate_called": False,
            "assess_called": False,
            "capture_called": False,
            "capture_extra_labels": [],
            "capture_return_id": "real-captured-1",
            "posted_notes": [],
        }
    )
    _state.update(overrides)


# ---------------------------------------------------------------------------
# Stub activities
# ---------------------------------------------------------------------------


@activity.defn(name="check_dedup")
async def stub_check_dedup(fingerprint: str, hours: int) -> dict:
    return _state["check_dedup_result"]


@activity.defn(name="find_open_task_for_signature")
async def stub_find_open_task_for_signature(signature: str) -> str | None:
    return None


@activity.defn(name="record_signature_new_task")
async def stub_record_signature_new_task(signature: str, task_id: str) -> None:
    return None


@activity.defn(name="record_signature_recurrence")
async def stub_record_signature_recurrence(signature: str) -> None:
    return None


@activity.defn(name="get_verification_delay")
async def stub_get_verification_delay(alert: dict) -> dict:
    return _state["delay_result"]


@activity.defn(name="check_alert_resolved")
async def stub_check_alert_resolved(fingerprint: str, window_minutes: int) -> dict:
    return _state["resolved_check_result"]


@activity.defn(name="resolve_alert_resource")
async def stub_resolve_alert_resource(alert: dict) -> dict:
    return _state["resource_result"]


@activity.defn(name="score_resource_relevance")
async def stub_score_resource_relevance(alert: dict, resolved_resource_id: str) -> dict:
    return {"confident": True, "resolved_resource_id": resolved_resource_id, "candidates": []}


@activity.defn(name="gather_alert_knowledge")
async def stub_gather_alert_knowledge(title: str, project: str, alert_name: str = "") -> str:
    return _state["knowledge_result"]


@activity.defn(name="run_investigation")
async def stub_run_investigation(alert: dict, resources: list[dict], runbook: str, *_a) -> dict:
    _state["run_investigation_called"] = True
    return _state["run_investigation_result"]


@activity.defn(name="investigate")
async def stub_investigate(alert: dict, system_prompt: str) -> dict:
    _state["investigate_called"] = True
    return _state["investigate_result"]


@activity.defn(name="assess_investigation")
async def stub_assess_investigation(alert: dict, investigation_output: str) -> dict:
    _state["assess_called"] = True
    return _state["assess_result"]


@activity.defn(name="log_alert")
async def stub_log_alert(alert: dict) -> None:
    _state["log_alert_called"] = True
    _state.setdefault("log_alert_fingerprints", []).append(alert.get("fingerprint"))


@activity.defn(name="send_system_event")
async def stub_send_system_event(msg: str) -> None:
    pass


@activity.defn(name="send_message")
async def stub_send_message(
    agent_id: str, msg: str, chat_id: int, reply_markup: dict | None = None
) -> None:
    pass


@activity.defn(name="enrich_task_spec")
async def stub_enrich_task_spec(draft: dict) -> dict | None:
    return draft


@activity.defn(name="update_task_status")
async def stub_update_task_status(
    task_id: str, status: str, event_type: str, details: str = ""
) -> None:
    pass


@activity.defn(name="check_alert_mute")
async def stub_check_alert_mute_v2(_inp) -> bool:
    return False


@activity.defn(name="write_alert_mute")
async def stub_write_alert_mute_v2(_inp) -> None:
    pass


@activity.defn(name="accumulate_digest_item")
async def stub_accumulate_digest_item(payload: dict) -> None:
    pass


@activity.defn(name="capture_to_inbox")
async def stub_capture_to_inbox(
    source_tag: str,
    external_id: str,
    title: str,
    description: str | None = None,
    extra_labels: list[str] | None = None,
) -> str | None:
    _state["capture_called"] = True
    _state["capture_extra_labels"] = list(extra_labels or [])
    return _state.get("capture_return_id", "real-captured-1")


@activity.defn(name="post_task_note")
async def stub_post_task_note(
    task_id: str,
    content: str,
    file_attachment: dict | None = None,
    workflow_id: str | None = None,
    run_id: str | None = None,
) -> dict:
    _state.setdefault("posted_notes", []).append(
        {
            "task_id": task_id,
            "content": content,
            "file_attachment": file_attachment,
            "workflow_id": workflow_id,
            "run_id": run_id,
        }
    )
    return {"ok": True, "error": None}


@activity.defn(name="upload_kimi_log")
async def stub_upload_kimi_log(output_file: str, filename_hint: str) -> dict:
    _state.setdefault("uploads", []).append(
        {"output_file": output_file, "filename_hint": filename_hint}
    )
    return _state.get(
        "upload_result",
        {
            "ok": True,
            "file_attachment": {"file_url": "x", "file_name": "kimi.log.gz"},
            "file_name": "kimi.log.gz",
            "error": None,
        },
    )


# ── InteractionFlow activity stubs ─────────────────────────────────
# Needed because the post-verdict decision gate (Gate-2, 2026-05-22) spawns
# an InteractionFlow child for every non-Jira non-self-resolved
# investigation. start_time_skipping advances past the 48h archive timeout
# so the child returns status='archived' and the parent continues without
# any explicit signal from these tests.


@activity.defn(name="insert_interaction")
async def stub_insert_interaction_v2(inp: InsertInteractionInput) -> InsertInteractionResult:
    _state.setdefault("insert_ia", []).append((inp.kind, inp.origin))
    return InsertInteractionResult(interaction_id="ia-v2-test")


@activity.defn(name="send_interaction_card")
async def stub_send_card_v2(
    interaction_id: str,
    agent_id: str,
    kind: str,
    prompt: str,
    options,
    allow_hint: bool = False,
) -> dict:
    return {"ok": True, "message_id": 1}


@activity.defn(name="resolve_interaction")
async def stub_resolve_v2(inp: ResolveInteractionInput) -> ResolveInteractionResult:
    return ResolveInteractionResult(already_resolved=False)


@activity.defn(name="apply_interaction_timeout")
async def stub_timeout_v2(inp: ApplyTimeoutInput) -> None:
    return None


@activity.defn(name="resolve_agents")
async def stub_resolve_agents(tags):
    # Seed mapping: infra → pandoras-actor, so behavior is unchanged.
    return {t: {"infra": "pandoras-actor"}.get(t) for t in tags}


@activity.defn(name="get_alert_routing_config")
async def stub_get_alert_routing_config() -> dict:
    return {"infra_cluster": ""}


ALL_ACTIVITIES = [
    stub_resolve_agents,
    stub_get_alert_routing_config,
    stub_check_alert_mute_v2,
    stub_write_alert_mute_v2,
    stub_check_dedup,
    stub_find_open_task_for_signature,
    stub_record_signature_new_task,
    stub_record_signature_recurrence,
    stub_get_verification_delay,
    stub_check_alert_resolved,
    stub_resolve_alert_resource,
    stub_score_resource_relevance,
    stub_gather_alert_knowledge,
    stub_run_investigation,
    stub_investigate,
    stub_assess_investigation,
    stub_log_alert,
    stub_send_system_event,
    stub_send_message,
    stub_accumulate_digest_item,
    stub_capture_to_inbox,
    stub_post_task_note,
    stub_upload_kimi_log,
    stub_insert_interaction_v2,
    stub_send_card_v2,
    stub_resolve_v2,
    stub_timeout_v2,
]


def _make_alert(**overrides) -> dict:
    base = {
        "title": "HighCPU on node-a",
        "fingerprint": "fp-test-123",
        "severity": "error",
        "source": "grafana",
        "description": "CPU usage above 90%",
        "service": "node-exporter",
        "labels": {},
        "raw_payload": {},
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_flow_self_resolved_during_delay():
    """Alert with verification delay -> check_alert_resolved returns True -> self_resolved."""
    _reset(
        delay_result={"delay_seconds": 60, "reason": "test delay"},
        resolved_check_result={"resolved": True},
    )

    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue="test-q",
            workflows=[AlertInvestigationFlow, InteractionFlow],
            activities=ALL_ACTIVITIES,
        ),
    ):
        result = await env.client.execute_workflow(
            AlertInvestigationFlow.run,
            _make_alert(),
            id="test-self-resolved",
            task_queue="test-q",
        )

    assert result["status"] == "self_resolved"
    assert _state["run_investigation_called"] is False
    assert _state["investigate_called"] is False
    assert _state["assess_called"] is False


async def test_flow_resolved_by_investigation():
    """Alert -> no delay -> investigate -> assess returns resolved -> no task."""
    _reset(
        delay_result={"delay_seconds": 0, "reason": "immediate"},
        assess_result={
            "status": "resolved",
            "root_cause": "Transient spike, self-recovered",
            "suggested_fix": "No action needed",
            "confidence": 0.95,
        },
    )

    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue="test-q",
            workflows=[AlertInvestigationFlow, InteractionFlow],
            activities=ALL_ACTIVITIES,
        ),
    ):
        result = await env.client.execute_workflow(
            AlertInvestigationFlow.run,
            _make_alert(),
            id="test-resolved-invest",
            task_queue="test-q",
        )

    assert result["status"] == "resolved"
    assert result["verdict"]["status"] == "resolved"
    assert _state["run_investigation_called"] is True
    assert _state["assess_called"] is True


async def test_flow_actionable_verdict_logs_only():
    """v3 has no tasks table; "actionable" verdict goes through Gate-2.
    Under `start_time_skipping`, the 48h Gate-2 timer fires immediately —
    archived Gate-2 is treated as terminal (sub-fix 2 of workflow-leanness
    sweep): we do NOT auto-emit the verdict ping for an unattended decision."""
    _reset(
        assess_result={
            "status": "actionable",
            "root_cause": "Needs human review",
            "suggested_fix": "Investigate in Grafana",
            "confidence": 0.6,
        },
    )

    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue="test-q",
            workflows=[AlertInvestigationFlow, InteractionFlow],
            activities=ALL_ACTIVITIES,
        ),
    ):
        result = await env.client.execute_workflow(
            AlertInvestigationFlow.run,
            _make_alert(),
            id="test-actionable-logged",
            task_queue="test-q",
        )

    assert result["status"] == "gate2_archived"


async def test_flow_actionable_verdict_goes_through_gate2():
    """v3 has no tasks table; an `actionable` verdict goes through Gate-2.
    Under `start_time_skipping` the 48h Gate-2 timer fires immediately,
    producing `gate2_archived`. `auto_fixable` was removed as a valid
    Haiku verdict status during the workflow-leanness sweep — `actionable`
    covers everything that needs a fix."""
    _reset(
        assess_result={
            "status": "actionable",
            "root_cause": "Null check missing",
            "suggested_fix": "Add guard",
            "confidence": 0.9,
        },
    )

    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue="test-q",
            workflows=[AlertInvestigationFlow, InteractionFlow],
            activities=ALL_ACTIVITIES,
        ),
    ):
        result = await env.client.execute_workflow(
            AlertInvestigationFlow.run,
            _make_alert(severity="warning"),
            id="test-auto-fixable-logged",
            task_queue="test-q",
        )

    assert result["status"] == "gate2_archived"


# ── Pandora ↔ Todoist task-binding (2026-05-20) ────────────────────────


async def test_flow_existing_todoist_task_id_skips_capture():
    """When alert.todoist_task_id is provided (clarify-APP path), the flow
    does NOT call capture_to_inbox; comments target the existing task.

    Under `start_time_skipping` Gate-2 archives in 48h immediately, so the
    flow returns `gate2_archived` before the final-comment fires. The
    start-comment ("investigation has begun") still lands, and the gate-2
    archived skip-comment also targets the existing task — both prove the
    binding works."""
    _reset()
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue="test-q",
            workflows=[AlertInvestigationFlow, InteractionFlow],
            activities=ALL_ACTIVITIES,
        ),
    ):
        result = await env.client.execute_workflow(
            AlertInvestigationFlow.run,
            _make_alert(todoist_task_id="EXISTING_TASK_42"),
            id="test-existing-task-skip-capture",
            task_queue="test-q",
        )
    assert result["status"] == "gate2_archived"
    assert result["todoist_task_id"] == "EXISTING_TASK_42"
    assert _state["capture_called"] is False
    note_targets = {n["task_id"] for n in _state["posted_notes"]}
    assert "EXISTING_TASK_42" in note_targets
    contents = [n["content"] for n in _state["posted_notes"]]
    # Start-comment still fires before Gate-2.
    assert any("investigation has begun" in c.lower() for c in contents)


async def test_flow_no_task_id_captures_with_pandora_label():
    """When no todoist_task_id, the flow creates one via capture_to_inbox
    with extra_labels including @pandora — and comments are addressed to
    the returned id."""
    _reset(capture_return_id="real-new-task-99")
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue="test-q",
            workflows=[AlertInvestigationFlow, InteractionFlow],
            activities=ALL_ACTIVITIES,
        ),
    ):
        result = await env.client.execute_workflow(
            AlertInvestigationFlow.run,
            _make_alert(),
            id="test-no-task-id-captures",
            task_queue="test-q",
        )
    assert _state["capture_called"] is True
    assert "@pandora" in _state["capture_extra_labels"]
    assert result["todoist_task_id"] == "real-new-task-99"
    note_targets = {n["task_id"] for n in _state["posted_notes"]}
    assert "real-new-task-99" in note_targets


async def test_flow_outbox_temp_id_skips_note_posting():
    """If capture lands in outbox (returns temp_id like `item-...`), the
    flow gracefully skips note posting (real id isn't available yet)."""
    _reset(capture_return_id="item-temp-1")
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue="test-q",
            workflows=[AlertInvestigationFlow, InteractionFlow],
            activities=ALL_ACTIVITIES,
        ),
    ):
        await env.client.execute_workflow(
            AlertInvestigationFlow.run,
            _make_alert(),
            id="test-outbox-temp-id",
            task_queue="test-q",
        )
    # Capture happened but no notes were posted (temp_id guard)
    assert _state["capture_called"] is True
    assert len(_state["posted_notes"]) == 0


async def test_flow_jira_timeout_synthesises_verdict_skips_haiku():
    """For Jira-source runs, a `timed_out` run_investigation result means we
    use kimi's partial transcript as the scoping summary and skip Haiku
    entirely. Final status = "logged" (verdict status = actionable), and the
    final-comment carries the [partial] marker so the user can spot it."""
    partial_kimi_output = (
        "Looked at sorted_set.py:286 — uses <= where sorted_set_ric.py:265 "
        "uses <. Likely the windsorization comparison-operator mismatch. "
        "(kimi cut off here)"
    )
    _reset(
        run_investigation_result={
            "status": "timed_out",
            "output": partial_kimi_output,
            "session_id": "sess-jira-1",
            "branch": "",
            "branches": {},
        },
    )

    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue="test-q",
            workflows=[AlertInvestigationFlow, InteractionFlow],
            activities=ALL_ACTIVITIES,
        ),
    ):
        result = await env.client.execute_workflow(
            AlertInvestigationFlow.run,
            _make_alert(
                source="todoist-jira",
                service="acme",
                title="APP-10741: > rule bug",
                todoist_task_id="JIRA_TASK_10741",
            ),
            id="test-jira-timeout-partial",
            task_queue="test-q",
        )

    assert result["status"] == "logged"
    assert result["verdict"]["status"] == "actionable"
    assert "sorted_set" in result["verdict"]["root_cause"]
    # Haiku and the LLM fallback were both skipped
    assert _state["assess_called"] is False
    assert _state["investigate_called"] is False
    # Final-comment uses "Scoping" wording with [partial] marker
    final_notes = [n["content"] for n in _state["posted_notes"]]
    # Pandora-voiced (PR #222): "scoping complete" + "actionable" + "[partial]" marker
    assert any(
        "scoping complete" in c.lower() and "actionable" in c.lower() and "[partial]" in c
        for c in final_notes
    )


async def test_flow_jira_succeeded_uses_scoping_wording():
    """Jira-source successful kimi run uses 'Scoping' wording, not 'Investigation'."""
    _reset(
        run_investigation_result={
            "status": "succeeded",
            "output": "Found bug at sorted_set.py:286\nSTATUS: scoped",
            "session_id": "sess-jira-2",
            "branch": "",
            "branches": {},
        },
        assess_result={
            "status": "actionable",
            "root_cause": "Comparison operator mismatch in sorted_set.py:286",
            "suggested_fix": "Align <= / < across windsorization branch",
            "confidence": 0.85,
        },
    )

    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue="test-q",
            workflows=[AlertInvestigationFlow, InteractionFlow],
            activities=ALL_ACTIVITIES,
        ),
    ):
        result = await env.client.execute_workflow(
            AlertInvestigationFlow.run,
            _make_alert(
                source="todoist-jira",
                service="acme",
                title="APP-10741",
                todoist_task_id="JIRA_TASK_OK",
            ),
            id="test-jira-succeeded",
            task_queue="test-q",
        )

    assert result["status"] == "logged"
    assert _state["assess_called"] is True
    notes = [n["content"] for n in _state["posted_notes"]]
    # Pandora-voiced scoping wording (PR #222)
    assert any("scoping the ticket" in c.lower() for c in notes)
    assert any("scoping complete" in c.lower() and "actionable" in c.lower() for c in notes)
    # No "[partial]" marker on a fully-completed run
    assert not any("[partial]" in c for c in notes)
    # Jira-source uses Summary/Next step, not Root cause/Suggested fix
    assert any("Summary:" in c and "Next step:" in c for c in notes)


async def test_flow_non_jira_timeout_falls_back_to_llm():
    """Non-Jira sources keep the existing behaviour: timed_out → LLM fallback,
    Haiku still gets called on the LLM output. No partial-output short-circuit."""
    _reset(
        run_investigation_result={
            "status": "timed_out",
            "output": "partial stuff that should NOT become the verdict",
            "session_id": "",
            "branch": "",
            "branches": {},
        },
    )

    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue="test-q",
            workflows=[AlertInvestigationFlow, InteractionFlow],
            activities=ALL_ACTIVITIES,
        ),
    ):
        await env.client.execute_workflow(
            AlertInvestigationFlow.run,
            _make_alert(),  # default source=grafana
            id="test-non-jira-timeout-fallback",
            task_queue="test-q",
        )

    # Both the LLM fallback and Haiku ran
    assert _state["investigate_called"] is True
    assert _state["assess_called"] is True


async def test_flow_resource_tag_filter_passes_through():
    """The clarify-APP alert dict carries resource_tag_filter=['acme'];
    the flow passes the entire alert dict to resolve_alert_resource so the
    activity can honour the filter when querying candidates."""
    captured: dict = {}

    @activity.defn(name="resolve_alert_resource")
    async def stub_resolve_with_capture(alert: dict) -> dict:
        captured["alert"] = alert
        return _state["resource_result"]

    # Replace the existing resolve stub with our capturing version
    replaced = []
    for a in ALL_ACTIVITIES:
        if a is stub_resolve_alert_resource:
            replaced.append(stub_resolve_with_capture)
        else:
            replaced.append(a)

    _reset()
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue="test-q",
            workflows=[AlertInvestigationFlow, InteractionFlow],
            activities=replaced,
        ),
    ):
        await env.client.execute_workflow(
            AlertInvestigationFlow.run,
            _make_alert(
                todoist_task_id="EXISTING_TASK_77",
                resource_tag_filter=["acme"],
            ),
            id="test-resource-tag-filter",
            task_queue="test-q",
        )
    assert captured["alert"]["resource_tag_filter"] == ["acme"]
    assert captured["alert"]["todoist_task_id"] == "EXISTING_TASK_77"


# ── Audit fixes: self-resolve dedup + Gate-2 source-guard ──


async def test_flow_self_resolved_writes_log_alert_for_dedup():
    """Self-resolved alerts must call log_alert so the 24h dedup window
    short-circuits flapping re-deliveries. Pre-fix: self_resolved skipped
    log_alert → dedup leak → every re-fire spun up a full investigation."""
    _reset(
        delay_result={"delay_seconds": 60, "reason": "test delay"},
        resolved_check_result={"resolved": True},
    )

    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue="test-q",
            workflows=[AlertInvestigationFlow, InteractionFlow],
            activities=ALL_ACTIVITIES,
        ),
    ):
        await env.client.execute_workflow(
            AlertInvestigationFlow.run,
            _make_alert(),
            id="test-self-resolved-dedup",
            task_queue="test-q",
        )

    assert _state.get("log_alert_called") is True, (
        "self_resolved path must call log_alert to register the dedup record"
    )


async def test_flow_jira_source_skips_gate2_even_with_branches():
    """For source='todoist-jira', kimi MAY accidentally emit a BRANCH: line
    despite the scoping prompt forbidding fixes. The flow must NOT enter
    Gate-2 because the repo metadata points at acme repos we don't
    own — staging a `pending_pr` would risk an unauthorised push.
    Verified by asserting the workflow terminates in 'logged' status with
    no Gate-2 child workflow spawned."""
    _reset(
        delay_result={"delay_seconds": 0, "reason": "immediate"},
        run_investigation_result={
            # Branches present + kimi succeeded — the unguarded code path
            # would have spawned Gate-2 here.
            "status": "succeeded",
            "output": "Found a possible fix\nSTATUS: scoped",
            "session_id": "sess-jira",
            "branch": "acme-fix/x",
            "branches": {"screener-p-server": "acme-fix/x"},
        },
        assess_result={
            "status": "actionable",
            "root_cause": "x",
            "suggested_fix": "y",
            "confidence": 0.8,
        },
    )

    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue="test-q",
            workflows=[AlertInvestigationFlow, InteractionFlow],
            activities=ALL_ACTIVITIES,
        ),
    ):
        result = await env.client.execute_workflow(
            AlertInvestigationFlow.run,
            _make_alert(
                source="todoist-jira",
                service="acme",
                fingerprint="jira-T_GATE2_GUARD",
                todoist_task_id="JIRA_TASK_X",
            ),
            id="test-jira-gate2-skip",
            task_queue="test-q",
        )

    # Flow terminated normally (logged), no PR staging side-effects.
    assert result["status"] in {"logged", "actionable"}, result["status"]
