"""A schedule can be written in the flow's own clock, not only UTC.

A cron is UTC unless the line carries a `CRON_TZ=<zone>` prefix. This matters
where a flow's sense of time is not UTC: the trading desk reads "today" from its
market's clock and must plan before that market opens, so putting the trigger on
a different clock from the guard is how a schedule drifts away from the thing it
is supposed to be synchronised with.
"""

from __future__ import annotations

from zoneinfo import ZoneInfoNotFoundError

import pytest
from aegis_worker.schedule_sync import split_cron_timezone


def test_a_bare_cron_is_utc_exactly_as_before():
    assert split_cron_timezone("30 2 * * 1-5") == ("30 2 * * 1-5", "")


def test_a_prefixed_cron_yields_the_expression_and_the_zone():
    assert split_cron_timezone("CRON_TZ=Asia/Kolkata 0 8,11,14 * * 1-5") == (
        "0 8,11,14 * * 1-5",
        "Asia/Kolkata",
    )


def test_a_multi_fire_expression_survives_the_split():
    """The desk's three fires are one expression. Splitting must not disturb the
    comma lists inside it."""
    expr, zone = split_cron_timezone("CRON_TZ=Europe/London 7,37 9-17 * * 1,3,5")
    assert (expr, zone) == ("7,37 9-17 * * 1,3,5", "Europe/London")


@pytest.mark.parametrize(
    "cron",
    [
        "CRON_TZ=Not/AZone 0 8 * * *",
        "CRON_TZ=Asia/Kolkatta 0 8 * * *",  # one letter out
    ],
)
def test_a_zone_that_is_not_a_zone_raises(cron):
    """Firing in UTC because a name was misspelt is the silent failure this
    exists to prevent, so it has to be loud."""
    with pytest.raises(ZoneInfoNotFoundError):
        split_cron_timezone(cron)


@pytest.mark.parametrize("cron", ["CRON_TZ=Asia/Kolkata", "CRON_TZ= 0 8 * * *", "CRON_TZ=Asia/Kolkata "])
def test_a_malformed_prefix_raises_rather_than_guessing(cron):
    with pytest.raises(ValueError):
        split_cron_timezone(cron)
