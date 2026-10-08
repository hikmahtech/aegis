"""PR 3b hub additions that stay: the fresh-problem decision, status
transitions, mutes set before the infra lane left, and task linking. The
verification delay, stale-problem lookup, investigation claims and the alert
source normalisation left with the infra lane (DevOps vertical, a2-devops)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from aegis.services import hub_project
from aegis.services.hub import (
    Event,
    IngestResult,
    get_problem,
    ingest_event,
    list_events,
    set_status,
    slug,
)

from tests.hub_helpers import mute_problem

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)


def _subject() -> str:
    return f"svc_{uuid.uuid4().hex[:8]}"


def _occ(subject: str, n: int = 1, **kw) -> Event:
    return Event(
        source="flow_health",
        external_id=f"{subject}@{n}",
        kind="occurrence",
        title=f"Service {subject} down",
        klass="DockerServiceDown",
        subject=subject,
        occurred_at=kw.pop("occurred_at", NOW + timedelta(minutes=n)),
    )


# --- pure ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("action", "muted", "expected"),
    [
        ("created", False, True),
        ("reopened", False, True),
        ("rolled_over", False, True),
        ("attached", False, False),
        ("duplicate", False, False),
        ("resolved", False, False),
        ("noted", False, False),
        ("ignored", False, False),
        ("created", True, False),
    ],
)
def test_investigate_decision(action, muted, expected):
    r = IngestResult("p", action, "k", occurrences=1, muted=muted)
    assert r.investigate is expected
    assert r.to_dict()["investigate"] is expected


def test_slug_is_the_hub_normalisation():
    assert slug("Monitoring CAdvisor") == "monitoring-cadvisor"
    assert slug("aegis_core") == "aegis_core"


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
    # A move into `resolved` is written as `resolve`, the same word an
    # incoming resolved alert writes — it is what the projector closes a task
    # on, so a resolve reached from an investigation or the admin panel says
    # the same thing on the task as one reached from the producer.
    assert [c["status"] for c in changes if c.get("action") == "set_status"] == ["investigating"]
    assert [c["status"] for c in changes if c.get("action") == "resolve"] == ["resolved"]


async def test_set_status_rejects_closed_and_bad_values(db_pool):
    s = _subject()
    r = await ingest_event(db_pool, _occ(s), now=NOW)
    with pytest.raises(ValueError):
        await set_status(db_pool, r.problem_id, "closed", reason="x")
    with pytest.raises(ValueError):
        await set_status(db_pool, r.problem_id, "exploded", reason="x")
    await db_pool.execute("UPDATE problems SET closed_at = $2 WHERE id = $1::uuid", r.problem_id, NOW)
    assert await set_status(db_pool, r.problem_id, "investigating", reason="x", now=NOW) is False
    assert await set_status(db_pool, str(uuid.uuid4()), "investigating", reason="x", now=NOW) is False
    with pytest.raises(ValueError):  # `fixing` left with the investigations
        await set_status(db_pool, r.problem_id, "fixing", reason="x")


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


