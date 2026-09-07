"""hub.py's operator-side helpers: `find_problem_for_task`, `add_link`,
`merge_problems`. Real database — a merge is a transaction over four tables."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from aegis.services import work_sessions
from aegis.services.hub import (
    Event,
    add_link,
    find_problem_for_task,
    get_problem,
    ingest_event,
    list_events,
    merge_problems,
)
from aegis.services.hub_project import link_task

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)


def _subject() -> str:
    return f"svc_{uuid.uuid4().hex[:8]}"


def _occ(subject: str, n: int = 1) -> Event:
    return Event(
        source="heartbeat",
        external_id=f"{subject}@{n}",
        kind="occurrence",
        title=f"Service {subject} down",
        klass="DockerServiceDown",
        subject=subject,
        severity="critical",
        occurred_at=NOW + timedelta(minutes=n),
    )


async def _task(db_pool, task_id: str) -> None:
    await db_pool.execute(
        "INSERT INTO todoist_tasks (id, content, labels, is_completed, updated_at) "
        "VALUES ($1, 'fix it', ARRAY['@pandora','@code'], false, now()) ON CONFLICT (id) DO NOTHING",
        task_id,
    )


async def test_find_problem_for_task_prefers_the_live_problem(db_pool):
    task = f"zzm-{uuid.uuid4().hex[:6]}"
    await _task(db_pool, task)
    assert await find_problem_for_task(db_pool, task) is None
    assert await find_problem_for_task(db_pool, "") is None

    old = await ingest_event(db_pool, _occ(_subject()), now=NOW)
    assert await link_task(db_pool, old.problem_id, task)
    assert (await find_problem_for_task(db_pool, task))["id"] == old.problem_id

    # A second, live problem linked to the same task wins over the closed one.
    await db_pool.execute(
        "UPDATE problems SET status = 'closed', closed_at = $2 WHERE id = $1::uuid",
        old.problem_id,
        NOW,
    )
    fresh = await ingest_event(db_pool, _occ(_subject()), now=NOW + timedelta(hours=1))
    assert await link_task(db_pool, fresh.problem_id, task)
    assert (await find_problem_for_task(db_pool, task))["id"] == fresh.problem_id


async def test_add_link_is_idempotent_and_refuses_blank(db_pool):
    r = await ingest_event(db_pool, _occ(_subject()), now=NOW)
    assert await add_link(db_pool, r.problem_id, "github_pr", "https://github.com/o/r/pull/5")
    assert not await add_link(db_pool, r.problem_id, "github_pr", "https://github.com/o/r/pull/5")
    assert not await add_link(db_pool, r.problem_id, "github_pr", "  ")
    refs = await db_pool.fetch(
        "SELECT link_kind, ref FROM problem_links WHERE problem_id = $1::uuid", r.problem_id
    )
    assert [(x["link_kind"], x["ref"]) for x in refs] == [("github_pr", "https://github.com/o/r/pull/5")]


async def test_merge_moves_events_links_sessions_and_closes_the_duplicate(db_pool):
    keep_task, dup_task = f"zzk-{uuid.uuid4().hex[:6]}", f"zzd-{uuid.uuid4().hex[:6]}"
    await _task(db_pool, keep_task)
    await _task(db_pool, dup_task)
    keep = await ingest_event(db_pool, _occ(_subject(), 1), now=NOW)
    dup_subject = _subject()
    dup = await ingest_event(db_pool, _occ(dup_subject, 1), now=NOW - timedelta(hours=2))
    await ingest_event(db_pool, _occ(dup_subject, 2), now=NOW + timedelta(hours=2))
    await link_task(db_pool, keep.problem_id, keep_task)
    await link_task(db_pool, dup.problem_id, dup_task)
    await add_link(db_pool, dup.problem_id, "github_pr", "o/r#9")
    await work_sessions.create_session(db_pool, task_id=dup_task, agent_id="pandoras-actor")

    out = await merge_problems(db_pool, keep.problem_id, dup.problem_id, by="chat:x", now=NOW)
    # Two occurrences and the duplicate's own `create` state change.
    assert out["events_moved"] == 3
    assert out["merged_task_id"] == dup_task

    kept = await get_problem(db_pool, keep.problem_id)
    merged = await get_problem(db_pool, dup.problem_id)
    assert kept["occurrences"] == 3 and kept["closed_at"] is None
    # first/last seen follow the events' own `occurred_at`, and the merged
    # problem's range widens the kept one's.
    assert kept["first_seen_at"] == NOW + timedelta(minutes=1)
    assert kept["last_seen_at"] == NOW + timedelta(minutes=2)
    assert merged["status"] == "closed" and merged["closed_at"] is not None
    kinds = [e["kind"] for e in await list_events(db_pool, keep.problem_id)]
    assert kinds.count("occurrence") == 3
    assert any(
        e["payload"].get("action") == "merge" and e["payload"].get("merged") == dup.problem_id
        for e in await list_events(db_pool, keep.problem_id)
    )
    assert await list_events(db_pool, dup.problem_id) == []
    links = {
        (r["link_kind"], r["ref"])
        for r in await db_pool.fetch(
            "SELECT link_kind, ref FROM problem_links WHERE problem_id = $1::uuid", keep.problem_id
        )
    }
    assert ("github_pr", "o/r#9") in links
    assert ("problem", dup.problem_id) in links
    assert ("todoist_task", dup_task) not in links, "the duplicate's task stays its own"
    back = await db_pool.fetchval(
        "SELECT ref FROM problem_links WHERE problem_id = $1::uuid AND link_kind = 'problem'",
        dup.problem_id,
    )
    assert back == keep.problem_id
    sess = await work_sessions.get_session(db_pool, dup_task)
    assert sess["problem_id"] == keep.problem_id


async def test_merge_refuses_self_missing_and_closed_targets(db_pool):
    keep = await ingest_event(db_pool, _occ(_subject()), now=NOW)
    with pytest.raises(ValueError, match="same problem"):
        await merge_problems(db_pool, keep.problem_id, keep.problem_id, by="x")
    with pytest.raises(ValueError, match="must exist"):
        await merge_problems(db_pool, keep.problem_id, str(uuid.uuid4()), by="x")
    dup = await ingest_event(db_pool, _occ(_subject()), now=NOW)
    await db_pool.execute(
        "UPDATE problems SET status = 'closed', closed_at = $2 WHERE id = $1::uuid",
        keep.problem_id,
        NOW,
    )
    with pytest.raises(ValueError, match="is closed"):
        await merge_problems(db_pool, keep.problem_id, dup.problem_id, by="x")
