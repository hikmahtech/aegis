"""The admin Problems routes: reads, and mutations that reuse the hub's own
transitions rather than re-implementing them.

Real database and the real app — the point of these routes is what they do to
the tables, and a mocked pool would let the handlers do anything.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from aegis.api.app import create_app
from aegis.api.auth import verify_auth
from aegis.api.deps import get_settings
from aegis.config import Settings
from aegis.services.hub import Event, get_problem, ingest_event, list_events, set_status
from httpx import ASGITransport, AsyncClient

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 9, 8, 9, 0, tzinfo=UTC)

_SETTINGS = {
    "database_url": "postgresql://test:test@localhost:5432/test",
    "litellm_url": "https://litellm.example.com/v1",
    "temporal_ui_url": "https://temporal.example.com",
    "n8n_ui_url": "https://n8n.example.com",
    "admin_username": "admin",
    "admin_password": "admin",
    "n8n_webhook_secret": "test-secret",
}


@pytest_asyncio.fixture(loop_scope="function")
async def client(db_pool):
    app = create_app(run_lifespan=False)
    app.state.db_pool = db_pool
    app.dependency_overrides[get_settings] = lambda: Settings(**_SETTINGS)
    app.dependency_overrides[verify_auth] = lambda: True
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


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
        occurred_at=kw.pop("occurred_at", NOW + timedelta(minutes=n)),
        **kw,
    )


async def test_the_list_is_live_first_and_filters(client, db_pool):
    a, b = _subject(), _subject()
    live = await ingest_event(db_pool, _occ(a), now=NOW)
    gone = await ingest_event(db_pool, _occ(b), now=NOW)
    await db_pool.execute(
        "UPDATE problems SET status = 'closed', closed_at = $2 WHERE id = $1::uuid",
        gone.problem_id,
        NOW,
    )

    ids = [p["id"] for p in (await client.get("/api/admin/problems?limit=500")).json()["problems"]]
    assert live.problem_id in ids and gone.problem_id not in ids

    with_closed = await client.get("/api/admin/problems?include_closed=true&limit=500")
    assert gone.problem_id in [p["id"] for p in with_closed.json()["problems"]]

    by_subject = await client.get(f"/api/admin/problems?subject={a}")
    assert [p["id"] for p in by_subject.json()["problems"]] == [live.problem_id]

    by_status = await client.get(f"/api/admin/problems?status=open&subject={a}")
    assert [p["id"] for p in by_status.json()["problems"]] == [live.problem_id]
    assert (await client.get(f"/api/admin/problems?status=fixing&subject={a}")).json()[
        "problems"
    ] == []


async def test_detail_carries_the_timeline_links_and_sessions(client, db_pool):
    from aegis.services import work_sessions
    from aegis.services.hub_project import link_task

    s = _subject()
    task = f"zzd-{uuid.uuid4().hex[:6]}"
    r = await ingest_event(db_pool, _occ(s), now=NOW)
    await db_pool.execute(
        "INSERT INTO todoist_tasks (id, content, labels, is_completed, updated_at) "
        "VALUES ($1, 'fix it', ARRAY['@pandora'], false, now()) ON CONFLICT (id) DO NOTHING",
        task,
    )
    await link_task(db_pool, r.problem_id, task)
    await work_sessions.create_session(db_pool, task_id=task, agent_id="pandoras-actor")

    body = (await client.get(f"/api/admin/problems/{r.problem_id}")).json()
    assert body["problem"]["id"] == r.problem_id
    assert [e["kind"] for e in body["events"]].count("occurrence") == 1
    assert ("todoist_task", task) in [(x["link_kind"], x["ref"]) for x in body["links"]]
    assert [x["owner"] for x in body["sessions"]] == ["aegis"]
    assert body["window"] is None

    missing = await client.get(f"/api/admin/problems/{uuid.uuid4()}")
    assert missing.status_code == 404


async def test_mute_resolve_and_close_reuse_the_hub_transitions(client, db_pool):
    s = _subject()
    r = await ingest_event(db_pool, _occ(s), now=NOW)

    muted = await client.post(f"/api/admin/problems/{r.problem_id}/mute", json={"hours": 3})
    assert muted.status_code == 200 and muted.json()["muted_until"]
    assert (await get_problem(db_pool, r.problem_id))["muted_until"] is not None

    resolved = await client.post(
        f"/api/admin/problems/{r.problem_id}/resolve", json={"reason": "fixed by hand"}
    )
    assert resolved.status_code == 200
    p = await get_problem(db_pool, r.problem_id)
    assert p["status"] == "resolved" and p["resolved_at"] is not None
    # The transition wrote its own event, exactly as the tool path does.
    changes = [e for e in await list_events(db_pool, r.problem_id) if e["kind"] == "state_change"]
    assert any(e["payload"].get("reason") == "fixed by hand" for e in changes)
    # Resolving twice is not an error the operator caused; it is a 404 that
    # says nothing moved.
    assert (
        await client.post(f"/api/admin/problems/{r.problem_id}/resolve", json={})
    ).status_code == 404

    closed = await client.post(f"/api/admin/problems/{r.problem_id}/close")
    assert closed.status_code == 200 and closed.json()["closed"] is True
    assert (await get_problem(db_pool, r.problem_id))["status"] == "closed"


async def test_close_refuses_a_problem_that_is_not_resolved(client, db_pool):
    r = await ingest_event(db_pool, _occ(_subject()), now=NOW)
    assert (await client.post(f"/api/admin/problems/{r.problem_id}/close")).status_code == 409


async def test_merge_moves_the_duplicate_and_reports_what_moved(client, db_pool):
    keep = await ingest_event(db_pool, _occ(_subject()), now=NOW)
    dup = await ingest_event(db_pool, _occ(_subject()), now=NOW)

    r = await client.post(
        f"/api/admin/problems/{keep.problem_id}/merge", json={"merge_id": dup.problem_id}
    )
    assert r.status_code == 200
    assert r.json()["events_moved"] >= 1
    assert (await get_problem(db_pool, dup.problem_id))["status"] == "closed"

    bad = await client.post(
        f"/api/admin/problems/{keep.problem_id}/merge", json={"merge_id": str(uuid.uuid4())}
    )
    assert bad.status_code == 400 and "must exist" in bad.json()["detail"]


async def test_service_state_round_trip(client, db_pool):
    s = _subject()
    put = await client.put(
        "/api/admin/service-state",
        json={"subject": s, "state": "maintenance", "minutes": 15, "note": "disk swap"},
    )
    assert put.status_code == 200 and put.json()["state"] == "maintenance"
    windows = (await client.get("/api/admin/service-state")).json()["windows"]
    assert [w for w in windows if w["subject"] == s][0]["note"] == "disk swap"

    cleared = await client.put("/api/admin/service-state", json={"subject": s, "state": "ok"})
    assert cleared.status_code == 200 and cleared.json()["cleared"] is True
    assert [w for w in (await client.get("/api/admin/service-state")).json()["windows"] if w["subject"] == s] == []

    bad = await client.put("/api/admin/service-state", json={"subject": s, "state": "bogus"})
    assert bad.status_code == 400


async def test_the_digest_route_is_the_briefing_query(client, db_pool):
    # A REAL timestamp, not the module's fixed `NOW`. The digest's window is
    # relative to the wall clock, so an event stamped `NOW + 1min`
    # (2026-09-08 09:01) sat inside `hours=24` only while the wall clock was
    # within a day of it. On 2026-09-09 09:01 UTC it fell out, and this test
    # began failing for everyone, permanently — a time bomb, not a flake.
    # Passing `now=` to `ingest_event` was never enough: the event row takes its
    # timestamp from `Event.occurred_at`, which `_occ` fills from `NOW`.
    real_now = datetime.now(UTC)
    s = _subject()
    r = await ingest_event(db_pool, _occ(s, occurred_at=real_now), now=real_now)
    body = (await client.get("/api/admin/problems/digest?hours=24")).json()
    assert body["counts"]["total"] >= 1
    assert r.problem_id in [p["id"] for p in body["problems"]]
    # The literal path wins over `/problems/{id}` — without that ordering this
    # asks for a problem whose id is the word "digest" and 404s.
    assert "counts" in body

    # A window is a real filter, not decoration: an event outside it is not in
    # the digest. `now` is inside `hours=24` above and outside this one.
    await db_pool.execute(
        "UPDATE problem_events SET occurred_at = now() - interval '3 days' "
        "WHERE problem_id = $1::uuid",
        r.problem_id,
    )
    narrow = (await client.get("/api/admin/problems/digest?hours=1")).json()
    assert r.problem_id not in [p["id"] for p in narrow["problems"]]


async def test_resolving_a_closed_problem_is_a_404(client, db_pool):
    r = await ingest_event(db_pool, _occ(_subject()), now=NOW)
    await set_status(db_pool, r.problem_id, "resolved", reason="x")
    await db_pool.execute(
        "UPDATE problems SET status = 'closed', closed_at = now() WHERE id = $1::uuid",
        r.problem_id,
    )
    assert (
        await client.post(f"/api/admin/problems/{r.problem_id}/resolve", json={})
    ).status_code == 404
    assert (
        await client.post(f"/api/admin/problems/{r.problem_id}/mute", json={"hours": 1})
    ).status_code == 404
