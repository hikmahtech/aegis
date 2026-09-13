"""Resolving an alertmanager problem whose alert alertmanager forgot (#551).

Alertmanager keeps its firing alerts in memory. A restart makes it forget every
one it was holding, so their `resolved` webhooks are never sent — and the
alertmanager lane was the only producer on the hub with no other way back, so
one lost webhook stranded the problem AND its Todoist task for good. Seen live:
a repaired overlay fault sat `waiting_human` for 15 hours with zero resolution
events while alertmanager reported no active alerts at all.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from aegis.services.hub import Event, get_problem, ingest_event, set_status
from aegis.services.hub_watch import reconcile_alertmanager

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)


def _fp() -> str:
    return uuid.uuid4().hex[:16]


async def _alert_problem(pool, fingerprint: str, *, at: datetime, klass: str = "clickhousedown"):
    """A problem exactly as the alertmanager webhook raises one: the occurrence
    id is `<fingerprint>@<startsAt>`."""
    subject = f"host_{uuid.uuid4().hex[:8]}"
    result = await ingest_event(
        pool,
        Event(
            source="alertmanager",
            external_id=f"{fingerprint}@{at.isoformat()}",
            kind="occurrence",
            title=f"{klass} on {subject}",
            klass=klass,
            subject=subject,
            subject_kind="service",
            severity="critical",
            occurred_at=at,
        ),
        now=at,
    )
    return result.problem_id


async def test_an_alert_alertmanager_no_longer_lists_is_resolved(db_pool):
    """Falsifiable: stop matching on the fingerprint and this stays live."""
    gone, still = _fp(), _fp()
    old = NOW - timedelta(hours=3)
    gone_id = await _alert_problem(db_pool, gone, at=old)
    still_id = await _alert_problem(db_pool, still, at=old)

    out = await reconcile_alertmanager(
        db_pool, active_fingerprints={still}, now=NOW
    )

    assert gone_id in [r["problem_id"] for r in out["resolved"]]
    assert still_id not in [r["problem_id"] for r in out["resolved"]]
    assert (await get_problem(db_pool, gone_id))["status"] == "resolved"
    assert (await get_problem(db_pool, still_id))["status"] == "open"


async def test_the_resolve_says_what_it_actually_knows(db_pool):
    """It knows alertmanager stopped listing the alert. It does NOT know the
    alert cleared, and the timeline must not claim it did."""
    fingerprint = _fp()
    pid = await _alert_problem(db_pool, fingerprint, at=NOW - timedelta(hours=2))

    await reconcile_alertmanager(db_pool, active_fingerprints=set(), now=NOW)

    reason = await db_pool.fetchval(
        "SELECT payload->>'reason' FROM problem_events "
        "WHERE problem_id = $1::uuid AND kind = 'resolved' ORDER BY id DESC LIMIT 1",
        pid,
    )
    assert "no longer lists" in reason
    assert "restart" in reason


async def test_a_problem_inside_the_grace_period_is_left_alone(db_pool):
    """A problem raised seconds ago must not be resolved before alertmanager has
    even grouped its alert.

    Falsifiable: drop the `first_seen_at` bound and this resolves.
    """
    pid = await _alert_problem(db_pool, _fp(), at=NOW - timedelta(minutes=2))

    out = await reconcile_alertmanager(
        db_pool, active_fingerprints=set(), now=NOW, grace_minutes=10.0
    )

    assert pid not in [r["problem_id"] for r in out["resolved"]]
    assert (await get_problem(db_pool, pid))["status"] == "open"


async def test_a_problem_from_another_producer_is_never_touched(db_pool):
    """The heartbeat resolves its own problems by re-checking the swarm. Reading
    alertmanager says nothing about them, so they are out of scope.

    Falsifiable: drop the `source` filter and this heartbeat problem resolves.
    """
    subject = f"svc_{uuid.uuid4().hex[:8]}"
    at = NOW - timedelta(hours=3)
    result = await ingest_event(
        db_pool,
        Event(
            source="heartbeat",
            external_id=f"aegis-heartbeat:DockerServiceDown:{subject}@{at.isoformat()}",
            kind="occurrence",
            title=f"Service {subject} down",
            klass="dockerservicedown",
            subject=subject,
            subject_kind="service",
            severity="critical",
            occurred_at=at,
        ),
        now=at,
    )

    out = await reconcile_alertmanager(db_pool, active_fingerprints=set(), now=NOW)

    assert result.problem_id not in [r["problem_id"] for r in out["resolved"]]
    assert (await get_problem(db_pool, result.problem_id))["status"] == "open"


async def test_a_problem_a_person_is_working_is_still_resolved(db_pool):
    """`waiting_human` is the state the stranded problems were found in — an
    investigation parked them and then nothing could ever close them. If the
    alert is gone, the chore is done, whatever state it parked in."""
    pid = await _alert_problem(db_pool, _fp(), at=NOW - timedelta(hours=15))
    await set_status(db_pool, pid, "waiting_human", reason="parked", now=NOW)

    out = await reconcile_alertmanager(db_pool, active_fingerprints=set(), now=NOW)

    assert pid in [r["problem_id"] for r in out["resolved"]]
    assert (await get_problem(db_pool, pid))["status"] == "resolved"


async def test_a_recurring_alert_gets_a_new_fingerprint_and_is_not_resolved(db_pool):
    """The defect that reached production (#561).

    An alertmanager fingerprint is a hash of the label set, so the same
    recurring fault arrives under a NEW fingerprint each time — one prod problem
    had 26 distinct fingerprints across 27 occurrences. Judging the problem on
    its FIRST occurrence compares against a hash that can never be active again,
    so the problem was resolved within a minute of every legitimate reopen, for
    ever. The Dagster pipeline failure this was found on was closed twice in 35
    minutes, the second time one minute after a real new failure reopened it.

    Falsifiable: go back to the first occurrence's fingerprint and this resolves
    a problem whose alert alertmanager is actively listing.
    """
    old_fp, new_fp = _fp(), _fp()
    subject = f"host_{uuid.uuid4().hex[:8]}"
    first = NOW - timedelta(days=4)
    # Same problem, two episodes, different label sets.
    for fingerprint, at in ((old_fp, first), (new_fp, NOW - timedelta(hours=1))):
        result = await ingest_event(
            db_pool,
            Event(
                source="alertmanager",
                external_id=f"{fingerprint}@{at.isoformat()}",
                kind="occurrence",
                title=f"dagster-pipeline-failure on {subject}",
                klass="dagster-pipeline-failure",
                subject=subject,
                subject_kind="service",
                severity="critical",
                occurred_at=at,
            ),
            now=at,
        )
    pid = result.problem_id

    # Alertmanager lists the CURRENT alert. The stale one is long gone.
    out = await reconcile_alertmanager(
        db_pool, active_fingerprints={new_fp}, now=NOW
    )

    assert pid not in [r["problem_id"] for r in out["resolved"]], (
        "a problem whose current alert is firing was resolved on a stale fingerprint"
    )
    assert (await get_problem(db_pool, pid))["status"] == "open"

    # And when alertmanager lists neither, it does resolve — on the evidence of
    # every fingerprint, not one of them.
    out = await reconcile_alertmanager(db_pool, active_fingerprints=set(), now=NOW)
    resolved = [r for r in out["resolved"] if r["problem_id"] == pid]
    assert resolved and resolved[0]["fingerprints_checked"] == 2


async def test_a_problem_that_fired_seconds_ago_is_not_resolved(db_pool):
    """The second half of the same defect: the grace period bounded
    `first_seen_at`, so a four-day-old problem with an occurrence thirty seconds
    ago sailed straight through it. What matters is the LAST occurrence — an
    alert that just fired is firing now, whatever alertmanager has grouped.

    Falsifiable: bound `first_seen_at` again and this resolves.
    """
    subject = f"host_{uuid.uuid4().hex[:8]}"
    old = NOW - timedelta(days=4)
    await ingest_event(
        db_pool,
        Event(
            source="alertmanager",
            external_id=f"{_fp()}@{old.isoformat()}",
            kind="occurrence",
            title=f"clickhousedown on {subject}",
            klass="clickhousedown",
            subject=subject,
            subject_kind="service",
            severity="critical",
            occurred_at=old,
        ),
        now=old,
    )
    just_now = NOW - timedelta(seconds=30)
    result = await ingest_event(
        db_pool,
        Event(
            source="alertmanager",
            external_id=f"{_fp()}@{just_now.isoformat()}",
            kind="occurrence",
            title=f"clickhousedown on {subject}",
            klass="clickhousedown",
            subject=subject,
            subject_kind="service",
            severity="critical",
            occurred_at=just_now,
        ),
        now=just_now,
    )

    out = await reconcile_alertmanager(db_pool, active_fingerprints=set(), now=NOW)

    assert result.problem_id not in [r["problem_id"] for r in out["resolved"]]
    assert (await get_problem(db_pool, result.problem_id))["status"] == "open"
