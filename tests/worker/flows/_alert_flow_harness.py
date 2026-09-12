"""Stub activities and a fake decision card for AlertInvestigationFlow tests.

Shared by the no-card tests (#500), the restart-once tests (#501) and the
replay tests. Every stub carries the real activity's name and returns the
shape the real one returns, so the flow cannot tell them apart; each records
what it was called with in `S`, which a test resets with `reset()`.

`FakeInteractionFlow` stands in for the Gate-2 card: it records the card it
was asked to post and answers at once with `S.answer` (or times out, when
`S.card_status` is `archived`), so a test that expects no card can simply
check `S.cards` is empty.

`S.kg` is every knowledge-store write with its outcome and how many cards had
gone out when it happened (#502: the verdict is stored after the decision).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from aegis.services.infra_alert_routing import DEFAULT_INFRA_ALERTNAMES
from temporalio import activity, workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

with workflow.unsafe.imports_passed_through():
    from aegis_worker.flows.interaction import InteractionFlowInput, InteractionResult


@dataclass
class _State:
    records: list[dict] = field(default_factory=list)
    notes: list[tuple[str, str]] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)
    cards: list[InteractionFlowInput] = field(default_factory=list)
    investigations: list[list] = field(default_factory=list)
    restarts: list[dict] = field(default_factory=list)
    restart_checks: list[tuple[str, dict]] = field(default_factory=list)
    answer: dict = field(default_factory=lambda: {"value": "ack"})
    run_investigation: dict = field(default_factory=dict)
    verdict: dict = field(default_factory=dict)
    remediation: dict = field(default_factory=dict)
    restart_history: dict | Exception = field(default_factory=dict)
    kg: list[dict] = field(default_factory=list)
    card_status: str = "resolved"
    pr_url: str = "https://github.com/acme/shop/pull/7"
    staged: list[Any] = field(default_factory=list)


S = _State()


def reset(**overrides: Any) -> _State:
    S.__init__()  # type: ignore[misc]
    S.run_investigation = {
        "status": "succeeded",
        "output": "Looked at the logs. Nothing to change.",
        "session_id": "sess-1",
        "branch": "",
        "branches": {},
    }
    S.verdict = {
        "status": "not_actionable",
        "root_cause": "An upstream API was down for four minutes.",
        "suggested_fix": "",
        "confidence": 0.8,
    }
    S.remediation = {
        "attempted": False,
        "recovered": False,
        "service": "",
        "command": "",
        "output": "",
        "reason": "not_remediable_class:x",
        "diagnostics": [],
    }
    S.restart_history = {"repeat": False, "window_minutes": 60}
    for key, value in overrides.items():
        setattr(S, key, value)
    return S


_RESOURCE = {
    "resource_id": "res-1",
    "resource_title": "shop",
    "resource_path": "shop",
    "github_repo": "acme/shop",
    "confidence": 0.9,
}


@activity.defn(name="resolve_agents")
async def stub_resolve_agents(tags: list[str]) -> dict:
    return {t: {"infra": "pandoras-actor"}.get(t) for t in tags}


@activity.defn(name="get_alert_routing_config")
async def stub_routing() -> dict:
    return {
        "infra_cluster": "",
        "slack_owner_member_id": "",
        "infra_alertnames": sorted(DEFAULT_INFRA_ALERTNAMES),
    }


@activity.defn(name="ingest_alert")
async def stub_ingest_alert(alert: dict, resolved: bool = False) -> dict:
    return {
        "problem_id": "prob-1",
        "action": "created",
        "key": "k",
        "occurrences": 1,
        "suppressed": False,
        "muted": False,
        "absorbed": False,
        "investigate": True,
        "todoist_task_id": alert.get("todoist_task_id") or "task-1",
    }


@activity.defn(name="problem_status")
async def stub_problem_status(problem_id: str) -> dict:
    return {
        "found": True,
        "status": "open",
        "resolved": False,
        "occurrences": 1,
        "todoist_task_id": "task-1",
        "muted": False,
    }


@activity.defn(name="record_investigation")
async def stub_record_investigation(inp: dict) -> dict:
    S.records.append(inp)
    return {"recorded": True, "status_changed": True}


@activity.defn(name="project_problem")
async def stub_project_problem(problem_id: str) -> dict:
    """The flow asks for this once its verification delay is over, to learn a
    task the settle window deferred (#537). Here the task already exists."""
    return {"task_id": "task-1", "skipped": ""}


@activity.defn(name="mute_problem")
async def stub_mute_problem(problem_id: str, hours: float, by: str = "gate2") -> dict:
    return {"muted_until": "2026-09-12T12:00:00+00:00"}


@activity.defn(name="verification_delay")
async def stub_verification_delay(alert: dict) -> dict:
    return {"delay_seconds": 0}


@activity.defn(name="resolve_alert_resource")
async def stub_resolve_alert_resource(alert: dict) -> dict:
    return {**_RESOURCE, "source": "service", "resources": [_RESOURCE]}


@activity.defn(name="resolve_infra_resource")
async def stub_resolve_infra_resource(alert: dict) -> dict:
    infra = {**_RESOURCE, "resource_title": "infra", "github_repo": "acme/infra"}
    return {**infra, "source": "infra", "resources": [infra]}


@activity.defn(name="score_resource_relevance")
async def stub_score_resource_relevance(alert: dict, resolved_resource_id: str) -> dict:
    return {"confident": True, "resolved_resource_id": resolved_resource_id, "candidates": []}


@activity.defn(name="gather_alert_knowledge")
async def stub_gather_alert_knowledge(title: str, project: str, alert_name: str = "") -> str:
    return ""


@activity.defn(name="run_investigation")
async def stub_run_investigation(
    alert: dict,
    resources: list[dict],
    runbook: str,
    engine_override: str = "",
    allow_fix: bool = True,
) -> dict:
    # `runbook` is what the flow calls knowledge_context: runbook, prior
    # incidents, and the infra framing, in that order.
    S.investigations.append([alert, resources, runbook, engine_override, allow_fix])
    return S.run_investigation


@activity.defn(name="investigate")
async def stub_investigate(alert: dict, agent_system_prompt: str = "") -> dict:
    return {"investigation": "LLM-only look", "actionable": False, "auto_fixable": False}


@activity.defn(name="assess_investigation")
async def stub_assess_investigation(alert: dict, investigation_output: str) -> dict:
    return S.verdict


@activity.defn(name="record_verdict_to_kg")
async def stub_record_verdict_to_kg(
    alert: dict, verdict: dict, investigation_output: str, outcome: str = ""
) -> dict:
    S.kg.append(
        {
            "outcome": outcome,
            "verdict": verdict.get("status"),
            "output": investigation_output,
            "cards_before": len(S.cards),
        }
    )
    return {"ingested": True, **({"outcome": outcome} if outcome else {})}


@activity.defn(name="post_task_note")
async def stub_post_task_note(
    task_id: str,
    content: str,
    file_attachment: dict | None = None,
    workflow_id: str | None = None,
    run_id: str | None = None,
) -> dict:
    S.notes.append((task_id, content))
    return {"ok": True, "error": None}


@activity.defn(name="send_system_event")
async def stub_send_system_event(message: str, chat_id: int = 0) -> dict:
    return {"ok": True}


@activity.defn(name="send_message")
async def stub_send_message(
    agent_id: str,
    message: str,
    chat_id: int = 0,
    thread_ref: dict | None = None,
    thread_overflow: bool = False,
) -> dict:
    S.messages.append(message)
    return {"ok": True}


@activity.defn(name="send_voice")
async def stub_send_voice(agent_id: str, text: str) -> dict:
    return {"ok": True}


@activity.defn(name="remediate_infra_service")
async def stub_remediate_infra_service(alert: dict) -> dict:
    S.restarts.append(alert)
    return S.remediation


@activity.defn(name="recent_auto_restart")
async def stub_recent_auto_restart(problem_id: str, alert: dict) -> dict:
    S.restart_checks.append((problem_id, alert))
    if isinstance(S.restart_history, Exception):
        raise S.restart_history
    return S.restart_history


@activity.defn(name="run_remediation_commands")
async def stub_run_remediation_commands(commands: list[str], host: str = "") -> dict:
    return {"ran": [], "refused": "no_commands"}


@activity.defn(name="stage_pending_pr")
async def stub_stage_pending_pr(inp: Any) -> str:
    # The real one returns a PLAIN STRING id, not a dict.
    S.staged.append(inp)
    return "pending-pr-1"


@activity.defn(name="create_github_pr")
async def stub_create_github_pr(inp: Any) -> dict:
    if not S.pr_url:
        return {"pr_url": "", "status": "failed", "error": "gh pr create failed"}
    return {"pr_url": S.pr_url, "status": "opened", "error": ""}


STUBS = [
    stub_resolve_agents,
    stub_routing,
    stub_ingest_alert,
    stub_problem_status,
    stub_record_investigation,
    stub_project_problem,
    stub_mute_problem,
    stub_verification_delay,
    stub_resolve_alert_resource,
    stub_resolve_infra_resource,
    stub_score_resource_relevance,
    stub_gather_alert_knowledge,
    stub_run_investigation,
    stub_investigate,
    stub_assess_investigation,
    stub_record_verdict_to_kg,
    stub_post_task_note,
    stub_send_system_event,
    stub_send_message,
    stub_send_voice,
    stub_remediate_infra_service,
    stub_recent_auto_restart,
    stub_run_remediation_commands,
    stub_stage_pending_pr,
    stub_create_github_pr,
]


@workflow.defn(name="InteractionFlow", sandboxed=False)
class FakeInteractionFlow:
    """The Gate-2 card, answered the moment it is posted."""

    @workflow.signal
    async def submit_response(self, response: dict) -> None:
        return None

    @workflow.run
    async def run(self, input: InteractionFlowInput) -> InteractionResult:
        S.cards.append(input)
        if S.card_status == "archived":
            return InteractionResult(interaction_id="ia-1", status="archived", response=None)
        return InteractionResult(interaction_id="ia-1", status="resolved", response=S.answer)


def app_alert(**overrides: Any) -> dict:
    """An application alert: not infra, so no swarm framing and no commands."""
    return {
        "title": "TimeoutError in checkout",
        "fingerprint": "sentry:12345",
        "severity": "error",
        "source": "sentry",
        "service": "shop",
        "description": "checkout timed out",
        "labels": {},
        "raw_payload": {},
        "problem_id": "prob-1",
        "todoist_task_id": "task-1",
        **overrides,
    }


def service_down_alert(service: str = "shop_web", **overrides: Any) -> dict:
    """A swarm service below its replicas: infra, and eligible for the restart."""
    return {
        "title": f"Service {service} down",
        "fingerprint": f"aegis-heartbeat:DockerServiceDown:{service}",
        "severity": "critical",
        "source": "aegis-heartbeat",
        "service": service,
        "description": f"{service} below desired replicas",
        "labels": {"alertname": "DockerServiceDown", "service_name": service},
        "escalate": False,
        "problem_id": "prob-1",
        "todoist_task_id": "task-1",
        **overrides,
    }


def fix_branch() -> None:
    """The investigation committed a fix to the `shop` checkout, so the card
    offers Open PR(s) and Discard."""
    S.run_investigation = {
        **S.run_investigation,
        "branch": "aegis-fix/checkout",
        "branches": {"shop": "aegis-fix/checkout"},
    }
    S.verdict = {**S.verdict, "status": "actionable"}


def task_queue() -> str:
    return f"tq-alert-{uuid.uuid4().hex[:8]}"


async def run_flow(flow_cls: type, alert: dict, *, activities: list | None = None) -> dict:
    tq = task_queue()
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue=tq,
            workflows=[flow_cls, FakeInteractionFlow],
            activities=activities or STUBS,
        ),
    ):
        return await env.client.execute_workflow(
            "AlertInvestigationFlow",
            alert,
            id=f"alert-{uuid.uuid4().hex[:8]}",
            task_queue=tq,
        )


def steps(records: list[dict]) -> list[str]:
    """The step names the flow recorded on the problem, in order."""
    return [r["external_id"].rsplit(":", 1)[-1] for r in records]
