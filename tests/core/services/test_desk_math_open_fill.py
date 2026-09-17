"""Which print a fill gets, and the fallback behind it (spec §6).

`test_desk_math_orders.py` covers the arithmetic under the code default,
`fill_at: "close"`. This covers the other setting, and the rule that a value
which is neither degrades to the close rather than refusing to trade.
"""

from __future__ import annotations

from datetime import date

from aegis.services.desk_math import (
    Bar,
    Book,
    Rules,
    benchmark_values,
    fill_orders,
    fill_price_on,
    open_on,
)

from .test_desk_math_orders import DAYS, p

MON = date(2026, 9, 14)
OPEN_RULES = Rules(fill_at="open")


def test_the_default_is_the_close_so_an_unconfigured_desk_is_unchanged():
    assert Rules().fill_at == "close"


def test_a_buy_fills_at_the_open_of_its_fill_day():
    bars = {"TCS": [Bar(MON, 3100.0, open=3010.0)]}
    res = fill_orders([p("o", "TCS", "buy", 2)], bars, DAYS, Book(cash=10_000.0), OPEN_RULES)
    assert (res[0].status, res[0].price, res[0].kind) == ("filled", 3010.0, "open")


def test_the_close_is_used_when_that_day_has_no_open():
    """An order the desk meant to place is not dropped because one source never
    published one field."""
    bars = {"TCS": [Bar(MON, 3100.0)]}
    res = fill_orders([p("o", "TCS", "buy", 2)], bars, DAYS, Book(cash=10_000.0), OPEN_RULES)
    assert (res[0].status, res[0].price, res[0].kind) == ("filled", 3100.0, "close")


def test_a_junk_fill_at_behaves_as_the_close():
    """`Rules.from_config` validates nothing by design — the strict check is at
    the write boundary. So a typo that reached the stored row must degrade to
    the old behaviour, not stop the desk trading."""
    bars = {"TCS": [Bar(MON, 3100.0, open=3010.0)]}
    res = fill_orders([p("o", "TCS", "buy", 2)], bars, DAYS, Book(cash=10_000.0), Rules(fill_at="OPENING"))
    assert (res[0].price, res[0].kind) == (3100.0, "close")


def test_a_day_with_neither_price_still_leaves_the_order_pending():
    bars = {"TCS": [Bar(MON, None)]}
    res = fill_orders([p("o", "TCS", "buy", 2)], bars, DAYS[:1], Book(cash=10_000.0), OPEN_RULES)
    assert (res[0].status, res[0].kind) == ("pending", "")


def test_costs_are_charged_on_the_price_actually_paid():
    bars = {"TCS": [Bar(MON, 5000.0, open=1000.0)]}
    res = fill_orders([p("o", "TCS", "buy", 2)], bars, DAYS, Book(cash=10_000.0), OPEN_RULES)
    assert res[0].costs == 2 * 1000.0 * OPEN_RULES.cost_pct_per_side


def test_affordability_is_judged_on_the_open_too():
    """Sizing used the previous close; the open is what the cash actually has to
    cover, so a gap up cuts the order rather than overdrawing the book."""
    bars = {"TCS": [Bar(MON, 100.0, open=1000.0)]}
    res = fill_orders([p("o", "TCS", "buy", 5)], bars, DAYS, Book(cash=2_500.0), OPEN_RULES)
    assert (res[0].status, res[0].qty) == ("filled", 2)


# --- open_on ------------------------------------------------------------------


def test_an_open_never_carries_forward():
    """A close carries forward because it is the best mark available for a day
    the market did not price. An open is a statement about one session's first
    trade, and carrying yesterday's into today would fill an order at a price
    from a day that has already closed."""
    series = [Bar(MON, 3100.0, open=3010.0)]
    assert open_on(series, MON) == 3010.0
    assert open_on(series, date(2026, 9, 15)) is None


def test_fill_price_on_a_day_with_no_bar_at_all():
    assert fill_price_on([], MON, OPEN_RULES) == (None, "")


# --- the benchmark ------------------------------------------------------------


def test_the_benchmark_enters_on_the_same_print_as_the_desk():
    """Both sides must enter alike, or the headline gap carries a fixed slice of
    one session's move that has nothing to do with the picking."""
    series = [Bar(MON, 200.0, open=100.0)]
    (_, value), = benchmark_values(series, capital=1000.0, cost_pct=0.0, days=[MON])
    assert value == 2000.0  # 10 units bought at the open, marked at the close


def test_the_benchmark_falls_back_to_the_close_like_the_desk():
    series = [Bar(MON, 200.0)]
    (_, value), = benchmark_values(series, capital=1000.0, cost_pct=0.0, days=[MON])
    assert value == 1000.0
