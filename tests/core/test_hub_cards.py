"""Retiring a problem's stale decision cards (#629). Real test database.

A card belongs to a problem through the problem's own timeline: the run that
posted it recorded `<run id>:gate2` on the problem first, and the card's
workflow id ends with `-<run id>`. Retiring moves the `interactions` row out
of `pending`, which is what makes the resolve endpoint refuse a later click.
"""

from __future__ import annotations

import base64
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest_asyncio
from aegis.api.app import create_app
from aegis.api.deps import get_settings
from aegis.api.routes.interactions import get_workflow_client
from aegis.config import Settings
from aegis.services import hub_cards
from aegis.services.hub import Event, get_problem, ingest_event, set_status
from httpx import ASGITransport, AsyncClient

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
AGENT = "pandoras-actor"
_CARD_PREFIX = "gate2-aegis-heartbeat-DockerServiceDown-x-"


@pytest_asyncio.fixture(autouse=True, loop_scope="function")
async def _clean_cards(db_pool):
    """Leave no pending card behind for a later file's pending counts."""
    yield
    await db_pool.execute("DELETE FROM interactions WHERE flow_run_id LIKE $1", _CARD_PREFIX + "%")


def _svc() -> str:
    return f"svc_{uuid.uuid4().hex[:8]}"


async def _problem(pool, subject: str) -> str:
    r = await ingest_event(
        pool,
        Event(
            source="heartbeat",
            external_id=f"{subject}@1",
            kind="occurrence",
            title=f"Service {subject} down",
            klass="DockerServiceDown",
            subject=subject,
            severity="critical",
            occurred_at=NOW,
        ),
        now=NOW,
    )
    return r.problem_id


async def _card(pool, problem_id: str, *, run: str | None = None, origin: str = hub_cards.GATE_ORIGIN,
                status: str = "pending", record: bool = True) -> tuple[str, str]:
    """A Gate-2 card posted by investigation run `run` for `problem_id`, as the
    flow leaves it: the `gate2` event on the problem, then the card."""
    run = run or f"investigate-{problem_id}-{uuid.uuid4().hex[:4]}"
    if record:
        await ingest_event(
            pool,
            Event(
                source="investigation",
                external_id=f"{run}:gate2",
                kind="investigation",
                title="Decision card posted: actionable.",
                problem_id=problem_id,
                occurred_at=NOW,
            ),
            now=NOW,
        )
    iid = await pool.fetchval(
        "INSERT INTO interactions (flow_run_id, agent_id, kind, origin, prompt, options, status, "
        "timeout_policy, delivery_ref) VALUES ($1, $2, 'choice', $3, $4, $5, $6, 'archive', $7) "
        "RETURNING id::text",
        f"{_CARD_PREFIX}{run}",
        AGENT,
        origin,
        "<b>Investigation — actionable</b>\nRestart it.",
        {"run_fix": "🔧 Run fix", "ack": "✅ Acknowledge"},
        status,
        {"adapter": "slack", "channel": "C1", "ts": "1.0"},
    )
    return iid, run


async def _row(pool, iid: str) -> dict:
    return dict(
        await pool.fetchrow(
            "SELECT status, response, metadata FROM interactions WHERE id = $1::uuid", iid
        )
    )


async def test_a_resolve_retires_the_problems_pending_cards(db_pool):
    s = _svc()
    pid = await _problem(db_pool, s)
    first, _ = await _card(db_pool, pid)
    second, _ = await _card(db_pool, pid)

    r = await ingest_event(
        db_pool,
        Event(
            source="heartbeat",
            external_id=f"{s}@1@resolved",
            kind="resolved",
            title="recovered",
            klass="DockerServiceDown",
            subject=s,
            occurred_at=NOW + timedelta(minutes=5),
        ),
        now=NOW + timedelta(minutes=5),
    )
    assert r.action == "resolved"
    for iid in (first, second):
        row = await _row(db_pool, iid)
        assert row["status"] == hub_cards.RETIRED
        # The value the waiting flow already acts on.
        assert row["response"]["value"] == "self_resolved"
        assert row["metadata"]["retired"]["reason"] == hub_cards.RESOLVED
        assert row["metadata"]["retired"]["problem_id"] == pid


async def test_every_other_way_of_resolving_retires_them_too(db_pool):
    """`set_status` is the Problems page, a fix that held, a completed task and
    an investigation's own verdict."""
    pid = await _problem(db_pool, _svc())
    iid, _ = await _card(db_pool, pid)
    assert await set_status(db_pool, pid, "resolved", reason="resolved by hand", source="admin")
    assert (await _row(db_pool, iid))["status"] == hub_cards.RETIRED


async def test_a_live_status_move_retires_nothing(db_pool):
    pid = await _problem(db_pool, _svc())
    iid, _ = await _card(db_pool, pid)
    await set_status(db_pool, pid, "waiting_human", reason="card posted")
    assert (await _row(db_pool, iid))["status"] == "pending"


async def test_superseding_retires_older_cards_and_nothing_else(db_pool):
    pid = await _problem(db_pool, _svc())
    other = await _problem(db_pool, _svc())
    older, _ = await _card(db_pool, pid)
    newer, newer_run = await _card(db_pool, pid)
    others, _ = await _card(db_pool, other)
    repo_card, _ = await _card(db_pool, pid, origin="alert_confirm_repo")
    answered, _ = await _card(db_pool, pid, status="resolved")

    retired = await hub_cards.retire(
        db_pool, pid, reason=hub_cards.SUPERSEDED, exclude_run=newer_run, now=NOW
    )

    assert [r["id"] for r in retired] == [older]
    row = await _row(db_pool, older)
    assert row["status"] == hub_cards.RETIRED and row["response"]["value"] == "superseded"
    # The run posting the new card, another problem's card, a card that is not
    # a Gate-2 card, and a card a person already answered are all left alone.
    assert (await _row(db_pool, newer))["status"] == "pending"
    assert (await _row(db_pool, others))["status"] == "pending"
    assert (await _row(db_pool, repo_card))["status"] == "pending"
    assert (await _row(db_pool, answered))["status"] == "resolved"
    # With no run excluded the newer card goes too; after that nothing is left
    # pending, so a second call moves nothing.
    assert await hub_cards.retire(db_pool, pid, reason=hub_cards.SUPERSEDED, now=NOW) != []
    assert await hub_cards.retire(db_pool, pid, reason=hub_cards.SUPERSEDED, now=NOW) == []


async def test_a_retired_card_refuses_a_later_click(db_pool):
    """The whole point: the resolve endpoint's pending-only gate refuses it, so
    no signal reaches the flow and nothing runs."""
    pid = await _problem(db_pool, _svc())
    iid, _ = await _card(db_pool, pid)
    await hub_cards.retire(db_pool, pid, reason=hub_cards.SUPERSEDED, now=NOW)

    app = create_app(run_lifespan=False)
    app.state.db_pool = db_pool
    handle = AsyncMock()
    client = AsyncMock()
    client.get_workflow_handle = lambda wid: handle
    settings = Settings(
        database_url="postgresql://test:test@localhost:5432/test",
        litellm_url="https://litellm.example.com/v1",
        temporal_ui_url="https://temporal.example.com",
        n8n_ui_url="https://n8n.example.com",
        admin_username="admin",
        admin_password="admin",
        n8n_webhook_secret="test-secret",
    )
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_workflow_client] = lambda: client
    auth = {"Authorization": "Basic " + base64.b64encode(b"admin:admin").decode()}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        resp = await http.post(
            f"/api/interactions/{iid}/resolve", json={"response": {"value": "run_fix"}}, headers=auth
        )
    assert resp.status_code == 200
    assert resp.json()["status"] == hub_cards.RETIRED
    assert resp.json()["already_resolved"] is True
    handle.signal.assert_not_awaited()
    assert (await _row(db_pool, iid))["response"]["value"] == "superseded"


async def test_a_resolve_survives_a_retirement_that_fails(db_pool, monkeypatch):
    """The card is secondary: a retirement that raises inside the resolving
    transaction costs the retirement, never the resolve."""

    async def broken(conn, *a, **kw):
        await conn.execute("SELECT * FROM no_such_table_zz")

    monkeypatch.setattr(hub_cards, "retire", broken)
    pid = await _problem(db_pool, _svc())
    iid, _ = await _card(db_pool, pid)
    assert await set_status(db_pool, pid, "resolved", reason="by hand", source="admin")
    assert (await get_problem(db_pool, pid))["status"] == "resolved"
    assert (await _row(db_pool, iid))["status"] == "pending"


async def test_finish_is_done_when_both_happened_or_after_three_tries(db_pool):
    pid = await _problem(db_pool, _svc())
    a, _ = await _card(db_pool, pid)
    b, _ = await _card(db_pool, pid)
    await hub_cards.retire(db_pool, pid, reason=hub_cards.RESOLVED, now=NOW)
    waiting = {r["id"] for r in await hub_cards.unfinished(db_pool, problem_id=pid)}
    assert waiting == {a, b}

    assert await hub_cards.finish(db_pool, a, edited=True, signalled=True) is True
    for attempt in range(1, hub_cards.MAX_TRIES + 1):
        done = await hub_cards.finish(db_pool, b, edited=False, signalled=True)
        assert done is (attempt == hub_cards.MAX_TRIES)
    assert await hub_cards.unfinished(db_pool, problem_id=pid) == []


def test_the_edited_card_says_why_and_keeps_what_it_offered():
    text = hub_cards.edit_text(hub_cards.SUPERSEDED, "<b>Fix proposed</b>\nrestart it")
    assert text.startswith("⏭ <b>Replaced by a newer card.</b>")
    assert text.endswith("<b>Fix proposed</b>\nrestart it")
    assert hub_cards.edit_text(hub_cards.RESOLVED, "x").startswith("✅ <b>Resolved on its own.</b>")
    assert hub_cards.answer(hub_cards.RESOLVED)["value"] == "self_resolved"
    assert hub_cards.answer(hub_cards.SUPERSEDED)["value"] == "superseded"
