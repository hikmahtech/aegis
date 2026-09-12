"""The desk's rules and its checks before trading (spec §5, §12)."""

from __future__ import annotations

from datetime import date

from aegis.services.desk_math import Decision, Rules, check_decisions, last_trading_day


def d(symbol, weight, cls="equity", rank=1, halal="COMPLIANT", direction="LONG", state="NORMAL", kill=""):
    return Decision(symbol, cls, halal, direction, weight, rank, state, kill)


def test_rules_from_config_merges_over_the_defaults():
    r = Rules.from_config({"capital": 50000, "tax_rate": {"etf": 0.2}, "asset_classes": ["equity"]})
    assert r.capital == 50000.0
    assert r.tax_rate == {"equity": 0.20, "etf": 0.2}
    assert r.asset_classes == ("equity",)
    assert r.band_abs == 0.02 and r.mode == "paper" and r.benchmark == "SHARIABEES.NS"


def test_rules_from_no_config_are_the_defaults():
    assert Rules.from_config(None) == Rules()
    assert Rules.from_config({}) == Rules()


def test_last_trading_day_is_strictly_before_today():
    days = [date(2026, 9, 10), date(2026, 9, 11), date(2026, 9, 14)]
    assert last_trading_day(days, date(2026, 9, 14)) == date(2026, 9, 11)
    assert last_trading_day(days, date(2026, 9, 15)) == date(2026, 9, 14)
    assert last_trading_day(days, date(2026, 9, 10)) is None


def test_an_empty_day_is_stale():
    c = check_decisions([], set(), Rules())
    assert (c.outcome, c.rows, c.problems) == ("held_stale", (), ())


def test_an_empty_day_the_source_calls_halted_is_a_flatten():
    c = check_decisions([], set(), Rules(), halted=True)
    assert (c.outcome, c.rows, c.problems) == ("flatten", (), ())


def test_an_empty_day_is_only_a_flatten_when_the_source_says_halted():
    """The default is hold. A pipeline failure and a risk halt both write no
    rows, and dumping the portfolio over a failure is what §5 rules out."""
    assert check_decisions([], set(), Rules(), halted=False).outcome == "held_stale"


def test_a_day_with_rows_trades_its_rows_whatever_the_halt_flag_says():
    c = check_decisions([d("TCS", 0.1)], set(), Rules(), halted=True)
    assert c.outcome == "ok" and [r.symbol for r in c.rows] == ["TCS"]


def test_a_clean_day_passes_and_leaves_out_disabled_classes():
    rows = [d("TCS", 0.1), d("GOLDBEES", 0.1, cls="etf"), d("BTCUSDT", 0.1, cls="crypto")]
    c = check_decisions(rows, set(), Rules())
    assert c.outcome == "ok"
    assert [r.symbol for r in c.rows] == ["TCS", "GOLDBEES"]
    assert c.problems == ()


def test_a_non_compliant_row_is_dropped_and_the_rest_trades():
    rows = [d("TCS", 0.1), d("XYZ", 0.1, halal="NON_COMPLIANT")]
    c = check_decisions(rows, set(), Rules())
    assert c.outcome == "ok"
    assert [r.symbol for r in c.rows] == ["TCS"]
    assert len(c.problems) == 1 and "XYZ" in c.problems[0]


def test_a_short_row_is_dropped_too():
    c = check_decisions([d("TCS", 0.1, direction="SHORT")], set(), Rules())
    assert c.rows == () and "TCS" in c.problems[0]


def test_weights_over_the_whole_portfolio_are_suspect():
    c = check_decisions([d(f"S{i}", 0.2) for i in range(6)], set(), Rules())
    assert c.outcome == "held_suspect" and c.rows == ()
    assert "120.0%" in c.problems[0]


def test_a_weight_above_the_order_cap_is_suspect():
    c = check_decisions([d("TCS", 0.30)], set(), Rules())
    assert c.outcome == "held_suspect" and "TCS" in c.problems[0]


def test_a_zero_weight_is_suspect():
    assert check_decisions([d("TCS", 0.0)], set(), Rules()).outcome == "held_suspect"


def test_a_held_class_that_vanishes_without_a_reason_is_suspect():
    c = check_decisions([d("GOLDBEES", 0.1, cls="etf")], {"equity"}, Rules())
    assert c.outcome == "held_suspect" and "equity" in c.problems[0]


def test_a_vanished_class_is_fine_when_a_kill_switch_explains_it():
    rows = [d("GOLDBEES", 0.1, cls="etf", kill="VIX_HIGH")]
    assert check_decisions(rows, {"equity"}, Rules()).outcome == "ok"


def test_a_vanished_class_is_fine_in_a_recovery_state():
    rows = [d("GOLDBEES", 0.1, cls="etf", state="OBSERVATION")]
    assert check_decisions(rows, {"equity"}, Rules()).outcome == "ok"


def test_a_held_class_the_desk_no_longer_trades_is_not_suspect():
    rules = Rules.from_config({"asset_classes": ["equity"]})
    assert check_decisions([d("TCS", 0.1)], {"etf"}, rules).outcome == "ok"
