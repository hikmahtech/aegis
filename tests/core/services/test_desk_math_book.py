"""Replaying the desk's fills into a book (spec §6, §7)."""

from __future__ import annotations

from datetime import date

import pytest
from aegis.services.desk_math import (
    Bar,
    Book,
    Fill,
    Lot,
    Realised,
    Rules,
    close_on,
    fy,
    replay,
    tax_owed,
    value,
)


def test_buy_then_partial_sell_books_cash_lots_and_a_short_term_gain():
    fills = [
        Fill("TCS", "equity", "buy", 10, 1000.0, 20.0, date(2026, 9, 15)),
        Fill("TCS", "equity", "sell", 4, 1100.0, 24.8, date(2026, 9, 22)),
    ]
    book = replay(fills, {}, 100_000.0, date(2026, 9, 30))
    assert book.cash == pytest.approx(94_355.2)
    assert book.qty("TCS") == pytest.approx(6)
    [r] = book.realised
    assert r.gain == pytest.approx(367.2) and not r.long_term
    assert book.held_classes() == {"equity"}


def test_fills_after_upto_are_ignored():
    fills = [Fill("TCS", "equity", "buy", 1, 100.0, 0.0, date(2026, 9, 15))]
    assert replay(fills, {}, 1000.0, date(2026, 9, 14)).cash == 1000.0


def test_fifo_takes_the_oldest_lot_first_and_splits_short_from_long():
    fills = [
        Fill("A", "equity", "buy", 10, 100.0, 0.0, date(2025, 1, 10)),
        Fill("A", "equity", "buy", 10, 200.0, 0.0, date(2025, 6, 10)),
        Fill("A", "equity", "sell", 15, 300.0, 0.0, date(2026, 1, 15)),
    ]
    book = replay(fills, {}, 10_000.0, date(2026, 1, 31))
    assert [(r.gain, r.long_term) for r in book.realised] == [(2000.0, True), (500.0, False)]
    assert book.qty("A") == 5


def test_held_exactly_twelve_months_is_still_short_term():
    fills = [
        Fill("A", "equity", "buy", 1, 100.0, 0.0, date(2025, 3, 1)),
        Fill("A", "equity", "sell", 1, 150.0, 0.0, date(2026, 3, 1)),
    ]
    assert not replay(fills, {}, 1000.0, date(2026, 3, 2)).realised[0].long_term


def test_a_split_doubles_the_quantity_and_halves_the_cost():
    bars = {"B": [Bar(date(2026, 9, 20), 510.0, split_ratio=2.0)]}
    fills = [
        Fill("B", "equity", "buy", 10, 1000.0, 0.0, date(2026, 9, 15)),
        Fill("B", "equity", "sell", 20, 520.0, 0.0, date(2026, 9, 25)),
    ]
    before = replay(fills[:1], bars, 20_000.0, date(2026, 9, 21))
    assert before.qty("B") == 20 and before.lots["B"][0].cost == 500.0
    after = replay(fills, bars, 20_000.0, date(2026, 9, 30))
    assert after.realised[0].gain == pytest.approx(400.0)
    assert after.qty("B") == 0 and "B" not in after.lots


def test_a_fractional_split_entitlement_is_paid_in_cash():
    bars = {"C": [Bar(date(2026, 9, 20), 100.0, split_ratio=1.5)]}
    fills = [Fill("C", "equity", "buy", 5, 150.0, 0.0, date(2026, 9, 15))]
    book = replay(fills, bars, 1000.0, date(2026, 9, 21))
    assert book.qty("C") == 7
    assert book.cash == pytest.approx(1000.0 - 750.0 + 0.5 * 100.0)


def test_a_dividend_is_paid_on_the_ex_date_to_the_shares_held():
    bars = {"D": [Bar(date(2026, 9, 18), 100.0, dividend=5.0)]}
    fills = [Fill("D", "equity", "buy", 10, 100.0, 0.0, date(2026, 9, 15))]
    book = replay(fills, bars, 2000.0, date(2026, 9, 30))
    assert book.cash == pytest.approx(2000.0 - 1000.0 + 50.0)
    assert book.dividends == pytest.approx(50.0)


def test_shares_bought_on_the_ex_date_get_no_dividend():
    bars = {"D": [Bar(date(2026, 9, 18), 100.0, dividend=5.0)]}
    fills = [Fill("D", "equity", "buy", 10, 100.0, 0.0, date(2026, 9, 18))]
    assert replay(fills, bars, 2000.0, date(2026, 9, 30)).dividends == 0.0


def test_close_on_takes_the_last_known_close():
    series = [Bar(date(2026, 9, 15), 1000.0), Bar(date(2026, 9, 16), None), Bar(date(2026, 9, 18), 1050.0)]
    assert close_on(series, date(2026, 9, 14)) is None
    assert close_on(series, date(2026, 9, 16)) == 1000.0
    assert close_on(series, date(2026, 9, 17)) == 1000.0
    assert close_on(series, date(2026, 9, 18)) == 1050.0


def test_value_marks_holdings_at_the_last_close_on_or_before_the_day():
    bars = {"TCS": [Bar(date(2026, 9, 15), 1000.0), Bar(date(2026, 9, 16), None), Bar(date(2026, 9, 17), 1050.0)]}
    fills = [Fill("TCS", "equity", "buy", 10, 1000.0, 0.0, date(2026, 9, 15))]
    book = replay(fills, bars, 20_000.0, date(2026, 9, 16))
    assert value(book, bars, date(2026, 9, 16)) == pytest.approx(20_000.0)
    assert value(book, bars, date(2026, 9, 17)) == pytest.approx(10_000.0 + 10_500.0)


def test_value_falls_back_to_cost_when_a_holding_has_no_price():
    book = replay([Fill("X", "equity", "buy", 2, 500.0, 10.0, date(2026, 9, 15))], {}, 5000.0, date(2026, 9, 16))
    assert value(book, {}, date(2026, 9, 16)) == pytest.approx(5000.0)


def test_book_helpers():
    book = Book(cash=0.0, lots={"A": [Lot(2, 100.0, date(2026, 1, 1)), Lot(2, 200.0, date(2026, 2, 1))]}, classes={"A": "etf"})
    assert book.avg_cost("A") == 150.0 and book.held() == {"A": 4} and book.held_classes() == {"etf"}
    assert book.avg_cost("NONE") == 0.0


def test_financial_year_runs_april_to_march():
    assert fy(date(2026, 3, 31)) == 2025
    assert fy(date(2026, 4, 1)) == 2026


def _r(day, gain, cls="equity", lt=False):
    return Realised(day, "S", cls, gain, lt)


def test_tax_nets_short_term_gains_per_class_within_a_year():
    rows = [_r(date(2026, 5, 1), 1000.0), _r(date(2026, 6, 1), -400.0), _r(date(2026, 7, 1), 500.0, cls="etf")]
    assert tax_owed(rows, Rules()) == pytest.approx(0.20 * 600 + 0.30 * 500)


def test_tax_does_not_net_across_the_31_march_boundary():
    rows = [_r(date(2026, 3, 31), 1000.0), _r(date(2026, 4, 1), -1000.0)]
    assert tax_owed(rows, Rules()) == pytest.approx(200.0)


def test_long_term_equity_gains_are_taxed_only_above_the_exemption():
    assert tax_owed([_r(date(2026, 5, 1), 200_000.0, lt=True)], Rules()) == pytest.approx(0.125 * 75_000)


def test_a_long_term_etf_gain_gets_no_exemption():
    """Section 112A's ₹1.25L covers listed equity and equity-oriented units. A
    gold or silver ETF is neither."""
    rows = [_r(date(2026, 5, 1), 200_000.0, cls="etf", lt=True)]
    assert tax_owed(rows, Rules()) == pytest.approx(0.125 * 200_000)


def test_a_same_day_sell_and_buy_both_land():
    fills = [
        Fill("A", "equity", "buy", 10, 100.0, 0.0, date(2026, 9, 14)),
        Fill("B", "equity", "buy", 5, 100.0, 0.0, date(2026, 9, 21)),
        Fill("A", "equity", "sell", 10, 110.0, 0.0, date(2026, 9, 21)),
    ]
    book = replay(fills, {}, 1000.0, date(2026, 9, 30))
    assert book.cash == pytest.approx(1000.0 - 1000.0 + 1100.0 - 500.0)
    assert book.qty("B") == 5 and "A" not in book.lots


def test_a_loss_year_owes_nothing():
    assert tax_owed([_r(date(2026, 5, 1), -500.0)], Rules()) == 0.0


def test_an_unknown_class_pays_the_highest_configured_rate():
    assert tax_owed([_r(date(2026, 5, 1), 100.0, cls="crypto")], Rules()) == pytest.approx(30.0)
