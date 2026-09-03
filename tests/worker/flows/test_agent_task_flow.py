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

    @activity.defn(name="find_task_turns_due")
    async def find_task_turns_due(limit: int = 20) -> list[dict]:
        return []

    async with await WorkflowEnvironment.start_time_skipping() as env:
        queue = f"tq-{uuid.uuid4()}"
        async with Worker(
            env.client,
            task_queue=queue,
            workflows=[AgentTaskSweepFlow, AgentTaskFlow],
            activities=[find_actionable_tasks, find_task_turns_due],
        ):
            result = await env.client.execute_workflow(
                AgentTaskSweepFlow.run,
                AgentTaskSweepConfig(agent_id="pandoras-actor"),
                id=f"sweep-{uuid.uuid4()}",
                task_queue=queue,
            )

    assert result == {"found": 3, "spawned": 3, "resumed": 0}


# --- Issue #154: parametrised proof over all 17 AgentTaskFlow.run exit paths ---
#
# `find_actionable_tasks` excludes @waiting, so every exit MUST complete or
# park the task — otherwise the 6h cooldown re-picks (and re-fails) it
# forever. This is the single mechanical proof of that invariant: one case
# per terminal return/raise statement in AgentTaskFlow (17 total — 3 in
# run(), 3 in _run_infra, 2 in _run_email, 2 in _run_finance, 7 in
# _run_coding; see issue #154 for the original enumeration).
#
# THREE exits deliberately do not park, and each carries its own terminal proof
# instead (`case.expect_terminal`):
#   * `unknown_task` — the task was deleted before we loaded it. There is
#     nothing to park and nothing to comment on.
#   * `you_are_in_it` with a HUMAN owner — the operator is sitting in this
#     task's session, so the comment is already in front of them. Parking would
#     stamp @waiting on a task somebody is actively working; what stops the
#     fallback sweep re-dispatching the same comment is the `record_task_turn`
#     watermark, so THAT is what the case asserts.
#   * `you_are_in_it` with an AEGIS owner — an orphan turn of our own. The
#     comment has been read by nobody, so this exit deliberately leaves the
#     task IN the pool: the fallback sweep must re-dispatch it once the run
#     ends. "No park and no watermark" is the correct terminal state here, and
#     `test_agent_task_coding.py` pins the missing watermark specifically.
#
# Each case asserts the ACTUAL park/complete/record activity call fired, not
# just the returned status string — the literal `return {...}` dict on every
# exit is unchanged by deleting the park_task call above it, so asserting on
# the return value alone would not be falsifiable.

# Module-level stub — Temporal does not allow @workflow.defn on local classes
# (see tests/worker/test_clarify_flow_agent_spawn.py:14). One shape covers
# every remaining card: only the infra and finance verbs raise one, and both
# park immediately afterwards whatever the answer is. The coding verb no
# longer cards anything — it comments and waits for the user's reply instead.
@workflow.defn(name="InteractionFlow")
class _StubInteractionApprove:
    @workflow.run
    async def run(self, input: InteractionFlowInput) -> InteractionResult:
        return InteractionResult(interaction_id="ia-stub", status="resolved", response={"value": "approve"})


_ALERT_TASK = dict(_TASK, source_tag="#alert", content="PROLONGED: redis_redis degraded for over 2 hours")
_NO_SVC_TASK = dict(_TASK, source_tag="#alert", content="Something went wrong today")
_EMAIL_TASK = dict(_TASK, source_tag="#email", content="a note")
_FINANCE_TASK = dict(_TASK, source_tag="#receipt", content="Anomaly: something weird")
_CODE_TASK = dict(_TASK, source_tag=None, labels=["@code"], content="Fix the bug")

_SESSION = {
    "task_id": "x", "agent_id": "pandoras-actor", "session_id": "sess-1",
    "repo": "repo", "github_repo": "org/repo", "branch": "aegis-task/x",
    "worktree_path": "/srv/repo-aegis-wt/task-x", "host": "h",
    "slack_ref": "", "turns": 0, "last_turn_at": "", "created_at": "",
}
_ENSURE_READY = {"status": "ready", "session": _SESSION, "candidates": [], "error": ""}
_PROCEED = {"verdict": "proceed", "session": None, "sessions": [], "reason": ""}
_LAUNCH_OK = {
    "status": "running", "run_id": "r1", "output_file": "turn.jsonl", "host": "h",
    "engine": "claude", "tmux_window": "w", "worktree_path": _SESSION["worktree_path"],
    "error": "",
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
    # Which activity call proves this exit reached a terminal state. "park" and
    # "complete" are the two label writes; "record" is the session watermark
    # (the you_are_in_it exit, which must NOT park); "none" is a task that no
    # longer exists to write anything to.
    expect_terminal: str = "park"
    # Start the flow with an EMPTY task dict — the webhook/sweep-fallback shape,
    # which makes run() load the task through the `load_task` activity.
    load_from_id: bool = False


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
        "coding_repo_ambiguous", _CODE_TASK,
        {
            "ensure_task_session": {
                "status": "candidates", "session": _SESSION, "error": "",
                "candidates": [{"github_repo": "org/one"}, {"github_repo": "org/two"}],
            },
        },
        expect_status="repo_ambiguous",
    ),
    _ExitCase(
        "coding_repo_unresolved", _CODE_TASK,
        {
            "ensure_task_session": {
                "status": "unresolved", "session": None, "candidates": [], "error": "no repo",
            },
        },
        expect_status="parked",
    ),
    _ExitCase(
        "coding_you_are_in_it", _CODE_TASK,
        {
            "check_task_collision": {
                "verdict": "you_are_in_it",
                "session": {"name": "repo fix", "owner": "human"},
                "sessions": [], "reason": "live",
            },
        },
        expect_status="operator_in_session",
        expect_terminal="record",
    ),
    _ExitCase(
        "coding_orphan_aegis_turn", _CODE_TASK,
        {
            "check_task_collision": {
                "verdict": "you_are_in_it",
                "session": {"name": "task x", "owner": "aegis"},
                "sessions": [], "reason": "live",
            },
        },
        expect_status="turn_still_running",
        expect_terminal="none",
    ),
    _ExitCase(
        "coding_hand_to_you", _CODE_TASK,
        {
            "check_task_collision": {
                "verdict": "hand_to_you",
                "session": {"name": "repo fix", "owner": "human", "branch": "fix/x"},
                "sessions": [], "reason": "same branch",
            },
        },
        expect_status="handed_to_operator",
    ),
    _ExitCase(
        "coding_launch_failed", _CODE_TASK,
        {"launch_task_turn": {**_LAUNCH_OK, "status": "failed", "error": "no route to host"}},
        expect_status="launch_failed",
    ),
    _ExitCase("coding_turn_ran", _CODE_TASK, expect_status="parked"),
    _ExitCase("run_unknown_task", _CODE_TASK, {"load_task": {}},
              expect_status="unknown_task", expect_terminal="none", load_from_id=True),
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

    @activity.defn(name="load_task")
    async def load_task(task_id: str) -> dict:
        return r.get("load_task", dict(case.task, id=task_id))

    @activity.defn(name="ensure_task_session")
    async def ensure_task_session(
        task_id: str, agent_id: str, task: dict, comment: str
    ) -> dict:
        return r.get("ensure_task_session", _ENSURE_READY)

    @activity.defn(name="check_task_collision")
    async def check_task_collision(
        task_id: str, repo: str, session_id: str, override: bool = False
    ) -> dict:
        return r.get("check_task_collision", _PROCEED)

    @activity.defn(name="record_task_turn")
    async def record_task_turn(task_id: str, launched: bool) -> dict:
        events.append(("record", str(launched)))
        return {"recorded": True}

    @activity.defn(name="launch_task_turn")
    async def launch_task_turn(
        session: dict, prompt: str, agent_id: str, resume: bool,
        name: str, turn_timeout_minutes: int,
    ) -> dict:
        return r.get("launch_task_turn", _LAUNCH_OK)

    @activity.defn(name="check_agent_run")
    async def check_agent_run(output_file: str, host: str = "", probe_alive: bool = True) -> dict:
        return {"status": "finished", "output": "done", "reason": "", "final": "STATUS: plan"}

    @activity.defn(name="kill_task_turn")
    async def kill_task_turn(output_file: str, host: str) -> dict:
        return {"killed": True}

    @activity.defn(name="send_message")
    async def send_message(
        agent_id: str, message: str, chat_id: int = 0, thread_ref: dict | None = None
    ) -> dict:
        events.append(("slack", message))
        return {"ok": True}

    @activity.defn(name="set_task_slack_ref")
    async def set_task_slack_ref(task_id: str, ref: dict) -> dict:
        events.append(("slack_ref", ref))
        return {"stored": True}

    return [
        load_task, load_task_context, comment, park_task, complete_task,
        service_health, service_logs, triage_email, merchant_history,
        ensure_task_session, check_task_collision, record_task_turn,
        launch_task_turn, check_agent_run, kill_task_turn, send_message,
        set_task_slack_ref,
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
                task={} if case.load_from_id else dict(case.task, id=f"{case.id}-1"),
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
    terminal = case.expect_terminal
    if terminal == "park" and case.expect_status in ("resolved", "archived"):
        terminal = "complete"
    if terminal == "none":
        assert not any(kind in ("park", "complete") for kind, _ in events), (
            f"{case.id}: a task that no longer exists has nothing to label"
        )
        return
    assert any(kind == terminal for kind, _ in events), (
        f"{case.id}: expected a {terminal} activity call, got {events}"
    )
    if terminal == "record":
        assert not any(kind == "park" for kind, _ in events), (
            f"{case.id}: this exit must NOT stamp @waiting, got {events}"
        )
