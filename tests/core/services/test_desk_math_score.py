"""Scoring the desk (spec §8)."""

from __future__ import annotations

from datetime import date

import pytest
from aegis.services.desk_math import (
    Bar,
    Fill,
    Stats,
    below_expectation,
    benchmark_values,
    big_moves,
    desk_series,
    desk_values,
    label,
    stats,
    weekly_excess,
    weekly_shares,
)


def test_desk_values_mark_the_book_each_day():
    bars = {"TCS": [Bar(date(2026, 9, 14), 1000.0), Bar(date(2026, 9, 15), 1100.0)]}
    fills = [Fill("TCS", "equity", "buy", 5, 1000.0, 10.0, date(2026, 9, 14))]
    out = desk_values(fills, bars, 10_000.0, [date(2026, 9, 14), date(2026, 9, 15)])
    assert [v for _, v in out] == pytest.approx([9_990.0, 10_490.0])


def test_benchmark_holds_units_bought_at_the_start_with_one_buy_cost():
    series = [Bar(date(2026, 9, 14), 100.0), Bar(date(2026, 9, 15), 110.0)]
    out = benchmark_values(series, 10_000.0, 0.002, [date(2026, 9, 14), date(2026, 9, 15)])
    assert [v for _, v in out] == pytest.approx([9_980.0, 9_980.0 * 1.10])


def test_benchmark_applies_splits_to_units_and_pays_dividends_as_cash():
    series = [
        Bar(date(2026, 9, 14), 100.0),
        Bar(date(2026, 9, 15), 50.0, split_ratio=2.0),
        Bar(date(2026, 9, 16), 50.0, dividend=1.0),
    ]
    days = [date(2026, 9, 14), date(2026, 9, 15), date(2026, 9, 16)]
    assert [v for _, v in benchmark_values(series, 1000.0, 0.0, days)] == pytest.approx([1000.0, 1000.0, 1020.0])


def test_benchmark_with_no_starting_price_is_empty():
    assert benchmark_values([], 1000.0, 0.0, [date(2026, 9, 14)]) == []
    assert benchmark_values([Bar(date(2026, 9, 14), 1.0)], 1000.0, 0.0, []) == []


def test_weekly_excess_compares_week_end_values():
    desk = [(date(2026, 9, 10), 99.0), (date(2026, 9, 11), 100.0), (date(2026, 9, 18), 102.0), (date(2026, 9, 25), 101.0)]
    bench = [(date(2026, 9, 11), 100.0), (date(2026, 9, 18), 101.0), (date(2026, 9, 25), 101.0)]
    assert weekly_excess(desk, bench) == pytest.approx([0.01, 101 / 102 - 1])


# --- the alarm measures selection, not cash drag (spec §8) -------------------

FRI1, FRI2, FRI3 = date(2026, 9, 11), date(2026, 9, 18), date(2026, 9, 25)


def test_desk_series_reports_the_invested_share():
    """5 shares at 1000 out of 10,000 capital is half the book invested; the
    next day they are worth 1100 each."""
    bars = {"TCS": [Bar(date(2026, 9, 14), 1000.0), Bar(date(2026, 9, 15), 1100.0)]}
    fills = [Fill("TCS", "equity", "buy", 5, 1000.0, 10.0, date(2026, 9, 14))]
    out = desk_series(fills, bars, 10_000.0, [date(2026, 9, 14), date(2026, 9, 15)])
    assert [v for _, v, _ in out] == pytest.approx([9_990.0, 10_490.0])
    assert [w for _, _, w in out] == pytest.approx([5_000 / 9_990, 5_500 / 10_490])


def test_a_third_invested_desk_tracking_the_market_shows_no_gap_per_rupee():
    """The killer case. The desk is a third invested and its holdings match the
    benchmark exactly, so per rupee at risk it is level. Against the whole
    benchmark it looks two thirds behind every week, which is what made the
    alarm fire on exposure alone."""
    bench = [(FRI1, 100.0), (FRI2, 110.0), (FRI3, 121.0)]  # +10% a week
    step = 1 + 0.10 / 3  # a third invested, so a third of the market's move
    desk = [(FRI1, 100.0), (FRI2, 100 * step), (FRI3, 100 * step**2)]
    shares = [(FRI1, 1 / 3), (FRI2, 1 / 3), (FRI3, 1 / 3)]

    assert weekly_excess(desk, bench) == pytest.approx([-0.10 * 2 / 3] * 2)
    assert weekly_excess(desk, bench, shares) == pytest.approx([0.0, 0.0], abs=1e-12)


def test_the_invested_scaling_uses_the_share_carried_into_the_week():
    """The week's return comes from the exposure the desk started it with, so
    the share at the previous week's close is the one that scales."""
    desk = [(FRI1, 100.0), (FRI2, 105.0)]
    bench = [(FRI1, 100.0), (FRI2, 110.0)]
    shares = [(FRI1, 0.5), (FRI2, 0.9)]

    # Half invested and up 5%, so the invested half earned 10%: level with the
    # benchmark. The 0.9 the desk ended the week at does not come into it.
    assert weekly_excess(desk, bench, shares) == pytest.approx([0.05 / 0.5 - 0.10])
    assert weekly_shares(desk, bench, shares) == pytest.approx([0.5])


def test_selection_still_shows_through_the_scaling():
    """Same third-invested desk, but its picks beat the benchmark. The gap per
    rupee invested is positive while the gap to the whole benchmark is not."""
    bench = [(FRI1, 100.0), (FRI2, 110.0)]
    desk = [(FRI1, 100.0), (FRI2, 104.0)]  # a third of +12%, not +10%
    shares = [(FRI1, 1 / 3), (FRI2, 1 / 3)]

    assert weekly_excess(desk, bench)[0] < 0
    assert weekly_excess(desk, bench, shares) == pytest.approx([0.12 - 0.10])


def test_a_week_entered_with_almost_nothing_invested_is_left_out():
    """A return over a share near zero is noise, not a measurement."""
    desk = [(FRI1, 100.0), (FRI2, 100.1), (FRI3, 105.0)]
    bench = [(FRI1, 100.0), (FRI2, 110.0), (FRI3, 120.0)]
    shares = [(FRI1, 0.001), (FRI2, 0.5), (FRI3, 0.5)]

    assert weekly_excess(desk, bench, shares) == pytest.approx([(105 / 100.1 - 1) / 0.5 - (120 / 110 - 1)])
    assert weekly_shares(desk, bench, shares) == pytest.approx([0.5])


def test_a_week_with_no_invested_share_is_left_out():
    """Only weeks every series covers are measured."""
    desk = [(FRI1, 100.0), (FRI2, 105.0), (FRI3, 110.0)]
    bench = [(FRI1, 100.0), (FRI2, 110.0), (FRI3, 120.0)]
    assert len(weekly_excess(desk, bench, [(FRI2, 0.5), (FRI3, 0.5)])) == 1


def test_stats_on_a_known_series():
    s = stats([0.01, 0.03, 0.02, 0.04])
    assert s.n == 4 and s.mean == pytest.approx(0.025)
    assert s.sd == pytest.approx(0.0129099445)
    assert s.t == pytest.approx(0.025 / 0.0129099445 * 2)


def test_stats_on_too_few_points():
    assert stats([]) == Stats(0, 0.0, 0.0, 0.0)
    assert stats([0.01]) == Stats(1, 0.01, 0.0, 0.0)
    assert stats([0.01, 0.01]).t == 0.0


@pytest.mark.parametrize(
    ("n", "t", "expected"),
    [
        (11, 5.0, "too early"),
        (12, -2.0, "clearly behind the benchmark"),
        (12, -1.5, "behind the benchmark"),
        (12, -1.0, "behind the benchmark"),
        (12, 0.0, "no evidence yet"),
        (12, 0.99, "no evidence yet"),
        (12, 1.0, "suggestive"),
        (12, 1.99, "suggestive"),
        (12, 2.0, "strong"),
    ],
)
def test_label(n, t, expected):
    assert label(Stats(n, 0.0, 0.0, t)) == expected


def test_below_expectation_fires_only_when_two_standard_errors_short():
    assert below_expectation(Stats(16, -0.002, 0.004, 0.0), 0.06) is True
    assert below_expectation(Stats(16, 0.0, 0.004, 0.0), 0.06) is False
    assert below_expectation(Stats(11, -0.01, 0.004, 0.0), 0.06) is False


def test_below_expectation_sits_exactly_on_its_boundary():
    """Two standard errors here is 2 x 0.004 / 4 = 0.002, so the boundary is the
    weekly equivalent of the yearly figure, minus that."""
    weekly = 1.06 ** (1 / 52) - 1
    assert below_expectation(Stats(16, weekly - 0.002, 0.004, 0.0), 0.06) is False
    assert below_expectation(Stats(16, weekly - 0.002 - 1e-6, 0.004, 0.0), 0.06) is True


def test_big_moves_flag_unexplained_jumps_only():
    bars = {
        "X": [Bar(date(2026, 9, 14), 100.0), Bar(date(2026, 9, 15), 45.0), Bar(date(2026, 9, 16), 44.0)],
        "Y": [Bar(date(2026, 9, 14), 100.0), Bar(date(2026, 9, 15), 33.0, split_ratio=3.0)],
    }
    out = big_moves(bars, {"X", "Y"}, date(2026, 9, 1), date(2026, 9, 30))
    assert out == [("X", date(2026, 9, 15), pytest.approx(-0.55))]


def test_big_moves_outside_the_window_are_left_out():
    bars = {"X": [Bar(date(2026, 8, 14), 100.0), Bar(date(2026, 8, 15), 45.0)]}
    assert big_moves(bars, {"X"}, date(2026, 9, 1), date(2026, 9, 30)) == []
