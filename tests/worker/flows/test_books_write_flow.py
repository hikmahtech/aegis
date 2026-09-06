"""BooksWriteFlow — the durable half of a chat-tool books write (issue #388).

The flow is thin on purpose, so these tests pin the two decisions it actually
makes: it relays whatever the writer said, and it delivers that sentence itself
whenever the tool has stopped waiting for it.

`reply_after_seconds` is what the tool waited, so a test that wants the "the
user was already told it was still running" branch sets it low rather than
burning twenty seconds of wall clock. The comparison under test is
`elapsed + margin >= reply_after_seconds`, and both sides of it are exercised.
"""

from __future__ import annotations

import asyncio

import pytest
from temporalio import activity, workflow
from temporalio.client import WorkflowFailureError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

with workflow.unsafe.imports_passed_through():
    from aegis_worker.flows.books_write import BooksWriteFlow, BooksWriteInput

_sent: list[tuple[str, str]] = []
_written: list[tuple[str, dict]] = []


@activity.defn(name="books_write")
async def stub_write(op: str, payload: dict) -> dict:
    _written.append((op, payload))
    return {"ok": True, "message": "posted manual/abc123 to personal/2026.journal"}


@activity.defn(name="books_write")
async def stub_refused(op: str, payload: dict) -> dict:
    _written.append((op, payload))
    return {"ok": False, "message": "error: account expenses:zzz is not declared in the chart"}


@activity.defn(name="books_write")
async def stub_raises(op: str, payload: dict) -> dict:
    _written.append((op, payload))
    raise RuntimeError("git push exploded")


@activity.defn(name="books_write")
async def stub_write_slow(op: str, payload: dict) -> dict:
    """A write that really does outlast a short wait — 2.5s against the 4s the
    test gives the tool, so the decision has to come from a measured elapsed
    time and not from a constant."""
    await asyncio.sleep(2.5)
    _written.append((op, payload))
    return {"ok": True, "message": "posted manual/abc123 to personal/2026.journal"}


@activity.defn(name="send_message")
async def stub_send(agent_id: str, message: str) -> dict:
    _sent.append((agent_id, message))
    return {"ok": True}


@activity.defn(name="send_message")
async def stub_send_down(agent_id: str, message: str) -> dict:
    _sent.append((agent_id, message))
    raise RuntimeError("comms is down")


async def _run(write_stub, wid: str, *, reply_after: int, send_stub=stub_send) -> dict:
    _sent.clear()
    _written.clear()
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue="tq",
            workflows=[BooksWriteFlow],
            activities=[write_stub, send_stub],
        ),
    ):
        return await env.client.execute_workflow(
            BooksWriteFlow.run,
            BooksWriteInput(
                agent_id="maou",
                op="post",
                payload={"entity": "personal", "date": "2026-09-06"},
                reply_after_seconds=reply_after,
            ),
            id=wid,
            task_queue="tq",
        )


@pytest.mark.asyncio
async def test_a_write_the_tool_waited_for_is_not_announced_twice():
    """The tool got the sentence back and put it in the reply. A message here
    as well would be the same answer delivered twice."""
    out = await _run(stub_write, "bw-fast", reply_after=600)
    assert out["status"] == "ok"
    assert out["message"] == "posted manual/abc123 to personal/2026.journal"
    assert out["notified"] is False
    assert _sent == []
    assert _written == [("post", {"entity": "personal", "date": "2026-09-06"})]


@pytest.mark.asyncio
async def test_a_write_the_tool_stopped_waiting_for_reports_itself():
    """The user was told "still running". Without this the outcome exists only
    in `workflow_runs` and nobody who asked for the write ever sees it."""
    out = await _run(stub_write, "bw-late", reply_after=0)
    assert out["notified"] is True
    assert len(_sent) == 1
    agent_id, message = _sent[0]
    assert agent_id == "maou"
    assert "posted manual/abc123 to personal/2026.journal" in message


@pytest.mark.asyncio
async def test_the_margin_covers_a_result_that_landed_on_the_tools_deadline():
    """A write finishing a moment before the deadline can still miss the tool.
    The band errs towards telling the user twice, so a two-second write against
    a two-second wait is announced."""
    out = await _run(stub_write, "bw-margin", reply_after=2)
    assert out["notified"] is True
    assert len(_sent) == 1


@pytest.mark.asyncio
async def test_a_write_that_really_outlasted_the_wait_is_announced():
    """The one test that pins the measurement rather than the comparison: the
    activity takes 2.5s against a 4s wait, which only crosses the line because
    elapsed time is read off the clock. Answer it with a constant and this test
    goes quiet while every user of a slow write stops hearing about it."""
    out = await _run(stub_write_slow, "bw-really-slow", reply_after=4)
    assert out["elapsed_s"] >= 2
    assert out["notified"] is True
    assert len(_sent) == 1


@pytest.mark.asyncio
async def test_a_refusal_is_relayed_as_a_result_not_raised():
    """`hledger` turning a write down is an answer the model can act on. A
    raise would retry the activity and fail the workflow instead."""
    out = await _run(stub_refused, "bw-refused", reply_after=600)
    assert out["status"] == "refused"
    assert out["message"].startswith("error: account expenses:zzz")
    assert _sent == []


@pytest.mark.asyncio
async def test_a_failed_write_is_reported_to_the_user_before_it_fails_the_flow():
    """The failure has to reach the person who was told the write was running,
    and it still has to be a failed run in `workflow_runs`."""
    with pytest.raises(WorkflowFailureError):
        await _run(stub_raises, "bw-raises", reply_after=0)
    assert len(_sent) == 1
    assert "failed" in _sent[0][1]


@pytest.mark.asyncio
async def test_a_dead_comms_server_does_not_undo_a_finished_write():
    """The write is committed and pushed by now; failing the workflow over the
    message would put a failed run against a ledger that is correct."""
    out = await _run(stub_write, "bw-comms-down", reply_after=0, send_stub=stub_send_down)
    assert out["status"] == "ok"
    assert out["notified"] is False, "a failed send must not be recorded as delivered"
