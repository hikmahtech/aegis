"""The trading desk's daily run against a real Postgres (spec §3, §5, §10)."""

from __future__ import annotations

import inspect
from datetime import date

import httpx
import pytest
import pytest_asyncio
from aegis.connectors.ansaar import AnsaarClient, AnsaarError
from aegis.connectors.finance import FinanceConnector
from aegis.services import trading_desk as td

THU, FRI, MON, TUE, WED = (date(2026, 9, d) for d in (10, 11, 14, 15, 16))

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
    original = await db_pool.fetchrow("SELECT config FROM activities WHERE slug = $1", td.DESK_SLUG)
    yield db_pool
    for sql in _WIPE:
        await db_pool.execute(sql)
    if original is None:
        await db_pool.execute("DELETE FROM activities WHERE slug = $1", td.DESK_SLUG)
    else:
        await db_pool.execute("UPDATE activities SET config = $2 WHERE slug = $1", td.DESK_SLUG, original["config"])


def bar(day, close, split=None, div=None):
    return {"day": day, "close": close, "split_ratio": split, "dividend": div}


def row(symbol, weight, day=FRI, cls="equity", rank=1, halal="COMPLIANT", state="NORMAL", kill=""):
    """A decision as ansaar-data #30 serves it."""
    return {
        "data_date": day.isoformat(), "symbol": symbol, "asset_class": cls, "halal_status": halal,
        "direction": "LONG", "combined_forecast": 0.01, "confidence": 0.7, "target_weight": weight,
        "selection_rank": rank, "selection_score": 1.0, "drawdown_scalar": 1.0,
        "vol_scalar_portfolio": 1.0, "active_kill_conditions": kill, "recovery_state": state,
        "regime_label": "BULL_TREND", "ml_model_version": "v170", "was_held_previous": 0,
        "updated_at": f"{day.isoformat()} 17:30:11.358",
    }


class FakeFinance:
    """FinanceConnector.daily_bars with canned bars; the same parameters as the real one."""

    def __init__(self, bars=None, fail=False):
        self.bars = bars or {}
        self.fail = fail

    async def daily_bars(self, symbol, start, end):
        if self.fail:
            raise httpx.ConnectError("yahoo is down")
        return [b for b in self.bars.get(symbol, []) if start <= b["day"] <= end]


class FakeAnsaar:
    """AnsaarClient.decisions and .prices with canned data; the same parameters as the real ones."""

    def __init__(self, days=None, fail=False, prices=None):
        self.days = days or {}
        self.fail = fail
        self.price_rows = prices or {}
        self.price_calls = []

    async def decisions(self, day):
        if self.fail:
            raise AnsaarError("/api/execution/trade-decisions: ConnectError")
        return list(self.days.get(day, [])), {"date": day.isoformat()}

    async def prices(self, symbol, asset_class, start, end):
        self.price_calls.append((symbol, start, end))
        return [b for b in self.price_rows.get(symbol, []) if start <= b["day"] <= end]


def test_the_fakes_take_the_real_parameters():
    for fake, real, name in (
        (FakeFinance, FinanceConnector, "daily_bars"),
        (FakeAnsaar, AnsaarClient, "decisions"),
        (FakeAnsaar, AnsaarClient, "prices"),
    ):
        assert list(inspect.signature(getattr(fake, name)).parameters) == list(
            inspect.signature(getattr(real, name)).parameters
        )


INDEX_BARS = [bar(THU, 25000.0), bar(FRI, 25100.0), bar(MON, 25200.0), bar(TUE, 25300.0)]


def market(extra=None):
    bars = {"^NSEI": INDEX_BARS, "SHARIABEES.NS": [bar(FRI, 400.0), bar(MON, 402.0)]}
    bars.update(extra or {})
    return FakeFinance(bars)


async def run(pool, ansaar, finance, today):
    return await td.run_tick(pool, ansaar=ansaar, finance=finance, today=today, project=False)


async def open_problems(pool):
    rows = await pool.fetch(
        "SELECT class, subject FROM problems WHERE subject_kind = 'trading_desk' "
        "AND status NOT IN ('resolved', 'closed')"
    )
    return sorted((r["class"], r["subject"]) for r in rows)


async def filled(pool, day, symbol, side, qty, price, costs=18.0, cls="equity", seq=0):
    """An order that filled on ``day``, as if an earlier run had placed it."""
    await pool.execute(
        "INSERT INTO finance.desk_plans (data_date, mode, outcome) VALUES ($1, 'paper', 'orders') "
        "ON CONFLICT DO NOTHING",
        day,
    )
    await pool.execute(
        "INSERT INTO finance.desk_orders (mode, data_date, seq, created_day, symbol, asset_class, side, "
        "qty, ref_price, status, fill_date, fill_price, costs, price_source) "
        "VALUES ('paper', $1, $2, $1, $3, $4, $5, $6, $7, 'filled', $1, $7, $8, 'yahoo')",
        day, seq, symbol, cls, side, qty, price, costs,
    )


async def test_day_one_plans_and_day_two_fills(pool):
    finance = market({
        "TCS.NS": [bar(FRI, 3000.0), bar(MON, 3100.0)],
        "GOLDBEES.NS": [bar(FRI, 100.0), bar(MON, 101.0)],
    })
    ansaar = FakeAnsaar({
        FRI: [row("TCS", 0.10), row("GOLDBEES", 0.10, cls="etf", rank=2)],
        MON: [row("TCS", 0.10, day=MON), row("GOLDBEES", 0.10, day=MON, cls="etf", rank=2)],
    })

    out = await run(pool, ansaar, finance, MON)
    assert out["day"] == "2026-09-11" and out["planned"] == "orders" and out["findings"] == []
    orders = await pool.fetch("SELECT symbol, side, qty, status, created_day FROM finance.desk_orders ORDER BY seq")
    assert [(o["symbol"], o["side"], o["qty"], o["status"]) for o in orders] == [
        ("TCS", "buy", 3, "pending"),
        ("GOLDBEES", "buy", 100, "pending"),
    ]
    assert {o["created_day"] for o in orders} == {MON}

    out = await run(pool, ansaar, finance, TUE)
    assert out["filled"] == 2 and out["planned"] == "no_change"
    filled = await pool.fetch(
        "SELECT symbol, fill_date, fill_price, costs, price_source FROM finance.desk_orders ORDER BY seq"
    )
    assert [(f["symbol"], f["fill_date"], float(f["fill_price"]), float(f["costs"]), f["price_source"]) for f in filled] == [
        ("TCS", MON, 3100.0, pytest.approx(18.6), "yahoo"),
        ("GOLDBEES", MON, 101.0, pytest.approx(20.2), "yahoo"),
    ]


async def test_a_second_run_the_same_day_changes_nothing(pool):
    finance = market({"TCS.NS": [bar(FRI, 3000.0)]})
    ansaar = FakeAnsaar({FRI: [row("TCS", 0.10)]})
    await run(pool, ansaar, finance, MON)
    await run(pool, ansaar, finance, MON)
    assert await pool.fetchval("SELECT count(*) FROM finance.desk_orders") == 1
    assert await pool.fetchval("SELECT count(*) FROM finance.desk_plans") == 1


async def test_a_stale_day_holds_raises_one_problem_and_the_next_good_day_clears_it(pool):
    finance = market({"TCS.NS": [bar(FRI, 3000.0), bar(MON, 3000.0)]})
    ansaar = FakeAnsaar({FRI: [], MON: [row("TCS", 0.10, day=MON)]})
    out = await run(pool, ansaar, finance, MON)
    assert out["planned"] == "held_stale"
    assert await open_problems(pool) == [("desk_decisions_stale", "decisions")]
    await run(pool, ansaar, finance, MON)
    assert await pool.fetchval("SELECT count(*) FROM problems WHERE subject_kind = 'trading_desk'") == 1
    assert await open_problems(pool) == [("desk_decisions_stale", "decisions")]
    out = await run(pool, ansaar, finance, TUE)
    assert out["planned"] == "orders"
    assert await open_problems(pool) == []


async def test_ansaar_down_holds_and_says_so_without_a_stale_problem(pool):
    out = await run(pool, FakeAnsaar(fail=True), market(), MON)
    assert out["planned"] == "held_stale"
    assert await open_problems(pool) == [("desk_source_error", "ansaar")]


async def test_a_rerun_after_ansaar_recovers_keeps_the_day_s_problem(pool):
    await run(pool, FakeAnsaar(fail=True), market(), MON)
    await run(pool, FakeAnsaar({FRI: [row("TCS", 0.10)]}), market({"TCS.NS": [bar(FRI, 3000.0)]}), MON)
    assert await open_problems(pool) == [("desk_source_error", "ansaar")]
    assert await pool.fetchval("SELECT count(*) FROM finance.desk_orders") == 0


async def test_yahoo_down_writes_no_plan_and_raises_a_source_error(pool):
    out = await run(pool, FakeAnsaar(), FakeFinance(fail=True), MON)
    assert out["skipped"] == "yahoo"
    assert await pool.fetchval("SELECT count(*) FROM finance.desk_plans") == 0
    assert await open_problems(pool) == [("desk_source_error", "yahoo")]


async def test_a_failed_morning_resolves_nothing_it_did_not_check(pool):
    """A run that stops early checked only itself, so it must leave every other
    problem alone. Resolving one would complete its task and raise it again the
    next good morning (spec §3)."""
    finance = market({"TCS.NS": [bar(FRI, 3000.0), bar(MON, 3000.0)]})
    await run(pool, FakeAnsaar({FRI: []}), finance, MON)
    assert await open_problems(pool) == [("desk_decisions_stale", "decisions")]

    out = await run(pool, FakeAnsaar({FRI: []}), FakeFinance(fail=True), MON)
    assert out["skipped"] == "yahoo"
    assert await open_problems(pool) == [
        ("desk_decisions_stale", "decisions"),
        ("desk_source_error", "yahoo"),
    ]


async def test_a_vanished_holding_class_holds_the_portfolio(pool):
    await filled(pool, THU, "TCS", "buy", 3, 3000.0)
    finance = market({"TCS.NS": [bar(THU, 3000.0), bar(FRI, 3000.0)], "GOLDBEES.NS": [bar(FRI, 100.0)]})
    out = await run(pool, FakeAnsaar({FRI: [row("GOLDBEES", 0.10, cls="etf")]}), finance, MON)
    assert out["planned"] == "held_suspect"
    assert await pool.fetchval("SELECT count(*) FROM finance.desk_orders WHERE status = 'pending'") == 0
    assert await open_problems(pool) == [("desk_decisions_suspect", "decisions")]


async def test_a_non_compliant_row_is_dropped_the_rest_trades_and_it_is_reported(pool):
    finance = market({"TCS.NS": [bar(FRI, 3000.0)], "XYZ.NS": [bar(FRI, 50.0)]})
    ansaar = FakeAnsaar({FRI: [row("TCS", 0.10), row("XYZ", 0.10, rank=2, halal="NON_COMPLIANT")]})
    out = await run(pool, ansaar, finance, MON)
    assert out["planned"] == "orders"
    assert [r["symbol"] for r in await pool.fetch("SELECT symbol FROM finance.desk_orders")] == ["TCS"]
    assert await open_problems(pool) == [("desk_decisions_suspect", "decisions")]


async def test_the_copy_keeps_what_was_served_first(pool):
    finance = market({"TCS.NS": [bar(FRI, 3000.0)]})
    await run(pool, FakeAnsaar({FRI: [row("TCS", 0.10)]}), finance, MON)
    await run(pool, FakeAnsaar({FRI: [row("TCS", 0.20)]}), finance, MON)
    weight = await pool.fetchval("SELECT target_weight FROM finance.desk_decisions WHERE data_date = $1", FRI)
    assert float(weight) == 0.10


async def test_a_stored_close_is_never_rewritten(pool):
    finance = market({"TCS.NS": [bar(FRI, 3000.0)]})
    ansaar = FakeAnsaar({FRI: [row("TCS", 0.10)]})
    await run(pool, ansaar, finance, MON)
    finance.bars["TCS.NS"] = [bar(FRI, 1500.0), bar(MON, 1510.0, split=2.0)]
    await run(pool, ansaar, finance, TUE)
    rows = await pool.fetch("SELECT date, close, split_ratio FROM finance.desk_prices WHERE symbol = 'TCS.NS' ORDER BY date")
    assert [(r["date"], float(r["close"]), r["split_ratio"] and float(r["split_ratio"])) for r in rows] == [
        (FRI, 3000.0, None),
        (MON, 1510.0, 2.0),
    ]


async def test_store_bars_never_keeps_today(pool):
    await td._store_bars(pool, "TCS.NS", [bar(FRI, 3000.0), bar(MON, 3010.0)], "yahoo", MON)
    assert await pool.fetchval("SELECT count(*) FROM finance.desk_prices WHERE symbol = 'TCS.NS'") == 1


async def test_two_desk_names_for_one_yahoo_symbol_both_get_the_bars(pool):
    """The desk holds this ETF under its NSE name and names the benchmark in
    Yahoo's form. Both ask for SHARIABEES.NS, so both must come back with its bars."""
    await td._store_bars(pool, "SHARIABEES.NS", [bar(FRI, 400.0), bar(MON, 402.0)], "yahoo", TUE)
    bars = await td._bars(pool, {"SHARIABEES", "SHARIABEES.NS"})
    assert [b.day for b in bars["SHARIABEES"]] == [FRI, MON]
    assert bars["SHARIABEES"] == bars["SHARIABEES.NS"]


async def test_a_symbol_yahoo_lacks_is_priced_from_ansaar_and_marked(pool):
    ansaar = FakeAnsaar(
        {FRI: [row("GOLDBEES", 0.10, cls="etf")], MON: [row("GOLDBEES", 0.10, day=MON, cls="etf")]},
        prices={"GOLDBEES": [bar(FRI, 100.0), bar(MON, 101.0)]},
    )
    await run(pool, ansaar, market(), MON)
    await run(pool, ansaar, market(), TUE)
    assert await pool.fetchval("SELECT price_source FROM finance.desk_orders WHERE symbol = 'GOLDBEES'") == "ansaar"


async def test_a_day_yahoo_left_unpriced_is_filled_from_ansaar_and_sized_on_it(pool):
    """Yahoo answers for a market day with a bar whose close is None. The
    fallback is per day, so that day is asked of ansaar; without it the desk
    sizes on the day before's close (spec §6)."""
    finance = market({"GOLDCASE.NS": [bar(THU, 24.04), bar(FRI, None)]})
    ansaar = FakeAnsaar(
        {FRI: [row("GOLDCASE", 0.10, cls="etf")]},
        prices={"GOLDCASE": [bar(THU, 24.10), bar(FRI, 23.82)]},
    )
    await run(pool, ansaar, finance, MON)

    stored = await pool.fetch(
        "SELECT date, close, source FROM finance.desk_prices WHERE symbol = 'GOLDCASE.NS' ORDER BY date"
    )
    assert [(r["date"], r["close"] and float(r["close"]), r["source"]) for r in stored] == [
        (THU, 24.04, "yahoo"),  # Yahoo had this one, so ansaar never overwrites it
        (FRI, 23.82, "ansaar"),
    ]
    order = await pool.fetchrow("SELECT symbol, qty, ref_price FROM finance.desk_orders")
    assert (order["symbol"], order["qty"], float(order["ref_price"])) == ("GOLDCASE", 419, 23.82)


async def test_ansaar_is_not_asked_when_yahoo_priced_every_market_day(pool):
    """The fallback is for days that have no close, not for every day."""
    finance = market({"TCS.NS": [bar(THU, 3000.0), bar(FRI, 3010.0)]})
    ansaar = FakeAnsaar({FRI: [row("TCS", 0.10)]})
    await run(pool, ansaar, finance, MON)
    assert ansaar.price_calls == []


async def test_one_unfillable_order_holds_back_its_own_name_only(pool):
    """A close Yahoo never publishes leaves an order pending for three market
    days. The rest of the desk must carry on: only that name is held back, and
    the plan row says so."""
    finance = market({
        "GOLDCASE.NS": [bar(THU, 24.0), bar(FRI, 23.8), bar(MON, None)],
        "TCS.NS": [bar(FRI, 3000.0), bar(MON, 3100.0)],
    })
    ansaar = FakeAnsaar({
        FRI: [row("GOLDCASE", 0.10, cls="etf")],
        MON: [row("GOLDCASE", 0.10, day=MON, cls="etf"), row("TCS", 0.10, day=MON, rank=2)],
    })

    await run(pool, ansaar, finance, MON)
    assert await pool.fetchval("SELECT count(*) FROM finance.desk_orders WHERE status = 'pending'") == 1

    out = await run(pool, ansaar, finance, TUE)
    assert out["planned"] == "orders"
    orders = await pool.fetch(
        "SELECT symbol, qty, status, created_day FROM finance.desk_orders ORDER BY created_day, seq"
    )
    assert [(o["symbol"], o["qty"], o["status"], o["created_day"]) for o in orders] == [
        ("GOLDCASE", 420, "pending", MON),  # still waiting for a close
        ("TCS", 3, "pending", TUE),  # planned anyway
    ]
    skipped = await pool.fetchval("SELECT skipped FROM finance.desk_plans WHERE data_date = $1", MON)
    assert skipped == ["GOLDCASE: pending_order"]


async def test_a_held_name_with_no_decision_still_gets_a_fresh_price(pool):
    """The refresh list is built from what the desk holds, so a name that is held
    but absent from today's decisions, with no pending order, must still be priced."""
    await filled(pool, THU, "TCS", "buy", 3, 3000.0)
    finance = market({"TCS.NS": [bar(FRI, 3100.0)], "GOLDBEES.NS": [bar(FRI, 100.0)]})
    await run(pool, FakeAnsaar({FRI: [row("GOLDBEES", 0.10, cls="etf")]}), finance, MON)
    stored = await pool.fetchval(
        "SELECT close FROM finance.desk_prices WHERE symbol = 'TCS.NS' AND date = $1", FRI
    )
    assert stored is not None and float(stored) == 3100.0


async def test_a_holding_survives_a_split_and_a_trim(pool):
    """Buys minus sells says this position is closed; the split says it is not.
    The desk must load bars for every symbol it has ever filled, replay, and take
    what it holds from that."""
    await filled(pool, THU, "TCS", "buy", 10, 3000.0)
    await filled(pool, FRI, "TCS", "sell", 12, 1500.0, costs=52.0)
    await pool.execute(
        "INSERT INTO finance.desk_prices (symbol, date, close, split_ratio, source) "
        "VALUES ('TCS.NS', $1, 3000, NULL, 'yahoo'), ('TCS.NS', $2, 1500, 2, 'yahoo')",
        THU, FRI,
    )
    finance = market({"GOLDBEES.NS": [bar(MON, 100.0)]})
    ansaar = FakeAnsaar({MON: [row("GOLDBEES", 0.10, day=MON, cls="etf")]})
    out = await run(pool, ansaar, finance, TUE)
    # 10 bought, doubled by the split, 12 sold: 8 are still held, so equity
    # vanishing from the decisions is suspect and nothing trades.
    assert out["planned"] == "held_suspect"
    assert await pool.fetchval("SELECT count(*) FROM finance.desk_orders WHERE status = 'pending'") == 0


async def test_a_holding_with_no_recent_price_raises_price_missing(pool):
    await filled(pool, THU, "TCS", "buy", 3, 3000.0)
    await pool.execute("INSERT INTO finance.desk_prices (symbol, date, close, source) VALUES ('TCS.NS', $1, 3000, 'yahoo')", THU)
    await run(pool, FakeAnsaar({TUE: [row("TCS", 0.10, day=TUE)]}), market(), WED)
    assert ("desk_price_missing", "tcs") in await open_problems(pool)


async def test_live_mode_is_refused(pool):
    await pool.execute(
        "INSERT INTO activities (slug, workflow_type, agent_id, schedule_cron, config, active) "
        "VALUES ($1, 'TradingDeskFlow', 'maou', '30 2 * * 1-5', $2, false) "
        "ON CONFLICT (slug) DO UPDATE SET config = EXCLUDED.config",
        td.DESK_SLUG,
        {"mode": "live"},
    )
    out = await run(pool, FakeAnsaar(), market(), MON)
    assert out["skipped"] == "mode"
    assert await open_problems(pool) == [("desk_source_error", "config")]


def test_yahoo_symbol():
    assert td.yahoo_symbol("TCS") == "TCS.NS"
    assert td.yahoo_symbol("^NSEI") == "^NSEI"
    assert td.yahoo_symbol("SHARIABEES.NS") == "SHARIABEES.NS"
