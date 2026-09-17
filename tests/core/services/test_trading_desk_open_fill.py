"""Filling at the session's open, and firing several times a day (spec §3, §6).

`test_trading_desk.py` exercises the code default, `fill_at: "close"`, which is
what keeps a deployment that has not opted in arithmetically unchanged. This
file pins the other setting, and the run-order rules that only matter once the
desk fires more than once a day.

The fixtures and fakes are that module's — reusing them is what makes these
tests a statement about the same desk rather than a parallel one.
"""

from __future__ import annotations

import pytest_asyncio
from aegis.services import trading_desk as td

# The fakes, dates and helpers come from the module next door, so these tests
# are a statement about the same desk rather than a parallel one. The `pool`
# fixture is rebuilt here instead of imported: importing a fixture binds its
# name in this module too, and every test parameter called `pool` would then
# read as redefining it.
from .test_trading_desk import (
    _WIPE,
    FRI,
    INDEX_BARS,
    MON,
    THU,
    TUE,
    FakeAnsaar,
    bar,
    market,
    open_problems,
    row,
    run,
)


@pytest_asyncio.fixture(loop_scope="function")
async def pool(db_pool):
    for sql in _WIPE:
        await db_pool.execute(sql)
    original = await db_pool.fetchrow("SELECT config FROM activities WHERE slug = $1", td.DESK_SLUG)
    yield db_pool
    for sql in _WIPE:
        await db_pool.execute(sql)
    if original is not None:
        await db_pool.execute(
            "UPDATE activities SET config = $2 WHERE slug = $1", td.DESK_SLUG, original["config"]
        )


async def fill_at_open(pool):
    await pool.execute(
        """UPDATE activities SET config = config || '{"fill_at": "open"}'::jsonb WHERE slug = $1""",
        td.DESK_SLUG,
    )


def opened(index_close, index_open):
    """Monday's index bar, as it looks once the market has opened: a settled
    open, and a `close` that is really the live price."""
    return [*INDEX_BARS[:2], bar(MON, index_close, open=index_open)]


async def test_an_order_planned_before_the_open_fills_at_that_open(pool):
    """The whole point: a signal taken from Friday's close is acted on at
    Monday's first trade, not held back until Monday's close."""
    await fill_at_open(pool)
    finance = market({"TCS.NS": [bar(THU, 2990.0), bar(FRI, 3000.0)]})
    ansaar = FakeAnsaar({FRI: [row("TCS", 0.20)]})

    await run(pool, ansaar, finance, MON)  # 08:00, before the open
    assert await pool.fetchval("SELECT count(*) FROM finance.desk_orders WHERE status = 'pending'") == 1

    finance.bars["TCS.NS"] = [bar(THU, 2990.0), bar(FRI, 3000.0), bar(MON, 3080.0, open=3010.0)]
    finance.bars["^NSEI"] = opened(25555.0, 25200.0)
    out = await run(pool, ansaar, finance, MON)  # 11:00, after it

    assert out["filled"] == 1
    got = await pool.fetchrow(
        "SELECT fill_date, fill_price, price_kind FROM finance.desk_orders WHERE symbol = 'TCS'"
    )
    assert (got["fill_date"], float(got["fill_price"]), got["price_kind"]) == (MON, 3010.0, "open")


async def test_the_fill_falls_back_to_the_close_when_no_open_arrives(pool):
    """An order the desk meant to place is not dropped because one source never
    published one field. The fallback records itself, so a desk quietly filling
    everything at the close cannot be mistaken for one filling at the open."""
    await fill_at_open(pool)
    finance = market({"TCS.NS": [bar(THU, 2990.0), bar(FRI, 3000.0)]})
    ansaar = FakeAnsaar({FRI: [row("TCS", 0.20)]})
    await run(pool, ansaar, finance, MON)

    finance.bars["TCS.NS"] = [bar(THU, 2990.0), bar(FRI, 3000.0), bar(MON, 3080.0)]  # no open
    await run(pool, ansaar, finance, TUE)

    got = await pool.fetchrow("SELECT fill_price, price_kind FROM finance.desk_orders WHERE symbol = 'TCS'")
    assert (float(got["fill_price"]), got["price_kind"]) == (3080.0, "close")


async def test_a_plan_is_never_made_after_the_market_has_opened(pool):
    """An order planned at 11:00 would take that day's 09:15 open as its fill
    price — a print struck before the decision existed. Not a lag: an execution
    nobody could have got. The day goes unplanned instead."""
    await fill_at_open(pool)
    finance = market({"TCS.NS": [bar(THU, 2990.0), bar(FRI, 3000.0), bar(MON, 3080.0, open=3010.0)]})
    finance.bars["^NSEI"] = opened(25555.0, 25200.0)
    ansaar = FakeAnsaar({FRI: [row("TCS", 0.20)]})

    out = await run(pool, ansaar, finance, MON)

    assert out["skipped_plan"] == "after_open"
    assert await pool.fetchval("SELECT count(*) FROM finance.desk_orders") == 0
    assert await pool.fetchval("SELECT count(*) FROM finance.desk_plans") == 0


async def test_a_session_that_has_opened_is_tradable_but_is_not_a_decision_day(pool):
    """The calendar `day` is read off must stay completed sessions only. Let a
    day in on its open alone and `day` becomes today, ansaar has no decisions
    for today, and the desk reports `held_stale` every single trading day."""
    await fill_at_open(pool)
    finance = market({"TCS.NS": [bar(FRI, 3000.0), bar(MON, 3080.0, open=3010.0)]})
    finance.bars["^NSEI"] = opened(25555.0, 25200.0)
    ansaar = FakeAnsaar({FRI: [row("TCS", 0.20)]})

    out = await run(pool, ansaar, finance, MON)

    assert out["day"] == FRI.isoformat()  # not MON, though MON has opened
    assert "desk_decisions_stale" not in out["findings"]


async def test_three_runs_in_one_day_plan_once_fill_once_then_do_nothing(pool):
    """The desk fires before the open, after it, and again later. Together they
    are one day: one plan, one fill."""
    await fill_at_open(pool)
    finance = market({"TCS.NS": [bar(THU, 2990.0), bar(FRI, 3000.0)]})
    ansaar = FakeAnsaar({FRI: [row("TCS", 0.20)]})

    first = await run(pool, ansaar, finance, MON)
    finance.bars["TCS.NS"] = [bar(THU, 2990.0), bar(FRI, 3000.0), bar(MON, 3080.0, open=3010.0)]
    finance.bars["^NSEI"] = opened(25555.0, 25200.0)
    second = await run(pool, ansaar, finance, MON)
    third = await run(pool, ansaar, finance, MON)

    assert (first["filled"], second["filled"], third["filled"]) == (0, 1, 0)
    assert await pool.fetchval("SELECT count(*) FROM finance.desk_plans") == 1
    assert await pool.fetchval("SELECT count(*) FROM finance.desk_orders WHERE status = 'filled'") == 1
    # Both numbers on every path: a missing key reads as "the step never ran"
    # rather than "there was nothing to fill" (#565).
    assert all("pending_checked" in r and "filled" in r for r in (first, second, third))


async def test_a_later_run_does_not_re_raise_a_planned_days_ansaar_failure(pool):
    """ansaar up at 08:00 and down at 14:00 must not report "can't reach ansaar"
    for a day already planned — that is one flaky call resolving and re-raising
    the same problem twice in a day."""
    await fill_at_open(pool)
    finance = market({"TCS.NS": [bar(THU, 2990.0), bar(FRI, 3000.0)]})
    ansaar = FakeAnsaar({FRI: [row("TCS", 0.20)]})
    await run(pool, ansaar, finance, MON)

    ansaar.fail = True
    out = await run(pool, ansaar, finance, MON)

    assert "desk_source_error" not in out["findings"]


async def test_only_the_run_that_could_have_planned_calls_the_morning_idle(pool):
    """`idle_weekday` is `planned`-dependent, so without this every run after
    the first would report it and one day would disagree with itself."""
    await fill_at_open(pool)
    finance = market({"TCS.NS": [bar(THU, 2990.0), bar(FRI, 3000.0)]})
    ansaar = FakeAnsaar({FRI: [row("TCS", 0.20)]})
    await run(pool, ansaar, finance, MON)
    finance.bars["^NSEI"] = opened(25555.0, 25200.0)

    assert (await run(pool, ansaar, finance, MON))["idle_weekday"] == 0


async def test_all_of_a_days_runs_are_one_hub_occurrence(pool):
    """Three fires a day looking at the same facts are one occurrence, not
    three. Otherwise the count stops meaning "how often did this happen", and a
    problem cleared after the morning run is reopened by the afternoon one."""
    await fill_at_open(pool)
    ansaar = FakeAnsaar({FRI: [row("TCS", 0.20)]}, fail=True)
    finance = market()

    for _ in range(3):
        await run(pool, ansaar, finance, MON)

    occurrences = await pool.fetchval(
        "SELECT count(*) FROM problem_events e JOIN problems p ON p.id = e.problem_id "
        "WHERE p.subject_kind = 'trading_desk' AND e.kind = 'occurrence'"
    )
    assert occurrences == 1


# --- a lost trading day is a problem (#593) -----------------------------------


async def test_a_day_the_desk_could_not_plan_is_raised_as_a_problem(pool):
    """The pre-open run never fired, so the first run to see the day found the
    market already open and refused to plan. That is a day of trading lost,
    and before this it was a key in a JSON column nobody reads."""
    await fill_at_open(pool)
    finance = market({"TCS.NS": [bar(THU, 2990.0), bar(FRI, 3000.0), bar(MON, 3080.0, open=3010.0)]})
    finance.bars["^NSEI"] = opened(25555.0, 25200.0)
    ansaar = FakeAnsaar({FRI: [row("TCS", 0.20)]})

    out = await run(pool, ansaar, finance, MON)

    assert out["skipped_plan"] == "after_open"
    assert "desk_plan_skipped" in out["findings"]
    assert await open_problems(pool) == [("desk_plan_skipped", "plan")]


async def test_the_afternoon_run_does_not_raise_the_lost_day_twice(pool):
    """Every later run that day sees the same missing plan. They are one
    occurrence, not one per fire."""
    await fill_at_open(pool)
    finance = market({"TCS.NS": [bar(THU, 2990.0), bar(FRI, 3000.0), bar(MON, 3080.0, open=3010.0)]})
    finance.bars["^NSEI"] = opened(25555.0, 25200.0)
    ansaar = FakeAnsaar({FRI: [row("TCS", 0.20)]})

    await run(pool, ansaar, finance, MON)
    await run(pool, ansaar, finance, MON)

    occurrences = await pool.fetchval(
        "SELECT count(*) FROM problem_events e JOIN problems p ON p.id = e.problem_id "
        "WHERE p.class = 'desk_plan_skipped' AND e.kind = 'occurrence'"
    )
    assert occurrences == 1


async def test_the_next_morning_that_plans_resolves_the_lost_day(pool):
    """Tomorrow's pre-open run plans as normal. Nothing is left to raise, and
    the class is one this run checks, so the problem resolves itself rather
    than waiting for a human to notice it is stale."""
    await fill_at_open(pool)
    finance = market({"TCS.NS": [bar(THU, 2990.0), bar(FRI, 3000.0), bar(MON, 3080.0, open=3010.0)]})
    finance.bars["^NSEI"] = opened(25555.0, 25200.0)
    ansaar = FakeAnsaar({FRI: [row("TCS", 0.20)], MON: [row("TCS", 0.20, day=MON)]})
    await run(pool, ansaar, finance, MON)
    assert await open_problems(pool) == [("desk_plan_skipped", "plan")]

    # Tuesday, before the open: Monday now has a close, Tuesday has no bar yet.
    out = await run(pool, ansaar, finance, TUE)

    assert out.get("planned") == "orders"
    assert "desk_plan_skipped" not in out["findings"]
    assert await open_problems(pool) == []
