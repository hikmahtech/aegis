"""#675 — story feedback: reactions become verdicts, the judge sees them, the
month is scored per area."""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from aegis.services import research_areas as ra


def test_reactions_map_to_verdicts():
    assert [ra.verdict_for(r) for r in ("+1", ":thumbsup:", "+1::skin-tone-3", "-1", "x", "eyes", "")] == [
        "up", "up", "up", "down", "down", None, None,
    ]


def test_the_judge_sees_the_persons_verdicts():
    area = ra.Area("World", "wars that hit markets")
    prompt = ra.judge_prompt(
        area, [{"title": "T", "sources": 1}], [], {"up": ["Oil jumps on strait closure"], "down": ["Talks continue"]}
    )
    assert "marked these earlier picks useful:\n- Oil jumps on strait closure" in prompt
    assert "marked these earlier picks noise:\n- Talks continue" in prompt
    assert "marked" not in ra.judge_prompt(area, [{"title": "T", "sources": 1}], [])


def test_scorecard_text_names_idle_areas():
    card = [
        {"area": "India", "shown": 12, "up": 3, "down": 1, "saved": 1, "idle": False},
        {"area": "World", "shown": 9, "up": 0, "down": 4, "saved": 0, "idle": True},
    ]
    text = ra.scorecard_text(card)
    assert "  • India: 12 · 3 · 1 · 1" in text
    assert "No 👍 or save in 60 days: World." in text
    assert ra.scorecard_text([]) == ""


@pytest_asyncio.fixture(loop_scope="function")
async def pool(db_pool):
    tag = uuid.uuid4().hex[:8]
    yield db_pool, tag
    await db_pool.execute("DELETE FROM area_stories WHERE area LIKE $1", f"%{tag}")
    await db_pool.execute("DELETE FROM knowledge_content WHERE url LIKE $1", f"https://t/{tag}/%")


def _story(tag, n, why=""):
    return {"key": f"{tag}-{n}", "title": f"Story {n}", "url": f"https://t/{tag}/{n}", "why": why}


@pytest.mark.asyncio
async def test_a_reaction_on_a_story_message_is_its_verdict(pool):
    db, tag = pool
    area = f"World {tag}"
    await ra.record_shown(db, area, _story(tag, 1), {"channel": "C1", "ts": f"{tag}.1"})
    await ra.record_shown(db, area, _story(tag, 2), {"channel": "C1", "ts": f"{tag}.2"})
    # Recording again keeps the first ref; a story is shown once.
    await ra.record_shown(db, area, _story(tag, 1), {"channel": "C9", "ts": "other"})

    assert await ra.record_verdict(db, channel="C1", ts=f"{tag}.1", reaction="+1") is True
    assert await ra.record_verdict(db, channel="C1", ts=f"{tag}.2", reaction="eyes") is False
    assert await ra.record_verdict(db, channel="C1", ts="not-a-story", reaction="+1") is False
    assert await ra.record_verdict(db, channel="C1", ts=f"{tag}.2", reaction="-1") is True
    # The latest verdict wins.
    await ra.record_verdict(db, channel="C1", ts=f"{tag}.2", reaction="heart")

    assert await ra.recent_feedback(db, area) == {"up": ["Story 2", "Story 1"], "down": []}
    count = await db.fetchval("SELECT count(*) FROM area_stories WHERE area = $1", area)
    assert count == 2


@pytest.mark.asyncio
async def test_the_scorecard_counts_saves_and_names_an_area_nobody_liked(pool):
    db, tag = pool
    liked, idle = f"India {tag}", f"World {tag}"
    await ra.record_shown(db, liked, _story(tag, 1), None)
    await ra.record_shown(db, liked, _story(tag, 2), None)
    await ra.record_shown(db, idle, _story(tag, 3), {"channel": "C1", "ts": f"{tag}.3"})
    await ra.record_verdict(db, channel="C1", ts=f"{tag}.3", reaction="-1")
    # Story 2 was saved to raindrop.
    await db.execute(
        "INSERT INTO knowledge_content (content_id, url, title, source_type, tags) "
        "VALUES ($1, $2, 'saved', 'article', ARRAY['raindrop'])",
        f"kc-{tag}",
        f"https://t/{tag}/2",
    )

    card = {c["area"]: c for c in await ra.scorecard(db)}
    assert (card[liked]["shown"], card[liked]["saved"], card[liked]["idle"]) == (2, 1, False)
    assert (card[idle]["shown"], card[idle]["down"], card[idle]["idle"]) == (1, 1, True)
