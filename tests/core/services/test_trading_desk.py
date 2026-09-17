"""The trading desk's daily run against a real Postgres (spec §3, §5, §10)."""

from __future__ import annotations

import inspect
from datetime import date

import httpx
import pytest
import pytest_asyncio
from aegis.connectors.ansaar import AnsaarClient, AnsaarError
from aegis.connectors.finance import FinanceConnector
from aegis.services import desk_math as dm
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


def bar(day, close, split=None, div=None, open=None):
    return {"day": day, "open": open, "close": close, "split_ratio": split, "dividend": div}


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

    def __init__(self, days=None, fail=False, prices=None, halts=None):
        self.days = days or {}
        self.fail = fail
        self.price_rows = prices or {}
        self.price_calls = []
        # meta.halted / meta.halt per day, as ansaar-data serves them.
        self.halts = halts or {}

    async def decisions(self, day):
        if self.fail:
            raise AnsaarError("/api/execution/trade-decisions: ConnectError")
        meta = {"date": day.isoformat(), "halted": False, "halt": None}
        if day in self.halts:
            meta |= {"halted": True, "halt": self.halts[day]}
        return list(self.days.get(day, [])), meta

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


def test_the_fake_bar_has_every_field_the_real_one_does():
    """The signature check above guards parameter NAMES, so a connector that
    starts returning a new field passes it while every test here keeps feeding
    the old shape — which is how a field could be added and never once
    exercised. This pins the return shape instead."""
    assert set(bar(FRI, 1.0)) == {"day", "open", "close", "split_ratio", "dividend"}


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


async def test_store_bars_keeps_todays_open_and_never_its_close(pool):
    """Today's bar is worth storing for its open, which is settled the moment
    the market opens. Its close is the live price and still moving — and since a
    stored close is never overwritten, keeping it would pin an intraday number
    as the day's close for ever."""
    await td._store_bars(
        pool, "TCS.NS", [bar(FRI, 3000.0, open=2990.0), bar(MON, 3010.0, open=3005.0)], "yahoo", MON
    )
    rows = await pool.fetch(
        "SELECT date, open, close FROM finance.desk_prices WHERE symbol = 'TCS.NS' ORDER BY date"
    )
    assert [(r["date"], r["open"] and float(r["open"]), r["close"] and float(r["close"])) for r in rows] == [
        (FRI, 2990.0, 3000.0),
        (MON, 3005.0, None),
    ]


async def test_todays_close_is_filled_in_by_a_later_run(pool):
    """The day after, that same bar is complete and its close lands through the
    same COALESCE — while the open it was stored with stays put."""
    await td._store_bars(pool, "TCS.NS", [bar(MON, 3010.0, open=3005.0)], "yahoo", MON)
    await td._store_bars(pool, "TCS.NS", [bar(MON, 3020.0, open=3005.0)], "yahoo", TUE)
    row_ = await pool.fetchrow("SELECT open, close FROM finance.desk_prices WHERE symbol = 'TCS.NS'")
    assert (float(row_["open"]), float(row_["close"])) == (3005.0, 3020.0)


async def test_a_row_is_attributed_to_whoever_supplied_its_close(pool):
    """Two prices, one source column. The close decides, so ansaar still gets
    the credit when it fills in a close Yahoo never published — only while
    there is no close at all does the open's supplier name the row, which is
    the state today's bar sits in until the next morning."""
    await td._store_bars(pool, "TCS.NS", [bar(MON, 3010.0, open=3005.0)], "yahoo", MON)
    assert await pool.fetchval("SELECT source FROM finance.desk_prices WHERE symbol = 'TCS.NS'") == "yahoo"

    await td._store_bars(pool, "TCS.NS", [bar(MON, 3020.0)], "ansaar", TUE)
    assert await pool.fetchval("SELECT source FROM finance.desk_prices WHERE symbol = 'TCS.NS'") == "ansaar"


async def test_a_future_bar_is_never_stored(pool):
    await td._store_bars(pool, "TCS.NS", [bar(MON, 3010.0, open=3005.0)], "yahoo", FRI)
    assert await pool.fetchval("SELECT count(*) FROM finance.desk_prices WHERE symbol = 'TCS.NS'") == 0


async def test_two_desk_names_for_one_yahoo_symbol_both_get_the_bars(pool):
    """The desk holds this ETF under its NSE name and names the benchmark in
    Yahoo's form. Both ask for SHARIABEES.NS, so both must come back with its bars."""
    await td._store_bars(pool, "SHARIABEES.NS", [bar(FRI, 400.0), bar(MON, 402.0)], "yahoo", TUE)
    bars = await td._bars(pool, await td.load_rules(pool), {"SHARIABEES", "SHARIABEES.NS"})
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
    assert [c for c in ansaar.price_calls if c[0] == "TCS"] == []


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


def test_the_price_source_symbol_comes_from_the_configured_suffix(seeded_desk_rules):
    """The suffix is the operator's exchange, not a literal in the code. An
    index and a symbol that already names its exchange are left alone."""
    assert seeded_desk_rules.price_symbol("TCS") == "TCS.NS"
    assert seeded_desk_rules.price_symbol("^NSEI") == "^NSEI"
    assert seeded_desk_rules.price_symbol("SHARIABEES.NS") == "SHARIABEES.NS"


def test_with_no_suffix_configured_a_symbol_is_left_alone():
    """A US desk needs no suffix, which is why the default is none."""
    assert dm.Rules().price_symbol("AAPL") == "AAPL"


# --- a stated risk halt sells the whole book (spec §5) ------------------------

HALT = {
    "trigger": "DAILY_LOSS",
    "triggered_on": "2026-09-11",
    "recovery_state": "COOLING",
    "resumed_on": None,
    "reason": "The risk manager halted trading: DAILY_LOSS fired on 2026-09-11 "
    "and the pipeline has decided nothing since.",
}


async def test_a_stated_halt_sells_the_whole_book(pool):
    """The pipeline is flat on a halt, so the desk must not stay invested. Every
    holding gets a full exit, and the plan says the day was flattened."""
    await filled(pool, THU, "TCS", "buy", 3, 3000.0)
    await filled(pool, THU, "GOLDBEES", "buy", 100, 100.0, cls="etf", seq=1)
    finance = market({
        "TCS.NS": [bar(THU, 3000.0), bar(FRI, 3100.0)],
        "GOLDBEES.NS": [bar(THU, 100.0), bar(FRI, 101.0)],
    })

    out = await run(pool, FakeAnsaar({FRI: []}, halts={FRI: HALT}), finance, MON)

    assert out["planned"] == "flattened"
    orders = await pool.fetch("SELECT symbol, side, qty, ref_price, status FROM finance.desk_orders WHERE created_day = $1 ORDER BY seq", MON)
    assert [(o["symbol"], o["side"], o["qty"], float(o["ref_price"]), o["status"]) for o in orders] == [
        ("TCS", "sell", 3, 3100.0, "pending"),
        ("GOLDBEES", "sell", 100, 101.0, "pending"),
    ]
    plan = await pool.fetchrow("SELECT outcome, note FROM finance.desk_plans WHERE data_date = $1", FRI)
    assert plan["outcome"] == "flattened" and "DAILY_LOSS" in plan["note"]
    # A halt is the risk manager working, not a fault: no stale problem.
    assert await open_problems(pool) == []


async def test_a_halt_with_nothing_held_is_still_recorded(pool):
    """Nothing to sell, but the month close still has to be able to say the
    pipeline halted that day."""
    out = await run(pool, FakeAnsaar({FRI: []}, halts={FRI: HALT}), market(), MON)

    assert out["planned"] == "flattened"
    assert await pool.fetchval("SELECT count(*) FROM finance.desk_orders") == 0
    assert await open_problems(pool) == []


async def test_an_empty_day_with_no_halt_stated_still_holds(pool):
    """Silence is never a halt. ansaar says halted: false, which means no halt is
    on record — the pipeline may simply have failed — so the desk holds."""
    await filled(pool, THU, "TCS", "buy", 3, 3000.0)
    finance = market({"TCS.NS": [bar(THU, 3000.0), bar(FRI, 3100.0)]})

    out = await run(pool, FakeAnsaar({FRI: []}), finance, MON)

    assert out["planned"] == "held_stale"
    assert await pool.fetchval("SELECT count(*) FROM finance.desk_orders WHERE created_day = $1", MON) == 0
    assert await open_problems(pool) == [("desk_decisions_stale", "decisions")]


async def test_an_ansaar_outage_is_never_read_as_a_halt(pool):
    """A failed call tells the desk nothing about the pipeline's risk state."""
    await filled(pool, THU, "TCS", "buy", 3, 3000.0)
    finance = market({"TCS.NS": [bar(THU, 3000.0), bar(FRI, 3100.0)]})

    out = await run(pool, FakeAnsaar(fail=True), finance, MON)

    assert out["planned"] == "held_stale"
    assert await pool.fetchval("SELECT count(*) FROM finance.desk_orders WHERE created_day = $1", MON) == 0


async def test_a_halt_leaves_a_name_it_cannot_price_alone(pool):
    """A full exit still needs a price. The unpriced name stays held and the plan
    says which part of the book it did not sell."""
    await filled(pool, THU, "TCS", "buy", 3, 3000.0)
    stale = date(2026, 8, 20)  # the last close anyone has for XYZ, weeks old
    await filled(pool, stale, "XYZ", "buy", 10, 50.0, seq=1)
    await pool.execute(
        "INSERT INTO finance.desk_prices (symbol, date, close, source) VALUES ('XYZ.NS', $1, 50, 'yahoo')", stale
    )
    finance = market({"TCS.NS": [bar(THU, 3000.0), bar(FRI, 3100.0)]})

    out = await run(pool, FakeAnsaar({FRI: []}, halts={FRI: HALT}), finance, MON)

    assert out["planned"] == "flattened"
    orders = await pool.fetch("SELECT symbol FROM finance.desk_orders WHERE created_day = $1", MON)
    assert [o["symbol"] for o in orders] == ["TCS"]
    assert await pool.fetchval("SELECT skipped FROM finance.desk_plans WHERE data_date = $1", FRI) == ["XYZ: no_price"]


def test_only_an_explicit_halt_counts():
    """The reader that decides whether the desk flattens. Anything but a stated
    halted: true means the desk knows nothing."""
    assert td._halt({"halted": True, "halt": HALT}) == HALT
    assert td._halt({"halted": True, "halt": None}) == {}
    assert td._halt({"halted": False, "halt": None}) is None
    assert td._halt({"date": "2026-09-11"}) is None  # an older ansaar
    assert td._halt({"halted": "true"}) is None  # a string is not a statement
    assert td._halt(None) is None


# --- the benchmarks get the same price fallback (spec §8) ---------------------


async def test_a_benchmark_day_yahoo_leaves_null_is_filled_from_ansaar(pool):
    """SHARIABEES.NS came back with a bar whose close was None and stayed NULL
    for ever, while ansaar had the price. The monthly score is measured against
    this series, so a hole in it bends the number the owner reads."""
    finance = market({"TCS.NS": [bar(FRI, 3000.0)]})
    finance.bars["SHARIABEES.NS"] = [bar(THU, 438.57), bar(FRI, None)]
    ansaar = FakeAnsaar(
        {FRI: [row("TCS", 0.10)]},
        prices={"SHARIABEES": [bar(THU, 438.60), bar(FRI, 437.38)]},
    )

    await run(pool, ansaar, finance, MON)

    stored = await pool.fetch(
        "SELECT date, close, source FROM finance.desk_prices WHERE symbol = 'SHARIABEES.NS' ORDER BY date"
    )
    assert [(r["date"], r["close"] and float(r["close"]), r["source"]) for r in stored] == [
        (THU, 438.57, "yahoo"),  # Yahoo had this one, so ansaar never overwrites it
        (FRI, 437.38, "ansaar"),
    ]
    # ansaar is asked for the NSE symbol, not the benchmark's Yahoo name.
    assert "SHARIABEES" in [c[0] for c in ansaar.price_calls]
    assert "SHARIABEES.NS" not in [c[0] for c in ansaar.price_calls]


async def test_an_unmapped_benchmark_gets_no_fallback(pool):
    """No mapping, no fallback — the same as before this existed. A fork that
    names its own benchmark is not silently asked about someone else's."""
    await pool.execute(
        "UPDATE activities SET config = config - 'benchmark_prices' WHERE slug = $1", td.DESK_SLUG
    )
    finance = market({"TCS.NS": [bar(FRI, 3000.0)]})
    finance.bars["SHARIABEES.NS"] = [bar(THU, 438.57), bar(FRI, None)]
    ansaar = FakeAnsaar({FRI: [row("TCS", 0.10)]}, prices={"SHARIABEES": [bar(FRI, 437.38)]})

    await run(pool, ansaar, finance, MON)

    assert [c[0] for c in ansaar.price_calls if c[0].startswith("SHARIABEES")] == []
    close = await pool.fetchval(
        "SELECT close FROM finance.desk_prices WHERE symbol = 'SHARIABEES.NS' AND date = $1", FRI
    )
    assert close is None


# --- what the desk says about itself (#540, #565, #525, #524) -----------------


def _places(text: str) -> int:
    """How many decimal places a numeric column came back with."""
    return len(text.split(".")[1]) if "." in text else 0


async def test_money_columns_store_as_the_decimals_they_read_as(pool):
    """`round(x, 4)` returns the nearest float, and 24.04 is not one, so the
    first live plan stored a ₹24.04 close as
    24.039999999999999147… — right to about 1e-15 and unreadable to anyone
    checking the desk by hand in SQL (#540).

    Two names and all three write sites, because one column passing proves
    nothing about the other two."""
    finance = market({
        "GOLDCASE.NS": [bar(FRI, 24.04), bar(MON, 24.07)],
        "SILVERCASE.NS": [bar(FRI, 1.15), bar(MON, 1.16)],
    })
    ansaar = FakeAnsaar({
        FRI: [row("GOLDCASE", 0.10, cls="etf"), row("SILVERCASE", 0.10, cls="etf", rank=2)],
        MON: [row("GOLDCASE", 0.10, day=MON, cls="etf"), row("SILVERCASE", 0.10, day=MON, cls="etf", rank=2)],
    })

    await run(pool, ansaar, finance, MON)  # _store_bars, then _write_plan
    await run(pool, ansaar, finance, TUE)  # _apply_fills

    closes = await pool.fetch(
        "SELECT symbol, close::text AS close FROM finance.desk_prices "
        "WHERE symbol IN ('GOLDCASE.NS', 'SILVERCASE.NS') ORDER BY symbol, date"
    )
    assert [(c["symbol"], c["close"]) for c in closes] == [
        ("GOLDCASE.NS", "24.04"), ("GOLDCASE.NS", "24.07"),
        ("SILVERCASE.NS", "1.15"), ("SILVERCASE.NS", "1.16"),
    ]
    orders = await pool.fetch(
        "SELECT symbol, ref_price::text AS ref, fill_price::text AS fill, costs::text AS costs "
        "FROM finance.desk_orders WHERE created_day = $1 ORDER BY seq",
        MON,
    )
    assert [(o["symbol"], o["ref"], o["fill"]) for o in orders] == [
        ("GOLDCASE", "24.04", "24.07"),
        ("SILVERCASE", "1.15", "1.16"),
    ]
    # The charges are computed, not quoted, so they are the column most likely
    # to keep carrying a float's tail.
    assert [_places(o["costs"]) for o in orders] == [4, 4]


async def test_a_run_with_nothing_to_fill_says_so_rather_than_leaving_the_key_out(pool):
    """A missing key reads as "the step never ran", which is a different
    statement from "there was nothing to fill" (#565)."""
    finance = market({"TCS.NS": [bar(FRI, 3000.0)], "GOLDBEES.NS": [bar(FRI, 100.0)]})
    ansaar = FakeAnsaar({FRI: [row("TCS", 0.10), row("GOLDBEES", 0.10, cls="etf", rank=2)]})

    out = await run(pool, ansaar, finance, MON)

    assert out["pending_checked"] == 0 and out["filled"] == 0
    assert await pool.fetchval("SELECT count(*) FROM finance.desk_orders WHERE status = 'pending'") == 2


async def test_a_run_that_could_fill_nothing_says_how_many_it_tried(pool):
    """Two orders waiting on a close Yahoo never published. `filled: 0` alone
    cannot tell that from a morning with no orders at all (#565)."""
    finance = market({
        "TCS.NS": [bar(FRI, 3000.0), bar(MON, None)],
        "GOLDBEES.NS": [bar(FRI, 100.0), bar(MON, None)],
    })
    ansaar = FakeAnsaar({FRI: [row("TCS", 0.10), row("GOLDBEES", 0.10, cls="etf", rank=2)]})
    await run(pool, ansaar, finance, MON)

    out = await run(pool, ansaar, finance, TUE)

    assert out["pending_checked"] == 2 and out["filled"] == 0
    assert await pool.fetchval("SELECT count(*) FROM finance.desk_orders WHERE status = 'pending'") == 2


# A full trading week, because the desk reads its own week off these bars: a
# two-bar calendar would say this market trades on Thursdays and Fridays.
WEEK = [date(2026, 9, d) for d in (7, 8, 9, 10, 11, 14)]


async def test_a_weekday_the_calendar_never_gave_is_counted_not_silently_skipped(pool):
    """Yahoo drops a day, or serves one whose close is null, so the newest
    market day is one the desk planned yesterday. The run then does nothing at
    all and reads exactly like a quiet morning, while yesterday's decisions are
    never acted on and the stale-calendar alarm waits six days (#525)."""
    finance = FakeFinance({
        "^NSEI": [bar(d, 25_000.0) for d in WEEK],  # nothing for Tuesday the 15th
        "SHARIABEES.NS": [bar(FRI, 400.0)],
        "TCS.NS": [bar(d, 3000.0) for d in WEEK],
    })
    ansaar = FakeAnsaar({MON: [row("TCS", 0.10, day=MON)]})

    planning = await run(pool, ansaar, finance, TUE)
    assert (planning["day"], planning["idle_weekday"]) == (MON.isoformat(), 0)

    # A second run the same morning is not an idle day: yesterday's bar is there.
    assert (await run(pool, ansaar, finance, TUE))["idle_weekday"] == 0

    idle = await run(pool, ansaar, finance, WED)
    assert (idle["day"], idle["idle_weekday"]) == (MON.isoformat(), 1)
    assert "planned" not in idle  # the day was already planned, so nothing new happened
    # One missing bar overnight is normal, and a market holiday reads the same,
    # so it earns no problem and no task. The six-day alarm still escalates.
    assert await open_problems(pool) == []


# Three mornings of decisions, so a run on Wednesday is never held for stale
# input while the benchmark is what the test is about.
DECISIONS = FakeAnsaar({
    FRI: [row("TCS", 0.10)],
    MON: [row("TCS", 0.10, day=MON)],
    TUE: [row("TCS", 0.10, day=TUE)],
})


def dark_benchmarks():
    """A priced holding, a benchmark last seen on Thursday, and one never served
    at all. Two benchmarks, because one passes for the wrong reason."""
    finance = market({"TCS.NS": [bar(FRI, 3000.0), bar(MON, 3100.0), bar(TUE, 3050.0)]})
    finance.bars["SHARIABEES.NS"] = [bar(THU, 400.0)]
    finance.bars["NIFTYCASE.NS"] = []
    return finance


async def test_a_benchmark_that_stops_being_priced_is_a_finding(pool):
    """A holding going dark is loud; a benchmark going dark is silent.
    `benchmark_values` returns nothing, the weekly gap has nothing to compare,
    the label reads "too early" for ever and the rendered figure is blank, so
    the score stops meaning anything without saying so (#524)."""
    await pool.execute(
        "UPDATE activities SET config = config || $2::jsonb WHERE slug = $1",
        td.DESK_SLUG, {"context_benchmark": "NIFTYCASE.NS"},
    )
    finance = dark_benchmarks()
    await run(pool, DECISIONS, finance, MON)
    await run(pool, DECISIONS, finance, TUE)

    await run(pool, DECISIONS, finance, WED)

    # Both benchmarks, and not the holding, which Yahoo is still pricing.
    assert await open_problems(pool) == [
        ("desk_price_missing", "niftycase.ns"),
        ("desk_price_missing", "shariabees.ns"),
    ]
    said = await pool.fetchval(
        "SELECT payload->>'description' FROM problem_events e JOIN problems p ON p.id = e.problem_id "
        "WHERE p.subject = 'shariabees.ns' ORDER BY e.created_at LIMIT 1"
    )
    assert "scores itself against it" in said


async def test_a_benchmark_priced_this_week_raises_nothing(pool):
    """The half that matters: a finding raised unconditionally would look
    identical on the day it is written and tell the owner nothing ever after."""
    finance = market({"TCS.NS": [bar(FRI, 3000.0), bar(MON, 3100.0), bar(TUE, 3050.0)]})
    finance.bars["SHARIABEES.NS"] = [bar(FRI, 400.0), bar(MON, 402.0), bar(TUE, 401.0)]
    await run(pool, DECISIONS, finance, MON)
    await run(pool, DECISIONS, finance, TUE)

    await run(pool, DECISIONS, finance, WED)

    assert await open_problems(pool) == []


async def test_a_dark_benchmark_clears_when_its_price_comes_back(pool):
    """Same class and same subject shape as a holding's, so the daily run's own
    reconcile resolves it rather than leaving a task open for ever."""
    finance = dark_benchmarks()
    del finance.bars["NIFTYCASE.NS"]
    for day in (MON, TUE, WED):
        await run(pool, DECISIONS, finance, day)
    assert await open_problems(pool) == [("desk_price_missing", "shariabees.ns")]

    finance.bars["SHARIABEES.NS"] = [bar(THU, 400.0), bar(TUE, 402.0)]
    await run(pool, DECISIONS, finance, WED)

    assert await open_problems(pool) == []
