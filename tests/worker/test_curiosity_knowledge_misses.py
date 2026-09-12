"""The "track this?" lane (#513) counts what the knowledge tools really record.

Each case runs the real executor and the chat loop's own recording step
(`_truncate_result`, then `recorded_result`) and stores the row with
`record_tool_call`, as a chat turn does:

* `search_knowledge` always returns the nearest documents, so "nothing on it"
  is a best hit under the detector's similarity floor, not an empty list. Cut
  to the result budget it is `{"total", "results": [...], "truncated"}`.
* `find_reference` answers in prose, which is stored as `{"raw": ...}`.

The detector used to count only an empty list or `{"results": []}` — shapes
neither tool records — so the card could never fire."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from aegis.observability import record_tool_call
from aegis.services import research_topics
from aegis.services.knowledge import KnowledgeStore
from aegis.services.tools.base import ToolContext, _truncate_result, recorded_result
from aegis.services.tools.gtd import _exec_find_reference
from aegis.services.tools.knowledge import _exec_search_knowledge
from aegis_worker.activities.curiosity import CuriosityActivities

pytestmark = pytest.mark.asyncio

AGENT = "sebas"
# The chat loop's per-result budget in these tests: small enough that a
# search with long chunks is cut to the `{"total", "results"}` shape.
BUDGET = 4096


class _Store:
    """The knowledge connector. `search` returns rows shaped by the real
    store's own formatter, with the given similarities, best first."""

    def __init__(self, sims: list[float], chunk_chars: int = 40):
        self.sims = sims
        self.chunk_chars = chunk_chars

    async def search(self, query, limit=10, source_type=None, **_):
        return [
            KnowledgeStore._row_to_result(
                {
                    "content_id": f"c{i}{uuid.uuid4().hex[:6]}",
                    "title": f"Unrelated document {i}",
                    "url": f"https://docs.example/{i}",
                    "source_type": source_type or "article",
                    "tags": [],
                    "metadata": {},
                    "summary": "about something else",
                    "ingested_at": datetime(2026, 9, 1, tzinfo=UTC),
                    "content": "x" * self.chunk_chars,
                    "similarity": sim,
                }
            )
            for i, sim in enumerate(self.sims)
        ]


@pytest_asyncio.fixture(loop_scope="function")
async def world(db_pool):
    token = uuid.uuid4().hex[:6]
    await db_pool.execute(
        "INSERT INTO agents (id, name, role, system_prompt_path, active) "
        "VALUES ($1, 'Sebas', 'assistant', 'personalities/sebas', TRUE) ON CONFLICT (id) DO NOTHING",
        AGENT,
    )
    await db_pool.execute("DELETE FROM settings WHERE key = $1", research_topics.TOPICS_SETTING)
    yield db_pool, token
    await db_pool.execute("DELETE FROM settings WHERE key = $1", research_topics.TOPICS_SETTING)
    await db_pool.execute("DELETE FROM chat_tool_calls WHERE args::text LIKE $1", f"%{token}%")


async def _record(pool, tool: str, args: dict, raw: str) -> None:
    """What the chat loop does with a tool's return before storing it."""
    await record_tool_call(
        pool,
        agent_id=AGENT,
        thread_id=None,
        tool_name=tool,
        tool_args=args,
        tool_result=recorded_result(_truncate_result(raw, max_bytes=BUDGET)),
        status="success",
        latency_ms=1,
    )


async def _search(pool, query: str, store: _Store) -> None:
    args = {"query": query}
    raw = await _exec_search_knowledge(pool, args, ToolContext(knowledge_connector=store))
    await _record(pool, "search_knowledge", args, raw)


async def _find_reference(pool, query: str, store: _Store) -> None:
    args = {"query": query}
    raw = await _exec_find_reference(pool, args, ToolContext(knowledge_connector=store))
    await _record(pool, "find_reference", args, raw)


async def _gaps(pool, token: str) -> list[dict]:
    gaps = await CuriosityActivities(db_pool=pool)._detect_untracked_topic(AGENT, "")
    return [g for _, g in gaps if token in g["subject"]]


async def test_a_search_whose_best_hit_is_noise_is_a_miss(world):
    pool, token = world
    await _search(pool, f"quux {token} lattices", _Store([0.41, 0.38, 0.35]))
    await _search(pool, f"Quux {token} lattices?", _Store([0.44, 0.40]))
    mine = await _gaps(pool, token)
    assert [g["subject"] for g in mine] == [f"quux {token} lattices"]
    assert mine[0]["evidence"] == {"empty_searches": 2}


async def test_a_cut_search_and_a_prose_no_match_both_count(world):
    pool, token = world
    long = _Store([0.43, 0.40, 0.39, 0.37, 0.36, 0.35, 0.34, 0.33], chunk_chars=2000)
    raw = await _exec_search_knowledge(
        pool, {"query": "probe"}, ToolContext(knowledge_connector=long)
    )
    cut = recorded_result(_truncate_result(raw, max_bytes=BUDGET))
    assert isinstance(cut, dict) and "results" in cut, "the case must exercise the cut shape"
    await _search(pool, f"quux {token} lattices", long)
    await _find_reference(pool, f"quux {token} lattices", _Store([]))
    mine = await _gaps(pool, token)
    assert [g["evidence"] for g in mine] == [{"empty_searches": 2}]


async def test_a_search_or_reference_that_found_something_is_not_a_miss(world):
    pool, token = world
    await _search(pool, f"quux {token} lattices", _Store([0.72, 0.41]))
    await _search(pool, f"quux {token} lattices", _Store([0.66]))
    await _find_reference(pool, f"quux {token} lattices", _Store([0.9]))
    assert await _gaps(pool, token) == []
