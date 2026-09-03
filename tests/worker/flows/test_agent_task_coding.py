"""The coding verb: one persistent session per task, driven by comments.

The old one-shot lane (investigate → plan card → implement → PR card) is gone.
What this file pins is the shape that replaced it: a turn runs, its output is
posted as a task comment with the take-over footer, and the flow parks unless a
`comment` signal arrived while the turn was running — in which case it runs
another turn in the SAME session.

Every test asserts on the ACTIVITY CALLS the flow made, never on the returned
status literal: the literal survives deleting the call above it, so a
return-value assertion would pass for a flow that stopped doing the work.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from temporalio import activity, workflow
from temporalio.client import WorkflowFailureError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

with workflow.unsafe.imports_passed_through():
    from aegis.connectors.remote_script import _PROMPT_CAP_BYTES
    from aegis_worker.flows.agent_task import (
        AgentTaskFlow,
        AgentTaskFlowInput,
        AgentTaskSweepConfig,
        AgentTaskSweepFlow,
        _first_turn_prompt,
        _later_turn_prompt,
        _render_thread,
        _status_line,
        _thread_root,
    )
    from aegis_worker.shared.retry import TIMEOUT_LLM, TIMEOUT_LONG, TIMEOUT_STANDARD

_CODE_TASK = {
    "id": "tc-1",
    "content": "Fix phantom EPS downgrade",
    "description": "duplicate current=true rows",
    "labels": ["@pandora", "@code"],
    "source_tag": None,
    "project_id": "pr-bcp",
    "assignee_label": "@pandora",
    "notes": [
        {"content": "the screener shows two rows", "posted_at": "2026-09-01T10:00:00+00:00"},
        {"content": "only the newest is current", "posted_at": "2026-09-01T10:05:00+00:00"},
    ],
}

_SESSION = {
    "task_id": "tc-1",
    "agent_id": "pandoras-actor",
    "session_id": "11111111-2222-3333-4444-555555555555",
    "repo": "stockopedia/bcp",
    "github_repo": "Stockopedia/bcp",
    "worktree_path": "/srv/repos/bcp-aegis-wt/task-tc-1",
    "branch": "aegis-task/tc-1",
    "host": "node-a",
    # jsonb: a dict once the task's Slack thread has a root, NULL until then.
    "slack_ref": None,
    "turns": 0,
    "last_turn_at": "",
    "created_at": "2026-09-01T09:00:00+00:00",
}

_CANDIDATES = [
    {"resource_title": "bcp", "github_repo": "Stockopedia/bcp", "resource_path": "bcp"},
    {"resource_title": "aegis", "github_repo": "hikmahtech/aegis", "resource_path": "aegis"},
]

_PROCEED = {"verdict": "proceed", "session": None, "sessions": [], "reason": ""}

_FINAL = "Plan: dedupe the rows\nSTATUS: plan"


def _activities(
    events: list,
    *,
    ensure_result: dict | None = None,
    ensure_raises: bool = False,
    collision: dict | None = None,
    launch_result: dict | None = None,
    slack_ref: dict | None = None,
    slack_raises: bool = False,
    never_exits: bool = False,
    timeout_tail: str = "",
    seen_first_ensure: asyncio.Event | None = None,
    release_first_ensure: asyncio.Event | None = None,
    seen_first_poll: asyncio.Event | None = None,
    release_first_poll: asyncio.Event | None = None,
):
    """Fakes for every activity the coding path calls.

    Signatures mirror the real ones on `AgentTaskActivities` / the poll loop's
    `AgentRunActivities.check_agent_run` — a fake that drifts from the real
    signature is a test that proves nothing about production.
    """
    # `turns` is the session's own watermark: record_task_turn(launched=True)
    # bumps it, which is what makes the NEXT ensure_task_session return a
    # session the flow must RESUME rather than create.
    state = {"turns": 0, "polls": 0, "killed": False, "slack_ref": slack_ref}

    @activity.defn(name="load_task")
    async def load_task(task_id: str) -> dict:
        events.append(("load_task", task_id))
        return dict(_CODE_TASK, id=task_id)

    @activity.defn(name="load_task_context")
    async def load_task_context(task_id: str) -> dict:
        return {"external_id": "", "fingerprint": "", "gmail_message_id": ""}

    @activity.defn(name="ensure_task_session")
    async def ensure_task_session(
        task_id: str, agent_id: str, task: dict, comment: str
    ) -> dict:
        events.append(("ensure", comment))
        if ensure_raises:
            raise RuntimeError("the coding host is unreachable")
        if seen_first_ensure is not None and not seen_first_ensure.is_set():
            seen_first_ensure.set()
            if release_first_ensure is not None:
                await release_first_ensure.wait()
        if ensure_result is not None:
            return ensure_result
        return {
            "status": "ready",
            # Re-read every turn in production, so a root stored by an earlier
            # turn comes back on the row rather than living in flow memory.
            "session": dict(
                _SESSION,
                task_id=task_id,
                turns=state["turns"],
                slack_ref=state["slack_ref"],
            ),
            "candidates": [],
            "error": "",
        }

    @activity.defn(name="check_task_collision")
    async def check_task_collision(
        task_id: str, repo: str, session_id: str, override: bool = False
    ) -> dict:
        events.append(("collide", override))
        return collision if collision is not None else _PROCEED

    @activity.defn(name="record_task_turn")
    async def record_task_turn(task_id: str, launched: bool) -> dict:
        events.append(("record", launched))
        if launched:
            state["turns"] += 1
        return {"recorded": True}

    @activity.defn(name="launch_task_turn")
    async def launch_task_turn(
        session: dict,
        prompt: str,
        agent_id: str,
        resume: bool,
        name: str,
        turn_timeout_minutes: int,
    ) -> dict:
        events.append(("launch", {"resume": resume, "name": name, "prompt": prompt}))
        if launch_result is not None:
            return launch_result
        return {
            "status": "running",
            "run_id": f"r{state['turns']}",
            "output_file": f"/tmp/aegis-task-{state['turns']}.jsonl",
            "host": "node-a",
            "engine": "claude",
            "tmux_window": "claude-bcp-r1",
            "worktree_path": session.get("worktree_path", ""),
            "error": "",
        }

    @activity.defn(name="check_agent_run")
    async def check_agent_run(output_file: str, host: str = "", probe_alive: bool = True) -> dict:
        state["polls"] += 1
        events.append(("poll", {"file": output_file, "probe_alive": probe_alive}))
        if state["killed"]:
            # The post-kill tail fetch, not a poll: the run is gone, and all
            # the flow wants back is whatever it wrote before the deadline.
            return {"status": "failed", "output": timeout_tail, "reason": "killed", "final": ""}
        if seen_first_poll is not None and not seen_first_poll.is_set():
            seen_first_poll.set()
            if release_first_poll is not None:
                await release_first_poll.wait()
        if never_exits or state["polls"] % 2 == 1:
            return {"status": "running", "output": "", "reason": "", "final": ""}
        return {
            "status": "finished",
            "output": "read the loader\n" + _FINAL,
            "reason": "",
            "final": _FINAL,
        }

    @activity.defn(name="kill_task_turn")
    async def kill_task_turn(output_file: str, host: str) -> dict:
        events.append(("kill", output_file))
        state["killed"] = True
        return {"killed": True}

    @activity.defn(name="comment")
    async def comment(task_id: str, agent_id: str, body: str) -> dict:
        events.append(("comment", body))
        return {"ok": True}

    @activity.defn(name="park_task")
    async def park_task(task_id: str, reason: str) -> dict:
        events.append(("park", reason))
        return {"parked": True}

    @activity.defn(name="send_message")
    async def send_message(
        agent_id: str,
        message: str,
        chat_id: int = 0,
        thread_ref: dict | None = None,
        thread_overflow: bool = False,
    ) -> dict:
        events.append(("slack", message))
        # Recorded as its own event so `_bodies(events, "thread")[i]` pairs
        # with `_bodies(events, "slack")[i]` without changing the payload
        # shape every existing assertion here reads.
        events.append(("thread", thread_ref))
        events.append(("overflow", thread_overflow))
        if slack_raises:
            raise RuntimeError("comms is down")
        # PRODUCTION shape, copied from `SendResult.to_response()`:
        # `DeliveryRef.to_dict()` spreads the ref's data FLAT beside `adapter`
        # (there is no `data` sub-object), and `to_response` then mirrors those
        # same keys at the top level for legacy dispatch logging. A fake that
        # nests them proves `_thread_root` handles a shape comms never sends.
        return {
            "ok": True,
            "used_html": False,
            "delivery_ref": {"adapter": "slack", "channel": "C1", "ts": "1.1"},
            "channel": "C1",
            "ts": "1.1",
        }

    @activity.defn(name="set_task_slack_ref")
    async def set_task_slack_ref(task_id: str, ref: dict) -> dict:
        events.append(("slack_ref", ref))
        state["slack_ref"] = ref
        return {"stored": True}

    return [
        load_task,
        load_task_context,
        ensure_task_session,
        check_task_collision,
        record_task_turn,
        launch_task_turn,
        check_agent_run,
        kill_task_turn,
        comment,
        park_task,
        send_message,
        set_task_slack_ref,
    ]


def _kinds(events: list) -> list[str]:
    return [kind for kind, _ in events]


def _bodies(events: list, kind: str) -> list:
    return [body for k, body in events if k == kind]


async def _run(
    events: list,
    *,
    task: dict | None = None,
    comment: str = "",
    turn_timeout_minutes: int = 60,
    **fakes,
) -> dict:
    async with await WorkflowEnvironment.start_time_skipping() as env:
        queue = f"tq-{uuid.uuid4().hex[:8]}"
        async with Worker(
            env.client,
            task_queue=queue,
            workflows=[AgentTaskFlow],
            activities=_activities(events, **fakes),
        ):
            return await env.client.execute_workflow(
                AgentTaskFlow.run,
                AgentTaskFlowInput(
                    agent_id="pandoras-actor",
                    todoist_task_id="tc-1",
                    task=_CODE_TASK if task is None else task,
                    comment=comment,
                    turn_timeout_minutes=turn_timeout_minutes,
                ),
                id=f"agent-task-tc-1-{uuid.uuid4().hex[:8]}",
                task_queue=queue,
            )


@pytest.mark.asyncio
async def test_first_turn_posts_plan_with_footer_and_parks():
    """Turn 1 end to end: a fresh session is launched WITHOUT --resume, the
    run's final message is posted as a task comment carrying the take-over
    footer, and the task is parked at @waiting because nothing more is queued."""
    events: list = []
    result = await _run(events)

    launches = _bodies(events, "launch")
    assert len(launches) == 1
    assert launches[0]["resume"] is False, "turn 1 creates the session, it does not resume one"
    assert launches[0]["name"].startswith("task tc-1:")
    # The first-turn prompt investigates and carries the thread it was given.
    assert "This is your first turn on this task." in launches[0]["prompt"]
    assert "the screener shows two rows" in launches[0]["prompt"]
    assert "aegis-task/tc-1" in launches[0]["prompt"]

    assert ("record", True) in events, "a launched turn must bump the session watermark"
    assert result["status_line"] == "plan", "the turn's own STATUS verdict, on the result"
    bodies = _bodies(events, "comment")
    assert len(bodies) == 1
    assert "STATUS: plan" in bodies[0]
    assert "Session: 11111111-2222-3333-4444-555555555555 · turn 1" in bodies[0]
    assert "Take over: cd /srv/repos/bcp-aegis-wt/task-tc-1 && claude --resume" in bodies[0]

    assert ("park", "waiting on you") in events
    assert result["status"] == "parked"
    assert result["turns"] == 1


@pytest.mark.asyncio
async def test_first_turn_loads_the_thread_the_sweep_did_not_carry():
    """`find_actionable_tasks` selects task COLUMNS — no comment thread — and
    the sweep starts every first turn, so without this load the "Comment thread
    so far" block would be empty on the path that matters most. The
    instructions on a parked @code task usually live in that thread."""
    events: list = []
    sweep_shaped = {k: v for k, v in _CODE_TASK.items() if k != "notes"}
    await _run(events, task=sweep_shaped)

    assert ("load_task", "tc-1") in events
    prompt = _bodies(events, "launch")[0]["prompt"]
    assert "the screener shows two rows" in prompt
    assert "only the newest is current" in prompt


@pytest.mark.asyncio
async def test_comment_signal_during_turn_runs_a_second_turn():
    """The whole point of the signal: a comment that lands WHILE a turn is
    running queues, and is drained into a second turn on the same session
    instead of colliding with the first or being lost.

    Falsifiable: drop the post-turn `_drain()` and only one launch happens.
    """
    events: list = []
    seen = asyncio.Event()
    release = asyncio.Event()

    async with await WorkflowEnvironment.start_time_skipping() as env:
        queue = f"tq-{uuid.uuid4().hex[:8]}"
        async with Worker(
            env.client,
            task_queue=queue,
            workflows=[AgentTaskFlow],
            activities=_activities(
                events, seen_first_ensure=seen, release_first_ensure=release
            ),
        ):
            handle = await env.client.start_workflow(
                AgentTaskFlow.run,
                # Empty task on purpose: this is the webhook shape, so it also
                # proves load_task fills the thread the prompt renders.
                AgentTaskFlowInput(agent_id="pandoras-actor", todoist_task_id="tc-1"),
                id=f"agent-task-tc-1-{uuid.uuid4().hex[:8]}",
                task_queue=queue,
            )
            # The first ensure_task_session runs AFTER the flow's initial
            # drain, so a signal sent here can only be picked up by the
            # post-turn drain — which is exactly the behaviour under test.
            with env.auto_time_skipping_disabled():
                await asyncio.wait_for(seen.wait(), timeout=30)
                await handle.signal(AgentTaskFlow.comment, "also fix tests")
            release.set()
            result = await handle.result()

    assert ("load_task", "tc-1") in events, "the webhook shape must load the task"
    launches = _bodies(events, "launch")
    assert len(launches) == 2, f"expected a second turn from the queued comment: {_kinds(events)}"
    assert launches[1]["resume"] is True, "turn 2 must RESUME the session, not start a new one"
    assert "> also fix tests" in launches[1]["prompt"]
    assert "This is your first turn" not in launches[1]["prompt"]
    assert _kinds(events).count("park") == 1, "the flow parks once, after the last turn"
    assert result["turns"] == 2


@pytest.mark.asyncio
async def test_comment_signal_while_the_cli_turn_runs_is_queued_not_lost():
    """The same drain, at the moment it actually happens in production: the
    comment lands while the CLI session itself is mid-turn, not merely while
    the flow is setting up. Nothing can interrupt a running turn, so the only
    correct behaviour is to queue and answer it in the next one."""
    events: list = []
    seen = asyncio.Event()
    release = asyncio.Event()

    async with await WorkflowEnvironment.start_time_skipping() as env:
        queue = f"tq-{uuid.uuid4().hex[:8]}"
        async with Worker(
            env.client,
            task_queue=queue,
            workflows=[AgentTaskFlow],
            activities=_activities(events, seen_first_poll=seen, release_first_poll=release),
        ):
            handle = await env.client.start_workflow(
                AgentTaskFlow.run,
                AgentTaskFlowInput(
                    agent_id="pandoras-actor", todoist_task_id="tc-1", task=_CODE_TASK
                ),
                id=f"agent-task-tc-1-{uuid.uuid4().hex[:8]}",
                task_queue=queue,
            )
            # The result call has to be OUTSTANDING while we wait: the
            # time-skipping server only advances the clock while a client is
            # waiting on a workflow, so without this the poll loop's 30s sleep
            # is thirty real seconds.
            pending = asyncio.ensure_future(handle.result())
            await asyncio.wait_for(seen.wait(), timeout=60)
            await handle.signal(AgentTaskFlow.comment, "also fix tests")
            release.set()
            result = await pending

    launches = _bodies(events, "launch")
    assert len(launches) == 2, f"a mid-turn comment must run a second turn: {_kinds(events)}"
    assert launches[1]["resume"] is True
    assert "> also fix tests" in launches[1]["prompt"]
    assert _kinds(events).count("park") == 1
    assert result["turns"] == 2


@pytest.mark.asyncio
async def test_operator_in_session_sends_slack_note_and_does_not_park():
    """`you_are_in_it` with a HUMAN owner means the comment is already in front
    of the operator. Commenting on Todoist would duplicate it and parking would
    stamp @waiting on a task somebody is actively working — so the flow does
    neither. The watermark still moves, because the comment HAS been delivered;
    without that the 15-minute fallback sweep re-dispatches it forever.

    Falsifiable: add a park_task call to that branch and this fails.
    """
    events: list = []
    result = await _run(
        events,
        collision={
            "verdict": "you_are_in_it",
            # The realistic takeover: the footer AEGIS posts says `cd
            # <worktree_path> && claude --resume <id>`, so the operator IS in
            # the task's own `-aegis-wt/` tree — the directory the session
            # registry tags `owner="aegis"`. Only `check_task_collision`'s
            # liveness probe can call this a person, and the flow acts on the
            # owner the activity reports, never on the path.
            "session": {
                "name": "bcp eps",
                "owner": "human",
                "cwd": "/srv/repos/bcp-aegis-wt/task-tc-1",
            },
            "sessions": [],
            "reason": "session is live",
        },
    )

    assert ("record", False) in events
    assert not _bodies(events, "comment"), "the comment is already in the session"
    assert "park" not in _kinds(events), "must not stamp @waiting on a live session"
    assert "launch" not in _kinds(events)
    notes = _bodies(events, "slack")
    assert len(notes) == 1
    assert "your comment is waiting for you there" in notes[0]
    assert "tc-1" in notes[0] and "bcp eps" in notes[0]
    assert result["status"] == "operator_in_session"


@pytest.mark.asyncio
async def test_an_orphan_aegis_turn_keeps_the_comment_due():
    """`you_are_in_it` with an AEGIS owner is a turn of our own that outlived
    its kill. NOBODY has read the comment — not a person, and not the running
    turn, which was launched before it existed — so the watermark must not
    move: leaving the row due is what makes the 15-minute fallback re-dispatch
    it once the orphan ends. There is also no one to Slack.

    Falsifiable: call record_task_turn on that branch and this fails.
    """
    events: list = []
    result = await _run(
        events,
        collision={
            "verdict": "you_are_in_it",
            "session": {"name": "task tc-1: Fix phantom", "owner": "aegis"},
            "sessions": [],
            "reason": "session is live",
        },
    )

    assert "record" not in _kinds(events), "the comment has not been consumed by anything"
    assert "slack" not in _kinds(events), "there is no person in this session to tell"
    assert not _bodies(events, "comment")
    assert "park" not in _kinds(events)
    assert "launch" not in _kinds(events)
    assert result["status"] == "turn_still_running"


@pytest.mark.asyncio
async def test_hand_to_you_comments_and_parks_without_launching():
    """A person is already on this task in their own session: AEGIS says so,
    parks, and tells them the phrase that overrides the check."""
    events: list = []
    result = await _run(
        events,
        collision={
            "verdict": "hand_to_you",
            "session": {"name": "bcp eps", "owner": "human", "branch": "fix/eps"},
            "sessions": [{"name": "bcp eps", "owner": "human", "branch": "fix/eps"}],
            "reason": "same branch",
        },
    )

    assert ("record", False) in events
    assert "launch" not in _kinds(events), "must not run a turn against a live human session"
    bodies = _bodies(events, "comment")
    assert len(bodies) == 1
    assert "session 'bcp eps'" in bodies[0]
    assert "on branch `fix/eps`" in bodies[0]
    assert "Reply `take over`" in bodies[0]
    assert ("park", "operator already on it") in events
    assert result["status"] == "handed_to_operator"


@pytest.mark.asyncio
async def test_the_first_task_message_opens_a_thread_and_remembers_its_root():
    """Everything AEGIS says about a task belongs to ONE Slack thread. The
    first message has no root to post under, so it BECOMES the root: it leads
    with the task id and title, goes out with no `thread_ref`, and the ref
    comms hands back is stored on the session row.

    Falsifiable: drop the `set_task_slack_ref` call and the last assertion
    fails while every other test here still passes.
    """
    events: list = []
    await _run(events)

    assert _bodies(events, "thread") == [None], "no root yet — this message IS the root"
    note = _bodies(events, "slack")[0]
    assert note.startswith("Task tc-1: Fix phantom EPS downgrade")
    assert "STATUS: plan" in note, "the thread carries the turn's own output, not a stub"
    assert _bodies(events, "slack_ref") == [{"channel": "C1", "ts": "1.1"}]


@pytest.mark.asyncio
async def test_every_task_message_asks_comms_to_keep_its_chunks_threaded():
    """A turn's output is capped at 6000 characters and Slack chunks at 2800,
    so the message that opens a thread routinely spans three posts. Without the
    flag, chunks 2..N land in the channel as siblings of the root, and a reply
    typed under one of them carries a `ts` no task session owns — it is routed
    to chat instead of back into the task.

    Falsifiable: drop `thread_overflow=True` from `_mirror_to_thread` and this
    fails while every other test in this file still passes.
    """
    events: list = []
    await _run(events)

    assert _bodies(events, "overflow") == [True]


@pytest.mark.asyncio
async def test_a_task_that_already_has_a_thread_posts_into_it():
    """The root lives on the session row, so a later turn — a different
    workflow run — threads under it. Re-rooting instead would scatter one
    conversation over a new thread per turn.

    Falsifiable: ignore `session["slack_ref"]` and both the thread ref and the
    "stored once" assertion fail.
    """
    events: list = []
    await _run(events, slack_ref={"channel": "C1", "ts": "1.1"})

    assert _bodies(events, "thread") == [{"channel": "C1", "ts": "1.1"}]
    note = _bodies(events, "slack")[0]
    assert not note.startswith("Task tc-1:"), "the title belongs to the root, not to every reply"
    assert "STATUS: plan" in note
    assert "slack_ref" not in _kinds(events), "the root is stored once, not rewritten each turn"


@pytest.mark.asyncio
async def test_the_hand_back_comment_is_mirrored_into_the_thread():
    """Mirroring covers the comments that never launch a turn too — the ones
    where AEGIS is waiting on a person, which are exactly the ones a Todoist
    comment alone is too quiet for."""
    events: list = []
    await _run(
        events,
        collision={
            "verdict": "hand_to_you",
            "session": {"name": "bcp eps", "owner": "human", "branch": "fix/eps"},
            "sessions": [{"name": "bcp eps", "owner": "human", "branch": "fix/eps"}],
            "reason": "same branch",
        },
    )

    note = _bodies(events, "slack")[0]
    assert note.startswith("Task tc-1: Fix phantom EPS downgrade")
    assert _bodies(events, "comment")[0] in note, "the thread says what the Todoist comment says"


@pytest.mark.asyncio
async def test_the_operator_note_lands_in_the_task_thread_when_there_is_one():
    """`you_are_in_it` writes no Todoist comment, so its Slack note is the only
    thing the operator gets — it belongs in the task's thread. It is a note,
    not a task message, so it must never CREATE the root."""
    events: list = []
    await _run(
        events,
        slack_ref={"channel": "C1", "ts": "1.1"},
        collision={
            "verdict": "you_are_in_it",
            "session": {"name": "bcp eps", "owner": "human"},
            "sessions": [],
            "reason": "session is live",
        },
    )

    assert _bodies(events, "thread") == [{"channel": "C1", "ts": "1.1"}]
    assert "your comment is waiting for you there" in _bodies(events, "slack")[0]
    assert "slack_ref" not in _kinds(events), "a note is not a thread root"


@pytest.mark.asyncio
async def test_a_failing_slack_delivery_leaves_the_turn_alone():
    """Todoist is the record of record; Slack is the notification. A comms
    outage costs the notification and nothing else — the turn still ran, its
    output is still commented, and the task still parks.

    Falsifiable: let the delivery exception propagate and the workflow fails
    instead of returning a parked result.
    """
    events: list = []
    result = await _run(events, slack_raises=True)

    assert len(_bodies(events, "comment")) == 1
    assert "STATUS: plan" in _bodies(events, "comment")[0]
    assert ("park", "waiting on you") in events
    assert "slack_ref" not in _kinds(events), "nothing came back to store"
    assert result["status"] == "parked"
    assert result["turns"] == 1


@pytest.mark.asyncio
async def test_take_over_comment_passes_override():
    """`take over` is the operator overruling rule 2 — it has to reach the
    collision check as override=True, or the same verdict comes back and the
    task can never be handed back.

    Falsifiable: hard-code override=False and this fails while every other
    test still passes.
    """
    events: list = []
    await _run(events, comment="take over, go ahead")

    assert ("collide", True) in events
    assert ("collide", False) not in events


@pytest.mark.asyncio
async def test_timeout_kills_and_reports():
    """A turn that outlives its deadline is asked to stop and reported. The
    kill matters: an orphan run still writing this session while the next turn
    starts is worse than a lost turn."""
    events: list = []
    result = await _run(
        events,
        never_exits=True,
        turn_timeout_minutes=1,
        timeout_tail="ran the migration, then hung on the lock",
    )

    kinds = _kinds(events)
    assert "kill" in kinds, "a timed-out turn must be asked to stop"
    bodies = _bodies(events, "comment")
    assert len(bodies) == 1
    assert "was asked to stop after 1 min" in bodies[0]
    # poll_until_exit returns NO output on a timeout, so the tail has to be
    # fetched after the kill or every deadline comment is a bare "it stopped".
    assert "ran the migration, then hung on the lock" in bodies[0]
    assert kinds.index("kill") < len(kinds) - 1 - kinds[::-1].index("poll"), (
        "the tail must be fetched AFTER the kill, or it is a stale snapshot"
    )
    assert _bodies(events, "poll")[-1]["probe_alive"] is False, (
        "the tail fetch must not probe liveness on a run it just killed"
    )
    assert "Session: " in bodies[0]
    assert ("park", "waiting on you") in events
    assert result["turns"] == 1


@pytest.mark.asyncio
async def test_timeout_says_so_when_the_run_wrote_nothing():
    events: list = []
    await _run(events, never_exits=True, turn_timeout_minutes=1, timeout_tail="")

    assert "(no output captured)" in _bodies(events, "comment")[0]


@pytest.mark.asyncio
async def test_ambiguous_repo_lists_candidates_and_parks():
    """Two plausible repos: ask by name rather than run an unattended coding
    session in the wrong checkout."""
    events: list = []
    result = await _run(
        events,
        ensure_result={
            "status": "candidates",
            "session": dict(_SESSION, repo="", github_repo="", worktree_path="", branch=""),
            "candidates": _CANDIDATES,
            "error": "",
        },
    )

    assert ("record", False) in events
    assert "launch" not in _kinds(events)
    bodies = _bodies(events, "comment")
    assert len(bodies) == 1
    assert "Stockopedia/bcp" in bodies[0] and "hikmahtech/aegis" in bodies[0]
    assert ("park", "repo ambiguous") in events
    assert result["status"] == "repo_ambiguous"


@pytest.mark.asyncio
async def test_unresolved_repo_parks_with_the_reason():
    """No repo and no candidates: park with whatever the resolver said, so the
    next comment can name one."""
    events: list = []
    result = await _run(
        events,
        ensure_result={
            "status": "unresolved",
            "session": None,
            "candidates": [],
            "error": "the task worktree could not be created",
        },
    )

    assert "launch" not in _kinds(events)
    assert "the task worktree could not be created" in _bodies(events, "comment")[0]
    assert ("park", "repo unresolved") in events
    assert result["status"] == "parked"


@pytest.mark.asyncio
async def test_launch_failure_parks_with_the_error():
    """A turn that never started is reported as such — the watermark has
    already moved, so silence here would strand the comment."""
    events: list = []
    result = await _run(
        events,
        launch_result={
            "status": "failed",
            "run_id": "",
            "output_file": "",
            "host": "",
            "engine": "claude",
            "tmux_window": "",
            "worktree_path": "",
            "error": "ssh: connect to host node-a port 22: no route to host",
        },
    )

    assert ("record", False) in events, "the comment was consumed; the watermark moves"
    assert ("record", True) not in events, (
        "a turn that never started is not a turn: counting it leaves `turns` at 1, "
        "so every later turn resumes a session that was never created"
    )
    assert "no route to host" in _bodies(events, "comment")[0]
    assert ("park", "turn failed to start") in events
    assert result["status"] == "launch_failed"


@pytest.mark.asyncio
async def test_a_launched_turn_is_counted_only_once_it_is_running():
    """Two calls, in this order, around the launch: the WATERMARK moves before
    it (the comment is consumed either way, and a watermark left behind has the
    fallback sweep re-dispatch it every 15 minutes for ever) and the turn COUNT
    only after the launch reported `running`.

    Falsifiable: put the count back above `launch_task_turn` and the order
    inverts.
    """
    events: list = []
    await _run(events)

    around = [(kind, body) for kind, body in events if kind in ("record", "launch")]
    assert [kind for kind, _ in around[:3]] == ["record", "launch", "record"], around
    assert around[0] == ("record", False)
    assert around[2] == ("record", True)


@pytest.mark.asyncio
async def test_the_slow_activities_are_not_scheduled_on_the_60s_budget():
    """Two activities in this path routinely outlast 60 seconds, and a
    start-to-close timeout is not a retry — it surfaces as a workflow failure
    and the generic handler PARKS the task.

    `check_task_collision` makes a balanced-tier LLM call (kimi-class calls
    pass 120s in prod) and is written to return `proceed` on every failure; at
    60s the timeout would fire OUTSIDE that guard and park instead.
    `ensure_task_session` runs `git worktree add` over SSH on turn 1.

    Read off the workflow history rather than the source, so it is the schedule
    the server actually saw.
    """
    events: list = []
    async with await WorkflowEnvironment.start_time_skipping() as env:
        queue = f"tq-{uuid.uuid4().hex[:8]}"
        wf_id = f"agent-task-tc-1-{uuid.uuid4().hex[:8]}"
        async with Worker(
            env.client,
            task_queue=queue,
            workflows=[AgentTaskFlow],
            activities=_activities(events),
        ):
            await env.client.execute_workflow(
                AgentTaskFlow.run,
                AgentTaskFlowInput(
                    agent_id="pandoras-actor", todoist_task_id="tc-1", task=_CODE_TASK
                ),
                id=wf_id,
                task_queue=queue,
            )
        scheduled: dict[str, int] = {}
        async for event in env.client.get_workflow_handle(wf_id).fetch_history_events():
            attrs = event.activity_task_scheduled_event_attributes
            if attrs.activity_type.name:
                scheduled[attrs.activity_type.name] = attrs.start_to_close_timeout.seconds

    assert scheduled["check_task_collision"] == int(TIMEOUT_LLM.total_seconds())
    assert scheduled["ensure_task_session"] == int(TIMEOUT_LONG.total_seconds())
    for slow in ("check_task_collision", "ensure_task_session"):
        assert scheduled[slow] > TIMEOUT_STANDARD.total_seconds(), (
            f"{slow} outlasts 60s in prod; scheduling it there parks the task"
        )


@pytest.mark.asyncio
async def test_a_failing_coding_activity_parks_with_the_step_that_failed():
    """The coding path runs inside run()'s catch-all, so a raising activity
    must still park the task — and must name the phase, not "run_coding".
    Without the step, every coding crash in `workflow_runs` reads the same and
    there is nothing to grep for."""
    events: list = []
    async with await WorkflowEnvironment.start_time_skipping() as env:
        queue = f"tq-{uuid.uuid4().hex[:8]}"
        async with Worker(
            env.client,
            task_queue=queue,
            workflows=[AgentTaskFlow],
            activities=_activities(events, ensure_raises=True),
        ):
            with pytest.raises(WorkflowFailureError):
                await env.client.execute_workflow(
                    AgentTaskFlow.run,
                    AgentTaskFlowInput(
                        agent_id="pandoras-actor",
                        todoist_task_id="tc-1",
                        task=_CODE_TASK,
                    ),
                    id=f"agent-task-tc-1-{uuid.uuid4().hex[:8]}",
                    task_queue=queue,
                )

    parks = _bodies(events, "park")
    assert parks, "a crashed coding turn must still leave the task parked"
    assert "coding:ensure_task_session" in parks[0], parks


@pytest.mark.parametrize(
    ("notes", "expected"),
    [
        ([], "(no comments yet)"),
        ([{"content": "hi", "posted_at": "2026-09-01T10:00:00+00:00"}],
         "[2026-09-01T10:00:00+00:00] hi"),
    ],
)
def test_render_thread_shapes_each_line(notes: list, expected: str):
    assert _render_thread(notes) == expected


def test_render_thread_keeps_the_newest_lines_and_says_what_it_dropped():
    """The cap exists because the connector truncates the whole prompt at
    24 000 bytes — an uncapped thread would silently cut the INSTRUCTIONS off
    the bottom. Dropping from the oldest end is the whole point: the last thing
    the user said is what the turn has to act on."""
    notes = [
        {"content": f"note {n} " + "x" * 700, "posted_at": f"2026-09-0{n % 9 + 1}T10:00:00+00:00"}
        for n in range(30)
    ]
    out = _render_thread(notes)

    assert len(out) <= 13000, "the rendered thread must stay inside the cap"
    assert "note 29" in out, "the newest note is the one that must survive"
    assert "note 0 " not in out, "the oldest notes are the ones dropped"
    assert out.startswith("[... "), out[:80]
    assert "earlier comments omitted]" in out.splitlines()[0]


def test_the_first_turn_prompt_fits_the_connector_cap_in_bytes():
    """The connector cuts at `_PROMPT_CAP_BYTES` silently, and the STATUS
    contract is the LAST thing in this prompt — so a thread sized in characters
    is what would be delivered instead of the instructions. Non-ASCII is the
    whole point: 700 CJK characters are 2 100 bytes, so the old 12 000-character
    thread cap allowed a ~36 000-byte thread on its own.

    Falsifiable: count the budget in characters and this fails.
    """
    task = dict(
        _CODE_TASK,
        description="д" * 20_000,
        notes=[
            {"content": f"note {n} " + "な" * 700, "posted_at": "2026-09-01T10:00:00+00:00"}
            for n in range(200)
        ],
    )
    prompt = _first_turn_prompt("tc-1", task, _SESSION)

    assert len(prompt.encode("utf-8")) <= _PROMPT_CAP_BYTES
    assert prompt.endswith("STATUS: unactionable: <why>"), prompt[-200:]
    assert "STATUS: plan" in prompt
    assert "This is your first turn on this task." in prompt
    assert "note 199" in prompt, "the newest comment is the one that must survive"
    assert "earlier comments omitted]" in prompt


def test_a_20000_character_comment_is_cut_and_the_rules_survive():
    """A later turn's quoted comment is user text with no ceiling of its own —
    one pasted log is enough to push the session's rules off the bottom."""
    prompt = _later_turn_prompt("tc-1", _CODE_TASK, _SESSION, ["log dump\n" + "x" * 20_000])

    assert len(prompt.encode("utf-8")) <= _PROMPT_CAP_BYTES
    assert prompt.endswith("STATUS: unactionable: <why>"), prompt[-200:]
    assert "> log dump" in prompt, "the start of what the user said is kept"
    assert "[…]" in prompt, "and the cut is visible rather than silent"


def test_a_flood_of_queued_comments_keeps_the_newest_and_the_rules():
    """Several comments fold into one turn, so per-comment caps alone do not
    bound the prompt: six 4 000-character comments already exceed the cap."""
    prompt = _later_turn_prompt(
        "tc-1", _CODE_TASK, _SESSION, [f"c{n} " + "y" * 3_900 for n in range(20)]
    )

    assert len(prompt.encode("utf-8")) <= _PROMPT_CAP_BYTES
    assert prompt.endswith("STATUS: unactionable: <why>")
    assert "> c19 " in prompt, "the last thing the user said must survive"
    assert "> c0 " not in prompt
    assert "earlier comments omitted]" in prompt


def test_status_line_takes_the_last_one_and_tolerates_none():
    """The prompt lists the whole contract, so a model that quotes the menu back
    before choosing would otherwise be recorded as answering with its first line."""
    assert _status_line("STATUS: plan\nSTATUS: pr: https://x/1") == "pr: https://x/1"
    assert _status_line("done, no footer") == ""
    assert _status_line("  STATUS: plan") == "", "anchored: a mention mid-line is not a verdict"


def test_render_thread_truncates_one_enormous_note():
    out = _render_thread([{"content": "y" * 5000, "posted_at": "2026-09-01T10:00:00+00:00"}])
    assert out.count("y") == 800


@pytest.mark.parametrize(
    "resp,expected",
    [
        # What `DeliveryRef.to_dict()` actually emits: `data` spread flat next
        # to `adapter`. This is the PRODUCTION shape and the one a nested-only
        # reader would silently miss, re-rooting the thread on every turn.
        (
            {"ok": True, "delivery_ref": {"adapter": "slack", "channel": "C1", "ts": "1.1"}},
            {"channel": "C1", "ts": "1.1"},
        ),
        # The nested spelling, and the top-level back-compat mirror.
        (
            {"delivery_ref": {"adapter": "slack", "data": {"channel": "C2", "ts": "2.2"}}},
            {"channel": "C2", "ts": "2.2"},
        ),
        ({"ok": True, "channel": "C3", "ts": "3.3"}, {"channel": "C3", "ts": "3.3"}),
        # No thread to open: the web adapter sends nowhere a reply can land, a
        # failed send has no ref at all, and neither may be stored as a root.
        ({"ok": True, "delivery_ref": {"adapter": "web"}}, None),
        ({"ok": False, "error": "comms is down"}, None),
        (None, None),
    ],
)
def test_thread_root_reads_every_shape_comms_returns(resp, expected):
    assert _thread_root(resp) == expected


# --- the sweep's fallback dispatcher ---------------------------------------
#
# Registered under the REAL flow's name so the sweep's start_child_workflow
# lands on it: what is under test is the sweep's start-or-signal dance, not
# AgentTaskFlow itself.


@workflow.defn(name="AgentTaskFlow")
class _StubTaskFlow:
    def __init__(self) -> None:
        self._got: list[str] = []

    @workflow.signal
    def comment(self, text: str) -> None:
        self._got.append(text)

    @workflow.run
    async def run(self, input: AgentTaskFlowInput) -> dict:
        if input.comment:
            self._got.append(input.comment)
        await workflow.wait_condition(lambda: bool(self._got))
        return {"got": self._got, "timeout": input.turn_timeout_minutes}


@pytest.mark.asyncio
async def test_sweep_dispatches_due_turns_by_start_or_signal():
    """The webhook is the fast path; this is the fallback for a missed one.
    A task with no live workflow gets one STARTED carrying the comment; a task
    whose workflow is still running gets the comment SIGNALLED into it, which
    is what folds a comment posted mid-turn into the next turn."""
    events: list = []

    @activity.defn(name="find_actionable_tasks")
    async def find_actionable_tasks(
        max_tasks: int = 3, cooldown_hours: int = 6, max_coding: int = 1
    ) -> list[dict]:
        events.append(("actionable", max_coding))
        return []

    @activity.defn(name="find_task_turns_due")
    async def find_task_turns_due(limit: int = 20) -> list[dict]:
        events.append(("due", limit))
        return [
            {"task_id": "td-live", "agent_id": "pandoras-actor", "comment": "please rebase"},
            {"task_id": "td-cold", "agent_id": "pandoras-actor", "comment": "start it"},
        ]

    async with await WorkflowEnvironment.start_time_skipping() as env:
        queue = f"tq-{uuid.uuid4().hex[:8]}"
        async with Worker(
            env.client,
            task_queue=queue,
            workflows=[AgentTaskSweepFlow, _StubTaskFlow],
            activities=[find_actionable_tasks, find_task_turns_due],
        ):
            live = await env.client.start_workflow(
                _StubTaskFlow.run,
                AgentTaskFlowInput(agent_id="pandoras-actor", todoist_task_id="td-live"),
                id="agent-task-td-live",
                task_queue=queue,
            )
            result = await env.client.execute_workflow(
                AgentTaskSweepFlow.run,
                AgentTaskSweepConfig(agent_id="pandoras-actor", turn_timeout_minutes=45),
                id=f"sweep-{uuid.uuid4().hex[:8]}",
                task_queue=queue,
            )
            signalled = await live.result()
            started = await env.client.get_workflow_handle("agent-task-td-cold").result()

    assert signalled["got"] == ["please rebase"], "a live workflow takes the comment by signal"
    assert started["got"] == ["start it"], "a cold task is started carrying the comment"
    assert started["timeout"] == 45, "the sweep's configured turn deadline must reach the flow"
    assert result["resumed"] == 2
    assert ("due", 3) in events, "the due limit is max_coding, whose default moved to 3"


@pytest.mark.asyncio
async def test_sweep_spends_max_coding_on_new_and_resumed_turns_together():
    """`max_coding` is a ceiling on TURNS, not on first turns. A tick that
    spawns its whole budget as new tasks must not then dispatch that many
    resumed turns on top: both kinds land on the same coding host, and the
    tmux window cap is what the ceiling is protecting."""
    events: list = []
    coding = {
        "id": "sw-1", "content": "Fix the bug", "description": "",
        "labels": ["@pandora", "@code"], "source_tag": None,
        "project_id": "p1", "assignee_label": "@pandora",
    }

    @activity.defn(name="find_actionable_tasks")
    async def find_actionable_tasks(
        max_tasks: int = 3, cooldown_hours: int = 6, max_coding: int = 1
    ) -> list[dict]:
        # One coding task and one non-coding task: only the coding one spends
        # the budget, which is what makes this test fail on a naive `spawned`.
        return [coding, {**coding, "id": "sw-2", "source_tag": "#alert", "labels": ["@pandora"]}]

    @activity.defn(name="find_task_turns_due")
    async def find_task_turns_due(limit: int = 20) -> list[dict]:
        events.append(("due", limit))
        return []

    async with await WorkflowEnvironment.start_time_skipping() as env:
        queue = f"tq-{uuid.uuid4().hex[:8]}"
        async with Worker(
            env.client,
            task_queue=queue,
            workflows=[AgentTaskSweepFlow, _StubTaskFlow],
            activities=[find_actionable_tasks, find_task_turns_due],
        ):
            result = await env.client.execute_workflow(
                AgentTaskSweepFlow.run,
                AgentTaskSweepConfig(agent_id="pandoras-actor", max_coding=1),
                id=f"sweep-{uuid.uuid4().hex[:8]}",
                task_queue=queue,
            )

    assert result["spawned"] == 2
    assert result["resumed"] == 0
    assert not events, f"the budget was spent on the new turn; no due lookup: {events}"


@pytest.mark.asyncio
async def test_sweep_dispatches_what_is_left_of_the_coding_budget():
    """Two of three spent on first turns leaves one for a resumed turn."""
    events: list = []
    coding = {
        "id": "sw-a", "content": "Fix the bug", "description": "",
        "labels": ["@pandora", "@code"], "source_tag": None,
        "project_id": "p1", "assignee_label": "@pandora",
    }

    @activity.defn(name="find_actionable_tasks")
    async def find_actionable_tasks(
        max_tasks: int = 3, cooldown_hours: int = 6, max_coding: int = 1
    ) -> list[dict]:
        return [coding, {**coding, "id": "sw-b"}]

    @activity.defn(name="find_task_turns_due")
    async def find_task_turns_due(limit: int = 20) -> list[dict]:
        events.append(("due", limit))
        return []

    async with await WorkflowEnvironment.start_time_skipping() as env:
        queue = f"tq-{uuid.uuid4().hex[:8]}"
        async with Worker(
            env.client,
            task_queue=queue,
            workflows=[AgentTaskSweepFlow, _StubTaskFlow],
            activities=[find_actionable_tasks, find_task_turns_due],
        ):
            await env.client.execute_workflow(
                AgentTaskSweepFlow.run,
                AgentTaskSweepConfig(agent_id="pandoras-actor", max_coding=3),
                id=f"sweep-{uuid.uuid4().hex[:8]}",
                task_queue=queue,
            )

    assert events == [("due", 1)]


@pytest.mark.asyncio
async def test_sweep_survives_a_failing_due_lookup():
    """The spawn loop's children are already ABANDONED and running by the time
    the due lookup happens, so failing the sweep over it would report an outage
    for work that is under way."""

    @activity.defn(name="find_actionable_tasks")
    async def find_actionable_tasks(
        max_tasks: int = 3, cooldown_hours: int = 6, max_coding: int = 1
    ) -> list[dict]:
        return []

    @activity.defn(name="find_task_turns_due")
    async def find_task_turns_due(limit: int = 20) -> list[dict]:
        raise RuntimeError("the database is unreachable")

    async with await WorkflowEnvironment.start_time_skipping() as env:
        queue = f"tq-{uuid.uuid4().hex[:8]}"
        async with Worker(
            env.client,
            task_queue=queue,
            workflows=[AgentTaskSweepFlow, _StubTaskFlow],
            activities=[find_actionable_tasks, find_task_turns_due],
        ):
            result = await env.client.execute_workflow(
                AgentTaskSweepFlow.run,
                AgentTaskSweepConfig(agent_id="pandoras-actor"),
                id=f"sweep-{uuid.uuid4().hex[:8]}",
                task_queue=queue,
            )

    assert result == {"found": 0, "spawned": 0, "resumed": 0}


@pytest.mark.asyncio
async def test_sweep_survives_a_dispatch_failure():
    """One bad row must not cost the rest of the sweep: a due row whose id is
    unusable is logged and skipped, and the next row still dispatches."""

    @activity.defn(name="find_actionable_tasks")
    async def find_actionable_tasks(
        max_tasks: int = 3, cooldown_hours: int = 6, max_coding: int = 1
    ) -> list[dict]:
        return []

    @activity.defn(name="find_task_turns_due")
    async def find_task_turns_due(limit: int = 20) -> list[dict]:
        return [
            {"task_id": "", "agent_id": "pandoras-actor", "comment": "no id"},
            {"task_id": "td-ok", "agent_id": "pandoras-actor", "comment": "go on"},
        ]

    async with await WorkflowEnvironment.start_time_skipping() as env:
        queue = f"tq-{uuid.uuid4().hex[:8]}"
        async with Worker(
            env.client,
            task_queue=queue,
            workflows=[AgentTaskSweepFlow, _StubTaskFlow],
            activities=[find_actionable_tasks, find_task_turns_due],
        ):
            result = await env.client.execute_workflow(
                AgentTaskSweepFlow.run,
                AgentTaskSweepConfig(agent_id="pandoras-actor"),
                id=f"sweep-{uuid.uuid4().hex[:8]}",
                task_queue=queue,
            )
            started = await env.client.get_workflow_handle("agent-task-td-ok").result()

    assert started["got"] == ["go on"]
    assert result["resumed"] == 1
