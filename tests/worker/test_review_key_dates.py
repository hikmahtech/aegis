"""Weekly-review people radar — life.people.key_dates → "coming up" block.

Splits into the deterministic month/day recurrence maths (pure helper, fixed
`today`) and the activity itself against a real Postgres.
"""

from __future__ import annotations

import datetime as dt

import pytest
import pytest_asyncio
from aegis_worker.activities.review import (
    ReviewActivities,
    _upcoming_key_dates,
    format_key_dates,
)

PREFIX = "zzc3-"


@pytest_asyncio.fixture(loop_scope="function")
async def clean_people(db_pool):
    """Scope the DB tests to their own rows — the test database is shared."""
    await db_pool.execute("DELETE FROM life.people WHERE name LIKE $1", f"{PREFIX}%")
    yield db_pool
    await db_pool.execute("DELETE FROM life.people WHERE name LIKE $1", f"{PREFIX}%")


# ── pure recurrence maths ──


def test_upcoming_rolls_a_january_date_forward_over_the_year_boundary():
    """A birthday in early January is upcoming from a late-December review —
    matching on the stored year (1990) would never fire."""
    hits = _upcoming_key_dates({"birthday": "1990-01-02"}, dt.date(2026, 12, 28), 14)
    assert [(h["label"], h["date"], h["days_until"], h["years"]) for h in hits] == [
        ("birthday", "2027-01-02", 5, 37)
    ]


def test_upcoming_ignores_dates_outside_the_lead_window():
    assert _upcoming_key_dates({"birthday": "1990-04-02"}, dt.date(2026, 3, 1), 14) == []


def test_upcoming_handles_year_unknown_form():
    """migration 016 documents '--MM-DD' for an unknown birth year; the
    person's age is then unknown, not zero."""
    hits = _upcoming_key_dates({"birthday": "--03-05"}, dt.date(2026, 3, 1), 14)
    assert len(hits) == 1
    assert hits[0]["date"] == "2026-03-05"
    assert hits[0]["years"] is None


def test_upcoming_skips_unparseable_values():
    """One typo must not take down the whole weekly review."""
    hits = _upcoming_key_dates(
        {"birthday": "sometime in spring", "anniversary": "2016-03-04", "blank": ""},
        dt.date(2026, 3, 1),
        14,
    )
    assert [h["label"] for h in hits] == ["anniversary"]


def test_upcoming_skips_29_feb_in_a_non_leap_year():
    """dt.date(2027, 2, 29) raises — the row must be skipped, not crash."""
    assert _upcoming_key_dates({"birthday": "2000-02-29"}, dt.date(2027, 2, 20), 14) == []


# ── formatting ──


def test_format_key_dates_empty_is_blank_so_callers_append_nothing():
    assert format_key_dates([]) == ""


def test_format_key_dates_renders_name_relationship_and_age():
    body = format_key_dates(
        [
            {
                "name": "Amma",
                "relationship": "mother",
                "label": "birthday",
                "date": "2026-08-04",
                "days_until": 3,
                "years": 60,
            }
        ]
    )
    assert "Coming up" in body
    assert "Amma (mother): birthday in 3d — turning 60" in body


# ── the activity, against a real Postgres ──


async def test_check_upcoming_key_dates_selects_only_the_lead_window(clean_people):
    db_pool = clean_people
    today = dt.date.today()
    soon = today + dt.timedelta(days=3)
    far = today + dt.timedelta(days=90)
    await db_pool.execute(
        "INSERT INTO life.people (name, relationship, key_dates) VALUES "
        "($1, 'brother', $2), ($3, 'colleague', $4), ($5, NULL, $6)",
        f"{PREFIX}Soon",
        {"birthday": f"1985-{soon.month:02d}-{soon.day:02d}"},
        f"{PREFIX}Far",
        {"birthday": f"1985-{far.month:02d}-{far.day:02d}"},
        f"{PREFIX}NoDates",
        {},
    )

    hits = await ReviewActivities(db_pool=db_pool).check_upcoming_key_dates()

    names = [h["name"] for h in hits if h["name"].startswith(PREFIX)]
    assert names == [f"{PREFIX}Soon"], "only the in-window person should surface"
    hit = next(h for h in hits if h["name"] == f"{PREFIX}Soon")
    assert hit["days_until"] == 3
    assert hit["relationship"] == "brother"
    assert hit["label"] == "birthday"


async def test_check_upcoming_key_dates_honours_review_config_lead_days(clean_people):
    """The window is operator-tunable via the `review_config` settings row —
    a 1-day window must drop a 3-day-out birthday."""
    db_pool = clean_people
    soon = dt.date.today() + dt.timedelta(days=3)
    await db_pool.execute(
        "INSERT INTO life.people (name, key_dates) VALUES ($1, $2)",
        f"{PREFIX}Soon",
        {"birthday": f"1985-{soon.month:02d}-{soon.day:02d}"},
    )
    await db_pool.execute(
        "INSERT INTO settings (key, value) VALUES ('review_config', $1) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        {"key_dates_lead_days": 1},
    )
    try:
        hits = await ReviewActivities(db_pool=db_pool).check_upcoming_key_dates()
        assert [h["name"] for h in hits if h["name"].startswith(PREFIX)] == []
    finally:
        await db_pool.execute("DELETE FROM settings WHERE key='review_config'")


@pytest.mark.asyncio
async def test_check_upcoming_key_dates_no_pool_is_empty():
    assert await ReviewActivities(db_pool=None).check_upcoming_key_dates() == []
