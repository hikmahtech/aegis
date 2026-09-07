"""`ingest_event` against the real test database.

Each test uses a unique subject so it owns its correlation key; the hub's own
idempotency and uniqueness rules are what is under test, so nothing here
cleans up by hand.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from aegis.services.hub import (
    Event,
    get_problem,
    ingest_event,
    list_events,
)

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)


def _occ(subject: str, n: int = 1, **kw) -> Event:
    return Event(
        source="heartbeat",
        external_id=f"{subject}@{n}",
        kind="occurrence",
        title=f"DockerServiceDown: {subject}",
        klass="DockerServiceDown",
        subject=subject,
        severity="critical",
        occurred_at=kw.pop("occurred_at", NOW + timedelta(minutes=n)),
        **kw,
    )


def _resolved(subject: str, n: int = 1, **kw) -> Event:
    return Event(
        source="heartbeat",
        external_id=f"{subject}@{n}@resolved",
        kind="resolved",
        title=f"recovered: {subject}",
        klass="DockerServiceDown",
        subject=subject,
        occurred_at=kw.pop("occurred_at", NOW + timedelta(minutes=n)),
        **kw,
    )


def _subject() -> str:
    return f"svc_{uuid.uuid4().hex[:8]}"


async def _holder(pool, key: str) -> str | None:
    """The id of the live problem holding `key`, or None.

    The hub has no production reader for this — `ingest_event` does the lookup
    inside its own transaction — so the query lives here, with the tests that
    assert on it.
    """
    if not key:
        return None
    return await pool.fetchval(
        "SELECT id::text FROM problems WHERE correlation_key = $1 AND closed_at IS NULL", key
    )


async def test_first_occurrence_creates_an_open_problem(db_pool):
    s = _subject()
    r = await ingest_event(db_pool, _occ(s), now=NOW)
    assert r.action == "created" and r.occurrences == 1 and not r.muted
    p = await get_problem(db_pool, r.problem_id)
    assert p["status"] == "open"
    assert p["correlation_key"] == f"dockerservicedown:service:{s}"
    assert (p["class"], p["subject"], p["subject_kind"]) == ("dockerservicedown", s, "service")
    assert p["severity"] == "critical"
    assert p["first_seen_at"] == p["last_seen_at"] == NOW + timedelta(minutes=1)
    kinds = [e["kind"] for e in await list_events(db_pool, r.problem_id)]
    assert sorted(kinds) == ["occurrence", "state_change"]


async def test_second_occurrence_attaches_and_counts(db_pool):
    s = _subject()
    first = await ingest_event(db_pool, _occ(s, 1), now=NOW)
    second = await ingest_event(db_pool, _occ(s, 2), now=NOW)
    assert second.action == "attached"
    assert second.problem_id == first.problem_id
    assert second.occurrences == 2
    p = await get_problem(db_pool, first.problem_id)
    assert p["occurrences"] == 2
    assert p["last_seen_at"] == NOW + timedelta(minutes=2)
    # attach is not a transition, so no second state_change row
    kinds = [e["kind"] for e in await list_events(db_pool, first.problem_id)]
    assert kinds.count("state_change") == 1 and kinds.count("occurrence") == 2


async def test_same_external_id_is_a_duplicate_and_changes_nothing(db_pool):
    s = _subject()
    first = await ingest_event(db_pool, _occ(s, 1), now=NOW)
    again = await ingest_event(db_pool, _occ(s, 1), now=NOW)
    assert again.action == "duplicate" and again.problem_id == first.problem_id
    assert (await get_problem(db_pool, first.problem_id))["occurrences"] == 1


async def test_last_seen_never_moves_backwards(db_pool):
    s = _subject()
    r = await ingest_event(db_pool, _occ(s, 5), now=NOW)
    await ingest_event(db_pool, _occ(s, 2), now=NOW)  # an older occurrence arriving late
    assert (await get_problem(db_pool, r.problem_id))["last_seen_at"] == NOW + timedelta(minutes=5)


async def test_resolved_resolves_the_open_problem(db_pool):
    s = _subject()
    r = await ingest_event(db_pool, _occ(s), now=NOW)
    done = await ingest_event(db_pool, _resolved(s, 2), now=NOW)
    assert done.action == "resolved" and done.problem_id == r.problem_id
    p = await get_problem(db_pool, r.problem_id)
    assert p["status"] == "resolved"
    assert p["resolved_at"] == NOW + timedelta(minutes=2)
    # still holds the key until closed
    assert await _holder(db_pool, p["correlation_key"]) == r.problem_id


async def test_resolved_with_no_problem_is_ignored_and_stores_nothing(db_pool):
    s = _subject()
    r = await ingest_event(db_pool, _resolved(s), now=NOW)
    assert r == r.__class__(None, "ignored", f"dockerservicedown:service:{s}")
    assert await _holder(db_pool, r.key) is None
    assert await db_pool.fetchval(
        "SELECT count(*) FROM problem_events WHERE source='heartbeat' AND external_id=$1",
        f"{s}@1@resolved",
    ) == 0


async def test_occurrence_inside_the_window_reopens(db_pool):
    s = _subject()
    r = await ingest_event(db_pool, _occ(s, 1), now=NOW)
    await ingest_event(db_pool, _resolved(s, 2), now=NOW)
    later = NOW + timedelta(hours=3)
    again = await ingest_event(db_pool, _occ(s, 3, occurred_at=later), now=later)
    assert again.action == "reopened" and again.problem_id == r.problem_id
    p = await get_problem(db_pool, r.problem_id)
    assert p["status"] == "open" and p["resolved_at"] is None and p["occurrences"] == 2


async def test_occurrence_after_the_window_rolls_over_to_a_new_linked_problem(db_pool):
    s = _subject()
    r = await ingest_event(db_pool, _occ(s, 1), now=NOW)
    await ingest_event(db_pool, _resolved(s, 2), now=NOW)
    later = NOW + timedelta(days=3)
    again = await ingest_event(db_pool, _occ(s, 3, occurred_at=later), now=later)
    assert again.action == "rolled_over"
    assert again.problem_id != r.problem_id and again.occurrences == 1
    old = await get_problem(db_pool, r.problem_id)
    assert old["status"] == "closed" and old["closed_at"] == later
    new = await get_problem(db_pool, again.problem_id)
    assert new["status"] == "open" and new["correlation_key"] == old["correlation_key"]
    link = await db_pool.fetchrow(
        "SELECT ref FROM problem_links WHERE problem_id = $1::uuid AND link_kind = 'problem'",
        again.problem_id,
    )
    assert link["ref"] == r.problem_id
    # the key now belongs to the new one
    assert await _holder(db_pool, new["correlation_key"]) == again.problem_id


async def test_note_kinds_attach_to_a_named_problem_and_never_create(db_pool):
    s = _subject()
    r = await ingest_event(db_pool, _occ(s), now=NOW)
    note = Event(
        source="investigation",
        external_id=f"inv-{s}",
        kind="investigation",
        title="Investigation complete",
        payload={"verdict": "actionable"},
        problem_id=r.problem_id,
    )
    got = await ingest_event(db_pool, note, now=NOW)
    assert got.action == "noted" and got.problem_id == r.problem_id and got.occurrences == 1
    assert (await get_problem(db_pool, r.problem_id))["occurrences"] == 1

    orphan = Event(source="session", external_id=f"orphan-{s}", kind="session_note", title="hi")
    assert (await ingest_event(db_pool, orphan, now=NOW)).action == "ignored"


async def test_note_kind_can_find_its_problem_by_key(db_pool):
    s = _subject()
    r = await ingest_event(db_pool, _occ(s), now=NOW)
    note = Event(
        source="chat",
        external_id=f"note-{s}",
        kind="human_note",
        title="I restarted it",
        klass="DockerServiceDown",
        subject=s,
    )
    assert (await ingest_event(db_pool, note, now=NOW)).problem_id == r.problem_id


async def test_empty_key_is_never_found(db_pool):
    assert await _holder(db_pool, "") is None


async def test_uncorrelated_events_each_create_a_problem(db_pool):
    a = Event(source="chat", external_id=f"u-{uuid.uuid4()}", kind="occurrence", title="odd")
    b = Event(source="chat", external_id=f"u-{uuid.uuid4()}", kind="occurrence", title="odd")
    ra, rb = await ingest_event(db_pool, a, now=NOW), await ingest_event(db_pool, b, now=NOW)
    assert ra.action == rb.action == "created" and ra.key == rb.key == ""
    assert ra.problem_id != rb.problem_id
    assert (await get_problem(db_pool, ra.problem_id))["class"] == "manual"


async def test_muted_problem_reports_muted_on_attach(db_pool):
    s = _subject()
    r = await ingest_event(db_pool, _occ(s, 1), now=NOW)
    await db_pool.execute(
        "UPDATE problems SET muted_until = $2 WHERE id = $1::uuid",
        r.problem_id,
        NOW + timedelta(hours=6),
    )
    assert (await ingest_event(db_pool, _occ(s, 2), now=NOW)).muted is True
    assert (await ingest_event(db_pool, _occ(s, 3), now=NOW + timedelta(hours=7))).muted is False


async def test_invalid_event_raises_before_touching_the_db(db_pool):
    with pytest.raises(ValueError):
        await ingest_event(db_pool, Event(source="nope", external_id="1", kind="occurrence", title="t"))


async def test_concurrent_first_occurrences_yield_one_problem(db_pool):
    s = _subject()
    results = await asyncio.gather(*(ingest_event(db_pool, _occ(s, n), now=NOW) for n in range(6)))
    ids = {r.problem_id for r in results}
    assert len(ids) == 1
    assert sorted(r.action for r in results) == ["attached"] * 5 + ["created"]
    assert (await get_problem(db_pool, ids.pop()))["occurrences"] == 6


async def test_naive_occurred_at_is_stored_as_utc(db_pool):
    s = _subject()
    r = await ingest_event(db_pool, _occ(s, occurred_at=NOW.replace(tzinfo=None)), now=NOW)
    assert (await get_problem(db_pool, r.problem_id))["first_seen_at"] == NOW
