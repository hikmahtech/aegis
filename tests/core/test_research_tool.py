"""research_topic hands the question to ResearchFlow and relays its answer (#509).

The research itself is tested where it now lives: the shared steps in
`test_research_service.py`, the worker steps in
`tests/worker/activities/test_research_activities.py`, the flow in
`tests/worker/flows/test_research_flow.py`. These pin the hand-off — the
workflow id, the payload, the wait, re-attaching, and every way it can fail
without raising.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from aegis.services import research as rs
from aegis.services.chat import ToolContext, _exec_research_topic
from temporalio.exceptions import WorkflowAlreadyStartedError

_RESULT = {
    "status": "ok",
    "answer": "RAG retrieves, then generates [1].",
    "sources": [
        {"n": 1, "kind": "web", "title": "A", "url": "https://a.example/1"},
        {"n": 2, "kind": "knowledge", "title": "Stored", "url": "aegis://research/x"},
    ],
    "saved": True,
}


def _client(*, result=None, start_raises=None, result_raises=None, hang=False):
    handle = MagicMock()

    async def _result():
        if hang:
            await asyncio.sleep(3600)
        if result_raises is not None:
            raise result_raises
        return _RESULT if result is None else result

    handle.result = _result
    client = MagicMock()
    client.start_workflow = (
        AsyncMock(side_effect=start_raises) if start_raises else AsyncMock(return_value=handle)
    )
    client.get_workflow_handle = MagicMock(return_value=handle)
    return client


async def _call(args: dict, client=None, agent_id="raphael") -> dict:
    ctx = ToolContext(agent_id=agent_id, temporal_client=client)
    return json.loads(await _exec_research_topic(None, args, ctx))


async def test_the_question_is_handed_to_the_flow_and_its_answer_relayed():
    client = _client()
    data = await _call(
        {"query": "What is RAG?", "depth": "thorough", "domains": ["arxiv.org"]}, client
    )
    assert data["synthesis"] == "RAG retrieves, then generates [1]."
    assert data["saved"] is True
    # Only real web links are offered as URLs to follow.
    assert data["top_urls"] == ["https://a.example/1"]
    assert data["reattached"] is False

    call = client.start_workflow.await_args
    assert call.args[0] == "ResearchFlow"
    assert call.args[1] == {
        "agent_id": "raphael",
        "question": "What is RAG?",
        "depth": "thorough",
        "domains": ["arxiv.org"],
        "reply_after_seconds": rs.RESEARCH_WAIT_S,
    }
    assert call.kwargs["id"] == rs.research_workflow_id("what is rag", "thorough", ["arxiv.org"])
    assert call.kwargs["task_queue"] == "aegis-main"


async def test_a_retried_turn_attaches_to_the_run_in_flight():
    """The id is the question's hash, so asking again while it runs pays once."""
    client = _client(start_raises=WorkflowAlreadyStartedError("wid", "ResearchFlow"))
    data = await _call({"query": "What is RAG?"}, client)
    client.get_workflow_handle.assert_called_once_with(rs.research_workflow_id("What is RAG?"))
    assert data["reattached"] is True
    assert data["synthesis"] == _RESULT["answer"]


async def test_a_long_run_is_reported_as_still_running(monkeypatch):
    monkeypatch.setattr(rs, "RESEARCH_WAIT_S", 0.05)
    data = await _call({"query": "slow question"}, _client(hang=True))
    assert data["status"] == "running"
    assert data["workflow_id"] == rs.research_workflow_id("slow question")
    assert "Do not start it again" in data["message"]


async def test_no_temporal_means_nothing_ran():
    data = await _call({"query": "q"}, client=None)
    assert "Temporal is not reachable" in data["error"]


async def test_an_empty_query_is_refused():
    client = _client()
    data = await _call({"query": "  "}, client)
    assert data["error"] == "query is required"
    client.start_workflow.assert_not_called()


async def test_a_dispatch_failure_is_an_answer_not_a_raise():
    data = await _call({"query": "q"}, _client(start_raises=RuntimeError("frontend down")))
    assert "could not be started: frontend down" in data["error"]


async def test_a_failed_run_is_an_answer_not_a_raise():
    data = await _call({"query": "q"}, _client(result_raises=RuntimeError("boom")))
    assert data["error"] == "research failed: boom"


@pytest.mark.parametrize("depth", [None, "deep", 3])
async def test_an_unknown_depth_is_quick(depth):
    client = _client()
    await _call({"query": "q", "depth": depth}, client)
    assert client.start_workflow.await_args.args[1]["depth"] == "quick"


async def test_the_agent_defaults_to_raphael():
    client = _client()
    await _call({"query": "q"}, client, agent_id=None)
    assert client.start_workflow.await_args.args[1]["agent_id"] == "raphael"
