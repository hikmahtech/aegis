"""Migration 050 carries a live desk to open fills and a market-time schedule.

The risk this file covers is not the DDL — it is the two UPDATEs, which touch a
production row holding real paper positions. Each must be idempotent, and
neither may overwrite a choice an operator has already made.
"""

from __future__ import annotations

from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio
from aegis.services import trading_desk as td

MIGRATION = Path(__file__).resolve().parents[3] / "migrations" / "050_desk_open_fill.sql"

# The `trading-desk-daily` row as it stood in production on 2026-09-15: filling
# at the close because there was no other option, on a UTC cron.
LIVE_ROW = {
    "mode": "paper",
    "capital": 100000,
    "calendar_symbol": "^NSEI",
    "market_tz": "Asia/Kolkata",
    "max_order_pct": 0.25,
}
LIVE_CRON = "30 2 * * 1-5"


@pytest_asyncio.fixture(loop_scope="function")
async def pool(db_pool):
    original = await db_pool.fetchrow(
        "SELECT config, schedule_cron FROM activities WHERE slug = $1", td.DESK_SLUG
    )
    yield db_pool
    if original is not None:
        await db_pool.execute(
            "UPDATE activities SET config = $2, schedule_cron = $3 WHERE slug = $1",
            td.DESK_SLUG, original["config"], original["schedule_cron"],
        )


async def set_row(pool, config, cron):
    # The pool's jsonb codec dumps this itself; a pre-dumped string would be
    # stored as a jsonb string scalar rather than an object.
    await pool.execute(
        "UPDATE activities SET config = $2, schedule_cron = $3 WHERE slug = $1",
        td.DESK_SLUG, config, cron,
    )


async def read_row(pool):
    return await pool.fetchrow(
        "SELECT config, schedule_cron FROM activities WHERE slug = $1", td.DESK_SLUG
    )


async def test_it_switches_a_live_desk_to_the_open_and_to_market_time(pool):
    await set_row(pool, LIVE_ROW, LIVE_CRON)

    await pool.execute(MIGRATION.read_text())

    row = await read_row(pool)
    assert row["config"]["fill_at"] == "open"
    assert row["schedule_cron"] == "CRON_TZ=Asia/Kolkata 0 8,11,14 * * 1-5"
    # And the settings it had no business touching are untouched.
    assert row["config"]["capital"] == 100000
    assert row["config"]["max_order_pct"] == 0.25


async def test_it_is_safe_to_run_twice(pool):
    await set_row(pool, LIVE_ROW, LIVE_CRON)
    await pool.execute(MIGRATION.read_text())
    once = await read_row(pool)
    await pool.execute(MIGRATION.read_text())
    assert await read_row(pool) == once


async def test_it_leaves_an_operators_own_answer_alone(pool):
    """Someone who has already said they want the close, on their own cron,
    keeps both. A migration that overrode a deliberate choice would be worse
    than one that did nothing."""
    await set_row(pool, LIVE_ROW | {"fill_at": "close"}, "15 3 * * 1-5")

    await pool.execute(MIGRATION.read_text())

    row = await read_row(pool)
    assert row["config"]["fill_at"] == "close"
    assert row["schedule_cron"] == "15 3 * * 1-5"


async def test_the_columns_exist_and_take_the_values_the_desk_writes(pool):
    await pool.execute(MIGRATION.read_text())
    await pool.execute(
        "INSERT INTO finance.desk_prices (symbol, date, open, close, source) "
        "VALUES ('T.NS', '2026-09-15', 10.5, NULL, 'yahoo') "
        "ON CONFLICT (symbol, date) DO UPDATE SET open = EXCLUDED.open"
    )
    got = await pool.fetchrow(
        "SELECT open, close FROM finance.desk_prices WHERE symbol = 'T.NS' AND date = '2026-09-15'"
    )
    assert (float(got["open"]), got["close"]) == (10.5, None)
    await pool.execute("DELETE FROM finance.desk_prices WHERE symbol = 'T.NS'")


async def test_price_kind_refuses_anything_but_the_two_prints(pool):
    """A constraint rather than a convention: this column is what tells an
    operator whether the desk is really filling at the open or quietly falling
    back to the close, and a third value would make that unreadable."""
    await pool.execute(MIGRATION.read_text())
    await pool.execute(
        "INSERT INTO finance.desk_plans (data_date, mode, outcome) VALUES ('2026-09-15', 'paper', 'orders') "
        "ON CONFLICT DO NOTHING"
    )
    insert = (
        "INSERT INTO finance.desk_orders (mode, data_date, seq, created_day, symbol, asset_class, "
        "side, qty, ref_price, price_kind) "
        "VALUES ('paper', '2026-09-15', 99, '2026-09-15', 'T', 'equity', 'buy', 1, 1.0, $1)"
    )
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await pool.execute(insert, "vwap")
    await pool.execute(insert, "open")  # and the real ones are accepted
    await pool.execute("DELETE FROM finance.desk_orders WHERE symbol = 'T'")
    await pool.execute("DELETE FROM finance.desk_plans WHERE data_date = '2026-09-15'")
