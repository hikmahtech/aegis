"""ResearchFlow (#509): gather → read → synthesise → save, each step degrading
on its own, a real answer saved and an apology not, and a late answer sent to
the channel."""

from __future__ import annotations

import asyncio
import uuid

from aegis_worker.flows.research import ResearchFlow, ResearchInput
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

_ANSWER = {
    "answer": "It works [1].",
    "synthesized": True,
    "sources": [{"n": 1, "kind": "page", "title": "A", "url": "https://a.example/1"}],
}


def _stubs(calls: list, *, synth=None, read_raises=False, save_raises=False, sleep=0.0):
    @activity.defn(name="research_gather")
    async def gather(request: dict) -> dict:
        calls.append(("gather", request))
        return {
            "kg": [],
            "web": [{"title": "A", "url": "https://a.example/1", "snippet": "s"}],
            "papers": [],
            "to_read": ["https://a.example/1"],
            "errors": ["papers: timed out"],
        }

    @activity.defn(name="research_read")
    async def read(urls: list) -> dict:
        calls.append(("read", urls))
        if read_raises:
            raise RuntimeError("reader down")
        return {"pages": [{"url": "https://a.example/1", "title": "A", "text": "body"}], "errors": []}

    @activity.defn(name="research_synthesize")
    async def synthesize(question: str, context: str, gathered: dict, pages: list) -> dict:
        calls.append(("synth", question, context, len(pages)))
        if sleep:
            await asyncio.sleep(sleep)
        return synth or _ANSWER

    @activity.defn(name="research_save")
    async def save(question: str, answer: str, sources: list) -> dict:
        calls.append(("save", question, answer))
        if save_raises:
            raise RuntimeError("store down")
        return {"saved": True}

    @activity.defn(name="send_message")
    async def send(agent_id, message, chat_id=0, thread_ref=None, thread_overflow=False):
        calls.append(("send", agent_id, message))
        return {"ok": True}

    return [gather, read, synthesize, save, send]


async def _run(inp: ResearchInput, activities: list) -> dict:
    async with await WorkflowEnvironment.start_time_skipping() as env:
        queue = f"tq-{uuid.uuid4()}"
        async with Worker(
            env.client, task_queue=queue, workflows=[ResearchFlow], activities=activities
        ):
            return await env.client.execute_workflow(
                ResearchFlow.run, inp, id=f"research-{uuid.uuid4()}", task_queue=queue
            )


async def test_a_question_is_answered_saved_and_reported():
    calls: list = []
    out = await _run(
        ResearchInput(question="Does it work?", context="asked on a task", seed_urls=["https://s/1"]),
        _stubs(calls),
    )
    assert out["status"] == "ok"
    assert out["saved"] is True
    assert out["report"] == "It works [1].\n\nSources:\n[1] A — https://a.example/1"
    assert out["errors"] == ["papers: timed out"]
    gather = next(c for c in calls if c[0] == "gather")[1]
    assert gather["seed_urls"] == ["https://s/1"]
    assert ("synth", "Does it work?", "asked on a task", 1) in calls
    # Nobody waited on a channel (reply_after_seconds=0), so nothing is sent.
    assert not [c for c in calls if c[0] == "send"]
    assert out["notified"] is False


async def test_an_apology_is_returned_but_never_saved():
    calls: list = []
    out = await _run(
        ResearchInput(question="q"),
        _stubs(calls, synth={"answer": "the synthesis failed", "synthesized": False, "sources": []}),
    )
    assert out["status"] == "no_answer"
    assert out["saved"] is False
    assert not [c for c in calls if c[0] == "save"]
    assert out["answer"] == "the synthesis failed"


async def test_a_failed_save_is_reported_and_the_answer_stands():
    out = await _run(ResearchInput(question="q"), _stubs([], save_raises=True))
    assert out["status"] == "ok"
    assert out["saved"] is False
    assert out["save_failed"] is True


async def test_a_failed_read_still_ends_in_an_answer():
    calls: list = []
    out = await _run(ResearchInput(question="q"), _stubs(calls, read_raises=True))
    assert out["read_degraded"] is True
    assert ("synth", "q", "", 0) in calls
    assert out["status"] == "ok"


async def test_a_late_answer_is_sent_to_the_channel():
    """The chat tool gave up waiting; the flow delivers the answer itself."""
    calls: list = []
    out = await _run(
        ResearchInput(agent_id="raphael", question="Slow one?", reply_after_seconds=1),
        _stubs(calls, sleep=2.5),
    )
    sends = [c for c in calls if c[0] == "send"]
    assert len(sends) == 1
    assert sends[0][1] == "raphael"
    assert "Slow one?" in sends[0][2]
    assert "It works [1]." in sends[0][2]
    assert out["notified"] is True


async def test_an_empty_question_runs_nothing():
    calls: list = []
    out = await _run(ResearchInput(question="   "), _stubs(calls))
    assert out["status"] == "refused"
    assert calls == []
