"""ResearchActivities — the steps of ResearchFlow (#509)."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest_asyncio
from aegis.services import research as rs
from aegis_worker.activities.research import ResearchActivities
from temporalio.testing import ActivityEnvironment

from tests.llm_stub import StubbedLLMClient

_WEB = [
    {"title": "A", "url": "https://a.example/1", "content": "snippet a"},
    {"title": "B", "url": "https://b.example/2", "content": "snippet b"},
]


def _connectors(web=None, web_raises=None):
    kc = AsyncMock()
    kc.search = AsyncMock(
        return_value=[{"title": "Stored note", "url": "aegis://x", "summary": "known already"}]
    )
    kc.ingest_content = AsyncMock(return_value={"status": "ok"})
    sc = AsyncMock()
    sc.search = AsyncMock(side_effect=web_raises) if web_raises else AsyncMock(
        return_value=_WEB if web is None else web
    )
    return kc, sc


# --------------------------------------------------------------------------
# gather
# --------------------------------------------------------------------------


async def test_gather_collects_the_store_the_web_and_papers(monkeypatch):
    papers = AsyncMock(
        return_value={
            "papers": [{"id": "arxiv:1", "title": "P", "url": "https://arxiv.org/abs/1"}],
            "errors": ["arxiv: timed out"],
        }
    )
    monkeypatch.setattr(rs, "paper_search", papers)
    kc, sc = _connectors()
    out = await ActivityEnvironment().run(
        ResearchActivities(knowledge_connector=kc, search_connector=sc).research_gather,
        {
            "question": "a survey of sparse attention",
            "depth": "quick",
            "seed_urls": ["https://seed.example/x"],
        },
    )
    # The task's own link is read first, then search results, up to the depth's cap.
    assert out["to_read"] == ["https://seed.example/x", "https://a.example/1", "https://b.example/2"]
    assert out["kg"] == [{"title": "Stored note", "url": "aegis://x", "summary": "known already"}]
    assert [p["id"] for p in out["papers"]] == ["arxiv:1"]
    assert "papers: arxiv: timed out" in out["errors"]


async def test_gather_skips_papers_for_a_question_that_is_not_academic(monkeypatch):
    papers = AsyncMock()
    monkeypatch.setattr(rs, "paper_search", papers)
    kc, sc = _connectors()
    out = await ActivityEnvironment().run(
        ResearchActivities(knowledge_connector=kc, search_connector=sc).research_gather,
        {"question": "why is the cache slow after a deploy"},
    )
    papers.assert_not_awaited()
    assert out["papers"] == []


async def test_a_failed_search_is_an_error_line_not_a_failed_step():
    kc, sc = _connectors(web_raises=RuntimeError("searxng 502"))
    out = await ActivityEnvironment().run(
        ResearchActivities(knowledge_connector=kc, search_connector=sc).research_gather,
        {"question": "what is rag", "seed_urls": ["https://seed.example/x"]},
    )
    assert out["web"] == []
    assert out["to_read"] == ["https://seed.example/x"]
    assert any(e.startswith("web: searxng 502") for e in out["errors"])


# --------------------------------------------------------------------------
# read
# --------------------------------------------------------------------------


async def test_read_keeps_the_pages_that_read_and_lists_the_rest(monkeypatch):
    async def read_url(url, max_chars=0):
        if "bad" in url:
            return {"url": url, "error": "blocked"}
        return {"url": url, "title": "T", "text": "body", "truncated": False}

    monkeypatch.setattr(rs, "read_url", read_url)
    out = await ActivityEnvironment().run(
        ResearchActivities().research_read, ["https://ok.example/1", "https://bad.example/2"]
    )
    assert [p["url"] for p in out["pages"]] == ["https://ok.example/1"]
    assert out["errors"] == ["https://bad.example/2: blocked"]


# --------------------------------------------------------------------------
# synthesize
# --------------------------------------------------------------------------

_GATHERED = {
    "kg": [],
    "web": [{"title": "A", "url": "https://a.example/1", "snippet": "snippet a"}],
    "papers": [],
}


async def test_synthesis_is_recorded_in_llm_calls(db_pool):
    """Driven through the real client, read back from the real table: a mock
    assertion would pass against a row that never landed (#137)."""
    await db_pool.execute("DELETE FROM llm_calls WHERE purpose = 'research_synthesis'")
    llm = StubbedLLMClient(db_pool=db_pool, content="RAG retrieves, then generates [1].")
    act = ResearchActivities(llm_client=llm, model="stub-model", db_pool=db_pool)
    try:
        out = await ActivityEnvironment().run(
            act.research_synthesize, "what is rag", "", _GATHERED, []
        )
        row = await db_pool.fetchrow(
            "SELECT agent_id, status, model FROM llm_calls "
            "WHERE purpose = 'research_synthesis' ORDER BY created_at DESC LIMIT 1"
        )
    finally:
        await db_pool.execute("DELETE FROM llm_calls WHERE purpose = 'research_synthesis'")
    assert out["synthesized"] is True
    assert out["answer"] == "RAG retrieves, then generates [1]."
    assert out["sources"] == [{"n": 1, "kind": "web", "title": "A", "url": "https://a.example/1"}]
    assert row is not None
    assert row["agent_id"] == "raphael"
    assert row["status"] == "success"


async def test_a_failed_synthesis_says_so_and_is_not_marked_synthesized():
    llm = AsyncMock()
    llm.think = AsyncMock(side_effect=RuntimeError("model down"))
    out = await ActivityEnvironment().run(
        ResearchActivities(llm_client=llm).research_synthesize, "q", "", _GATHERED, []
    )
    assert out["synthesized"] is False
    assert "synthesis failed" in out["answer"]
    assert out["sources"]  # the sources are still reported


async def test_nothing_found_costs_no_model_call():
    llm = AsyncMock()
    out = await ActivityEnvironment().run(
        ResearchActivities(llm_client=llm).research_synthesize,
        "q",
        "",
        {"kg": [], "web": [], "papers": []},
        [],
    )
    llm.think.assert_not_called()
    assert out["synthesized"] is False
    assert out["sources"] == []


# --------------------------------------------------------------------------
# save
# --------------------------------------------------------------------------


async def test_save_is_keyed_on_the_question():
    kc, _ = _connectors()
    sources = [{"n": 1, "kind": "web", "title": "A", "url": "https://a.example/1"}]
    out = await ActivityEnvironment().run(
        ResearchActivities(knowledge_connector=kc).research_save,
        "What is RAG?",
        "RAG retrieves [1].",
        sources,
    )
    assert out == {"saved": True}
    kwargs = kc.ingest_content.await_args.kwargs
    assert kwargs["url"] == rs.research_content_url("what is rag")
    assert kwargs["source_type"] == "research"
    assert kwargs["raw_text"].startswith("RAG retrieves [1].\n\nSources:\n[1] A")


async def test_save_with_no_store_saves_nothing():
    out = await ActivityEnvironment().run(ResearchActivities().research_save, "q", "a", [])
    assert out["saved"] is False


# --------------------------------------------------------------------------
# the task's problem
# --------------------------------------------------------------------------


@pytest_asyncio.fixture(loop_scope="function")
async def research_task(db_pool):
    task_id = f"rt-{uuid.uuid4().hex[:8]}"
    await db_pool.execute(
        "INSERT INTO todoist_tasks (id, content, labels, source_tag, assignee_label, is_completed) "
        "VALUES ($1, 'Why does everyone hate AI?', ARRAY['#research', '@raphael'], "
        "'#research', '@raphael', false)",
        task_id,
    )
    yield task_id
    # Children first: problem_links and problem_events reference problems with
    # no ON DELETE (work_sessions and pending_prs SET NULL on their own).
    await db_pool.execute(
        "DELETE FROM problem_links WHERE problem_id IN "
        "(SELECT id FROM problems WHERE todoist_task_id = $1)",
        task_id,
    )
    await db_pool.execute(
        "DELETE FROM problem_events WHERE problem_id IN "
        "(SELECT id FROM problems WHERE todoist_task_id = $1)",
        task_id,
    )
    await db_pool.execute("DELETE FROM problems WHERE todoist_task_id = $1", task_id)
    await db_pool.execute("DELETE FROM todoist_tasks WHERE id = $1", task_id)


async def test_a_research_task_gets_a_problem_linked_to_it(db_pool, research_task):
    act = ResearchActivities(db_pool=db_pool)
    out = await ActivityEnvironment().run(act.research_task_problem, research_task)
    linked = await db_pool.fetchval(
        "SELECT id::text FROM problems WHERE todoist_task_id = $1", research_task
    )
    # `class` lets AgentTaskFlow tell a topic's task from a question (#513).
    assert out == {"problem_id": linked, "class": "question"}
    # Idempotent: the second run finds the same problem.
    again = await ActivityEnvironment().run(act.research_task_problem, research_task)
    assert again == out
