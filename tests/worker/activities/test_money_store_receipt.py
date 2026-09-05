"""MoneyActivities.store_receipt_email — raw-email persistence."""

from __future__ import annotations

import pytest
import pytest_asyncio
from aegis_worker.activities.money import MoneyActivities
from temporalio.testing import ActivityEnvironment


@pytest_asyncio.fixture(autouse=True, loop_scope="function")
async def _drop_receipts_on_exit(test_db_url):
    """Teardown, not just setup-time cleanup — see the sibling note in
    test_money_bundle_e.py. `finance.receipt_email` is read agent-agnostically
    by ProfileReflectionActivities._evidence_finance, so a recent leftover
    row makes another file's "quiet week" assertion fail depending on how
    `--dist loadfile` happened to shard the suite.

    Takes its own connection off the session-scoped `test_db_url` rather than
    borrowing the `db_pool` fixture: db_pool is function-scoped and would be
    closed before this teardown runs, and depending on it would force the
    no-Postgres tests in this file to require a database.
    """
    yield
    if test_db_url is None:
        return
    import asyncpg

    conn = await asyncpg.connect(test_db_url)
    try:
        await conn.execute(
            "DELETE FROM finance.receipt_email "
            "WHERE message_id LIKE 'rt-%' OR message_id LIKE 'stuck-%'"
        )
    finally:
        await conn.close()


def _make_act(db_pool):
    return MoneyActivities(
        db_pool=db_pool,
        llm=None,
        delivery=None,
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
    """Second insert on the same message_id never writes a second row.

    v2 changed what it *returns* (the existing id, so a pre-books row gets
    re-processed) but not the insert contract: one row per Gmail message.
    """
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
    assert second == first, "a pre-books row comes back by id for re-processing"
    async with db_pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT count(*) FROM finance.receipt_email WHERE message_id = 'rt-2'"
        )
    assert count == 1, "ON CONFLICT DO NOTHING must not write a second row"


@pytest.mark.asyncio
async def test_store_receipt_email_returns_existing_id_for_v1_rows(db_pool):
    """v2: a row that never reached the books pipeline is handed back for
    re-processing; only a `parsed.version >= 2` row is a real duplicate."""
    act = _make_act(db_pool)
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM finance.receipt_email WHERE message_id = 'rt-v1'")
    msg = {"id": "rt-v1", "sender": "a@b", "subject": "s", "internal_date_ms": 1700000000000}
    first = await act.store_receipt_email(msg, "sebas")
    assert first
    assert await act.store_receipt_email(msg, "sebas") == first  # no version → re-process
    await db_pool.execute(
        "UPDATE finance.receipt_email SET parsed = parsed || '{\"version\": 2}' "
        "WHERE message_id = 'rt-v1'"
    )
    assert await act.store_receipt_email(msg, "sebas") == ""  # v2 → duplicate


@pytest.mark.asyncio
async def test_find_stuck_receipts_selects_rows_below_version_2(db_pool):
    act = _make_act(db_pool)
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM finance.receipt_email WHERE message_id LIKE 'stuck-v%'")
        v1 = await _insert_receipt_email(
            conn, message_id="stuck-v1", parsed={"is_receipt": True},
            received_days_ago=_ANCIENT_DAYS,
        )
        # Compare on the returned row id, not the message_id: `ids` holds uuids,
        # so `"stuck-v2" not in ids` would pass however the predicate behaved.
        v2 = await _insert_receipt_email(
            conn, message_id="stuck-v2", parsed={"version": 2}, received_days_ago=_ANCIENT_DAYS
        )
    ids = await act.find_stuck_receipts(limit=50, older_than_days=1)
    assert v1 in ids and v2 not in ids


@pytest.mark.asyncio
async def test_store_receipt_email_no_pool():
    """Returns empty string gracefully when db_pool is None."""
    act = MoneyActivities(
        db_pool=None,
        llm=None,
        delivery=None,
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
async def test_find_stuck_receipts_selects_every_pre_v2_row(db_pool):
    """v2 widened the predicate from "lacks `is_receipt`" to "`parsed.version`
    below 2". Fix #113's rows (parsed NULL, or present but never classified)
    are still stuck. So now is a v1-classified row — `is_receipt` says the old
    extractor ran, not that the books pipeline ever saw it. Only a row stamped
    `version: 2` by store_money_result is done."""
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
        booked = await _insert_receipt_email(
            conn,
            message_id="stuck-sel-v2",
            parsed={"is_receipt": True, "version": 2},
            received_days_ago=_ANCIENT_DAYS,
        )

    env = ActivityEnvironment()
    ids = await env.run(act.find_stuck_receipts, 20, 1)

    assert stuck_null in ids
    assert stuck_no_key in ids
    # v1 classification is no longer a terminal state — these are stuck now.
    assert classified_true in ids
    assert classified_false in ids
    # WHERE-clause exclusion — safe regardless of ordering/limit/clutter.
    assert booked not in ids


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


@pytest.mark.asyncio
async def test_store_receipt_body_merges_into_parsed(db_pool):
    act = _make_act(db_pool)
    async with db_pool.acquire() as conn:
        rid = await _insert_receipt_email(
            conn, message_id="rt-body-1", parsed={"snippet": "snip"}, received_days_ago=0.1
        )
    await act.store_receipt_body(rid, "full body text")
    async with db_pool.acquire() as conn:
        parsed = await conn.fetchval(
            "SELECT parsed FROM finance.receipt_email WHERE id = $1::uuid", rid
        )
    assert parsed == {"snippet": "snip", "body_text": "full body text"}


@pytest.mark.asyncio
async def test_load_receipts_prefers_body_text_over_snippet(db_pool):
    act = _make_act(db_pool)
    async with db_pool.acquire() as conn:
        with_body = await _insert_receipt_email(
            conn,
            message_id="rt-body-2",
            parsed={"snippet": "snip", "body_text": "full"},
            received_days_ago=0.1,
        )
        without = await _insert_receipt_email(
            conn, message_id="rt-body-3", parsed={"snippet": "snip only"}, received_days_ago=0.1
        )
    rows = await act.load_receipts([with_body, without])
    by_id = {r["message_id"]: r["body_plain"] for r in rows}
    assert by_id == {"rt-body-2": "full", "rt-body-3": "snip only"}

