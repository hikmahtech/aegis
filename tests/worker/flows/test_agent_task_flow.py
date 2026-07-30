"""AgentTaskSweepFlow / AgentTaskFlow — dispatch and unknown-verb parking."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

import pytest
from aegis_worker.flows.agent_task import (
    AgentTaskFlow,
    AgentTaskFlowInput,
    AgentTaskSweepConfig,
    AgentTaskSweepFlow,
)
from temporalio import activity, workflow
from temporalio.client import WorkflowFailureError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

# Module is `interaction` (singular) — imported inside imports_passed_through
# per repo convention (mirror tests/worker/flows/test_agent_task_coding.py:15).
with workflow.unsafe.imports_passed_through():
    from aegis_worker.flows.interaction import InteractionFlowInput, InteractionResult

_TASK = {
    "id": "tf-1",
    "content": "PROLONGED: redis_redis degraded for over 2 hours",
    "description": "",
    "labels": ["@pandora"],
    "source_tag": "#chat",  # deliberately an unmapped verb
    "project_id": "p1",
    "assignee_label": "@pandora",
}


async def test_unknown_verb_parks_the_task_and_never_leaves_it_in_the_pool():
    calls: list[tuple[str, str]] = []

    @activity.defn(name="load_task_context")
    async def load_task_context(task_id: str) -> dict:
        return {"external_id": "", "fingerprint": "", "gmail_message_id": ""}

    @activity.defn(name="comment")
    async def comment(task_id: str, agent_id: str, body: str) -> dict:
        calls.append(("comment", body))
        return {"ok": True}

    @activity.defn(name="park_task")
    async def park_task(task_id: str, reason: str) -> dict:
        calls.append(("park", reason))
        return {"parked": True}

    async with await WorkflowEnvironment.start_time_skipping() as env:
        queue = f"tq-{uuid.uuid4()}"
        async with Worker(
            env.client,
            task_queue=queue,
            workflows=[AgentTaskFlow],
            activities=[load_task_context, comment, park_task],
        ):
            result = await env.client.execute_workflow(
                AgentTaskFlow.run,
                AgentTaskFlowInput(
                    agent_id="pandoras-actor", todoist_task_id="tf-1", task=_TASK
                ),
                id=f"agent-task-tf-1-{uuid.uuid4()}",
                task_queue=queue,
            )

    assert result["verb"] == "unknown"
    assert result["status"] == "parked"
    assert any(kind == "park" for kind, _ in calls)


async def test_activity_failure_still_parks_the_task_before_the_flow_fails():
    """Regression: AgentTaskFlow.run must reach a terminal state even when a
    step raises — otherwise the task is never parked, stays eligible, and the
    6h cooldown re-picks (and re-fails) it forever."""
    calls: list[tuple[str, str]] = []

    @activity.defn(name="load_task_context")
    async def load_task_context(task_id: str) -> dict:
        return {"external_id": "", "fingerprint": "", "gmail_message_id": ""}

    @activity.defn(name="comment")
    async def comment(task_id: str, agent_id: str, body: str) -> dict:
        raise RuntimeError("todoist unavailable")

    @activity.defn(name="park_task")
    async def park_task(task_id: str, reason: str) -> dict:
        calls.append(("park", reason))
        return {"parked": True}

    async with await WorkflowEnvironment.start_time_skipping() as env:
        queue = f"tq-{uuid.uuid4()}"
        async with Worker(
            env.client,
            task_queue=queue,
            workflows=[AgentTaskFlow],
            activities=[load_task_context, comment, park_task],
        ):
            with pytest.raises(WorkflowFailureError):
                await env.client.execute_workflow(
                    AgentTaskFlow.run,
                    AgentTaskFlowInput(
                        agent_id="pandoras-actor", todoist_task_id="tf-1", task=_TASK
                    ),
                    id=f"agent-task-tf-1-{uuid.uuid4()}",
                    task_queue=queue,
                )

    assert any(kind == "park" for kind, _ in calls), (
        "the task must be parked even though the flow ultimately fails"
    )


async def test_sweep_spawns_one_child_per_task_and_does_not_await_them():
    @activity.defn(name="find_actionable_tasks")
    async def find_actionable_tasks(
        max_tasks: int = 3, cooldown_hours: int = 6, max_coding: int = 1
    ) -> list[dict]:
        return [dict(_TASK, id=f"tf-{n}") for n in range(1, 4)]

    async with await WorkflowEnvironment.start_time_skipping() as env:
        queue = f"tq-{uuid.uuid4()}"
        async with Worker(
            env.client,
            task_queue=queue,
            workflows=[AgentTaskSweepFlow, AgentTaskFlow],
            activities=[find_actionable_tasks],
        ):
            result = await env.client.execute_workflow(
                AgentTaskSweepFlow.run,
                AgentTaskSweepConfig(agent_id="pandoras-actor"),
                id=f"sweep-{uuid.uuid4()}",
                task_queue=queue,
            )

    assert result == {"found": 3, "spawned": 3}


# --- Issue #154: parametrised proof over all 17 AgentTaskFlow.run exit paths ---
#
# `find_actionable_tasks` excludes @waiting, so every exit MUST complete or
# park the task — otherwise the 6h cooldown re-picks (and re-fails) it
# forever. This is the single mechanical proof of that invariant: one case
# per terminal return/raise statement in AgentTaskFlow (17 total — 2 in
# run(), 3 in _run_infra, 2 in _run_email, 2 in _run_finance, 8 in
# _run_coding; see issue #154 for the full enumeration).
#
# Each case asserts the ACTUAL park/complete activity call fired, not just
# the returned status string — the literal `return {...}` dict on every exit
# is unchanged by deleting the park_task call above it, so asserting on the
# return value alone would not be falsifiable.

# Module-level stubs — Temporal does not allow @workflow.defn on local
# classes (see tests/worker/test_clarify_flow_agent_spawn.py:14). Only three
# distinct response shapes are needed across all 17 cases; which card an
# instance is resolving is read from the InteractionFlowInput it's actually
# given (`input.origin`), not from any external test state.
@workflow.defn(name="InteractionFlow")
class _StubInteractionApprove:
    @workflow.run
    async def run(self, input: InteractionFlowInput) -> InteractionResult:
        return InteractionResult(interaction_id="ia-stub", status="resolved", response={"value": "approve"})


@workflow.defn(name="InteractionFlow")
class _StubInteractionSkip:
    @workflow.run
    async def run(self, input: InteractionFlowInput) -> InteractionResult:
        return InteractionResult(interaction_id="ia-stub", status="resolved", response={"value": "skip"})


@workflow.defn(name="InteractionFlow")
class _StubInteractionApprovePlanThenSkipPr:
    """Only case needing two different cards resolved differently: plan
    approved, then the PR card declined."""

    @workflow.run
    async def run(self, input: InteractionFlowInput) -> InteractionResult:
        value = "approve" if input.origin == "agent_task_coding_plan" else "skip"
        return InteractionResult(interaction_id="ia-stub", status="resolved", response={"value": value})


_ALERT_TASK = dict(_TASK, source_tag="#alert", content="PROLONGED: redis_redis degraded for over 2 hours")
_NO_SVC_TASK = dict(_TASK, source_tag="#alert", content="Something went wrong today")
_EMAIL_TASK = dict(_TASK, source_tag="#email", content="a note")
_FINANCE_TASK = dict(_TASK, source_tag="#receipt", content="Anomaly: something weird")
_CODE_TASK = dict(_TASK, source_tag=None, labels=["@code"], content="Fix the bug")

_REPO_OK = {"github_repo": "org/repo", "repo_path": "repo", "source": "project_map", "candidates": []}
_INVEST_OK = {"status": "running", "transcript": "", "run_id": "r1", "output_file": "inv.jsonl", "host": "h"}
_PLAN_OK = {"status": "succeeded", "transcript": "do the fix"}
_IMPL_RUNNING = {
    "status": "running", "transcript": "", "branch": "aegis-task/x",
    "run_id": "r2", "output_file": "impl.jsonl", "host": "h",
}


@dataclass
class _ExitCase:
    id: str
    task: dict
    responses: dict = field(default_factory=dict)
    interaction_stub: type = _StubInteractionApprove
    comment_raises: bool = False
    expect_raises: bool = False
    expect_status: str | None = None


_CASES = [
    _ExitCase("run_unknown_verb", _TASK, expect_status="parked"),
    _ExitCase("run_catch_all_except", _TASK, comment_raises=True, expect_raises=True),
    _ExitCase("infra_no_service_name", _NO_SVC_TASK, expect_status="parked"),
    _ExitCase(
        "infra_healthy", _ALERT_TASK,
        {"service_health": {"found": True, "healthy": True, "detail": "1/1"}},
        expect_status="resolved",
    ),
    _ExitCase(
        "infra_unhealthy_carded", _ALERT_TASK,
        {"service_health": {"found": True, "healthy": False, "detail": "0/1"}},
        expect_status="carded",
    ),
    _ExitCase(
        "email_archived", _EMAIL_TASK,
        {"triage_email": {"action": "archived", "account": "acct1"}},
        expect_status="archived",
    ),
    _ExitCase(
        "email_parked", _EMAIL_TASK,
        {"triage_email": {"action": "needs_human", "account": ""}},
        expect_status="parked",
    ),
    _ExitCase(
        "finance_no_merchant", _FINANCE_TASK,
        {"merchant_history": {"merchant": "", "charges": [], "summary": ""}},
        expect_status="parked",
    ),
    _ExitCase(
        "finance_carded", _FINANCE_TASK,
        {"merchant_history": {"merchant": "Acme", "charges": [], "summary": "..."}},
        expect_status="carded",
    ),
    _ExitCase(
        "coding_no_repo", _CODE_TASK,
        {"resolve_task_repo": {"github_repo": "", "repo_path": "", "source": "none", "candidates": []}},
        expect_status="parked",
    ),
    _ExitCase(
        "coding_investigation_failed", _CODE_TASK,
        {
            "resolve_task_repo": _REPO_OK,
            "run_task_investigation": {"status": "failed", "transcript": "", "run_id": ""},
        },
        expect_status="parked",
    ),
    _ExitCase(
        "coding_empty_transcript", _CODE_TASK,
        {
            "resolve_task_repo": _REPO_OK,
            "run_task_investigation": _INVEST_OK,
            "collect_coding_run": {"inv.jsonl": {"status": "succeeded", "transcript": ""}},
        },
        expect_status="parked",
    ),
    _ExitCase(
        "coding_plan_declined", _CODE_TASK,
        {
            "resolve_task_repo": _REPO_OK,
            "run_task_investigation": _INVEST_OK,
            "collect_coding_run": {"inv.jsonl": _PLAN_OK},
        },
        interaction_stub=_StubInteractionSkip,
        expect_status="plan_declined",
    ),
    _ExitCase(
        "coding_implementation_not_succeeded", _CODE_TASK,
        {
            "resolve_task_repo": _REPO_OK,
            "run_task_investigation": _INVEST_OK,
            "collect_coding_run": {
                "inv.jsonl": _PLAN_OK,
                "impl.jsonl": {"status": "failed", "transcript": ""},
            },
            "run_task_implementation": _IMPL_RUNNING,
        },
        expect_status="parked",
    ),
    _ExitCase(
        "coding_pr_declined", _CODE_TASK,
        {
            "resolve_task_repo": _REPO_OK,
            "run_task_investigation": _INVEST_OK,
            "collect_coding_run": {
                "inv.jsonl": _PLAN_OK,
                "impl.jsonl": {"status": "succeeded", "transcript": "done"},
            },
            "run_task_implementation": _IMPL_RUNNING,
        },
        interaction_stub=_StubInteractionApprovePlanThenSkipPr,
        expect_status="pr_declined",
    ),
    _ExitCase(
        "coding_pr_failed", _CODE_TASK,
        {
            "resolve_task_repo": _REPO_OK,
            "run_task_investigation": _INVEST_OK,
            "collect_coding_run": {
                "inv.jsonl": _PLAN_OK,
                "impl.jsonl": {"status": "succeeded", "transcript": "done"},
            },
            "run_task_implementation": _IMPL_RUNNING,
            "create_github_pr": {"pr_url": "", "status": "failed", "error": "git push failed"},
        },
        expect_status="pr_failed",
    ),
    _ExitCase(
        "coding_pr_opened", _CODE_TASK,
        {
            "resolve_task_repo": _REPO_OK,
            "run_task_investigation": _INVEST_OK,
            "collect_coding_run": {
                "inv.jsonl": _PLAN_OK,
                "impl.jsonl": {"status": "succeeded", "transcript": "done"},
            },
            "run_task_implementation": _IMPL_RUNNING,
            "create_github_pr": {
                "pr_url": "https://github.com/org/repo/pull/1", "status": "opened", "error": "",
            },
        },
        expect_status="pr_opened",
    ),
]

assert len(_CASES) == 17, "one case per AgentTaskFlow exit — see issue #154"


def _exit_case_activities(events: list, case: _ExitCase):
    r = case.responses

    @activity.defn(name="load_task_context")
    async def load_task_context(task_id: str) -> dict:
        return {"external_id": "", "fingerprint": "", "gmail_message_id": ""}

    @activity.defn(name="comment")
    async def comment(task_id: str, agent_id: str, body: str) -> dict:
        events.append(("comment", body))
        if case.comment_raises:
            raise RuntimeError("todoist unavailable")
        return {"ok": True}

    @activity.defn(name="park_task")
    async def park_task(task_id: str, reason: str) -> dict:
        events.append(("park", reason))
        return {"parked": True}

    @activity.defn(name="complete_task")
    async def complete_task(task_id: str) -> dict:
        events.append(("complete", task_id))
        return {"completed": True}

    @activity.defn(name="service_health")
    async def service_health(service_name: str) -> dict:
        return r["service_health"]

    @activity.defn(name="service_logs")
    async def service_logs(service_name: str, lines: int = 50) -> dict:
        return {"logs": "boot loop"}

    @activity.defn(name="triage_email")
    async def triage_email(task_id: str, title: str, gmail_message_id: str) -> dict:
        return r["triage_email"]

    @activity.defn(name="merchant_history")
    async def merchant_history(title: str, limit: int = 6) -> dict:
        return r["merchant_history"]

    @activity.defn(name="resolve_task_repo")
    async def resolve_task_repo(task: dict) -> dict:
        return r["resolve_task_repo"]

    @activity.defn(name="run_task_investigation")
    async def run_task_investigation(
        task_id: str, title: str, description: str, repo_path: str, github_repo: str
    ) -> dict:
        return r["run_task_investigation"]

    @activity.defn(name="collect_coding_run")
    async def collect_coding_run(output_file: str, host: str, max_polls: int = 40) -> dict:
        return r["collect_coding_run"][output_file]

    @activity.defn(name="run_task_implementation")
    async def run_task_implementation(
        task_id: str, title: str, description: str, plan: str, repo_path: str, github_repo: str
    ) -> dict:
        return r["run_task_implementation"]

    @activity.defn(name="stage_pending_pr")
    async def stage_pending_pr(inp) -> str:
        return "pr-uuid-stub"

    @activity.defn(name="create_github_pr")
    async def create_github_pr(inp) -> dict:
        return r["create_github_pr"]

    return [
        load_task_context, comment, park_task, complete_task,
        service_health, service_logs, triage_email, merchant_history,
        resolve_task_repo, run_task_investigation, collect_coding_run,
        run_task_implementation, stage_pending_pr, create_github_pr,
    ]


@pytest.mark.parametrize("case", _CASES, ids=[c.id for c in _CASES])
async def test_every_exit_path_ends_completed_or_parked(case: _ExitCase):
    """Issue #154: one row per AgentTaskFlow exit. Falsifiable by construction
    — deleting a park_task/complete_task call in the source makes exactly the
    case(s) exercising that branch fail, because the assertion is on the
    activity call actually firing, not on the (unchanged) return literal."""
    events: list = []
    raised = False
    result: dict = {}
    async with await WorkflowEnvironment.start_time_skipping() as env:
        queue = f"tq-{uuid.uuid4()}"
        async with Worker(
            env.client,
            task_queue=queue,
            workflows=[AgentTaskFlow, case.interaction_stub],
            activities=_exit_case_activities(events, case),
        ):
            wf_input = AgentTaskFlowInput(
                agent_id="pandoras-actor",
                todoist_task_id=f"{case.id}-1",
                task=dict(case.task, id=f"{case.id}-1"),
            )
            if case.expect_raises:
                with pytest.raises(WorkflowFailureError):
                    await env.client.execute_workflow(
                        AgentTaskFlow.run,
                        wf_input,
                        id=f"agent-task-{case.id}-{uuid.uuid4()}",
                        task_queue=queue,
                    )
                raised = True
            else:
                result = await env.client.execute_workflow(
                    AgentTaskFlow.run,
                    wf_input,
                    id=f"agent-task-{case.id}-{uuid.uuid4()}",
                    task_queue=queue,
                )

    if raised:
        assert any(kind == "park" for kind, _ in events), f"{case.id}: must park before re-raising"
        return

    assert result["status"] == case.expect_status
    terminal = "complete" if case.expect_status in ("resolved", "archived") else "park"
    assert any(kind == terminal for kind, _ in events), (
        f"{case.id}: expected a {terminal} activity call, got {events}"
    )
