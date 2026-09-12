"""Raphael's problems on the hub, from the audit of #511/#513:

* a feed that broke is Raphael's `#feeds` task, so the infra digest leaves it
  out — by the source that raised it, as it leaves out topics and questions;
* a round resolved by hand (the Problems page) is over, so the next article
  opens a fresh round instead of reopening it;
* a round closed by automation says why, rather than "closed by hand"."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from aegis.services import research_topics
from aegis.services.hub import (
    Event,
    close_problem,
    digest,
    get_problem,
    ingest_event,
    list_events,
    set_status,
    slug,
)
from aegis.services.hub_watch import reconcile_findings

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 9, 13, 9, 0, tzinfo=UTC)


@pytest_asyncio.fixture(loop_scope="function")
async def world(db_pool):
    await db_pool.execute("DELETE FROM settings WHERE key = $1", research_topics.TOPICS_SETTING)
    yield db_pool
    await db_pool.execute("DELETE FROM settings WHERE key = $1", research_topics.TOPICS_SETTING)


def _topic() -> tuple[str, str]:
    token = uuid.uuid4().hex[:6]
    return f"Topic {token}", f"kw{token}"


async def _closes(pool, problem_id: str) -> list[str]:
    return [
        (e["payload"] or {}).get("reason")
        for e in await list_events(pool, problem_id)
        if (e["payload"] or {}).get("action") == "close"
    ]


async def test_a_feed_finding_stays_out_of_the_infra_digest(world):
    feed = f"https://feeds.example/{uuid.uuid4().hex[:8]}"
    svc = f"svc_{uuid.uuid4().hex[:8]}"
    await reconcile_findings(
        world,
        source="feeds",
        subject_kind="feed",
        classes=["feed_failing"],
        findings=[
            {
                "klass": "feed_failing",
                "subject": feed,
                "title": "RSS feed Dead failed 3 fetches in a row",
                "severity": "warning",
            }
        ],
        now=NOW,
        project=False,
    )
    await ingest_event(
        world,
        Event(
            source="heartbeat",
            external_id=f"{svc}@1",
            kind="occurrence",
            title=f"Service {svc} down",
            klass="DockerServiceDown",
            subject=svc,
            severity="critical",
            occurred_at=NOW,
        ),
        now=NOW,
    )
    out = await digest(world, hours=1, now=NOW + timedelta(minutes=1))
    subjects = {p["subject"] for p in out["problems"]}
    assert slug(svc) in subjects, "an infra problem still reaches the digest"
    assert slug(feed) not in subjects


async def test_a_round_resolved_by_hand_is_over_and_the_next_article_opens_a_fresh_one(world):
    name, term = _topic()
    topic = research_topics.Topic(name, (term,))
    pid = (await research_topics.track(world, name, [term], "low", now=NOW))["problem_id"]
    # The Problems page's Resolve: the round is resolved, and not closed.
    assert await set_status(
        world, pid, "resolved", reason="resolved by hand", source="admin",
        now=NOW + timedelta(hours=1),
    )
    assert await research_topics.live_problem(world, topic) is None

    await research_topics.attach_items(
        world,
        [{"title": f"News about {term}", "url": f"https://news.example/{term}/1", "summary": ""}],
        origin="rss",
        now=NOW + timedelta(hours=2),
    )
    fresh = await research_topics.live_problem(world, topic)
    assert fresh is not None and fresh["id"] != pid, "a resolved round must not be reopened"
    old = await get_problem(world, pid)
    assert old["closed_at"] is not None
    assert await _closes(world, pid) == ["the round was resolved; the next item opens a new one"]


async def test_an_automated_round_close_says_why_rather_than_by_hand(world):
    name, term = _topic()
    pid = (await research_topics.track(world, name, [term], now=NOW))["problem_id"]
    out = await research_topics.untrack(world, name, now=NOW + timedelta(hours=1))
    assert out["round_closed"] is True
    assert await _closes(world, pid) == ["the topic is no longer tracked"]


async def test_the_problems_page_close_is_still_by_hand(world):
    svc = f"svc_{uuid.uuid4().hex[:8]}"
    res = await ingest_event(
        world,
        Event(
            source="heartbeat",
            external_id=f"{svc}@1",
            kind="occurrence",
            title=f"Service {svc} down",
            klass="DockerServiceDown",
            subject=svc,
            severity="critical",
            occurred_at=NOW,
        ),
        now=NOW,
    )
    await set_status(world, res.problem_id, "resolved", reason="ok", source="admin", now=NOW)
    assert await close_problem(world, res.problem_id, now=NOW + timedelta(minutes=5))
    assert await _closes(world, res.problem_id) == ["closed by hand"]
