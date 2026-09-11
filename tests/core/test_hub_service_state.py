"""`service_state`: windows, suppression inside `ingest_event`, promotion once
a window passes, and the heartbeat's converge-clear. Real test database."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from aegis.services.hub import (
    CONVERGE_GRACE,
    Event,
    clear_converged_deploys,
    get_problem,
    ingest_event,
    list_events,
    list_service_states,
    promote_expired_suppressions,
    set_service_state,
)

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)


def _subject() -> str:
    return f"svc_{uuid.uuid4().hex[:8]}"


def _occ(subject: str, n: int = 1, *, kind: str = "service", **kw) -> Event:
    return Event(
        source="heartbeat",
        external_id=f"{subject}@{n}",
        kind="occurrence",
        title=f"DockerServiceDown: {subject}",
        klass="DockerServiceDown",
        subject=subject,
        subject_kind=kind,
        severity="critical",
        occurred_at=kw.pop("occurred_at", NOW + timedelta(minutes=n)),
    )


def _resolved(subject: str, n: int) -> Event:
    return Event(
        source="heartbeat",
        external_id=f"{subject}@{n}@resolved",
        kind="resolved",
        title="recovered",
        klass="DockerServiceDown",
        subject=subject,
        occurred_at=NOW + timedelta(minutes=n),
    )


# --- the table itself ---------------------------------------------------------


async def test_set_list_and_clear(db_pool):
    s = _subject()
    row = await set_service_state(db_pool, s, "deploying", minutes=15, set_by="ansible", note="release", now=NOW)
    assert row["state"] == "deploying" and row["until_at"] == NOW + timedelta(minutes=15)
    assert row["subject_kind"] == "service" and row["note"] == "release"
    assert any(r["subject"] == s for r in await list_service_states(db_pool, now=NOW))
    # expired windows are not "in force"
    assert not any(r["subject"] == s for r in await list_service_states(db_pool, now=NOW + timedelta(hours=1)))
    ok = await set_service_state(db_pool, s, "ok", set_by="ansible", now=NOW)
    assert ok == {"subject": s, "subject_kind": "service", "state": "ok", "cleared": True}
    again = await set_service_state(db_pool, s, "ok", set_by="ansible", now=NOW)
    assert again["cleared"] is False


async def test_upsert_replaces_the_window(db_pool):
    s = _subject()
    await set_service_state(db_pool, s, "deploying", minutes=5, set_by="ansible", now=NOW)
    row = await set_service_state(db_pool, s, "maintenance", minutes=60, set_by="chat:pandora", note="disk swap", now=NOW)
    assert row["state"] == "maintenance" and row["set_by"] == "chat:pandora"
    assert row["until_at"] == NOW + timedelta(minutes=60)


async def test_subject_is_slugged_and_wildcard_kept(db_pool):
    row = await set_service_state(db_pool, "Monitoring CAdvisor", "degraded", set_by="api", now=NOW)
    assert row["subject"] == "monitoring-cadvisor"
    star = await set_service_state(db_pool, "*", "maintenance", subject_kind="*", minutes=1, set_by="api", now=NOW)
    assert (star["subject"], star["subject_kind"]) == ("*", "*")
    await set_service_state(db_pool, "*", "ok", subject_kind="*", set_by="api", now=NOW)


@pytest.mark.parametrize(
    ("subject", "state", "set_by"),
    [("", "deploying", "api"), ("svc", "exploded", "api"), ("svc", "deploying", " ")],
)
async def test_invalid_inputs_are_refused(db_pool, subject, state, set_by):
    with pytest.raises(ValueError):
        await set_service_state(db_pool, subject, state, set_by=set_by, now=NOW)


# --- suppression inside ingest ------------------------------------------------


async def test_occurrence_during_deploy_creates_a_suppressed_problem(db_pool):
    s = _subject()
    await set_service_state(db_pool, s, "deploying", minutes=15, set_by="ansible", note="rel", now=NOW)
    r = await ingest_event(db_pool, _occ(s, 1), now=NOW)
    assert r.action == "created" and r.suppressed is True
    p = await get_problem(db_pool, r.problem_id)
    assert p["status"] == "suppressed"
    occ = [e for e in await list_events(db_pool, r.problem_id) if e["kind"] == "occurrence"][0]
    assert occ["payload"]["suppressed_by"]["set_by"] == "ansible"
    assert occ["payload"]["suppressed_by"]["note"] == "rel"


async def test_an_unreadable_service_state_suppresses_nothing(db_pool, monkeypatch):
    """Spec §10: `service_state` reads fail open. The lookup runs inside the
    ingest transaction, and a failed statement aborts a Postgres transaction —
    so catching the exception is not enough on its own: every later statement
    in the ingest would fail too, and the alert would not be recorded at all.
    Here the lookup really does fail in the database, on the ingest's own
    connection."""

    async def unreadable(conn, subject, subject_kind, now):
        return await conn.fetchrow("SELECT * FROM service_state_gone_zz")

    monkeypatch.setattr("aegis.services.hub._active_suppression", unreadable)
    s = _subject()
    r = await ingest_event(db_pool, _occ(s, 1), now=NOW)
    assert r.action == "created" and r.suppressed is False
    assert (await get_problem(db_pool, r.problem_id))["status"] == "open"
    occ = [e for e in await list_events(db_pool, r.problem_id) if e["kind"] == "occurrence"]
    assert len(occ) == 1 and "suppressed_by" not in occ[0]["payload"]


async def test_occurrence_on_an_open_problem_during_deploy_attaches_quietly(db_pool):
    s = _subject()
    first = await ingest_event(db_pool, _occ(s, 1), now=NOW)
    assert first.suppressed is False
    await set_service_state(db_pool, s, "deploying", minutes=15, set_by="ansible", now=NOW)
    second = await ingest_event(db_pool, _occ(s, 2), now=NOW)
    assert second.action == "attached" and second.suppressed is True
    assert (await get_problem(db_pool, first.problem_id))["status"] == "open"


async def test_occurrence_after_the_window_promotes_the_problem(db_pool):
    s = _subject()
    await set_service_state(db_pool, s, "deploying", minutes=15, set_by="ansible", now=NOW)
    r = await ingest_event(db_pool, _occ(s, 1), now=NOW)
    later = NOW + timedelta(minutes=20)
    again = await ingest_event(db_pool, _occ(s, 2, occurred_at=later), now=later)
    assert again.action == "promoted" and again.suppressed is False and again.occurrences == 2
    p = await get_problem(db_pool, r.problem_id)
    assert p["status"] == "open"
    kinds = [e["kind"] for e in await list_events(db_pool, r.problem_id)]
    assert kinds.count("state_change") == 2


async def test_resolved_during_the_window_resolves_the_suppressed_problem(db_pool):
    s = _subject()
    await set_service_state(db_pool, s, "deploying", minutes=15, set_by="ansible", now=NOW)
    r = await ingest_event(db_pool, _occ(s, 1), now=NOW)
    done = await ingest_event(db_pool, _resolved(s, 2), now=NOW)
    assert done.action == "resolved" and done.problem_id == r.problem_id
    assert (await get_problem(db_pool, r.problem_id))["status"] == "resolved"


async def test_wildcard_maintenance_suppresses_every_kind(db_pool):
    node = f"node_{uuid.uuid4().hex[:6]}"
    await set_service_state(db_pool, "*", "maintenance", subject_kind="*", minutes=30, set_by="chat:x", note="power cut", now=NOW)
    try:
        r = await ingest_event(db_pool, _occ(node, 1, kind="node"), now=NOW)
        assert r.suppressed is True
        assert (await get_problem(db_pool, r.problem_id))["status"] == "suppressed"
    finally:
        await set_service_state(db_pool, "*", "ok", subject_kind="*", set_by="chat:x", now=NOW)


async def test_exact_window_beats_wildcard_in_the_payload(db_pool):
    s = _subject()
    await set_service_state(db_pool, "*", "maintenance", subject_kind="service", minutes=30, set_by="wild", now=NOW)
    await set_service_state(db_pool, s, "deploying", minutes=30, set_by="exact", now=NOW)
    try:
        r = await ingest_event(db_pool, _occ(s, 1), now=NOW)
        occ = [e for e in await list_events(db_pool, r.problem_id) if e["kind"] == "occurrence"][0]
        assert occ["payload"]["suppressed_by"]["set_by"] == "exact"
    finally:
        await set_service_state(db_pool, "*", "ok", subject_kind="service", set_by="wild", now=NOW)


async def test_degraded_does_not_suppress(db_pool):
    s = _subject()
    await set_service_state(db_pool, s, "degraded", set_by="api", now=NOW)
    r = await ingest_event(db_pool, _occ(s, 1), now=NOW)
    assert r.suppressed is False and (await get_problem(db_pool, r.problem_id))["status"] == "open"


async def test_reopen_inside_a_window_reopens_as_suppressed(db_pool):
    s = _subject()
    r = await ingest_event(db_pool, _occ(s, 1), now=NOW)
    await ingest_event(db_pool, _resolved(s, 2), now=NOW)
    await set_service_state(db_pool, s, "deploying", minutes=30, set_by="ansible", now=NOW)
    again = await ingest_event(db_pool, _occ(s, 3), now=NOW)
    assert again.action == "reopened" and again.suppressed is True
    assert (await get_problem(db_pool, r.problem_id))["status"] == "suppressed"


# --- the sweep ----------------------------------------------------------------


async def test_promote_expired_suppressions_opens_only_expired_windows(db_pool):
    a, b = _subject(), _subject()
    await set_service_state(db_pool, a, "deploying", minutes=10, set_by="ansible", now=NOW)
    await set_service_state(db_pool, b, "deploying", minutes=120, set_by="ansible", now=NOW)
    ra = await ingest_event(db_pool, _occ(a, 1), now=NOW)
    rb = await ingest_event(db_pool, _occ(b, 1), now=NOW)
    later = NOW + timedelta(minutes=30)
    promoted = await promote_expired_suppressions(db_pool, now=later)
    assert ra.problem_id in promoted and rb.problem_id not in promoted
    assert (await get_problem(db_pool, ra.problem_id))["status"] == "open"
    assert (await get_problem(db_pool, rb.problem_id))["status"] == "suppressed"
    ev = [e for e in await list_events(db_pool, ra.problem_id) if e["kind"] == "state_change"]
    assert any(e["payload"].get("reason") == "suppression_expired" for e in ev)
    # idempotent
    assert ra.problem_id not in await promote_expired_suppressions(db_pool, now=later)


async def test_cleared_window_promotes_on_the_next_sweep(db_pool):
    s = _subject()
    await set_service_state(db_pool, s, "deploying", set_by="ansible", now=NOW)  # open-ended
    r = await ingest_event(db_pool, _occ(s, 1), now=NOW)
    assert r.problem_id not in await promote_expired_suppressions(db_pool, now=NOW)
    await set_service_state(db_pool, s, "ok", set_by="ansible", now=NOW)
    assert r.problem_id in await promote_expired_suppressions(db_pool, now=NOW)


# --- the heartbeat's converge-clear ------------------------------------------


async def test_clear_converged_deploys_respects_grace_stuck_and_wildcard(db_pool):
    fresh, old, still, star = _subject(), _subject(), _subject(), "*"
    await set_service_state(db_pool, fresh, "deploying", set_by="ansible", now=NOW)
    await set_service_state(db_pool, old, "deploying", set_by="ansible", now=NOW - CONVERGE_GRACE * 2)
    await set_service_state(db_pool, still, "deploying", set_by="ansible", now=NOW - CONVERGE_GRACE * 2)
    await set_service_state(db_pool, star, "deploying", set_by="ansible", now=NOW - CONVERGE_GRACE * 2)
    try:
        cleared = await clear_converged_deploys(db_pool, [still], now=NOW)
        assert old in cleared
        assert fresh not in cleared and still not in cleared and star not in cleared
        left = {r["subject"] for r in await list_service_states(db_pool, now=NOW)}
        assert {fresh, still, star} <= left and old not in left
    finally:
        await set_service_state(db_pool, star, "ok", set_by="ansible", now=NOW)


async def test_clear_converged_leaves_maintenance_alone(db_pool):
    s = _subject()
    await set_service_state(db_pool, s, "maintenance", set_by="chat:x", now=NOW - CONVERGE_GRACE * 2)
    assert s not in await clear_converged_deploys(db_pool, [], now=NOW)
