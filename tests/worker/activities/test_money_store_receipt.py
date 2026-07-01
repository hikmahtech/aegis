"""MoneyActivities.store_receipt_email — raw-email persistence."""

from __future__ import annotations

import pytest
from aegis_worker.activities.money import MoneyActivities
from temporalio.testing import ActivityEnvironment


def _make_act(db_pool):
    return MoneyActivities(
        db_pool=db_pool,
        llm=None,
        delivery=None,
        fx_rates={},
    )


@pytest.mark.asyncio
async def test_store_receipt_email_inserts_and_returns_id(db_pool):
    act = _make_act(db_pool)
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM maou.receipt_email WHERE message_id LIKE 'rt-%'")

    msg = {
        "id": "rt-1",
        "sender": "billing@stripe.com",
        "subject": "Your receipt",
        "internal_date_ms": 1700000000000,
        "thread_id": "th-1",
        "to": "me@x.com",
        "date": "Wed, 01 Jan 2025",
        "snippet": "paid $9.99",
    }
    env = ActivityEnvironment()
    rid = await env.run(act.store_receipt_email, msg, "sebas")

    assert rid, "expected a non-empty UUID string"

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT account, sender FROM maou.receipt_email WHERE message_id='rt-1'"
        )
    assert row is not None
    assert row["account"] == "sebas"
    assert row["sender"] == "billing@stripe.com"


@pytest.mark.asyncio
async def test_store_receipt_email_idempotent(db_pool):
    """Second insert on same message_id returns empty string (conflict)."""
    act = _make_act(db_pool)
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM maou.receipt_email WHERE message_id LIKE 'rt-%'")

    msg = {
        "id": "rt-2",
        "sender": "a@b.com",
        "subject": "S",
        "internal_date_ms": 1700000000000,
        "thread_id": "",
        "to": "",
        "date": "",
        "snippet": "",
    }
    env = ActivityEnvironment()
    first = await env.run(act.store_receipt_email, msg, "sebas")
    second = await env.run(act.store_receipt_email, msg, "sebas")

    assert first, "first insert should return a UUID"
    assert second == "", "duplicate insert should return empty string"


@pytest.mark.asyncio
async def test_store_receipt_email_no_pool():
    """Returns empty string gracefully when db_pool is None."""
    act = MoneyActivities(
        db_pool=None,
        llm=None,
        delivery=None,
        fx_rates={},
    )
    env = ActivityEnvironment()
    result = await env.run(act.store_receipt_email, {"id": "x"}, "sebas")
    assert result == ""
