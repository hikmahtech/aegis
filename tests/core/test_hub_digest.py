"""`hub.digest` and `hub.close_resolved` — the two sweeps that replaced a
settings buffer and an open-ended resolved backlog."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from aegis.services.hub import (
    Event,
    close_resolved,
    digest,
    get_problem,
    ingest_event,
    list_events,
    set_service_state,
    set_status,
)

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 9, 8, 9, 0, tzinfo=UTC)


def _subject() -> str:
    return f"svc_{uuid.uuid4().hex[:8]}"


def _occ(subject: str, n: int = 1, **kw) -> Event:
    return Event(
        source="heartbeat",
        external_id=f"{subject}@{n}",
        kind="occurrence",
        title=f"Service {subject} down",
        klass="DockerServiceDown",
        subject=subject,
        severity="critical",
        occurred_at=kw.pop("occurred_at", NOW),
        **kw,
    )


async def _quiet_everything_else(db_pool) -> None:
    """The test database is shared, so age every existing event out of the
    window this file asks about."""
    await db_pool.execute(
        "UPDATE problem_events SET occurred_at = $1 WHERE occurred_at > $1",
        NOW - timedelta(days=30),
    )


async def test_the_digest_counts_what_the_window_saw(db_pool):
    await _quiet_everything_else(db_pool)
    fresh, older, quiet = _subject(), _subject(), _subject()
    a = await ingest_event(db_pool, _occ(fresh), now=NOW)
    b = await ingest_event(db_pool, _occ(older, occurred_at=NOW - timedelta(hours=40)), now=NOW - timedelta(hours=40))
    await ingest_event(db_pool, _occ(quiet, occurred_at=NOW - timedelta(days=10)), now=NOW - timedelta(days=10))
    # `older` is an old problem that recurred inside the window: it counts, but
    # not as new.
    await ingest_event(db_pool, _occ(older, 2, occurred_at=NOW - timedelta(hours=2)), now=NOW)
    await set_status(db_pool, a.problem_id, "resolved", reason="recovered", now=NOW)

    out = await digest(db_pool, hours=24, now=NOW)
    ids = [p["id"] for p in out["problems"]]
    assert a.problem_id in ids and b.problem_id in ids
    assert quiet not in [p["subject"] for p in out["problems"]], "outside the window"
    counts = out["counts"]
    assert counts["total"] == 2
    assert counts["new"] == 1, "only the problem first seen inside the window"
    assert counts["resolved"] == 1
    assert counts["open"] == 1
    assert counts["occurrences"] == 3
    assert counts["investigated"] == 0
    # Newest activity first.
    assert ids[0] == b.problem_id or out["problems"][0]["last_seen_at"] >= out["problems"][-1]["last_seen_at"]


async def test_the_digest_separates_what_was_not_raised(db_pool):
    await _quiet_everything_else(db_pool)
    suppressed, muted = _subject(), _subject()
    await set_service_state(db_pool, suppressed, "deploying", minutes=30, set_by="ansible", now=NOW)
    s = await ingest_event(db_pool, _occ(suppressed), now=NOW)
    m = await ingest_event(db_pool, _occ(muted), now=NOW)
    await db_pool.execute(
        "UPDATE problems SET muted_until = $2 WHERE id = $1::uuid",
        m.problem_id,
        NOW + timedelta(hours=6),
    )

    counts = (await digest(db_pool, hours=24, now=NOW))["counts"]
    assert counts["suppressed"] == 1 and counts["muted"] == 1
    assert counts["open"] == 1, "the suppressed one is not counted open"
    assert s.problem_id and m.problem_id
    await set_service_state(db_pool, suppressed, "ok", set_by="ansible", now=NOW)


async def test_an_investigation_in_the_window_is_counted(db_pool):
    await _quiet_everything_else(db_pool)
    s = _subject()
    r = await ingest_event(db_pool, _occ(s), now=NOW)
    await ingest_event(
        db_pool,
        Event(
            source="investigation",
            external_id=f"inv-{uuid.uuid4().hex[:8]}",
            kind="investigation",
            title="looked at it",
            problem_id=r.problem_id,
            occurred_at=NOW,
        ),
        now=NOW,
    )
    assert (await digest(db_pool, hours=24, now=NOW))["counts"]["investigated"] == 1


async def test_an_empty_window_is_empty_not_an_error(db_pool):
    await _quiet_everything_else(db_pool)
    out = await digest(db_pool, hours=1, now=NOW + timedelta(days=400))
    assert out["counts"]["total"] == 0 and out["problems"] == []


async def test_close_resolved_retires_old_ones_and_frees_the_key(db_pool):
    """Closing is what lets the same subject break again next month as a NEW
    problem: the unique index covers open keys only."""
    s = _subject()
    old = await ingest_event(db_pool, _occ(s), now=NOW - timedelta(days=30))
    await set_status(db_pool, old.problem_id, "resolved", reason="fixed", now=NOW - timedelta(days=30))
    recent = await ingest_event(db_pool, _occ(_subject()), now=NOW)
    await set_status(db_pool, recent.problem_id, "resolved", reason="fixed", now=NOW)
    live = await ingest_event(db_pool, _occ(_subject()), now=NOW)

    closed = await close_resolved(db_pool, days=7, now=NOW)

    assert old.problem_id in closed
    assert recent.problem_id not in closed, "still inside the keep window"
    assert live.problem_id not in closed, "never resolved"
    p = await get_problem(db_pool, old.problem_id)
    assert p["status"] == "closed" and p["closed_at"] is not None
    change = [
        e
        for e in await list_events(db_pool, old.problem_id)
        if e["kind"] == "state_change" and e["payload"].get("action") == "close"
    ]
    assert len(change) == 1 and "7 days" in change[0]["payload"]["reason"]

    # The key is free: the same subject now creates a fresh problem.
    again = await ingest_event(db_pool, _occ(s, 2), now=NOW)
    assert again.problem_id != old.problem_id and again.action == "created"

    # Idempotent: a second sweep finds nothing left to close.
    assert old.problem_id not in await close_resolved(db_pool, days=7, now=NOW)


async def test_close_resolved_zero_days_closes_everything_resolved(db_pool):
    r = await ingest_event(db_pool, _occ(_subject()), now=NOW)
    await set_status(db_pool, r.problem_id, "resolved", reason="fixed", now=NOW)
    assert r.problem_id in await close_resolved(db_pool, days=0, now=NOW)
