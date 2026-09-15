"""The trading desk's market is configuration, not code (spec §12).

Four things this file is here to prove, because each one has a way of going
wrong that nothing else would catch:

* a desk nobody has configured does nothing, rather than guessing a market and
  placing paper orders on someone else's holidays;
* the seeded example reproduces exactly the numbers the old hardcoded constants
  produced, so this was a configuration change and not a behaviour change;
* the three renamed keys are read under either name, so a live desk does not
  lose its sell charge or its exemption in the minutes between the deploy and
  the config write;
* migration 048 carries a real production row across, values intact.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio
from aegis.services import desk_math as dm
from aegis.services import desk_rules
from aegis.services import trading_desk as td

MIGRATION = Path(__file__).resolve().parents[3] / "migrations" / "048_desk_market_config.sql"

# The `trading-desk-daily` config as it stood in production on 2026-09-13,
# before any of this: the old key names, and not one of the market keys.
LIVE_ROW = {
    "mode": "paper",
    "capital": 100000,
    "band_abs": 0.02,
    "band_rel": 0.25,
    "tax_rate": {"etf": 0.3, "equity": 0.2},
    "benchmark": "SHARIABEES.NS",
    "ltcg_rate": 0.125,
    "asset_classes": ["equity", "etf"],
    "max_order_pct": 0.25,
    "sell_charge_inr": 16,
    "benchmark_prices": {"SHARIABEES.NS": {"symbol": "SHARIABEES", "asset_class": "etf"}},
    "context_benchmark": "^NSEI",
    "cost_pct_per_side": 0.002,
    "expected_excess_pa": 0.06,
    "ltcg_exemption_inr": 125000,
}

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
    """The real pool, with the desk's rows and its whole activities row restored.

    The WHOLE row, not just its config: one test here deletes it to prove a
    deployment with no desk says so, and an xdist worker's database is shared
    by every file that lands on it. Restoring only the config would leave the
    next file's `load_rules` reading a row that is not there."""
    for sql in _WIPE:
        await db_pool.execute(sql)
    original = await db_pool.fetchrow(
        "SELECT slug, workflow_type, agent_id, schedule_cron, config, active "
        "FROM activities WHERE slug = $1",
        td.DESK_SLUG,
    )
    yield db_pool
    for sql in _WIPE:
        await db_pool.execute(sql)
    if original is None:
        await db_pool.execute("DELETE FROM activities WHERE slug = $1", td.DESK_SLUG)
    else:
        await db_pool.execute(
            "INSERT INTO activities (slug, workflow_type, agent_id, schedule_cron, config, active) "
            "VALUES ($1, $2, $3, $4, $5, $6) ON CONFLICT (slug) DO UPDATE SET "
            "workflow_type = EXCLUDED.workflow_type, agent_id = EXCLUDED.agent_id, "
            "schedule_cron = EXCLUDED.schedule_cron, config = EXCLUDED.config, active = EXCLUDED.active",
            *original,
        )


async def set_config(pool: asyncpg.Pool, cfg: dict) -> None:
    await pool.execute(
        "INSERT INTO activities (slug, workflow_type, agent_id, schedule_cron, config, active) "
        "VALUES ($1, 'TradingDeskFlow', 'maou', '30 2 * * 1-5', $2, false) "
        "ON CONFLICT (slug) DO UPDATE SET config = EXCLUDED.config",
        td.DESK_SLUG, cfg,
    )


class NoFinance:
    """A price source that must never be asked anything."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def daily_bars(self, symbol, start, end):
        self.calls.append(symbol)
        raise AssertionError(f"the desk asked for {symbol} with no market configured")


class NoAnsaar:
    async def decisions(self, day):
        raise AssertionError("the desk asked ansaar for decisions with no market configured")

    async def prices(self, symbol, asset_class, start, end):
        raise AssertionError("the desk asked ansaar for prices with no market configured")


# --- an unconfigured desk does nothing ---------------------------------------


async def test_a_desk_with_no_calendar_symbol_trades_nothing_and_asks_nobody(pool):
    """No trading calendar means the desk cannot tell a market day from a
    holiday. It stops, silently, the way an empty `ansaar_url` stops it — and
    it must not fall back to anyone's exchange to find a day to trade."""
    await set_config(pool, {k: v for k, v in LIVE_ROW.items() if k != "calendar_symbol"})
    finance = NoFinance()

    out = await td.run_tick(pool, ansaar=NoAnsaar(), finance=finance, today=date(2026, 9, 14), project=False)

    assert out["skipped"] == "unconfigured"
    assert finance.calls == []
    assert await pool.fetchval("SELECT count(*) FROM finance.desk_plans") == 0
    assert await pool.fetchval("SELECT count(*) FROM finance.desk_prices") == 0


async def test_an_unconfigured_desk_raises_no_problem(pool):
    """Silence, not a finding: a fork that simply does not use the desk must not
    get a Todoist task every weekday morning telling it so."""
    await set_config(pool, {"mode": "paper"})

    out = await td.run_tick(pool, ansaar=NoAnsaar(), finance=NoFinance(), today=date(2026, 9, 14), project=False)

    assert out["findings"] == []
    assert await pool.fetchval(
        "SELECT count(*) FROM problems WHERE subject_kind = 'trading_desk' AND closed_at IS NULL"
    ) == 0


async def test_an_unconfigured_desk_has_no_month_to_score(pool):
    """`month_summary` reads the market's own days, so with no calendar there is
    nothing to score over — and it says None rather than raising."""
    await set_config(pool, {"mode": "paper"})
    assert await td.month_summary(pool, date(2026, 9, 1), date(2026, 10, 1)) is None


async def test_a_bad_mode_still_wins_over_an_unconfigured_market(pool):
    """The refusal to run a mode that is not built comes first: it is about what
    the desk would do, not about which market it would do it in."""
    await set_config(pool, {"mode": "live"})
    out = await td.run_tick(pool, ansaar=NoAnsaar(), finance=NoFinance(), today=date(2026, 9, 14), project=False)
    assert out["skipped"] == "mode"


# --- the seeded example is the old behaviour ---------------------------------

# What the code hardcoded before this change. The seeded example must still say
# exactly this, or the desk's numbers moved when nobody asked them to.
WAS_IN_CODE = {
    "calendar_symbol": "^NSEI",
    "market_tz": "Asia/Kolkata",
    "symbol_suffix": ".NS",
    "currency": "INR",
    "fy_start_month": 4,
    "stale_calendar_days": 6,
    "stale_price_days": 7,
    "capital": 100_000.0,
    "sell_charge": 16.0,
    "tax_rate": {"equity": 0.20, "etf": 0.30},
    "long_term_rate": 0.125,
    "long_term_exemption": 125_000.0,
    "long_term_exemption_classes": ("equity",),
    "benchmark": "SHARIABEES.NS",
    "context_benchmark": "^NSEI",
    "expected_excess_pa": 0.06,
}


@pytest.mark.parametrize(("field", "expected"), sorted(WAS_IN_CODE.items()))
def test_the_seeded_example_reproduces_the_old_hardcoded_values(seeded_desk_rules, field, expected):
    assert getattr(seeded_desk_rules, field) == expected


def test_the_code_defaults_name_no_market_and_no_tax_law():
    """The point of the whole change: a fork inherits nobody's exchange, nobody's
    currency and nobody's rates."""
    r = dm.Rules()
    assert not r.configured()
    assert (r.calendar_symbol, r.symbol_suffix, r.currency) == ("", "", "")
    assert (r.benchmark, r.context_benchmark) == ("", "")
    assert r.tax_rate == {} and r.long_term_rate == 0.0 and r.long_term_exemption == 0.0
    assert r.long_term_exemption_classes == ()
    assert r.capital == 0.0 and r.expected_excess_pa == 0.0
    assert r.market_tz == "UTC" and r.fy_start_month == 1


def test_an_unknown_timezone_falls_back_to_utc_rather_than_stopping_the_run():
    """Reading is lenient; the write path is what refuses a typo."""
    assert str(dm.Rules.from_config({"market_tz": "Mars/Olympus"}).tz()) == "UTC"
    assert str(dm.Rules.from_config({"market_tz": "Asia/Kolkata"}).tz()) == "Asia/Kolkata"


# --- the rename loses nothing ------------------------------------------------


def test_the_old_key_names_are_still_read():
    """A live row written before the rename keeps its sell charge and its
    exemption, because the deploy and the config write cannot be sequenced:
    `schedule_sync` re-reads this row every few minutes."""
    r = dm.Rules.from_config(LIVE_ROW)
    assert r.sell_charge == 16.0
    assert r.long_term_rate == 0.125
    assert r.long_term_exemption == 125_000.0


def test_the_new_key_name_wins_when_both_are_present():
    cfg = {"sell_charge": 20, "sell_charge_inr": 16, "ltcg_rate": 0.125, "long_term_rate": 0.2}
    r = dm.Rules.from_config(cfg)
    assert r.sell_charge == 20.0
    assert r.long_term_rate == 0.2


def test_neither_name_present_is_the_neutral_default():
    r = dm.Rules.from_config({"mode": "paper"})
    assert (r.sell_charge, r.long_term_rate, r.long_term_exemption) == (0.0, 0.0, 0.0)


def test_the_retired_key_names_are_named_so_the_deprecation_is_visible():
    assert dm.legacy_keys(LIVE_ROW) == ["ltcg_exemption_inr", "ltcg_rate", "sell_charge_inr"]
    assert dm.legacy_keys({"sell_charge": 16, "sell_charge_inr": 99}) == []
    assert dm.legacy_keys(None) == []


async def test_a_live_row_still_taxes_exactly_as_it_did(pool):
    """The whole change, measured on the arithmetic rather than on key names:
    the same gains, the same tax, off the production row carried across.

    The exemption's asset classes are the one setting with no old key to fall
    back on — `asset_class == "equity"` was a literal in the code, so only the
    migration can state it. It runs at Core startup, and until it does the desk
    reports MORE tax owed than it should, never less, and places no different
    order: `tax_if_sold_today` is a figure on a page, not a trading input."""
    await set_config(pool, LIVE_ROW)
    await pool.execute(MIGRATION.read_text())

    rules = await td.load_rules(pool)
    rows = [dm.Realised(date(2026, 5, 1), "S", "equity", 200_000.0, True)]
    assert dm.tax_owed(rows, rules) == pytest.approx(0.125 * 75_000)
    # And the trade-affecting settings need no migration at all: the old names
    # are read directly, so a tick between the deploy and the migration sizes
    # and charges exactly as it did yesterday.
    assert dm.Rules.from_config(LIVE_ROW).sell_charge == 16.0


# --- migration 048 carries the live row across -------------------------------


async def test_the_migration_keeps_a_live_rows_settings_and_fills_its_market(pool):
    await set_config(pool, LIVE_ROW)

    await pool.execute(MIGRATION.read_text())

    cfg = await pool.fetchval("SELECT config FROM activities WHERE slug = $1", td.DESK_SLUG)
    # Nothing was lost: the three renamed values moved to their new names.
    assert cfg["sell_charge"] == 16
    assert cfg["long_term_rate"] == 0.125
    assert cfg["long_term_exemption"] == 125000
    assert not any(key in cfg for key in dm.RENAMED.values())
    # The market the desk was already trading is now written down.
    assert cfg["calendar_symbol"] == "^NSEI"
    assert cfg["market_tz"] == "Asia/Kolkata"
    assert cfg["symbol_suffix"] == ".NS"
    assert cfg["fy_start_month"] == 4
    assert cfg["long_term_exemption_classes"] == ["equity"]
    # And the keys it never touched are untouched.
    assert cfg["benchmark_prices"] == LIVE_ROW["benchmark_prices"]
    assert cfg["max_order_pct"] == 0.25


async def test_the_migration_is_safe_to_run_twice(pool):
    await set_config(pool, LIVE_ROW)
    await pool.execute(MIGRATION.read_text())
    once = await pool.fetchval("SELECT config FROM activities WHERE slug = $1", td.DESK_SLUG)
    await pool.execute(MIGRATION.read_text())
    assert await pool.fetchval("SELECT config FROM activities WHERE slug = $1", td.DESK_SLUG) == once


async def test_the_migration_does_not_overwrite_a_market_someone_configured(pool):
    """A fork that has already said which market it trades keeps it."""
    await set_config(pool, LIVE_ROW | {"calendar_symbol": "^GSPC", "symbol_suffix": ""})

    await pool.execute(MIGRATION.read_text())

    cfg = await pool.fetchval("SELECT config FROM activities WHERE slug = $1", td.DESK_SLUG)
    assert cfg["calendar_symbol"] == "^GSPC"
    assert cfg["symbol_suffix"] == ""
    # The keys it had not set are still filled in.
    assert cfg["market_tz"] == "Asia/Kolkata"


# --- the write path checks before it stores ----------------------------------


def test_an_empty_calendar_symbol_saves_because_it_is_a_real_answer():
    assert desk_rules.validate({"calendar_symbol": "", "fy_start_month": 1})["calendar_symbol"] == ""


@pytest.mark.parametrize(
    ("body", "says"),
    [
        ({"market_tz": "Mars/Olympus"}, "timezone"),
        ({"fy_start_month": 13}, "between 1 and 12"),
        ({"fy_start_month": 0}, "between 1 and 12"),
        ({"stale_calendar_days": 0}, "between 1 and 60"),
        ({"currency": "rupees"}, "three-letter"),
        ({"calendar_symbol": "^NSEI ^BSESN"}, "no spaces"),
        ({"tax_rate": {"equity": 1.5}}, "between 0 and 1"),
        ({"tax_rate": {"equity": "lots"}}, "must be a number"),
        ({"long_term_rate": 2}, "between 0 and 1"),
        ({"long_term_exemption_classes": ["equity", ""]}, "empty asset class"),
        ({"fill_at": "opening"}, "must be one of"),
        ({"fill_at": "vwap"}, "must be one of"),
    ],
)
def test_a_setting_that_would_not_work_is_refused(body, says):
    """Loudly, at the write boundary. The read path stays forgiving, so a bad
    value can never stop the desk running — which is exactly why it must never
    be stored in the first place."""
    with pytest.raises(ValueError, match=says):
        desk_rules.validate({"fy_start_month": 1, "stale_calendar_days": 6, "stale_price_days": 7} | body)


async def test_saving_keeps_the_knobs_the_page_does_not_show(pool):
    """A merge, not a replacement: the trading bands and the benchmark price
    mappings are not on this page and must survive a save of the timezone."""
    await set_config(pool, LIVE_ROW)

    out = await desk_rules.save(
        pool,
        {
            "calendar_symbol": "^NSEI", "market_tz": "Asia/Kolkata", "symbol_suffix": ".NS",
            "currency": "INR", "fy_start_month": 4, "stale_calendar_days": 6, "stale_price_days": 7,
            "capital": 100000, "sell_charge": 16, "tax_rate": {"equity": 0.2, "etf": 0.3},
            "long_term_rate": 0.125, "long_term_exemption": 125000,
            "long_term_exemption_classes": ["equity"], "benchmark": "SHARIABEES.NS",
            "context_benchmark": "^NSEI", "expected_excess_pa": 0.06,
        },
    )

    cfg = await pool.fetchval("SELECT config FROM activities WHERE slug = $1", td.DESK_SLUG)
    assert cfg["benchmark_prices"] == LIVE_ROW["benchmark_prices"]
    assert cfg["band_abs"] == 0.02 and cfg["max_order_pct"] == 0.25
    # And the save is the cleanup: the retired names are gone.
    assert not any(key in cfg for key in dm.RENAMED.values())
    assert out["retired_keys"] == []
    assert out["configured"] is True


async def test_saving_a_desk_with_no_row_says_so_rather_than_inventing_one(pool):
    await pool.execute("DELETE FROM activities WHERE slug = $1", td.DESK_SLUG)
    with pytest.raises(LookupError):
        await desk_rules.save(pool, {"fy_start_month": 1, "stale_calendar_days": 6, "stale_price_days": 7})


# --- capital cannot be edited out from under a filled book (#526) ------------

SAVE = {
    "calendar_symbol": "^NSEI", "market_tz": "Asia/Kolkata", "symbol_suffix": ".NS",
    "currency": "INR", "fy_start_month": 4, "stale_calendar_days": 6, "stale_price_days": 7,
    "capital": 100000, "sell_charge": 16, "tax_rate": {"equity": 0.2, "etf": 0.3},
    "long_term_rate": 0.125, "long_term_exemption": 125000,
    "long_term_exemption_classes": ["equity"], "benchmark": "SHARIABEES.NS",
    "context_benchmark": "^NSEI", "expected_excess_pa": 0.06,
}


async def orders(pool, *statuses: str) -> None:
    """One order per status, on one plan day. More than one, because a guard
    that looked at `any order` rather than `any FILLED order` would pass on a
    book that holds only a pending one."""
    day = date(2026, 9, 11)
    await pool.execute(
        "INSERT INTO finance.desk_plans (data_date, mode, outcome) VALUES ($1, 'paper', 'orders')", day
    )
    for seq, status in enumerate(statuses):
        await pool.execute(
            "INSERT INTO finance.desk_orders (mode, data_date, seq, created_day, symbol, asset_class, "
            "side, qty, ref_price, status, fill_date, fill_price, costs) "
            "VALUES ('paper', $1, $2, $1, 'TCS', 'equity', 'buy', 3, 1000, $3, $1, 1000, 6)",
            day, seq, status,
        )


async def test_capital_cannot_be_changed_once_an_order_has_filled(pool):
    """`replay` starts the book from this number on every past day, so a new one
    restates the whole history — cash, value and every weekly gap. There is no
    deposits table yet, so the honest answer is to refuse (#526)."""
    await set_config(pool, LIVE_ROW)
    await orders(pool, "pending", "filled")

    with pytest.raises(ValueError, match="restate every past day"):
        await desk_rules.save(pool, SAVE | {"capital": 250000})

    # Nothing at all was written, not even the settings that were fine.
    assert (await td.load_rules(pool)).capital == 100_000.0
    assert await pool.fetchval("SELECT config->>'market_tz' FROM activities WHERE slug = $1", td.DESK_SLUG) is None


async def test_everything_else_still_saves_while_capital_is_locked(pool):
    """The lock is on one number, not on the page."""
    await set_config(pool, LIVE_ROW)
    await orders(pool, "pending", "filled")

    out = await desk_rules.save(pool, SAVE | {"market_tz": "America/New_York", "expected_excess_pa": 0.04})

    assert out["capital_locked"] is True
    rules = await td.load_rules(pool)
    assert (rules.market_tz, rules.expected_excess_pa, rules.capital) == ("America/New_York", 0.04, 100_000.0)


async def test_capital_is_editable_while_nothing_has_filled(pool):
    """A paper book with no history has nothing to restate, and a fork setting
    the desk up for the first time must be able to say what it is starting with."""
    await set_config(pool, LIVE_ROW)
    await orders(pool, "pending", "cancelled")

    out = await desk_rules.save(pool, SAVE | {"capital": 250000})

    assert out["capital_locked"] is False
    assert (await td.load_rules(pool)).capital == 250_000.0


async def test_the_page_is_told_whether_capital_is_still_editable(pool):
    """So the form greys the field rather than letting someone type a number and
    meet a 400 they could not have predicted."""
    await set_config(pool, LIVE_ROW)
    assert (await desk_rules.read(pool))["capital_locked"] is False

    await orders(pool, "filled")

    assert (await desk_rules.read(pool))["capital_locked"] is True


# --- which print the desk fills at -------------------------------------------


@pytest.mark.parametrize("value, stored", [("open", "open"), ("close", "close"), ("OPEN", "open")])
def test_fill_at_saves_either_print(value, stored):
    out = desk_rules.validate(
        {"fy_start_month": 1, "stale_calendar_days": 6, "stale_price_days": 7, "fill_at": value}
    )
    assert out["fill_at"] == stored


def test_a_form_that_omits_fill_at_leaves_it_alone():
    """A field the form did not send means "leave it alone", not "clear it" —
    the same rule the day counts follow, so a partial body saves rather than
    400s."""
    out = desk_rules.validate({"fy_start_month": 1, "stale_calendar_days": 6, "stale_price_days": 7})
    assert out["fill_at"] == dm.Rules().fill_at == "close"


def test_the_read_path_stays_forgiving_where_the_write_path_is_strict():
    """A junk value that somehow reached the stored row is read as the close, so
    the desk keeps trading. That leniency is exactly why the write boundary
    above has to refuse it — otherwise a typo saves with a 200 and then quietly
    does something other than what the form said, for ever."""
    junk = dm.Rules.from_config({"fill_at": "opening"})
    assert junk.fill_at == "opening"
    assert dm.fill_price_on([dm.Bar(date(2026, 9, 14), 100.0, open=90.0)], date(2026, 9, 14), junk) == (
        100.0,
        "close",
    )
