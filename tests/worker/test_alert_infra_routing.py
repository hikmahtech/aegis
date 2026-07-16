"""Tests for prod-fixes-2026-06-14: infra alert routing, resolve guard, storm collapse.

Covers:
1. is_infra_alert() classification
2. build_alert_signature() infra storm-collapse (cluster+alertname key)
3. resolve_infra_resource() activity
4. resolve_alert_resource() guard (activity raises → flow continues, no hard failure)
5. Infra alert → infra-gitops forced, Gate-0 skipped (flow-level)
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from aegis_worker.activities.alerts import (
    AlertActivities,
    build_alert_signature,
    is_infra_alert,
)
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
from temporalio.testing import ActivityEnvironment, WorkflowEnvironment
from temporalio.worker import Worker

# ---------------------------------------------------------------------------
# is_infra_alert — pure function
# ---------------------------------------------------------------------------


def test_is_infra_alert_nodedown():
    alert = {
        "source": "alertmanager",
        "labels": {"alertname": "NodeDown", "cluster": "homelab-swarm"},
    }
    assert is_infra_alert(alert) is True


def test_is_infra_alert_dockerservicedown():
    alert = {
        "source": "alertmanager",
        "labels": {"alertname": "DockerServiceDown"},
    }
    assert is_infra_alert(alert) is True


def test_is_infra_alert_cluster_label_alone():
    """A configured cluster label is sufficient even with an unknown alertname."""
    alert = {
        "source": "alertmanager",
        "labels": {"alertname": "SomeUnknownAlert", "cluster": "my-swarm"},
    }
    assert is_infra_alert(alert, infra_cluster="my-swarm") is True


def test_is_infra_alert_cluster_label_off_by_default():
    """With no configured cluster (default blank), a cluster label alone does
    NOT classify an alert as infra — only alertname matching does."""
    alert = {
        "source": "alertmanager",
        "labels": {"alertname": "SomeUnknownAlert", "cluster": "my-swarm"},
    }
    assert is_infra_alert(alert, infra_cluster="") is False


def test_is_infra_alert_lokidown():
    alert = {"source": "alertmanager", "labels": {"alertname": "LokiDown"}}
    assert is_infra_alert(alert) is True


def test_is_infra_alert_prometheusdown():
    alert = {"source": "alertmanager", "labels": {"alertname": "PrometheusDown"}}
    assert is_infra_alert(alert) is True


def test_is_infra_alert_case_insensitive():
    """alertname matching is lowercased."""
    alert = {"source": "alertmanager", "labels": {"alertname": "NODEDOWN"}}
    assert is_infra_alert(alert) is True


def test_is_infra_alert_sentry_not_infra():
    alert = {
        "source": "sentry",
        "service": "bcp",
        "labels": {"alertname": "Exception"},
    }
    assert is_infra_alert(alert) is False


def test_is_infra_alert_grafana_app_alert():
    alert = {
        "source": "grafana",
        "service": "aegis",
        "labels": {"alertname": "HighMemoryUsage", "cluster": "app-cluster"},
    }
    assert is_infra_alert(alert) is False


def test_is_infra_alert_no_labels():
    alert = {"source": "alertmanager"}
    assert is_infra_alert(alert) is False


def test_is_infra_alert_dagster_pipeline_failure():
    alert = {
        "source": "alertmanager",
        "labels": {"alertname": "Dagster Pipeline Failure"},
    }
    assert is_infra_alert(alert) is True


# ---------------------------------------------------------------------------
# build_alert_signature — infra storm collapse
# ---------------------------------------------------------------------------


def test_build_signature_infra_alert_collapses_by_cluster_alertname():
    """NodeDown across different instances should map to one signature."""
    alert_a = {
        "source": "alertmanager",
        "service": "node-b",
        "labels": {"alertname": "NodeDown", "cluster": "homelab-swarm", "instance": "node-b"},
    }
    alert_b = {
        "source": "alertmanager",
        "service": "node-a",
        "labels": {"alertname": "NodeDown", "cluster": "homelab-swarm", "instance": "node-a"},
    }
    sig_a = build_alert_signature(alert_a)
    sig_b = build_alert_signature(alert_b)
    # Both collapse to the same signature (cluster, not instance)
    assert sig_a == sig_b
    assert "nodedown" in sig_a
    assert "homelab-swarm" in sig_a
    # instance/service NOT in the key
    assert "node-b" not in sig_a
    assert "node-a" not in sig_b


def test_build_signature_infra_alert_different_alertname_different_sig():
    alert_nodedown = {
        "source": "alertmanager",
        "labels": {"alertname": "NodeDown", "cluster": "homelab-swarm"},
    }
    alert_servicedown = {
        "source": "alertmanager",
        "labels": {"alertname": "DockerServiceDown", "cluster": "homelab-swarm"},
    }
    assert build_alert_signature(alert_nodedown) != build_alert_signature(alert_servicedown)


def test_build_signature_sentry_alert_unchanged():
    """Existing Sentry signature behaviour must not be broken."""
    alert = {
        "source": "sentry",
        "service": "bcp",
        "raw_payload": {"metadata": {"type": "KeyError", "value": "foo"}},
    }
    assert build_alert_signature(alert) == "sentry-class:bcp:KeyError"


def test_build_alert_signature_infra_cluster_param():
    alert = {
        "source": "alertmanager",
        "labels": {"alertname": "SomeAppAlert", "cluster": "my-swarm", "instance": "node-a"},
    }
    # cluster match only via the explicit param now
    assert build_alert_signature(alert, infra_cluster="my-swarm").startswith("alertmanager-class:")
    assert build_alert_signature(alert) != build_alert_signature(alert, infra_cluster="my-swarm")


async def test_get_alert_routing_config_activity():
    act = AlertActivities(infra_cluster="homelab-swarm")
    env = ActivityEnvironment()
    assert await env.run(act.get_alert_routing_config) == {"infra_cluster": "homelab-swarm"}


def test_build_signature_non_infra_alertmanager_uses_service():
    """Non-infra alertmanager alerts still key on service."""
    alert = {
        "source": "alertmanager",
        "service": "my-app",
        "labels": {"alertname": "SomeAppAlert"},
    }
    sig = build_alert_signature(alert)
    assert "my-app" in sig
    assert "someappalert" in sig


# ---------------------------------------------------------------------------
# resolve_infra_resource — activity
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_db_pool():
    pool = AsyncMock()
    pool.fetchrow.return_value = None
    pool.execute.return_value = "OK"
    return pool


async def test_resolve_infra_resource_found(mock_db_pool):
    """Returns infra-gitops resource when row exists."""
    import json

    mock_db_pool.fetchrow.return_value = {
        "id": "aaaabbbb-cccc-dddd-eeee-111122223333",
        "title": "infra-gitops",
        "metadata": json.dumps({"path": "infra-gitops", "github_repo": "example/infra-gitops"}),
    }
    activities = AlertActivities(db_pool=mock_db_pool)
    env = ActivityEnvironment()
    alert = {
        "source": "alertmanager",
        "labels": {"alertname": "NodeDown", "cluster": "homelab-swarm"},
    }
    result = await env.run(activities.resolve_infra_resource, alert)
    assert result["source"] == "infra"
    assert result["confidence"] == 1.0
    assert result["github_repo"] == "example/infra-gitops"
    assert len(result["resources"]) == 1
    assert result["resource_id"] is not None


async def test_resolve_infra_resource_not_found_returns_null(mock_db_pool):
    """Falls back to null-resource when infra-gitops row is missing."""
    mock_db_pool.fetchrow.return_value = None
    activities = AlertActivities(db_pool=mock_db_pool)
    env = ActivityEnvironment()
    alert = {"source": "alertmanager", "labels": {"alertname": "NodeDown"}}
    result = await env.run(activities.resolve_infra_resource, alert)
    assert result["source"] == "none"
    assert result["confidence"] == 0.0
    assert result["resources"] == []


async def test_resolve_infra_resource_no_pool():
    """Returns null-resource gracefully when db_pool is None."""
    activities = AlertActivities(db_pool=None)
    env = ActivityEnvironment()
    alert = {"source": "alertmanager", "labels": {"alertname": "NodeDown"}}
    result = await env.run(activities.resolve_infra_resource, alert)
    assert result["source"] == "none"
    assert result["resource_id"] is None


async def test_resolve_infra_resource_db_error_returns_null(mock_db_pool):
    """DB exception falls back to null-resource, does not propagate."""
    mock_db_pool.fetchrow.side_effect = RuntimeError("connection refused")
    activities = AlertActivities(db_pool=mock_db_pool)
    env = ActivityEnvironment()
    alert = {"source": "alertmanager", "labels": {"alertname": "DockerServiceDown"}}
    result = await env.run(activities.resolve_infra_resource, alert)
    assert result["source"] == "none"
    assert result["confidence"] == 0.0


# ---------------------------------------------------------------------------
# Flow-level: resolve guard + infra routing (using WorkflowEnvironment)
# ---------------------------------------------------------------------------

_flow_state: dict = {}


def _reset_flow(**overrides):
    _flow_state.clear()
    _flow_state.update(
        {
            "check_dedup_result": {"is_duplicate": False},
            "delay_result": {"delay_seconds": 0, "reason": "test"},
            "resolved_check_result": {"resolved": False},
            "resolve_infra_result": {
                "resource_id": "homelab-res-1",
                "resource_title": "infra-gitops",
                "resource_path": "infra-gitops",
                "github_repo": "example/infra-gitops",
                "confidence": 1.0,
                "source": "infra",
                "resources": [
                    {
                        "resource_id": "homelab-res-1",
                        "resource_title": "infra-gitops",
                        "resource_path": "infra-gitops",
                        "github_repo": "example/infra-gitops",
                        "confidence": 1.0,
                    }
                ],
            },
            "resolve_alert_resource_raises": False,
            "resolve_alert_resource_result": {
                "resource_id": None,
                "resource_title": None,
                "resource_path": None,
                "github_repo": "",
                "confidence": 0.0,
                "source": "none",
                "resources": [],
            },
            "knowledge_result": "Check swarm state",
            "run_investigation_result": {
                "status": "succeeded",
                "output": "Root cause: node dropped off swarm.",
                "session_id": "sess-infra-1",
                "branch": "",
                "branches": {},
            },
            "investigate_result": {
                "investigation": "Infra root cause: node down",
                "actionable": True,
                "auto_fixable": False,
            },
            "assess_result": {
                "status": "actionable",
                "root_cause": "node dropped off homelab swarm",
                "suggested_fix": "Rejoin node to swarm",
                "confidence": 0.8,
            },
            "score_resource_called": False,
            "run_investigation_called": False,
            "investigate_called": False,
            "capture_return_id": "real-captured-infra-1",
        }
    )
    _flow_state.update(overrides)


# ── Stub activities ──────────────────────────────────────────────────────────


@activity.defn(name="check_dedup")
async def _stub_check_dedup(fingerprint: str, hours: int) -> dict:
    return _flow_state["check_dedup_result"]


@activity.defn(name="find_open_task_for_signature")
async def _stub_find_open_task(signature: str) -> str | None:
    return None


@activity.defn(name="record_signature_new_task")
async def _stub_record_new_task(signature: str, task_id: str) -> None:
    return None


@activity.defn(name="record_signature_recurrence")
async def _stub_record_recurrence(signature: str) -> None:
    return None


@activity.defn(name="get_verification_delay")
async def _stub_verification_delay(alert: dict) -> dict:
    return _flow_state["delay_result"]


@activity.defn(name="check_alert_resolved")
async def _stub_check_alert_resolved(fingerprint: str, window_minutes: int) -> dict:
    return _flow_state["resolved_check_result"]


@activity.defn(name="resolve_infra_resource")
async def _stub_resolve_infra_resource(alert: dict) -> dict:
    _flow_state["resolve_infra_called"] = True
    return _flow_state["resolve_infra_result"]


@activity.defn(name="resolve_alert_resource")
async def _stub_resolve_alert_resource(alert: dict) -> dict:
    _flow_state["resolve_alert_resource_called"] = True
    if _flow_state.get("resolve_alert_resource_raises"):
        raise RuntimeError("LLM proxy timed out")
    return _flow_state["resolve_alert_resource_result"]


@activity.defn(name="score_resource_relevance")
async def _stub_score_resource_relevance(alert: dict, resolved_resource_id: str) -> dict:
    _flow_state["score_resource_called"] = True
    return {"confident": True, "resolved_resource_id": resolved_resource_id, "candidates": []}


@activity.defn(name="gather_alert_knowledge")
async def _stub_gather_knowledge(title: str, project: str, alert_name: str = "") -> str:
    return _flow_state["knowledge_result"]


@activity.defn(name="run_investigation")
async def _stub_run_investigation(alert: dict, resources: list[dict], runbook: str, *_a) -> dict:
    _flow_state["run_investigation_called"] = True
    return _flow_state["run_investigation_result"]


@activity.defn(name="investigate")
async def _stub_investigate(alert: dict, system_prompt: str) -> dict:
    _flow_state["investigate_called"] = True
    return _flow_state["investigate_result"]


@activity.defn(name="assess_investigation")
async def _stub_assess_investigation(alert: dict, investigation_output: str) -> dict:
    return _flow_state["assess_result"]


@activity.defn(name="log_alert")
async def _stub_log_alert(alert: dict) -> None:
    pass


@activity.defn(name="send_system_event")
async def _stub_send_system_event(msg: str) -> None:
    pass


@activity.defn(name="send_message")
async def _stub_send_message(
    agent_id: str, msg: str, chat_id: int, reply_markup: dict | None = None
) -> None:
    _flow_state.setdefault("sends", []).append(agent_id)


@activity.defn(name="check_alert_mute")
async def _stub_check_alert_mute(_inp) -> bool:
    return False


@activity.defn(name="write_alert_mute")
async def _stub_write_alert_mute(_inp) -> None:
    pass


@activity.defn(name="accumulate_digest_item")
async def _stub_accumulate_digest_item(payload: dict) -> None:
    pass


@activity.defn(name="capture_to_inbox")
async def _stub_capture_to_inbox(
    source_tag: str,
    external_id: str,
    title: str,
    description: str | None = None,
    extra_labels: list[str] | None = None,
) -> str | None:
    return _flow_state.get("capture_return_id", "real-captured-1")


@activity.defn(name="post_task_note")
async def _stub_post_task_note(
    task_id: str,
    content: str,
    file_attachment: dict | None = None,
    workflow_id: str | None = None,
    run_id: str | None = None,
) -> dict:
    _flow_state.setdefault("posted_notes", []).append({"task_id": task_id, "content": content})
    return {"ok": True, "error": None}


@activity.defn(name="upload_kimi_log")
async def _stub_upload_kimi_log(output_file: str, filename_hint: str, host: str = "") -> dict:
    return {"ok": False, "file_attachment": None, "file_name": "", "error": "stub"}


@activity.defn(name="record_verdict_to_kg")
async def _stub_record_verdict_to_kg(alert: dict, verdict: dict, investigation_output: str) -> dict:
    return {"ingested": False, "reason": "stub"}


@activity.defn(name="get_alert_routing_config")
async def _stub_get_alert_routing_config() -> dict:
    return {"infra_cluster": _flow_state.get("infra_cluster", "")}


@activity.defn(name="insert_interaction")
async def _stub_insert_interaction(inp: InsertInteractionInput) -> InsertInteractionResult:
    return InsertInteractionResult(interaction_id="ia-infra-test")


@activity.defn(name="send_interaction_card")
async def _stub_send_card(
    interaction_id: str,
    agent_id: str,
    kind: str,
    prompt: str,
    options,
    allow_hint: bool = False,
) -> dict:
    _flow_state.setdefault("card_agents", []).append(agent_id)
    return {"ok": True, "message_id": 1}



@activity.defn(name="resolve_interaction")
async def _stub_resolve_interaction(inp: ResolveInteractionInput) -> ResolveInteractionResult:
    return ResolveInteractionResult(already_resolved=False)


@activity.defn(name="apply_interaction_timeout")
async def _stub_apply_timeout(inp: ApplyTimeoutInput) -> None:
    return None


@activity.defn(name="resolve_agents")
async def _stub_resolve_agents(tags):
    # Default seed mapping (infra → pandoras-actor); a test can override the
    # resolution via _flow_state["infra_map"] (e.g. {} for the no-holder case).
    mapping = _flow_state.get("infra_map", {"infra": "pandoras-actor"})
    return {t: mapping.get(t) for t in tags}


_ALL_FLOW_ACTIVITIES = [
    _stub_resolve_agents,
    _stub_check_alert_mute,
    _stub_write_alert_mute,
    _stub_check_dedup,
    _stub_find_open_task,
    _stub_record_new_task,
    _stub_record_recurrence,
    _stub_verification_delay,
    _stub_check_alert_resolved,
    _stub_resolve_infra_resource,
    _stub_resolve_alert_resource,
    _stub_score_resource_relevance,
    _stub_gather_knowledge,
    _stub_run_investigation,
    _stub_investigate,
    _stub_assess_investigation,
    _stub_log_alert,
    _stub_send_system_event,
    _stub_send_message,
    _stub_accumulate_digest_item,
    _stub_capture_to_inbox,
    _stub_post_task_note,
    _stub_upload_kimi_log,
    _stub_record_verdict_to_kg,
    _stub_get_alert_routing_config,
    _stub_insert_interaction,
    _stub_send_card,
    _stub_resolve_interaction,
    _stub_apply_timeout,
]


def _make_infra_alert(**overrides) -> dict:
    base = {
        "title": "NodeDown: node-b (homelab-swarm)",
        "fingerprint": "infra-fp-001",
        "severity": "critical",
        "source": "alertmanager",
        "description": "Node node-b is down",
        "service": "node-b",
        "labels": {"alertname": "NodeDown", "cluster": "homelab-swarm", "instance": "node-b"},
        "raw_payload": {},
    }
    base.update(overrides)
    return base


def _make_app_alert(**overrides) -> dict:
    base = {
        "title": "High error rate in bcp",
        "fingerprint": "app-fp-001",
        "severity": "error",
        "source": "sentry",
        "description": "NullPointerException in bcp",
        "service": "bcp",
        "labels": {"alertname": "Exception"},
        "raw_payload": {"metadata": {"type": "NullPointerException"}},
    }
    base.update(overrides)
    return base


async def test_infra_alert_routes_to_homelab_gitops_skips_gate0():
    """Infra alert uses resolve_infra_resource (not LLM resolve) and skips Gate-0.

    Under start_time_skipping, Gate-2 archives after 48h immediately →
    gate2_archived. The key assertions are:
    - resolve_infra_resource WAS called
    - resolve_alert_resource (LLM) was NOT called
    - score_resource_relevance (Gate-0) was NOT called
    - run_investigation WAS called (not the LLM fallback investigate)
    """
    _reset_flow()
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue="test-infra-q",
            workflows=[AlertInvestigationFlow, InteractionFlow],
            activities=_ALL_FLOW_ACTIVITIES,
        ),
    ):
        result = await env.client.execute_workflow(
            AlertInvestigationFlow.run,
            _make_infra_alert(),
            id="test-infra-homelab-route",
            task_queue="test-infra-q",
        )

    assert result["status"] == "gate2_archived"
    assert _flow_state.get("resolve_infra_called") is True
    assert _flow_state.get("resolve_alert_resource_called") is not True
    assert _flow_state.get("score_resource_called") is not True
    assert _flow_state.get("run_investigation_called") is True
    assert _flow_state.get("investigate_called") is not True


async def test_resolve_alert_resource_raises_flow_continues():
    """When resolve_alert_resource raises (LLM/proxy failure), the flow
    continues via the LLM-only investigate() path instead of dying.

    The guard converts the activity exception into a null-resource dict,
    which causes run_investigation to not be called (no resource_path) and
    the LLM fallback investigate() to run instead.
    """
    _reset_flow(
        resolve_alert_resource_raises=True,
    )
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue="test-guard-q",
            workflows=[AlertInvestigationFlow, InteractionFlow],
            activities=_ALL_FLOW_ACTIVITIES,
        ),
    ):
        result = await env.client.execute_workflow(
            AlertInvestigationFlow.run,
            _make_app_alert(),
            id="test-resolve-raises-guard",
            task_queue="test-guard-q",
        )

    # Flow must NOT fail with an activity error — it completes
    assert result["status"] in {"gate2_archived", "logged", "inconclusive", "not_actionable"}
    # resolve_alert_resource was attempted (not infra alert)
    assert _flow_state.get("resolve_alert_resource_called") is True
    # run_investigation was NOT called (null resource → no code path)
    assert _flow_state.get("run_investigation_called") is not True
    # LLM fallback investigate() WAS called
    assert _flow_state.get("investigate_called") is True


async def test_app_alert_still_uses_llm_resolve():
    """Non-infra alerts still go through the LLM resolve path."""
    _reset_flow()
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue="test-app-route-q",
            workflows=[AlertInvestigationFlow, InteractionFlow],
            activities=_ALL_FLOW_ACTIVITIES,
        ),
    ):
        await env.client.execute_workflow(
            AlertInvestigationFlow.run,
            _make_app_alert(),
            id="test-app-alert-llm-resolve",
            task_queue="test-app-route-q",
        )

    assert _flow_state.get("resolve_alert_resource_called") is True
    assert _flow_state.get("resolve_infra_called") is not True


# ── Issue #36: infra behavior-tag resolution (replaces the _PANDORA literal) ──


async def test_no_infra_agent_skips_investigation():
    """When no active agent holds the `infra` tag, the flow skips cleanly
    instead of driving the pipeline as a hardcoded 'pandoras-actor'."""
    _reset_flow(infra_map={})
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue="test-noinfra-q",
            workflows=[AlertInvestigationFlow, InteractionFlow],
            activities=_ALL_FLOW_ACTIVITIES,
        ),
    ):
        result = await env.client.execute_workflow(
            AlertInvestigationFlow.run,
            _make_infra_alert(),
            id="test-no-infra-agent-skip",
            task_queue="test-noinfra-q",
        )

    assert result["status"] == "skipped_no_infra_agent"
    # Nothing was investigated or delivered.
    assert _flow_state.get("run_investigation_called") is not True
    assert _flow_state.get("sends", []) == []


async def test_custom_infra_agent_receives_delivery():
    """A renamed infra agent (not 'pandoras-actor') owns the pipeline: all
    chat delivery is addressed to whichever id holds the `infra` tag."""
    _reset_flow(infra_map={"infra": "custom-ops"})
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue="test-custominfra-q",
            workflows=[AlertInvestigationFlow, InteractionFlow],
            activities=_ALL_FLOW_ACTIVITIES,
        ),
    ):
        await env.client.execute_workflow(
            AlertInvestigationFlow.run,
            _make_infra_alert(),
            id="test-custom-infra-agent",
            task_queue="test-custominfra-q",
        )

    # Every agent-addressed action (chat sends + interaction cards) goes to the
    # resolved infra agent, never the old 'pandoras-actor' literal.
    addressed = _flow_state.get("sends", []) + _flow_state.get("card_agents", [])
    assert addressed, "expected at least one agent-addressed action"
    assert all(a == "custom-ops" for a in addressed)
    assert "pandoras-actor" not in addressed
