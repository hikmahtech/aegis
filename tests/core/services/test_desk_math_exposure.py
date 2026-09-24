"""`target_exposure`: the desk invests a set share of capital (#669)."""

from __future__ import annotations

import pytest
from aegis.services import desk_rules
from aegis.services.desk_math import Decision, Rules, scale_to_exposure


def d(symbol, weight, cls="equity", rank=1):
    return Decision(symbol, cls, "COMPLIANT", "LONG", weight, rank)


# The pipeline's tradeable rows on 2026-09-23: 39.3% of the book, the rest cash.
TODAY = (
    d("GOLDIETF", 0.10, cls="etf"),
    d("JSLL", 0.088, rank=2),
    d("FDC", 0.088, rank=3),
    d("APCOTEXIND", 0.088, rank=4),
    d("GMDC", 0.0293, rank=5),
)


def weights(rows):
    return {r.symbol: r.target_weight for r in rows}


def test_todays_book_is_fully_invested_with_no_name_over_the_cap():
    out = weights(scale_to_exposure(TODAY, Rules(target_exposure=1.0)))
    assert sum(out.values()) == pytest.approx(1.0)
    assert out["GOLDIETF"] == pytest.approx(0.25)
    assert max(out.values()) <= 0.25 + 1e-12
    # The pipeline's relative sizes still hold.
    assert out["GOLDIETF"] >= out["JSLL"] == pytest.approx(out["FDC"]) == pytest.approx(out["APCOTEXIND"])
    assert out["APCOTEXIND"] > out["GMDC"]


def test_the_excess_of_a_capped_name_goes_to_the_rest_in_proportion():
    out = weights(scale_to_exposure(TODAY, Rules(target_exposure=1.0)))
    # GOLDIETF capped at 0.25; the other 0.75 is shared by 0.088:0.088:0.088:0.0293.
    rest = 0.088 * 3 + 0.0293
    assert out["JSLL"] == pytest.approx(0.75 * 0.088 / rest)
    assert out["GMDC"] == pytest.approx(0.75 * 0.0293 / rest)


def test_when_every_name_is_capped_the_rest_stays_cash():
    rows = (d("A", 0.10), d("B", 0.05), d("C", 0.02))
    out = weights(scale_to_exposure(rows, Rules(target_exposure=1.0)))
    assert out == {"A": pytest.approx(0.25), "B": pytest.approx(0.25), "C": pytest.approx(0.25)}


def test_no_target_leaves_the_pipeline_weights_alone():
    assert scale_to_exposure(TODAY, Rules()) == TODAY


def test_a_lower_target_scales_down_too():
    out = weights(scale_to_exposure(TODAY, Rules(target_exposure=0.2)))
    assert sum(out.values()) == pytest.approx(0.2)
    assert out["GOLDIETF"] == pytest.approx(0.2 * 0.10 / 0.3933)


def test_the_rest_of_a_row_is_kept():
    out = scale_to_exposure(TODAY, Rules(target_exposure=1.0))
    assert [(r.symbol, r.asset_class, r.selection_rank) for r in out] == [
        (r.symbol, r.asset_class, r.selection_rank) for r in TODAY
    ]


@pytest.mark.parametrize("raw, read", [(1.0, 1.0), ("0.8", 0.8), (None, None), (0, None), (1.5, None), ("x", None)])
def test_the_read_path_forgives_junk_by_following_the_pipeline(raw, read):
    assert Rules.from_config({"target_exposure": raw}).target_exposure == read


BASE = {"fy_start_month": 1, "stale_calendar_days": 6, "stale_price_days": 7}


@pytest.mark.parametrize("value", [1.5, 0, -1, "x", True])
def test_a_target_exposure_that_would_not_work_is_refused(value):
    with pytest.raises(ValueError, match="target_exposure"):
        desk_rules.validate(BASE | {"target_exposure": value})


@pytest.mark.parametrize("value, stored", [(None, None), ("", None), (1.0, 1.0), ("0.9", 0.9)])
def test_a_target_exposure_saves_or_clears(value, stored):
    assert desk_rules.validate(BASE | {"target_exposure": value})["target_exposure"] == stored
