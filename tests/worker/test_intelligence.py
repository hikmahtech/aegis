"""Tests for intelligence activities."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from aegis.llm import LLMClient, LLMTruncationError
from aegis_worker.activities.intelligence import IntelligenceActivities
from temporalio.testing import ActivityEnvironment


@pytest.fixture
def mock_kc():
    kc = AsyncMock()
    kc.search = AsyncMock(return_value=[])
    kc.ingest_claims = AsyncMock(return_value={"status": "ok"})
    kc.ingest_content = AsyncMock(return_value={"status": "ok"})
    return kc


async def test_dedup_items_passes_novel_items(mock_kc):
    mock_kc.search = AsyncMock(return_value=[])
    act = IntelligenceActivities(knowledge_connector=mock_kc)
    env = ActivityEnvironment()
    items = [
        {
            "title": "New AI breakthrough",
            "url": "https://example.com/ai",
            "snippet": "A new model...",
        },
        {
            "title": "BRICS expansion",
            "url": "https://example.com/brics",
            "snippet": "New members...",
        },
    ]
    result = await env.run(act.dedup_items, items)
    assert len(result) == 2


async def test_dedup_items_removes_duplicates(mock_kc):
    mock_kc.search = AsyncMock(return_value=[{"similarity": 0.9, "content": "Already seen"}])
    act = IntelligenceActivities(knowledge_connector=mock_kc)
    env = ActivityEnvironment()
    items = [{"title": "Old news", "url": "https://example.com/old", "snippet": "..."}]
    result = await env.run(act.dedup_items, items)
    assert len(result) == 0


async def test_score_significance(mock_kc):
    llm = MagicMock()
    llm.think = AsyncMock(
        return_value={
            "response": json.dumps(
                [
                    {"index": 0, "score": 4, "reason": "Major AI development"},
                    {"index": 1, "score": 2, "reason": "Minor financial news"},
                ]
            ),
            "model": "gemma4:e2b",
            "prompt_tokens": 10,
            "completion_tokens": 20,
        }
    )
    act = IntelligenceActivities(knowledge_connector=mock_kc, llm_client=llm)
    env = ActivityEnvironment()
    items = [
        {"title": "GPT-5 released", "snippet": "OpenAI announces..."},
        {"title": "Minor stock dip", "snippet": "S&P down 0.1%"},
    ]
    topics = [{"name": "ai", "queries": ["AI", "LLM"], "priority": "high"}]
    result = await env.run(act.score_significance, items, topics)
    assert len(result) == 2
    assert result[0]["significance"] == 4
    assert result[1]["significance"] == 2


async def test_score_significance_no_llm(mock_kc):
    act = IntelligenceActivities(knowledge_connector=mock_kc)
    env = ActivityEnvironment()
    items = [{"title": "Test", "snippet": "..."}]
    result = await env.run(act.score_significance, items, [])
    assert result[0]["significance"] == 3


async def test_score_significance_uses_light_model_with_headroom(mock_kc):
    """Scoring passes `model_light` through to think() with generous headroom —
    the model returns EMPTY content below ~900 tokens for this task, so a tight
    cap yields LLMTruncationError instead of a scored array.

    NOTE the scope of the model assertion: it pins the dataclass DEFAULT, which
    is what direct construction gets. A real worker overrides it —
    __main__.py passes `model_light=model_balanced` — so this does NOT show
    that production scoring runs on the fast tier. It does not."""
    captured: dict = {}

    async def fake_think(**kwargs):
        captured.update(kwargs)
        return {"response": "[]", "model": kwargs.get("model")}

    llm = MagicMock()
    llm.think = fake_think
    act = IntelligenceActivities(knowledge_connector=mock_kc, llm_client=llm)
    env = ActivityEnvironment()
    await env.run(act.score_significance, [{"title": "x", "snippet": "y"}], [{"name": "ai"}])
    assert captured["model"] == "gemma4:e2b"  # the dataclass default, not prod's tier
    assert captured["max_tokens"] >= 900  # headroom so the model doesn't return empty


async def test_ingest_intelligence(mock_kc):
    act = IntelligenceActivities(knowledge_connector=mock_kc)
    env = ActivityEnvironment()
    analyses = [
        {
            "title": "GPT-5",
            "summary": "Major release",
            "claims": [{"subject": "GPT-5", "predicate": "released_by", "object": "OpenAI"}],
            "topic": "ai",
            "url": "https://example.com",
        },
    ]
    result = await env.run(act.ingest_intelligence, analyses)
    assert result["ingested"] == 1
    # intel items are captured as content chunks, not graph claims
    mock_kc.ingest_content.assert_called_once()
    mock_kc.ingest_claims.assert_not_called()


async def test_ingest_intelligence_snippet_shape(mock_kc):
    """Regression: IntelligenceScanFlow items carry `snippet` (from
    intel_scan.search_source), NOT `summary`. Gating on `summary` alone
    silently ingested 0 worthy items into KS for weeks. The content gate
    must fall back to snippet."""
    act = IntelligenceActivities(knowledge_connector=mock_kc)
    env = ActivityEnvironment()
    analyses = [
        {
            "title": "UN climate report",
            "snippet": "The window is closing rapidly...",
            "url": "https://example.com/climate",
            "significance": 7,
        },
    ]
    result = await env.run(act.ingest_intelligence, analyses)
    assert result["ingested"] == 1, "snippet-shaped item must ingest into KS"
    mock_kc.ingest_content.assert_called_once()
    _, kwargs = mock_kc.ingest_content.call_args
    assert kwargs["summary"] == "The window is closing rapidly..."
    assert kwargs["source_type"] == "intelligence"


# --------------------------------------------------------------------------
# issue #137 — every scoring call must leave an llm_calls row.
#
# These assert on rows actually present in Postgres, not on a mock having been
# called: `record_llm_call` swallows its own errors, so a mock-based test would
# happily pass against a write that never lands.
# --------------------------------------------------------------------------

_AGENT = "zzis-raphael"
_PURPOSE = "intel_score_significance"


@pytest_asyncio.fixture(loop_scope="function")
async def scoring_agent(db_pool):
    """A prefix-scoped agent row to satisfy llm_calls.agent_id's FK.

    Teardown is children-before-parents: llm_calls rows first, then the agent.
    """
    await db_pool.execute(
        "INSERT INTO agents (id, name, role, system_prompt_path, active) "
        "VALUES ($1,$1,'test','/dev/null',true) ON CONFLICT (id) DO NOTHING",
        _AGENT,
    )
    await db_pool.execute("DELETE FROM llm_calls WHERE agent_id = $1", _AGENT)
    try:
        yield db_pool
    finally:
        await db_pool.execute("DELETE FROM llm_calls WHERE agent_id = $1", _AGENT)
        await db_pool.execute("DELETE FROM agents WHERE id = $1", _AGENT)


def _act(llm, pool, mock_kc):
    return IntelligenceActivities(
        knowledge_connector=mock_kc,
        llm_client=llm,
        db_pool=pool,
        agent_id=_AGENT,
    )


async def test_scoring_success_writes_an_llm_calls_row(scoring_agent, mock_kc):
    """Baseline for the two tests below: a successful score is recorded."""
    llm = MagicMock()
    llm.think = AsyncMock(
        return_value={
            "response": json.dumps([{"index": 0, "score": 4, "reason": "big"}]),
            "model": "kimi-k2.5",
            "prompt_tokens": 11,
            "completion_tokens": 22,
        }
    )
    env = ActivityEnvironment()
    await env.run(
        _act(llm, scoring_agent, mock_kc).score_significance,
        [{"title": "x", "snippet": "y"}],
        [{"name": "ai"}],
    )

    rows = await scoring_agent.fetch(
        "SELECT purpose, model, status, input_tokens, output_tokens "
        "FROM llm_calls WHERE agent_id = $1",
        _AGENT,
    )
    assert len(rows) == 1, f"expected one scoring row, got {len(rows)}"
    assert rows[0]["purpose"] == _PURPOSE
    assert rows[0]["status"] == "success"
    assert rows[0]["model"] == "kimi-k2.5"
    assert rows[0]["input_tokens"] == 11
    assert rows[0]["output_tokens"] == 22


async def test_truncated_scoring_writes_an_llm_calls_row(scoring_agent, mock_kc):
    """issue #137: think() raises LLMTruncationError from OUTSIDE its own
    failure-recording try, so a truncation reaches `llm_calls` only if this
    activity writes it. A model that truncates every scan used to look
    identical to a model nobody called — which is how "fast-tier calls went
    invisible" came to be reported."""
    llm = MagicMock()
    llm.think = AsyncMock(side_effect=LLMTruncationError("no budget left for content"))
    env = ActivityEnvironment()

    with pytest.raises(LLMTruncationError):
        await env.run(
            _act(llm, scoring_agent, mock_kc).score_significance,
            [{"title": "x", "snippet": "y"}],
            [{"name": "ai"}],
        )

    rows = await scoring_agent.fetch(
        "SELECT purpose, status, error FROM llm_calls WHERE agent_id = $1",
        _AGENT,
    )
    assert len(rows) == 1, "a truncated scoring call left no llm_calls row"
    assert rows[0]["purpose"] == _PURPOSE
    assert rows[0]["status"] == "error"
    assert "truncated" in (rows[0]["error"] or "")


async def test_failed_scoring_writes_an_llm_calls_row(scoring_agent, mock_kc):
    """The third status. Unlike truncation this row comes from think()'s own
    `_record_failure`, which fires only because the activity threads db_pool +
    purpose + agent_id down into the call — assert that wiring is intact."""
    client = LLMClient(base_url="http://localhost:4000/v1", api_key="test")

    class _UpstreamError(RuntimeError):
        pass

    env = ActivityEnvironment()
    with patch.object(client, "_client") as mock_openai:
        mock_openai.chat.completions.create = AsyncMock(side_effect=_UpstreamError("read timed out"))
        with pytest.raises(_UpstreamError):
            await env.run(
                _act(client, scoring_agent, mock_kc).score_significance,
                [{"title": "x", "snippet": "y"}],
                [{"name": "ai"}],
            )

    rows = await scoring_agent.fetch(
        "SELECT purpose, status, error FROM llm_calls WHERE agent_id = $1",
        _AGENT,
    )
    assert len(rows) == 1, "a failed scoring call left no llm_calls row"
    assert rows[0]["purpose"] == _PURPOSE
    assert rows[0]["status"] == "timeout"  # classified from "read timed out"
    assert "read timed out" in (rows[0]["error"] or "")
