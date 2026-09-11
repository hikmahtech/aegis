"""Sizing orders (spec §4) and paper fills (spec §6)."""

from __future__ import annotations

from datetime import date

import pytest
from aegis.services.desk_math import (
    Bar,
    Book,
    Decision,
    Lot,
    PendingOrder,
    Rules,
    fill_orders,
    plan_orders,
)


def d(symbol, weight, cls="equity", rank=1):
    return Decision(symbol, cls, "COMPLIANT", "LONG", weight, rank)


def held(symbol, qty, cost, cls="equity", cash=0.0):
    book = Book(cash=cash)
    book.lots[symbol] = [Lot(float(qty), cost, date(2026, 9, 1))]
    book.classes[symbol] = cls
    return book


def test_first_plan_buys_whole_shares_and_skips_a_share_too_dear():
    rows = (d("TCS", 0.10, rank=1), d("INFY", 0.10, rank=2), d("MRF", 0.10, rank=3))
    closes = {"TCS": 3000.0, "INFY": 1500.0, "MRF": 130_000.0}
    orders, skipped = plan_orders(rows, Book(cash=100_000.0), closes, Rules())
    assert [(o.symbol, o.side, o.qty, o.ref_price) for o in orders] == [
        ("TCS", "buy", 3, 3000.0),
        ("INFY", "buy", 6, 1500.0),
    ]
    assert skipped == ["MRF: below_one_share"]


def test_a_small_gap_inside_the_band_is_not_traded():
    book = held("TCS", 3, 3000.0, cash=91_000.0)
    assert plan_orders((d("TCS", 0.10),), book, {"TCS": 3000.0}, Rules()) == ([], [])


def test_a_gap_beyond_the_band_tops_up():
    book = held("TCS", 3, 3000.0, cash=91_000.0)
    orders, _ = plan_orders((d("TCS", 0.20),), book, {"TCS": 3000.0}, Rules())
    assert [(o.side, o.qty) for o in orders] == [("buy", 3)]


def test_names_not_in_the_decisions_are_sold_in_full_and_overweights_trimmed():
    book = held("TCS", 10, 3000.0, cash=40_000.0)
    book.lots["INFY"] = [Lot(20.0, 1500.0, date(2026, 9, 1))]
    book.classes["INFY"] = "equity"
    orders, _ = plan_orders((d("TCS", 0.10),), book, {"TCS": 3000.0, "INFY": 1500.0}, Rules())
    assert [(o.symbol, o.side, o.qty) for o in orders] == [("INFY", "sell", 20), ("TCS", "sell", 7)]


def test_buys_go_in_rank_order_and_the_last_one_runs_out_of_cash():
    rows = tuple(d(f"S{i}", 0.10, rank=i) for i in range(10, 0, -1))
    closes = {f"S{i}": 1000.0 for i in range(1, 11)}
    orders, skipped = plan_orders(rows, Book(cash=10_000.0), closes, Rules())
    assert [o.symbol for o in orders] == [f"S{i}" for i in range(1, 10)]
    assert skipped == ["S10: no_cash"]


def test_rank_ties_go_to_the_larger_weight_then_the_symbol():
    rows = (d("B", 0.05, cls="etf"), d("A", 0.05), d("C", 0.10))
    orders, _ = plan_orders(rows, Book(cash=100_000.0), {"A": 100.0, "B": 100.0, "C": 100.0}, Rules())
    assert [o.symbol for o in orders] == ["C", "A", "B"]


def test_a_decided_name_with_no_price_is_skipped():
    assert plan_orders((d("NEW", 0.1),), Book(cash=1000.0), {}, Rules()) == ([], ["NEW: no_price"])


def test_a_held_name_with_no_price_is_kept_and_reported():
    book = held("OLD", 5, 100.0, cash=1000.0)
    assert plan_orders((), book, {}, Rules()) == ([], ["OLD: no_price"])


DAYS = [date(2026, 9, 14), date(2026, 9, 15), date(2026, 9, 16), date(2026, 9, 17), date(2026, 9, 18)]


def p(order_id, symbol, side, qty, created=date(2026, 9, 14), data=date(2026, 9, 11), seq=0):
    return PendingOrder(order_id, symbol, "equity", side, qty, created, data, seq)


def test_a_buy_fills_at_the_close_of_its_fill_day_with_costs():
    bars = {"TCS": [Bar(date(2026, 9, 14), 3100.0)]}
    [r] = fill_orders([p("o1", "TCS", "buy", 3)], bars, DAYS, Book(cash=100_000.0), Rules())
    assert (r.status, r.fill_day, r.qty, r.price, r.source) == ("filled", date(2026, 9, 14), 3, 3100.0, "yahoo")
    assert r.costs == pytest.approx(18.6)


def test_an_order_created_on_a_holiday_fills_on_the_next_market_day():
    bars = {"TCS": [Bar(date(2026, 9, 14), 3100.0)]}
    [r] = fill_orders([p("o1", "TCS", "buy", 1, created=date(2026, 9, 13))], bars, DAYS, Book(cash=10_000.0), Rules())
    assert r.fill_day == date(2026, 9, 14)


def test_no_price_yet_stays_pending_then_cancels_after_three_market_days():
    """Two market days after the fill day it waits; the third cancels it."""
    order = p("o1", "TCS", "buy", 1)
    [r] = fill_orders([order], {}, DAYS[:3], Book(cash=10_000.0), Rules())
    assert r.status == "pending"
    [r] = fill_orders([order], {}, DAYS[:4], Book(cash=10_000.0), Rules())
    assert (r.status, r.reason) == ("cancelled", "price_missing")


def test_the_cancel_grace_is_a_parameter():
    [r] = fill_orders([p("o1", "TCS", "buy", 1)], {}, DAYS[:2], Book(cash=10.0), Rules(), grace_days=1)
    assert (r.status, r.reason) == ("cancelled", "price_missing")


def test_no_market_day_yet_stays_pending():
    [r] = fill_orders([p("o1", "TCS", "buy", 1, created=date(2026, 9, 19))], {}, DAYS, Book(cash=10.0), Rules())
    assert r.status == "pending"


def test_sells_fill_first_and_a_buy_is_cut_to_the_cash_left():
    book = held("X", 10, 100.0, cash=1000.0)
    bars = {"X": [Bar(date(2026, 9, 14), 100.0)], "Y": [Bar(date(2026, 9, 14), 100.0)]}
    pending = [p("buy", "Y", "buy", 20, seq=1), p("sell", "X", "sell", 10, seq=0)]
    res = {r.order_id: r for r in fill_orders(pending, bars, DAYS, book, Rules())}
    assert res["sell"].costs == pytest.approx(18.0)
    assert (res["buy"].status, res["buy"].qty) == ("filled", 19)


def test_a_buy_with_no_cash_left_is_cancelled():
    bars = {"Y": [Bar(date(2026, 9, 14), 100.0)]}
    [r] = fill_orders([p("o", "Y", "buy", 1)], bars, DAYS, Book(cash=50.0), Rules())
    assert (r.status, r.reason) == ("cancelled", "no_cash")


def test_a_split_between_sizing_and_fill_scales_the_quantity():
    bars = {"TCS": [Bar(date(2026, 9, 14), 1550.0, split_ratio=2.0)]}
    [r] = fill_orders([p("o", "TCS", "buy", 3)], bars, DAYS, Book(cash=100_000.0), Rules())
    assert r.qty == 6


def test_the_fill_records_where_its_price_came_from():
    bars = {"TCS": [Bar(date(2026, 9, 14), 3100.0, source="ansaar")]}
    [r] = fill_orders([p("o", "TCS", "buy", 1)], bars, DAYS, Book(cash=10_000.0), Rules())
    assert r.source == "ansaar"


def test_a_sell_never_exceeds_what_is_held():
    bars = {"X": [Bar(date(2026, 9, 14), 100.0)]}
    [r] = fill_orders([p("o", "X", "sell", 8)], bars, DAYS, held("X", 5, 100.0), Rules())
    assert r.qty == 5


def test_a_sell_of_nothing_held_is_cancelled():
    bars = {"X": [Bar(date(2026, 9, 14), 100.0)]}
    [r] = fill_orders([p("o", "X", "sell", 1)], bars, DAYS, Book(cash=0.0), Rules())
    assert (r.status, r.reason) == ("cancelled", "nothing_held")
