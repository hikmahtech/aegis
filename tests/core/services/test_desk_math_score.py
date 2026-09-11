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
    desk_values,
    label,
    stats,
    weekly_excess,
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
