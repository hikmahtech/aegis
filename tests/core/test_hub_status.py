"""PR 3b hub additions: the investigate decision, status transitions, mutes,
the verification delay by class, stale-problem lookup, and the source
normalisation `event_from_alert` applies to the synthetic-alert producers."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from aegis.services import hub_project
from aegis.services.hub import (
    Event,
    IngestResult,
    event_from_alert,
    get_problem,
    ingest_event,
    list_events,
    mute_problem,
    set_status,
    slug,
    stale_open_problems,
    verify_seconds,
)

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)


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
        occurred_at=kw.pop("occurred_at", NOW + timedelta(minutes=n)),
    )


# --- pure ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("action", "suppressed", "muted", "expected"),
    [
        ("created", False, False, True),
        ("reopened", False, False, True),
        ("rolled_over", False, False, True),
        ("promoted", False, False, True),
        ("attached", False, False, False),
        ("duplicate", False, False, False),
        ("resolved", False, False, False),
        ("noted", False, False, False),
        ("ignored", False, False, False),
        ("created", True, False, False),
        ("created", False, True, False),
    ],
)
def test_investigate_decision(action, suppressed, muted, expected):
    r = IngestResult("p", action, "k", occurrences=1, muted=muted, suppressed=suppressed)
    assert r.investigate is expected
    assert r.to_dict()["investigate"] is expected


@pytest.mark.parametrize(
    ("klass", "seconds"),
    [
        ("NodeDown", 300),
        ("DockerServiceDown", 300),
        ("ServiceDownProlonged", 0),
        ("HeartbeatCollectFailed", 0),
        ("DiskAlmostFull", 0),
        ("HostOutOfMemory", 0),
        ("OOMKilled", 0),
        ("HighCPU", 180),
        ("", 180),
    ],
)
def test_verify_seconds_by_class(klass, seconds):
    assert verify_seconds(klass) == seconds


def test_slug_is_the_hub_normalisation():
    assert slug("Monitoring CAdvisor") == "monitoring-cadvisor"
    assert slug("aegis_core") == "aegis_core"


def test_synthetic_alert_sources_map_onto_the_vocabulary():
    for src, expect in (("todoist-jira", "chat"), ("todoist-chat", "chat"), ("todoist-infra", "chat"), ("bogus", "manual"), ("grafana", "grafana")):
        e = event_from_alert({"source": src, "title": "t", "fingerprint": "f", "labels": {"alertname": "X"}}, occurred_at=NOW)
        assert e.source == expect, src


def test_sentry_occurrence_id_uses_last_seen_so_webhook_and_poll_meet():
    raw = {"id": "4711", "metadata": {"type": "ValueError"}, "lastSeen": "2026-09-07T10:00:00Z"}
    a = event_from_alert({"source": "sentry", "title": "t", "fingerprint": "sentry:4711", "service": "api", "raw_payload": raw}, occurred_at=NOW)
    b = event_from_alert({"source": "sentry", "title": "t", "fingerprint": "sentry:4711", "service": "api", "raw_payload": raw}, occurred_at=NOW + timedelta(hours=1))
    assert a.external_id == b.external_id == "sentry:4711@2026-09-07T10:00:00Z"


# --- set_status ---------------------------------------------------------------


async def test_set_status_moves_a_live_problem_and_records_it(db_pool):
    s = _subject()
    r = await ingest_event(db_pool, _occ(s), now=NOW)
    assert await set_status(db_pool, r.problem_id, "investigating", reason="started", now=NOW) is True
    assert await set_status(db_pool, r.problem_id, "investigating", reason="again", now=NOW) is False
    assert await set_status(db_pool, r.problem_id, "resolved", reason="fixed", now=NOW + timedelta(hours=1))
    p = await get_problem(db_pool, r.problem_id)
    assert p["status"] == "resolved" and p["resolved_at"] == NOW + timedelta(hours=1)
    changes = [e["payload"] for e in await list_events(db_pool, r.problem_id) if e["kind"] == "state_change"]
    assert [c["status"] for c in changes if c.get("action") == "set_status"] == ["resolved", "investigating"]


async def test_set_status_rejects_closed_and_bad_values(db_pool):
    s = _subject()
    r = await ingest_event(db_pool, _occ(s), now=NOW)
    with pytest.raises(ValueError):
        await set_status(db_pool, r.problem_id, "closed", reason="x")
    with pytest.raises(ValueError):
        await set_status(db_pool, r.problem_id, "exploded", reason="x")
    await db_pool.execute("UPDATE problems SET closed_at = $2 WHERE id = $1::uuid", r.problem_id, NOW)
    assert await set_status(db_pool, r.problem_id, "fixing", reason="x", now=NOW) is False
    assert await set_status(db_pool, str(uuid.uuid4()), "fixing", reason="x", now=NOW) is False


# --- mute ---------------------------------------------------------------------


async def test_mute_silences_projection_and_investigation_but_counts(db_pool):
    s = _subject()
    r = await ingest_event(db_pool, _occ(s, 1), now=NOW)
    until = await mute_problem(db_pool, r.problem_id, hours=24, by="gate2", reason="user", now=NOW)
    assert until == NOW + timedelta(hours=24)
    again = await ingest_event(db_pool, _occ(s, 2), now=NOW + timedelta(hours=1))
    assert again.muted is True and again.investigate is False and again.occurrences == 2
    assert (await hub_project.project(db_pool, r.problem_id, now=NOW + timedelta(hours=1)))["skipped"] == "muted"
    later = await ingest_event(db_pool, _occ(s, 3), now=NOW + timedelta(hours=25))
    assert later.muted is False
    assert any(e["payload"].get("action") == "mute" for e in await list_events(db_pool, r.problem_id))
    assert await mute_problem(db_pool, str(uuid.uuid4()), hours=1, by="x", now=NOW) is None


# --- stale problems -----------------------------------------------------------


async def test_stale_open_problems_needs_age_and_no_recent_investigation(db_pool):
    old, fresh, investigated = _subject(), _subject(), _subject()
    t0 = NOW - timedelta(hours=30)
    r_old = await ingest_event(db_pool, _occ(old, occurred_at=t0), now=t0)
    await ingest_event(db_pool, _occ(fresh, occurred_at=NOW - timedelta(hours=2)), now=NOW - timedelta(hours=2))
    r_inv = await ingest_event(db_pool, _occ(investigated, occurred_at=t0), now=t0)
    await ingest_event(
        db_pool,
        Event(source="investigation", external_id=f"inv-{investigated}", kind="investigation", title="looked", problem_id=r_inv.problem_id, occurred_at=NOW - timedelta(hours=1)),
        now=NOW - timedelta(hours=1),
    )
    rows = await stale_open_problems(db_pool, [old, fresh, investigated, "unknown"], hours=24, now=NOW)
    assert [r["id"] for r in rows] == [r_old.problem_id]
    assert rows[0]["subject"] == old and rows[0]["hours"] == 30.0
    assert await stale_open_problems(db_pool, [], hours=24, now=NOW) == []
    # a suppressed problem is never re-investigated; a resolved one neither
    await set_status(db_pool, r_old.problem_id, "resolved", reason="x", now=NOW)
    assert await stale_open_problems(db_pool, [old], hours=24, now=NOW) == []


# --- link_task ------------------------------------------------------------------


async def test_link_task_adopts_an_existing_task_once(db_pool):
    s = _subject()
    r = await ingest_event(db_pool, _occ(s, 1), now=NOW)
    assert await hub_project.link_task(db_pool, r.problem_id, "T_EXISTING") is True
    p = await get_problem(db_pool, r.problem_id)
    assert p["todoist_task_id"] == "T_EXISTING"
    assert p["metadata"]["projected_event_id"] > 0  # history before the link is not replayed
    assert await hub_project.link_task(db_pool, r.problem_id, "T_OTHER") is False
    assert (await get_problem(db_pool, r.problem_id))["todoist_task_id"] == "T_EXISTING"
    assert await hub_project.link_task(db_pool, str(uuid.uuid4()), "T") is False
