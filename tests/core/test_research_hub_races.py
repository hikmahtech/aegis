"""Races and edges in the research hub (#513), found in the programme's
validation: registry writes under a lock, the round close under the ingest
lock, a failed task retire, ongoing watchdog findings, and research questions
kept out of the infra digest."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from aegis.services import hub_project, research_topics
from aegis.services.hub import Event, digest, get_problem, ingest_event, slug
from aegis.services.hub_project import TASK_SUBJECT_KIND
from aegis.services.hub_watch import reconcile_findings

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 9, 12, 9, 0, tzinfo=UTC)


@pytest_asyncio.fixture(loop_scope="function")
async def world(db_pool):
    await db_pool.execute("DELETE FROM settings WHERE key = $1", research_topics.TOPICS_SETTING)
    yield db_pool
    await db_pool.execute("DELETE FROM settings WHERE key = $1", research_topics.TOPICS_SETTING)


def _name() -> str:
    return f"Topic {uuid.uuid4().hex[:6]}"


async def test_two_topics_tracked_at_once_are_both_kept(world):
    """A read-modify-write without a lock kept only the second writer."""
    a, b = _name(), _name()
    await asyncio.gather(
        research_topics.track(world, a, ["alpha"], now=NOW),
        research_topics.track(world, b, ["beta"], now=NOW),
    )
    names = {t.name for t in await research_topics.load_topics(world)}
    assert {a, b} <= names


async def test_untrack_reports_a_task_it_could_not_retire(world, monkeypatch):
    name = _name()
    pid = (await research_topics.track(world, name, ["alpha"], now=NOW))["problem_id"]
    await world.execute(
        "UPDATE problems SET todoist_task_id = $2 WHERE id = $1::uuid",
        pid,
        f"T{uuid.uuid4().hex[:8]}",
    )

    async def todoist_down(*_a, **_kw):
        raise RuntimeError("todoist is down")

    monkeypatch.setattr(hub_project, "retire_task", todoist_down)
    out = await research_topics.untrack(world, name, now=NOW)
    assert out["status"] == "removed"
    assert out["round_closed"] is True
    assert out["task_retired"] is False
    assert await research_topics.load_topics(world) == []


async def test_closing_a_round_waits_for_the_lock_an_attach_holds(world):
    """The resolve and the close run under the ingest lock for the round's key,
    so an item attached between them cannot reopen the round."""
    pid = (await research_topics.track(world, _name(), ["alpha"], now=NOW))["problem_id"]
    key = (await get_problem(world, pid))["correlation_key"]
    async with world.acquire() as conn:
        await conn.execute("SELECT pg_advisory_lock(hashtext($1))", key)
        try:
            closing = asyncio.create_task(
                research_topics.close_round(world, pid, reason="seen", now=NOW)
            )
            await asyncio.sleep(0.5)
            assert not closing.done(), "the close did not wait for the round's ingest lock"
        finally:
            await conn.execute("SELECT pg_advisory_unlock(hashtext($1))", key)
        assert await asyncio.wait_for(closing, 10) is True
    assert (await get_problem(world, pid))["closed_at"] is not None


async def test_a_research_question_stays_out_of_the_infra_digest(world):
    subject = f"task-{uuid.uuid4().hex[:8]}"
    await ingest_event(
        world,
        Event(
            source="research",
            external_id=f"{subject}@1",
            kind="occurrence",
            title="Why does everyone hate AI?",
            klass="question",
            subject=subject,
            subject_kind=TASK_SUBJECT_KIND,
            severity="info",
            occurred_at=NOW,
        ),
        now=NOW,
    )
    out = await digest(world, hours=1, now=NOW + timedelta(minutes=1))
    assert slug(subject) not in {p["subject"] for p in out["problems"]}


async def test_an_ongoing_finding_keeps_its_problem_without_a_new_occurrence(world):
    subject = f"https://feeds.example/{uuid.uuid4().hex[:8]}"
    finding = {"klass": "feed_failing", "subject": subject, "title": "Dead failed 3 fetches"}
    await reconcile_findings(
        world, source="feeds", subject_kind="feed", classes=["feed_failing"],
        findings=[finding], now=NOW, project=False,
    )
    pid = await world.fetchval(
        "SELECT id::text FROM problems WHERE class = 'feed_failing' AND subject = $1 "
        "AND closed_at IS NULL",
        slug(subject),
    )
    events = await world.fetchval(
        "SELECT count(*) FROM problem_events WHERE problem_id = $1::uuid", pid
    )

    later = await reconcile_findings(
        world, source="feeds", subject_kind="feed", classes=["feed_failing"],
        findings=[{**finding, "record": False}], now=NOW + timedelta(hours=1), project=False,
    )
    assert later["ongoing"] == 1
    assert pid not in {r["problem_id"] for r in later["resolved"]}
    assert (
        await world.fetchval("SELECT count(*) FROM problem_events WHERE problem_id = $1::uuid", pid)
        == events
    ), "an ongoing finding added an occurrence"
    assert (await get_problem(world, pid))["status"] not in ("resolved", "closed")

    recovered = await reconcile_findings(
        world, source="feeds", subject_kind="feed", classes=["feed_failing"],
        findings=[], now=NOW + timedelta(hours=2), project=False,
    )
    assert pid in {r["problem_id"] for r in recovered["resolved"]}


async def test_an_ongoing_finding_opens_nothing_on_its_own(world):
    subject = f"https://feeds.example/{uuid.uuid4().hex[:8]}"
    out = await reconcile_findings(
        world, source="feeds", subject_kind="feed", classes=["feed_failing"],
        findings=[{"klass": "feed_failing", "subject": subject, "title": "x", "record": False}],
        now=NOW, project=False,
    )
    assert out["fresh"] == [] and out["ongoing"] == 1
    assert await world.fetchval(
        "SELECT count(*) FROM problems WHERE class = 'feed_failing' AND subject = $1", slug(subject)
    ) == 0
