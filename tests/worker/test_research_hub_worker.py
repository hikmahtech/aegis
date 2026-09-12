"""#513 on the worker side: the "track this?" curiosity lane, the topic attach
activity, clarify's guard for the research agent's hub tasks, and the
briefing's topics line. Real test database throughout."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from aegis.services import research_topics
from aegis_worker.activities import clarify as clarify_mod
from aegis_worker.activities.briefing import BriefingActivities
from aegis_worker.activities.clarify import ClarifyActivities
from aegis_worker.activities.curiosity import CuriosityActivities
from aegis_worker.activities.intelligence import IntelligenceActivities
from temporalio.testing import ActivityEnvironment

pytestmark = pytest.mark.asyncio

AGENT = "sebas"


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
    await db_pool.execute("DELETE FROM agent_memory WHERE agent_id = $1", AGENT)


async def _search(pool, query: str, result, *, surface: str = "chat", status: str = "success"):
    await pool.execute(
        "INSERT INTO chat_tool_calls (agent_id, tool_name, args, result, status, surface) "
        "VALUES ($1, 'search_knowledge', $2, $3, $4, $5)",
        AGENT,
        {"query": query},
        result,
        status,
        surface,
    )


# --- curiosity: "track this?" ----------------------------------------------------


async def test_a_subject_searched_twice_with_nothing_found_is_a_gap(world):
    pool, token = world
    subject = f"zyx {token} widgets"
    await _search(pool, f"Zyx {token} widgets?", [])
    await _search(pool, f"zyx {token}   WIDGETS", {"total": 0, "results": []})
    # Not counted: the operator's MCP searches, an outage, a search that found something.
    await _search(pool, f"zyx {token} widgets", [], surface="mcp_operator")
    await _search(pool, f"zyx {token} widgets", {"error": "down", "status": "unavailable"})
    acts = CuriosityActivities(db_pool=pool)
    gaps = await acts._detect_untracked_topic(AGENT, "")
    mine = [g for _, g in gaps if g["subject"] == subject]
    assert len(mine) == 1
    assert mine[0]["evidence"] == {"empty_searches": 2}
    assert mine[0]["novelty_key"] == f"track:zyx-{token}-widgets"


async def test_a_subject_searched_once_or_already_tracked_is_not_a_gap(world):
    pool, token = world
    once = f"once {token}"
    tracked = f"tracked {token}"
    await _search(pool, once, [])
    await _search(pool, tracked, [])
    await _search(pool, tracked, [])
    await research_topics.track(pool, tracked, [tracked])
    gaps = await CuriosityActivities(db_pool=pool)._detect_untracked_topic(AGENT, "")
    assert not [g for _, g in gaps if token in g["subject"]]


async def test_a_yes_tracks_the_topic_and_a_no_does_not(world):
    pool, token = world
    acts = CuriosityActivities(db_pool=pool)
    subject = f"zyx {token}"
    meta = {"gap_type": "untracked_topic", "subject": subject, "agent_id": AGENT, "question": "q"}
    no = await acts.apply_curiosity_answer(str(uuid.uuid4()), {"value": "no thanks"}, meta)
    assert no["tracked"] is False
    assert await research_topics.load_topics(pool) == []
    yes = await acts.apply_curiosity_answer(str(uuid.uuid4()), {"value": "Yes please"}, meta)
    assert yes["tracked"] is True and yes["problem_id"]
    assert [t.name for t in await research_topics.load_topics(pool)] == [subject]


# --- the attach activity ---------------------------------------------------------


async def test_attach_topic_items_attaches_what_names_a_tracked_topic(world):
    pool, token = world
    await research_topics.track(pool, f"Topic {token}", [f"kw{token}"])
    items = [
        {"title": f"All about kw{token}", "url": f"https://x.example/{token}/1", "snippet": ""},
        {"title": "Unrelated", "url": f"https://x.example/{token}/2", "snippet": ""},
    ]
    out = await ActivityEnvironment().run(
        IntelligenceActivities(db_pool=pool).attach_topic_items, items, "intel:hn"
    )
    assert (out["matched"], out["attached"]) == (1, 1)


# --- clarify never files the research agent's hub tasks ---------------------------


def _task(tag: str, *, description: str = "") -> dict:
    return {
        "id": f"6h{uuid.uuid4().hex[:14]}",
        "content": "Something",
        "description": description,
        "labels": [tag, "@raphael", "@next"],
        "source_tag": tag,
        "latest_user_note": None,
    }


def _clarify(pool) -> ClarifyActivities:
    clarify_mod._routes_cache.update(routes=None, ts=0.0)
    return ClarifyActivities(db_pool=pool, todoist_connector=AsyncMock(), llm_client=AsyncMock())


async def test_a_feed_finding_task_is_hub_owned(world):
    pool, _ = world
    decision = await _clarify(pool).classify_one(_task("#feeds"))
    assert decision["classification"] == "hub_owned"


async def test_a_research_task_the_hub_projected_is_hub_owned_not_a_reference(world):
    pool, _ = world
    block = "<!-- aegis:problem 1234 -->\nStatus: open\n<!-- /aegis:problem -->"
    decision = await _clarify(pool).classify_one(_task("#research", description=block))
    assert decision["classification"] == "hub_owned"


async def test_a_raindrop_bookmark_still_takes_the_reference_rule(world):
    pool, _ = world
    task = _task("#research")
    task["labels"] = ["#research"]
    decision = await _clarify(pool).classify_one(task)
    assert decision["classification"] != "hub_owned"
    assert "skip_inbox" in decision["reason"]


# --- the briefing's topics line ---------------------------------------------------


async def test_the_briefing_names_a_topic_whose_round_gained_items(world):
    pool, token = world
    await pool.execute(
        "INSERT INTO settings (key, value) VALUES ('briefing_state', $1) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        {"last_briefing_at": (datetime.now(UTC) - timedelta(hours=12)).isoformat()},
    )
    name = f"Topic {token}"
    await research_topics.track(pool, name, [f"kw{token}"])
    await research_topics.attach_items(
        pool,
        [{"title": f"News on kw{token}", "url": f"https://x.example/{token}/b"}],
        origin="rss",
    )
    changes = await BriefingActivities(db_pool=pool).gather_briefing_changes()
    mine = [t for t in changes["topics"] if t["topic"] == name]
    assert mine == [{"topic": name, "new_items": 1, "round_items": 1, "task": False}]
    assert changes["quiet"] is False
    text = BriefingActivities(db_pool=pool)._format_changes_fallback(changes)
    assert "Your topics" in text and name in text
