"""The News page's reads: stories with verdicts, and watchers with what would
keep their items from the brief."""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from aegis.services import news_overview, research_areas, research_topics


def _w(**kw):
    return {"active": True, "topic": "T", "topic_tracked": True, "area": "A", "last_run": None, **kw}


def test_problems_name_every_reason_items_would_not_arrive():
    assert news_overview.problems_for(_w()) == []
    assert news_overview.problems_for(_w(active=False)) == ["switched off"]
    assert "not tracked" in news_overview.problems_for(_w(topic_tracked=False, area=None))[0]
    assert "in no area" in news_overview.problems_for(_w(area=None))[0]
    run = {"status": "completed", "summary": {"error": "scores are stale", "failed_topics": ["llm"]}}
    assert news_overview.problems_for(_w(last_run=run)) == [
        "last run: scores are stale", "searches failed for: llm",
    ]
    failed = {"status": "failed", "error": "boom", "summary": {}}
    assert news_overview.problems_for(_w(last_run=failed)) == ["last run failed: boom"]


@pytest_asyncio.fixture(loop_scope="function")
async def world(db_pool):
    tag = uuid.uuid4().hex[:6]
    yield db_pool, tag
    await db_pool.execute("DELETE FROM activities WHERE slug LIKE $1", f"watch-{tag}%")
    await db_pool.execute("DELETE FROM area_stories WHERE area LIKE $1", f"%{tag}")
    await db_pool.execute("DELETE FROM settings WHERE key = $1", research_topics.TOPICS_SETTING)


@pytest.mark.asyncio
async def test_a_watcher_shows_its_area_items_and_problems(world):
    pool, tag = world
    topic = f"Watch {tag}"
    await research_topics.track(pool, topic, [topic])
    await research_topics.attach_to_topic(
        pool, topic, [{"title": "Fed meets", "url": f"https://x/{tag}"}], origin="test"
    )
    for slug, t in ((f"watch-{tag}-a", topic), (f"watch-{tag}-b", f"Gone {tag}")):
        await pool.execute(
            "INSERT INTO activities (slug, workflow_type, agent_id, schedule_cron, config, active) "
            "VALUES ($1, 'NoSuchFlow', 'raphael', '0 9 * * *', $2, true)",
            slug,
            {"topic": t},
        )
    mine = {w["slug"]: w for w in await news_overview.watchers(pool) if w["slug"].startswith(f"watch-{tag}")}

    a = mine[f"watch-{tag}-a"]
    assert (a["items_7d"], a["recent_items"][0]["title"], a["last_run"]) == (1, "Fed meets", None)
    assert a["problems"] == [f"its topic {topic!r} is in no area, so nothing reaches the brief"]
    assert "not tracked" in mine[f"watch-{tag}-b"]["problems"][0]


@pytest.mark.asyncio
async def test_stories_list_newest_first_with_verdicts_and_filter_by_area(world):
    pool, tag = world
    for n, area in ((1, f"India {tag}"), (2, f"World {tag}")):
        await research_areas.record_shown(
            pool, area, {"key": f"{tag}-{n}", "title": f"S{n}", "url": ""}, {"channel": "C", "ts": f"{tag}.{n}"}
        )
    await research_areas.record_verdict(pool, channel="C", ts=f"{tag}.2", reaction="+1")
    rows = [s for s in await news_overview.stories(pool) if s["area"].endswith(tag)]
    assert [(s["title"], s["verdict"], s["posted"]) for s in rows] == [("S2", "up", True), ("S1", None, True)]
    only = await news_overview.stories(pool, area=f"India {tag}")
    assert [s["title"] for s in only] == ["S1"]
