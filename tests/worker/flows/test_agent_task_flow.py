"""AgentTaskSweepFlow / AgentTaskFlow — dispatch and unknown-verb parking."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import timedelta

import pytest
from aegis_worker.flows.agent_task import (
    AgentTaskFlow,
    AgentTaskFlowInput,
    AgentTaskSweepConfig,
    AgentTaskSweepFlow,
)
from temporalio import activity, workflow
from temporalio.client import WorkflowFailureError
from temporalio.common import RetryPolicy
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

# Module is `interaction` (singular) — imported inside imports_passed_through
# per repo convention.
# This module defines workflows of its own, so the sandbox re-imports it: the
# activities module (asyncpg underneath) must pass through too.
with workflow.unsafe.imports_passed_through():
    from aegis_worker.activities.agent_task import resolve_verb
    from aegis_worker.flows.agent_chat_reply import AgentChatReplyInput
    from aegis_worker.flows.agent_task import CODING_MOVED_COMMENT
    from aegis_worker.flows.interaction import InteractionFlowInput, InteractionResult

_TASK = {
    "id": "tf-1",
    "content": "PROLONGED: redis_redis degraded for over 2 hours",
    "description": "",
    "labels": ["@maou"],
    "source_tag": "#unmapped",  # deliberately a tag no table knows
    "project_id": "p1",
    "assignee_label": "@maou",
}


async def test_unknown_verb_parks_the_task_and_never_leaves_it_in_the_pool():
    calls: list[tuple[str, str]] = []

    @activity.defn(name="load_task_context")
    async def load_task_context(task_id: str) -> dict:
        return {"external_id": "", "gmail_message_id": "", "subject": "", "subject_kind": "",
                "verb": "unknown"}

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
                    agent_id="maou", todoist_task_id="tf-1", task=_TASK
                ),
                id=f"agent-task-tf-1-{uuid.uuid4()}",
                task_queue=queue,
            )

    assert result["verb"] == "unknown"
    assert result["status"] == "parked"
    assert any(kind == "park" for kind, _ in calls)
    # Parked once, with what a person does next — not an apology (#344).
    bodies = [body for kind, body in calls if kind == "comment"]
    assert len(bodies) == 1
    assert "#unmapped" in bodies[0]
    assert "agent_task_verbs" in bodies[0]
    assert "No executor" not in bodies[0]


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
                        agent_id="maou", todoist_task_id="tf-1", task=_TASK
                    ),
                    id=f"agent-task-tf-1-{uuid.uuid4()}",
                    task_queue=queue,
                )

    assert any(kind == "park" for kind, _ in calls), (
        "the task must be parked even though the flow ultimately fails"
    )


_SHORT = timedelta(seconds=30)
_SWEEP_CALLS: list[str] = []


@activity.defn(name="find_actionable_tasks")
async def _find_three(max_tasks: int = 3, cooldown_hours: int = 6, max_coding: int = 0) -> list[dict]:
    _SWEEP_CALLS.append("find_actionable_tasks")
    return [dict(_TASK, id=f"tf-{n}") for n in range(1, 4)]


@activity.defn(name="find_task_turns_due")
async def _no_turns_due(limit: int = 20) -> list[dict]:
    _SWEEP_CALLS.append("find_task_turns_due")
    return []


@activity.defn(name="reconcile_work_sessions")
async def _reconcile_sessions() -> dict:
    _SWEEP_CALLS.append("reconcile_work_sessions")
    return {"refreshed": 0, "parked": 0, "inventory": "ok"}


async def _run_sweep(flow=AgentTaskSweepFlow, config: AgentTaskSweepConfig | None = None):
    """One sweep; returns `(result, history)`. The children it spawns are
    abandoned and never run here (no AgentTaskFlow on the worker)."""
    _SWEEP_CALLS.clear()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        queue = f"tq-{uuid.uuid4()}"
        async with Worker(
            env.client,
            task_queue=queue,
            workflows=[flow],
            activities=[_find_three, _no_turns_due, _reconcile_sessions],
        ):
            handle = await env.client.start_workflow(
                flow.run,
                config or AgentTaskSweepConfig(agent_id="maou"),
                id=f"sweep-{uuid.uuid4()}",
                task_queue=queue,
            )
            result = await handle.result()
            return result, await handle.fetch_history()


async def test_sweep_spawns_one_child_per_task_and_does_not_await_them():
    result, _ = await _run_sweep()
    assert result == {"found": 3, "spawned": 3, "resumed": 0}
    # The coding lane left v1: a new tick neither dispatches turns nor
    # reconciles sessions.
    assert _SWEEP_CALLS == ["find_actionable_tasks"]


@workflow.defn(name="AgentTaskSweepFlow")
class _SweepBeforeTheCodingLaneWent:
    """The sweep as the worker before `PATCH_DROP_CODING_SWEEP` ran it: the
    spawn loop, the fallback turn dispatcher (only with a turn budget; prod's
    was 0) and the session reconcile. Every tick in flight at the PR 4 deploy
    has this history."""

    @workflow.run
    async def run(self, config: AgentTaskSweepConfig) -> dict:
        tasks = await workflow.execute_activity(
            "find_actionable_tasks",
            args=[config.max_tasks, config.cooldown_hours, config.max_coding],
            start_to_close_timeout=_SHORT,
        )
        for task in tasks:
            await workflow.start_child_workflow(
                "AgentTaskFlow",
                AgentTaskFlowInput(agent_id=config.agent_id, todoist_task_id=str(task["id"]), task=task),
                id=f"agent-task-{task['id']}",
                parent_close_policy=workflow.ParentClosePolicy.ABANDON,
            )
        if config.max_coding:
            await workflow.execute_activity(
                "find_task_turns_due", args=[config.max_coding], start_to_close_timeout=_SHORT
            )
        await workflow.execute_activity(
            "reconcile_work_sessions",
            start_to_close_timeout=_SHORT,
            retry_policy=RetryPolicy(maximum_attempts=1),
        )
        return {"found": len(tasks), "spawned": len(tasks), "resumed": 0}


@pytest.mark.parametrize("max_coding", [0, 2])
async def test_a_sweep_that_ran_the_coding_steps_replays_after_they_went(max_coding):
    """Falsifiable: delete the `if not workflow.patched(PATCH_DROP_CODING_SWEEP)`
    branch from `agent_task.py` and this fails with a nondeterminism error (the
    history schedules `reconcile_work_sessions`, the new shape never does).
    `max_coding=0` is prod's recorded config (054); 2 is a deployment that
    still had a turn budget."""
    config = AgentTaskSweepConfig(agent_id="maou", max_coding=max_coding)
    _, history = await _run_sweep(flow=_SweepBeforeTheCodingLaneWent, config=config)
    assert "reconcile_work_sessions" in _SWEEP_CALLS  # the premise
    assert ("find_task_turns_due" in _SWEEP_CALLS) is bool(max_coding)
    await Replayer(workflows=[AgentTaskSweepFlow]).replay_workflow(history)


async def test_a_new_sweep_records_the_coding_drop_marker_and_replays():
    _, history = await _run_sweep()
    markers = [
        e.marker_recorded_event_attributes.marker_name
        for e in history.events
        if e.HasField("marker_recorded_event_attributes")
    ]
    assert markers, "a new tick records PATCH_DROP_CODING_SWEEP"
    await Replayer(workflows=[AgentTaskSweepFlow]).replay_workflow(history)


# --- Issue #154: parametrised proof over every AgentTaskFlow.run exit path ---
#
# `find_actionable_tasks` excludes @waiting, so every exit MUST complete or
# park the task — otherwise the 6h cooldown re-picks (and re-fails) it
# forever. This is the single mechanical proof of that invariant: one case
# per terminal return/raise statement a run can reach today (10 total — 3 in
# run(), 2 in _run_ask, 2 in _run_email, 2 in _run_finance, 1 in
# _park_coding). The infra verb's exits left with the infra lane (a2-devops),
# and the coding loop's six with the development lane (a2-development): an
# untagged `@code` task now parks once with a note.
#
# ONE exit deliberately does not park, and carries its own terminal proof
# (`case.expect_terminal`): `no_task` — only the retired coding lane started
# this flow without the task, so there is nothing to park.
#
# Each case asserts the ACTUAL park/complete activity call fired, not just
# the returned status string — the literal `return {...}` dict on every exit
# is unchanged by deleting the park_task call above it, so asserting on the
# return value alone would not be falsifiable.

# Module-level stub — Temporal does not allow @workflow.defn on local classes
# (see tests/worker/test_clarify_flow_agent_spawn.py:14). One shape covers
# every remaining card: only the finance verb raises one, and it parks
# immediately afterwards whatever the answer is.
@workflow.defn(name="InteractionFlow")
class _StubInteractionApprove:
    @workflow.run
    async def run(self, input: InteractionFlowInput) -> InteractionResult:
        return InteractionResult(interaction_id="ia-stub", status="resolved", response={"value": "approve"})


# The `ask` verb's executor, stubbed for the exit table: the real one is
# driven end to end in test_agent_task_ask.py.
@workflow.defn(name="AgentChatReplyFlow")
class _StubAgentChatReply:
    @workflow.run
    async def run(self, inp: AgentChatReplyInput) -> dict:
        return {"status": "ok", "reason": None, "message_id": None, "agent_id": inp.target_agent}


_ALERT_TASK = dict(_TASK, source_tag="#alert", content="Flow TodoistSyncFlow keeps failing")
_EMAIL_TASK = dict(_TASK, source_tag="#email", content="a note")
_FINANCE_TASK = dict(_TASK, source_tag="#receipt", content="Anomaly: something weird")
_CODE_TASK = dict(_TASK, source_tag=None, labels=["@code"], content="Fix the bug")
_CHAT_TASK = dict(_TASK, source_tag="#chat", content="Why is the cache slow?")

_ASK = {"agent_id": "agent-x", "message": "m", "thread_id": "todoist-task-x", "comment": "",
        "reason": ""}

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
    # "complete" are the two label writes; "none" is a run with no task to
    # write anything to.
    expect_terminal: str = "park"
    # Start the flow with an EMPTY task dict — the shape only the retired
    # coding lane (webhook, fallback dispatcher) ever started it with.
    load_from_id: bool = False


_CASES = [
    _ExitCase("run_unknown_verb", _TASK, expect_status="parked"),
    _ExitCase("run_catch_all_except", _TASK, comment_raises=True, expect_raises=True),
    _ExitCase("ask_asked", _CHAT_TASK, {"prepare_agent_ask": _ASK}, expect_status="asked"),
    _ExitCase(
        "ask_no_agent", _CHAT_TASK,
        {"prepare_agent_ask": {**_ASK, "agent_id": "", "comment": "No active agent answers to @x.",
                               "reason": "no active agent answers to @x"}},
        expect_status="parked",
    ),
    _ExitCase("alert_left_to_the_user", _ALERT_TASK, expect_status="parked"),
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
    _ExitCase("coding_moved", _CODE_TASK, expect_status="parked"),
    _ExitCase("run_no_task", _CODE_TASK, expect_status="no_task", expect_terminal="none",
              load_from_id=True),
]

# 10 exits, plus `alert_left_to_the_user`: the `none` decision reaching
# run()'s unrouted park, which `run_unknown_verb` reaches as `unknown`.
assert len(_CASES) == 11, "one case per AgentTaskFlow exit — see issue #154"


def _exit_case_activities(events: list, case: _ExitCase):
    r = case.responses

    @activity.defn(name="load_task_context")
    async def load_task_context(task_id: str) -> dict:
        # The verb comes back from this activity since #344 (a setting can
        # change it, and the flow cannot read the database).
        return {"external_id": "", "gmail_message_id": "", "subject": "", "subject_kind": "",
                "verb": resolve_verb(case.task)}

    @activity.defn(name="prepare_agent_ask")
    async def prepare_agent_ask(task_id: str) -> dict:
        return r["prepare_agent_ask"]

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

    @activity.defn(name="triage_email")
    async def triage_email(task_id: str, title: str, gmail_message_id: str) -> dict:
        return r["triage_email"]

    @activity.defn(name="merchant_history")
    async def merchant_history(title: str, limit: int = 6) -> dict:
        return r["merchant_history"]

    return [
        load_task_context, comment, park_task, complete_task,
        prepare_agent_ask, triage_email, merchant_history,
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
            workflows=[AgentTaskFlow, case.interaction_stub, _StubAgentChatReply],
            activities=_exit_case_activities(events, case),
        ):
            wf_input = AgentTaskFlowInput(
                agent_id="maou",
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
    if case.id == "coding_moved":
        assert [body for kind, body in events if kind == "comment"] == [CODING_MOVED_COMMENT]


