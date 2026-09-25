"""#674 — the morning brief's area stories: budget, weekly/vault cadence, rendering."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from aegis.services import research_topics, topics_config
from aegis_worker.activities.briefing import BriefingActivities

pytestmark = pytest.mark.asyncio

NOW = datetime.now(UTC)


@pytest_asyncio.fixture(loop_scope="function")
async def pool(db_pool):
    keys = (research_topics.TOPICS_SETTING, topics_config.SETTINGS_KEY, "user_timezone")
    await db_pool.execute("DELETE FROM settings WHERE key = ANY($1::text[])", list(keys))
    topics_config.ROW.clear_cache()
    yield db_pool
    await db_pool.execute("DELETE FROM settings WHERE key = ANY($1::text[])", list(keys))
    topics_config.ROW.clear_cache()


async def _world(pool, *, weekly_day: int, brief_items: int) -> list:
    names = [f"T{i} {uuid.uuid4().hex[:6]}" for i in range(3)]
    for n, term in zip(names, ("alpha", "beta", "gamma"), strict=True):
        await research_topics.track(pool, n, [term], now=NOW)
    await research_topics.save_registry(
        pool,
        {
            "topics": [{"name": n, "queries": [t], "priority": "medium"}
                       for n, t in zip(names, ("alpha", "beta", "gamma"), strict=True)],
            "areas": [
                {"name": "First", "cadence": "daily", "cap": 2, "topics": [names[0]]},
                {"name": "Second", "cadence": "daily", "cap": 2, "topics": [names[1]]},
                {"name": "Curio", "cadence": "vault", "cap": 2, "topics": [names[2]]},
            ],
        },
        now=NOW,
    )
    await topics_config.save_topics_config(
        pool, {"weekly_day": weekly_day, "brief_items": brief_items}
    )
    # Two unrelated stories per topic (distinct words, so they do not fold).
    items = [
        {"title": f"{word} {event}", "url": f"https://x/{uuid.uuid4().hex}"}
        for word in ("alpha", "beta", "gamma")
        for event in ("council approves metro line", "festival draws record crowds")
    ]
    await research_topics.attach_items(pool, items, origin="rss", now=NOW)
    return await research_topics.load_areas(pool)


async def test_the_brief_budget_is_spent_in_area_order_and_vault_areas_go_to_the_vault(pool):
    areas = await _world(pool, weekly_day=NOW.weekday(), brief_items=1)
    act = BriefingActivities(db_pool=pool, llm_client=None)
    brief, vault, state = await act._gather_areas(areas, {}, NOW, NOW - timedelta(hours=1))

    assert [(a["area"], len(a["stories"])) for a in brief] == [("First", 1)]
    assert vault["kind"] == "weekly" and vault["slot"] == "reading"
    assert vault["text"].startswith("Curio:\n  - [gamma ")
    assert vault["text"].count("\n  - ") == 2
    assert len(state["seen_story_keys"]) == 1 + 2
    assert set(state["area_shown"]) == {"first", "curio"}

    # What was shown is not shown again.
    again, _, _ = await act._gather_areas(areas, state, NOW, NOW - timedelta(hours=1))
    assert [(a["area"], len(a["stories"])) for a in again] == [("First", 1)]
    assert again[0]["stories"][0]["key"] != brief[0]["stories"][0]["key"]


async def test_weekly_and_vault_areas_wait_for_the_weekly_day(pool):
    areas = await _world(pool, weekly_day=(NOW.weekday() + 1) % 7, brief_items=7)
    act = BriefingActivities(db_pool=pool, llm_client=None)
    brief, vault, _ = await act._gather_areas(areas, {}, NOW, NOW - timedelta(hours=1))
    assert [a["area"] for a in brief] == ["First", "Second"]
    assert vault is None


async def test_each_brief_story_is_a_thread_reply_and_every_story_is_recorded(pool):
    tag = uuid.uuid4().hex[:8]

    class _Delivery:
        def __init__(self):
            self.sent = []

        async def send_message(self, agent_id, text, chat_id=0, thread_ref=None):
            self.sent.append((text, thread_ref))
            return {"ok": True, "delivery_ref": {"adapter": "slack", "channel": "C1", "ts": f"{tag}.{len(self.sent)}"}}

    delivery = _Delivery()
    act = BriefingActivities(db_pool=pool, delivery=delivery)
    brief = [{"area": f"India {tag}", "stories": [
        {"key": f"{tag}-a", "title": "RBI <holds>", "url": "https://r/1", "why": "your loan"}]}]
    vault = [{"area": f"Curio {tag}", "stories": [{"key": f"{tag}-b", "title": "Old map", "url": "https://m/1"}]}]
    root = {"adapter": "slack", "channel": "C1", "ts": "root"}
    try:
        out = await act.post_area_stories("raphael", brief, root, vault)
        assert out == {"posted": 1, "recorded": 2}
        # A hint, then the story, both in the brief's thread.
        assert [ref for _, ref in delivery.sent] == [root, root]
        assert delivery.sent[1][0] == (
            f'<b>India {tag}</b> · <a href="https://r/1">RBI &lt;holds&gt;</a>\nyour loan'
        )
        rows = await pool.fetch(
            "SELECT story_key, channel, ts FROM area_stories WHERE story_key LIKE $1 ORDER BY 1", f"{tag}-%"
        )
        assert [tuple(r) for r in rows] == [(f"{tag}-a", "C1", f"{tag}.2"), (f"{tag}-b", None, None)]

        # With no message ref (not Slack), stories are recorded, not posted.
        delivery.sent.clear()
        brief[0]["stories"][0]["key"] = f"{tag}-c"
        out = await act.post_area_stories("raphael", brief, None, [])
        assert out == {"posted": 0, "recorded": 1} and delivery.sent == []
    finally:
        await pool.execute("DELETE FROM area_stories WHERE story_key LIKE $1", f"{tag}-%")


async def test_area_stories_are_their_own_block_and_stay_out_of_the_summary_prompt():
    story = {"key": "k", "title": "RBI <cuts> rates", "url": "https://r/1", "sources": 3, "why": "your loan"}
    changes = {"quiet": False, "nothing_else": True, "areas": [{"area": "Money", "stories": [story]}]}
    act = BriefingActivities(llm_client=None)
    out = await act.frame_briefing(changes)
    assert out == (
        "<b>Money</b>\n"
        '  • <a href="https://r/1">RBI &lt;cuts&gt; rates</a> — your loan'
    )

    prompts: list[str] = []

    class _LLM:
        async def think(self, prompt, **kw):
            prompts.append(prompt)
            return {"response": "One meeting today."}

    busy = {**changes, "nothing_else": False, "calendar": {"today": [], "new_ids": ["e"]}}
    out = await BriefingActivities(llm_client=_LLM()).frame_briefing(busy)
    assert out.startswith("One meeting today.\n\n<b>Money</b>")
    assert "RBI" not in prompts[0]
