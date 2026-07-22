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
        await conn.execute("DELETE FROM finance.receipt_email WHERE message_id LIKE 'rt-%'")

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
            "SELECT account, sender FROM finance.receipt_email WHERE message_id='rt-1'"
        )
    assert row is not None
    assert row["account"] == "sebas"
    assert row["sender"] == "billing@stripe.com"


@pytest.mark.asyncio
async def test_store_receipt_email_idempotent(db_pool):
    """Second insert on same message_id returns empty string (conflict)."""
    act = _make_act(db_pool)
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM finance.receipt_email WHERE message_id LIKE 'rt-%'")

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


async def _insert_receipt_email(
    conn, *, message_id: str, parsed, received_days_ago: float
) -> str:
    row = await conn.fetchrow(
        "INSERT INTO finance.receipt_email "
        "(message_id, account, sender, subject, received_at, parsed) "
        "VALUES ($1, 'sebas', 'a@b.com', 's', "
        "        NOW() - ($2 * INTERVAL '1 day'), $3) "
        "RETURNING id",
        message_id,
        received_days_ago,
        parsed,
    )
    return str(row["id"])



# find_stuck_receipts scans the WHOLE finance.receipt_email table (no
# per-test scoping — that's the real production query). This is a shared,
# persistent Postgres instance across test runs and parallel agents, so
# assertions below only rely on properties that hold regardless of
# whatever else is in the table:
#   - exclusion checks (WHERE-clause level) are safe at any LIMIT/order.
#   - inclusion checks use an implausibly old received_at (tens of
#     thousands of days back) so our rows sort first and can't be pushed
#     out of the result by a small LIMIT full of unrelated clutter.
_ANCIENT_DAYS = 99999


@pytest.mark.asyncio
async def test_find_stuck_receipts_selects_missing_is_receipt_key(db_pool):
    """Fix #113: a row whose `parsed` lacks `is_receipt` (the
    parse_failed/extract_failed short-circuit) is stuck and eligible;
    a row that was actually classified (is_receipt present, either value)
    is NOT — it already has a real result, refired or not."""
    act = _make_act(db_pool)
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM finance.receipt_email WHERE message_id LIKE 'stuck-sel-%'"
        )
        stuck_null = await _insert_receipt_email(
            conn, message_id="stuck-sel-null", parsed=None, received_days_ago=_ANCIENT_DAYS
        )
        stuck_no_key = await _insert_receipt_email(
            conn,
            message_id="stuck-sel-nokey",
            parsed={"snippet": "hi"},
            received_days_ago=_ANCIENT_DAYS,
        )
        classified_true = await _insert_receipt_email(
            conn,
            message_id="stuck-sel-classified-true",
            parsed={"is_receipt": True},
            received_days_ago=_ANCIENT_DAYS,
        )
        classified_false = await _insert_receipt_email(
            conn,
            message_id="stuck-sel-classified-false",
            parsed={"is_receipt": False},
            received_days_ago=_ANCIENT_DAYS,
        )

    env = ActivityEnvironment()
    ids = await env.run(act.find_stuck_receipts, 20, 1)

    assert stuck_null in ids
    assert stuck_no_key in ids
    # WHERE-clause exclusion — safe regardless of ordering/limit/clutter.
    assert classified_true not in ids
    assert classified_false not in ids


@pytest.mark.asyncio
async def test_find_stuck_receipts_excludes_recent(db_pool):
    """A row younger than `older_than_days` is excluded — it may still be
    mid-flight in its original MoneyProcessFlow run."""
    act = _make_act(db_pool)
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM finance.receipt_email WHERE message_id LIKE 'stuck-age-%'"
        )
        too_new = await _insert_receipt_email(
            conn, message_id="stuck-age-new", parsed=None, received_days_ago=0.1
        )

    env = ActivityEnvironment()
    ids = await env.run(act.find_stuck_receipts, 20, 1)

    # WHERE-clause exclusion — safe regardless of ordering/limit/clutter.
    assert too_new not in ids


@pytest.mark.asyncio
async def test_find_stuck_receipts_oldest_first(db_pool):
    """Result is ordered oldest-received-first, so the backlog drains in
    order (the longest-stuck row gets retried first)."""
    act = _make_act(db_pool)
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM finance.receipt_email WHERE message_id LIKE 'stuck-ord-%'"
        )
        older = await _insert_receipt_email(
            conn,
            message_id="stuck-ord-older",
            parsed=None,
            received_days_ago=_ANCIENT_DAYS + 1,
        )
        newer = await _insert_receipt_email(
            conn,
            message_id="stuck-ord-newer",
            parsed=None,
            received_days_ago=_ANCIENT_DAYS,
        )

    env = ActivityEnvironment()
    ids = await env.run(act.find_stuck_receipts, 20, 1)

    assert older in ids
    assert newer in ids
    assert ids.index(older) < ids.index(newer)
