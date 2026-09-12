"""The trading desk's monthly section and its check (spec §8, §9)."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
import pytest_asyncio
from aegis.services import trading_desk as td

SEP, OCT = date(2026, 9, 1), date(2026, 10, 1)
_WIPE = (
    "DELETE FROM finance.desk_orders",
    "DELETE FROM finance.desk_plans",
    "DELETE FROM finance.desk_decisions",
    "DELETE FROM finance.desk_prices",
    "DELETE FROM problem_events WHERE problem_id IN (SELECT id FROM problems WHERE subject_kind = 'trading_desk')",
    "DELETE FROM problems WHERE subject_kind = 'trading_desk'",
)


@pytest_asyncio.fixture(loop_scope="function")
async def pool(db_pool):
    for sql in _WIPE:
        await db_pool.execute(sql)
    yield db_pool
    for sql in _WIPE:
        await db_pool.execute(sql)


async def price(pool, symbol, day, close):
    await pool.execute(
        "INSERT INTO finance.desk_prices (symbol, date, close, source) VALUES ($1, $2, $3, 'yahoo')",
        symbol, day, close,
    )


async def fill(pool, day, side, qty, px, costs, seq=0):
    await pool.execute(
        "INSERT INTO finance.desk_plans (data_date, mode, outcome) VALUES ($1, 'paper', 'orders') ON CONFLICT DO NOTHING",
        day,
    )
    await pool.execute(
        "INSERT INTO finance.desk_orders (mode, data_date, seq, created_day, symbol, asset_class, side, qty, "
        "ref_price, status, fill_date, fill_price, costs, price_source) "
        "VALUES ('paper', $1, $2, $1, 'TCS', 'equity', $3, $4, $5, 'filled', $1, $5, $6, 'yahoo')",
        day, seq, side, qty, px, costs,
    )


async def september(pool):
    for day in (14, 18, 25, 30):
        await price(pool, "^NSEI", date(2026, 9, day), 25000.0 if day == 14 else 25250.0)
    await price(pool, "SHARIABEES.NS", date(2026, 9, 14), 400.0)
    await price(pool, "SHARIABEES.NS", date(2026, 9, 30), 404.0)
    await price(pool, "TCS.NS", date(2026, 9, 14), 1000.0)
    await price(pool, "TCS.NS", date(2026, 9, 30), 1100.0)
    await fill(pool, date(2026, 9, 14), "buy", 10, 1000.0, 20.0)


async def test_no_section_before_the_first_fill(pool):
    assert await td.month_summary(pool, SEP, OCT) is None


async def test_the_month_s_value_benchmarks_and_holdings(pool):
    await september(pool)
    s = await td.month_summary(pool, SEP, OCT)
    assert s["since"] == "2026-09-14"
    assert s["value"] == pytest.approx(100_980.0)
    assert s["after_tax"] == pytest.approx(100_980.0)
    assert s["benchmark_value"] == pytest.approx(100_000 * 0.998 * 404 / 400)
    assert s["context_value"] == pytest.approx(101_000.0)
    assert s["holdings"] == ["TCS"]
    assert s["cash_pct"] == pytest.approx(89_980 / 100_980)
    # The section reports both readings, so the difference is visible.
    assert s["invested_pct"] == pytest.approx(10_000 / 99_980)
    assert s["mean_gap_invested"] != s["mean_gap"]
    assert (s["filled"], s["costs"]) == (1, 20.0)
    assert s["label"] == "too early" and s["below_expectation"] is False


async def test_after_tax_takes_off_the_year_s_short_term_tax(pool):
    await september(pool)
    await fill(pool, date(2026, 9, 30), "sell", 10, 1100.0, 38.0)
    s = await td.month_summary(pool, SEP, OCT)
    assert s["value"] == pytest.approx(100_942.0)
    assert s["after_tax"] == pytest.approx(100_942.0 - 0.20 * 942.0)
    assert s["holdings"] == []


async def test_held_back_days_are_counted(pool):
    await september(pool)
    await pool.execute(
        "INSERT INTO finance.desk_plans (data_date, mode, outcome) VALUES ($1, 'paper', 'held_stale')",
        date(2026, 9, 21),
    )
    assert (await td.month_summary(pool, SEP, OCT))["held_back"] == {"held_stale": 1}


async def test_a_flattened_day_reaches_the_month_section(pool):
    """The month close has to be able to say the pipeline halted and the desk
    sold out, and why."""
    await september(pool)
    await pool.execute(
        "INSERT INTO finance.desk_plans (data_date, mode, outcome, note) VALUES ($1, 'paper', 'flattened', $2)",
        date(2026, 9, 21), "The risk manager halted trading: DAILY_LOSS fired on 2026-09-18.",
    )
    s = await td.month_summary(pool, SEP, OCT)
    assert s["halts"] == [
        {"day": "2026-09-21", "note": "The risk manager halted trading: DAILY_LOSS fired on 2026-09-18."}
    ]
    # A halt is not a day held back: the desk acted on it.
    assert s["held_back"] == {}


async def open_classes(pool):
    rows = await pool.fetch(
        "SELECT class FROM problems WHERE subject_kind = 'trading_desk' AND status NOT IN ('resolved', 'closed')"
    )
    return sorted(r["class"] for r in rows)


SUMMARY = {
    "weeks": 20, "benchmark": "SHARIABEES.NS", "mean_gap": -0.003, "t": -1.2,
    "mean_gap_invested": -0.001, "t_invested": -0.8, "invested_pct": 0.32, "expected_excess_pa": 0.06,
}


async def test_below_expectation_raises_then_clears(pool):
    await td.reconcile_expectation(pool, SUMMARY | {"below_expectation": True}, project=False)
    assert await open_classes(pool) == ["desk_below_expectation"]
    await td.reconcile_expectation(pool, SUMMARY | {"below_expectation": False}, project=False)
    assert await open_classes(pool) == []


async def test_the_warning_names_the_invested_figures_not_the_headline_gap(pool):
    """The alarm fires on the gap per rupee at risk, so that is what it must
    quote — quoting the headline gap would point the owner at the cash."""
    await td.reconcile_expectation(pool, SUMMARY | {"below_expectation": True}, project=False)
    title = await pool.fetchval(
        "SELECT payload->>'description' FROM problem_events WHERE problem_id IN "
        "(SELECT id FROM problems WHERE class = 'desk_below_expectation') LIMIT 1"
    )
    assert "-0.10%" in title and "32%" in title and "-0.8" in title
    assert "-0.30%" not in title


async def twelve_weeks(pool, weekly_book_return):
    """Twelve weeks of a third-invested desk whose one holding returns
    ``weekly_book_return`` a week, against a benchmark returning 1% a week."""
    start = date(2026, 6, 1)
    for i in range(13):
        day = start + timedelta(days=7 * i)
        await price(pool, "^NSEI", day, 25_000.0)
        await price(pool, "SHARIABEES.NS", day, 400.0 * 1.01**i)
        await price(pool, "TCS.NS", day, 1000.0 * (1 + weekly_book_return) ** i)
    await fill(pool, start, "buy", 33, 1000.0, 66.0)
    return await td.month_summary(pool, date(2026, 6, 1), date(2026, 9, 1))


async def test_a_desk_delivering_the_backtest_per_rupee_raises_no_alarm(pool):
    """A third invested, beating the benchmark by what the backtest promises on
    the money it actually put to work. Against the whole benchmark it is far
    behind on cash alone, and that is exactly what used to fire the alarm."""
    weekly_edge = 1.06 ** (1 / 52) - 1
    s = await twelve_weeks(pool, 1.01 * (1 + weekly_edge) - 1)

    assert s["weeks"] == 12
    assert 0.30 < s["invested_pct"] < 0.36  # a third, drifting up as the holding grows
    assert s["mean_gap"] < 0  # behind the whole benchmark, on exposure alone
    assert s["mean_gap_invested"] > weekly_edge
    assert s["below_expectation"] is False


async def test_a_desk_with_no_edge_per_rupee_still_raises_the_alarm(pool):
    """The other side of the same coin: a third invested and its picks only
    match the benchmark, so per rupee at risk it earns nothing over it. The
    alarm must still fire — taking cash drag out must not make it toothless."""
    s = await twelve_weeks(pool, 0.01)

    assert s["mean_gap_invested"] == pytest.approx(0.0, abs=1e-9)
    assert s["below_expectation"] is True


async def test_the_daily_run_never_resolves_the_monthly_problem(pool):
    await td.reconcile_expectation(pool, SUMMARY | {"below_expectation": True}, project=False)

    class YahooDown:
        async def daily_bars(self, symbol, start, end):
            raise RuntimeError("down")

    await td.run_tick(pool, ansaar=None, finance=YahooDown(), today=date(2026, 9, 14), project=False)
    assert "desk_below_expectation" in await open_classes(pool)
