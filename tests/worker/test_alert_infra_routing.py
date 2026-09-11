"""Tests for prod-fixes-2026-06-14: infra alert routing, resolve guard, storm collapse.

Covers:
1. is_infra_alert() classification
2. get_alert_routing_config() activity
3. resolve_infra_resource() activity
4. resolve_alert_resource() guard (activity raises → flow continues, no hard failure)
5. Infra alert → infra-gitops forced, Gate-0 skipped (flow-level)
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from aegis.services import infra_alert_routing
from aegis.services.infra_alert_routing import DEFAULT_INFRA_ALERTNAMES
from aegis_worker.activities.alerts import (
    AlertActivities,
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


def test_is_infra_alert_servicedownprolonged_without_cluster_label():
    """ServiceDownProlonged is already in _REMEDIABLE_ALERTNAMES, but
    _safe_remediate_infra only runs inside the is_infra_alert branch — so
    missing here (with `infra_cluster` at its blank default) meant Prometheus'
    2h escalation, and InfraHeartbeatFlow's #138 re-investigation, went down
    the LLM repo-match path and never got their force-restart."""
    alert = {
        "source": "aegis-heartbeat",
        "labels": {"alertname": "ServiceDownProlonged", "service_name": "miniflux_miniflux"},
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


def test_is_infra_alert_dagster_pipeline_failure_is_setup_config_not_a_default():
    """A Dagster alert is infra only because this deployment says so in the
    `infra_alert_routing` row (#498). The code default names nobody's setup;
    a history recorded before the move (no list) still replays as infra."""
    alert = {
        "source": "alertmanager",
        "labels": {"alertname": "Dagster Pipeline Failure"},
    }
    assert is_infra_alert(alert, "", sorted(DEFAULT_INFRA_ALERTNAMES)) is False
    assert is_infra_alert(alert, "", ["dagster pipeline failure"]) is True
    assert is_infra_alert(alert) is True


# ---------------------------------------------------------------------------
# get_alert_routing_config — activity
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _fresh_routing_cache():
    """The infra routing read is cached 30s per process; never let one test's
    row leak into the next."""
    infra_alert_routing._cache.update(value=None, ts=0.0)
    yield
    infra_alert_routing._cache.update(value=None, ts=0.0)


async def test_get_alert_routing_config_activity():
    act = AlertActivities(infra_cluster="homelab-swarm", slack_owner_member_id="U042")
    env = ActivityEnvironment()
    assert await env.run(act.get_alert_routing_config) == {
        "infra_cluster": "homelab-swarm",
        "slack_owner_member_id": "U042",
        # No pool: the generic defaults, never an empty list.
        "infra_alertnames": sorted(DEFAULT_INFRA_ALERTNAMES),
    }


# ---------------------------------------------------------------------------
# resolve_infra_resource — activity
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_db_pool():
    pool = AsyncMock()
    pool.fetchrow.return_value = None
    pool.execute.return_value = "OK"
    return pool


async def _infra_routing(db_pool, repo: str) -> None:
    await db_pool.execute("DELETE FROM settings WHERE key = 'infra_alert_routing'")
    if repo:
        await db_pool.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES ('infra_alert_routing', $1, NOW())",
            {"repo": repo},
        )
    infra_alert_routing._cache.update(value=None, ts=0.0)


async def test_resolve_infra_resource_found(db_pool):
    """Resolves to the repository the `infra_alert_routing` row names.

    This was #119: the infra repo used to be a slug/repo pair hard-coded for
    one deployment, and a mismatch sent every infra alert to source="none".
    It is now configuration, so a fork points it at its own repo."""
    await db_pool.execute("DELETE FROM resources WHERE slug = 'test-infra-gitops'")
    rid = await db_pool.fetchval(
        "INSERT INTO resources (kind, slug, title, metadata) VALUES "
        "('repository', 'test-infra-gitops', 'infra-gitops', $1) RETURNING id",
        {"path": "ops/infra-gitops", "github_repo": "example/infra-gitops"},
    )
    await _infra_routing(db_pool, "example/infra-gitops")
    try:
        alert = {
            "source": "alertmanager",
            "labels": {"alertname": "NodeDown", "cluster": "homelab-swarm"},
        }
        result = await ActivityEnvironment().run(
            AlertActivities(db_pool=db_pool).resolve_infra_resource, alert
        )
        assert result["source"] == "infra"
        assert result["confidence"] == 1.0
        assert result["github_repo"] == "example/infra-gitops"
        assert result["resource_id"] == str(rid)
        assert result["resource_path"] == "ops/infra-gitops"
        assert len(result["resources"]) == 1
    finally:
        await db_pool.execute("DELETE FROM resources WHERE slug = 'test-infra-gitops'")
        await _infra_routing(db_pool, "")


async def test_resolve_infra_resource_not_found_returns_null(db_pool):
    """Falls back to null-resource when the configured repo has no row."""
    await _infra_routing(db_pool, "example/no-such-repo")
    try:
        alert = {"source": "alertmanager", "labels": {"alertname": "NodeDown"}}
        result = await ActivityEnvironment().run(
            AlertActivities(db_pool=db_pool).resolve_infra_resource, alert
        )
        assert result["source"] == "none"
        assert result["confidence"] == 0.0
        assert result["resources"] == []
    finally:
        await _infra_routing(db_pool, "")


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
        }
    )
    _flow_state.update(overrides)


# ── Stub activities ──────────────────────────────────────────────────────────


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
    # _a = (engine_override, allow_fix); runbook is the knowledge context the
    # flow built, infra framing included.
    _flow_state["run_investigation_args"] = {
        "resources": resources,
        "runbook": runbook,
        "allow_fix": _a[1] if len(_a) > 1 else True,
    }
    return _flow_state["run_investigation_result"]


@activity.defn(name="investigate")
async def _stub_investigate(alert: dict, system_prompt: str) -> dict:
    _flow_state["investigate_called"] = True
    return _flow_state["investigate_result"]


@activity.defn(name="assess_investigation")
async def _stub_assess_investigation(alert: dict, investigation_output: str) -> dict:
    return _flow_state["assess_result"]


@activity.defn(name="send_system_event")
async def _stub_send_system_event(msg: str) -> None:
    pass


@activity.defn(name="send_message")
async def _stub_send_message(
    agent_id: str, msg: str, chat_id: int, reply_markup: dict | None = None
) -> None:
    _flow_state.setdefault("sends", []).append(agent_id)


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
async def _stub_record_verdict_to_kg(
    alert: dict, verdict: dict, investigation_output: str, outcome: str = ""
) -> dict:
    return {"ingested": False, "reason": "stub"}


@activity.defn(name="get_alert_routing_config")
async def _stub_get_alert_routing_config() -> dict:
    routing = {"infra_cluster": _flow_state.get("infra_cluster", "")}
    # Absent unless a test sets it — the shape a pre-#498 history recorded.
    if _flow_state.get("infra_alertnames") is not None:
        routing["infra_alertnames"] = _flow_state["infra_alertnames"]
    return routing


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


_ALL_FLOW_ACTIVITIES = [
    stub_ingest_alert,
    stub_problem_status,
    stub_record_investigation,
    stub_mute_problem,
    stub_verification_delay,
    _stub_resolve_agents,
    _stub_resolve_infra_resource,
    _stub_resolve_alert_resource,
    _stub_score_resource_relevance,
    _stub_gather_knowledge,
    _stub_run_investigation,
    _stub_investigate,
    _stub_assess_investigation,
    _stub_send_system_event,
    _stub_send_message,
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

    The investigation proposes no commands and the alert does not escalate,
    so the verdict needs no decision and no Gate-2 card goes out (#500): the
    run ends `logged`. The key assertions are:
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

    assert result["status"] == "logged"
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


# ── Issue #498: a claimed Dagster failure is investigated as application code ──

# The infra framing the flow prepends to the knowledge context (Step 5.5).
_INFRA_HINT = "Docker Swarm / homelab infrastructure alert"

# The shape AlertInvestigationFlow receives for a Dagster run failure in prod.
_DAGSTER_LABELS = {
    "alertname": "Dagster Pipeline Failure",
    "service": "dagster",
    "severity": "critical",
    "pipeline_name": "crypto_ml_training",
    "asset_or_job": "crypto_ml_training",
    "failed_step": "ml__crypto__training__crypto_ml_training",
    "error_class": "InvalidOperationError",
    "run_id": "16b4476d-5803-4f03-827c-7845a1ff894f",
    "partition_key": "2026-09-01",
    "backfill_id": "-",
    "grafana_folder": "Infrastructure",
}

_PIPELINE_REPO_RESOURCE = {
    "resource_id": "pipeline-res-1",
    "resource_title": "acme/trading-pipeline",
    "resource_path": "code/trading-pipeline",
    "github_repo": "acme/trading-pipeline",
    "engine": "",
    "claude_account": "",
    "confidence": 1.0,
}


def _make_dagster_alert() -> dict:
    return _make_infra_alert(
        title="Dagster Failed: crypto_ml_training [2026-09-01]",
        fingerprint="dagster-fp-001",
        service="",
        description="Job name: crypto_ml_training\nError Type: InvalidOperationError",
        labels=dict(_DAGSTER_LABELS),
    )


async def _run_dagster_flow(workflow_id: str) -> dict:
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue=f"q-{workflow_id}",
            workflows=[AlertInvestigationFlow, InteractionFlow],
            activities=_ALL_FLOW_ACTIVITIES,
        ),
    ):
        return await env.client.execute_workflow(
            AlertInvestigationFlow.run,
            _make_dagster_alert(),
            id=workflow_id,
            task_queue=f"q-{workflow_id}",
        )


async def test_claimed_dagster_failure_is_investigated_as_application_code():
    """Dagster is on the infra list, but the pipeline repo claims this job. The
    run goes to that repo, may stage a fix, and is not told it is looking at a
    swarm problem. Gate-0 is skipped: the claim is the operator's explicit
    mapping, and the scorer would ask "which repo?" for every Dagster failure."""
    _hub_reset()
    _reset_flow(
        infra_alertnames=["nodedown", "dagster pipeline failure"],
        resolve_infra_result={
            **_PIPELINE_REPO_RESOURCE,
            "source": "label_claim",
            "resources": [_PIPELINE_REPO_RESOURCE],
        },
    )

    await _run_dagster_flow("test-dagster-claimed")

    assert _flow_state.get("resolve_infra_called") is True
    assert _flow_state.get("resolve_alert_resource_called") is not True
    assert _flow_state.get("score_resource_called") is not True
    args = _flow_state["run_investigation_args"]
    assert args["resources"][0]["github_repo"] == "acme/trading-pipeline"
    assert args["allow_fix"] is True
    assert _INFRA_HINT not in args["runbook"]


async def test_unclaimed_dagster_failure_keeps_the_infra_investigation():
    """No repo claims it: it stays an investigate-only infra run, as before."""
    _hub_reset()
    _reset_flow(infra_alertnames=["nodedown", "dagster pipeline failure"])

    await _run_dagster_flow("test-dagster-unclaimed")

    assert _flow_state.get("resolve_infra_called") is True
    assert _flow_state.get("score_resource_called") is not True
    args = _flow_state["run_investigation_args"]
    assert args["resources"][0]["github_repo"] == "example/infra-gitops"
    assert args["allow_fix"] is False
    assert _INFRA_HINT in args["runbook"]


async def test_the_configured_list_decides_infra_inside_the_flow():
    """Take Dagster off the infra list in the DB and the flow sends its
    failures down the normal resolution ladder, not to the infra repo."""
    _hub_reset()
    _reset_flow(infra_alertnames=["nodedown"])

    await _run_dagster_flow("test-dagster-not-infra")

    assert _flow_state.get("resolve_alert_resource_called") is True
    assert _flow_state.get("resolve_infra_called") is not True
