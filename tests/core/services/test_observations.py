"""services/observations.py — write + query life.observations (migration 017).

Real Postgres (the session's freshly-migrated test database via `db_pool`);
no mocks, so the SQL, the `numeric` column's float round-trip and both
indexes are exercised.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from aegis.db import run_migrations
from aegis.services import observations as svc

# Prefix every fixture metric/source so assertions can't be satisfied (or
# broken) by rows another test in the shared database left behind.
PREFIX = "zzobs-"
METRIC = f"{PREFIX}weight_kg"
DECOY = f"{PREFIX}sleep_hours"
SOURCE = f"{PREFIX}scale"

NOW = datetime.now(UTC)


@pytest_asyncio.fixture(loop_scope="function")
async def pool(db_pool):
    await run_migrations(db_pool)
    await db_pool.execute("DELETE FROM life.observations WHERE metric LIKE $1", f"{PREFIX}%")
    yield db_pool
    await db_pool.execute("DELETE FROM life.observations WHERE metric LIKE $1", f"{PREFIX}%")


async def _seed(pool) -> None:
    """Three weight readings (inserted out of order) plus a decoy metric."""
    for days_ago, value in ((1, 71.0), (5, 73.5), (3, 72.0)):
        await svc.record_observation(
            pool, SOURCE, METRIC, value, observed_at=NOW - timedelta(days=days_ago)
        )
    await svc.record_observation(
        pool, SOURCE, DECOY, 6.5, observed_at=NOW - timedelta(days=2)
    )


async def test_trend_returns_that_metric_only_oldest_first(pool):
    """Insertion order is deliberately not chronological — the series must
    come back sorted by observed_at, and must not include the decoy metric
    recorded against the same source."""
    await _seed(pool)

    series = await svc.query_trend(pool, METRIC)

    assert [r["value"] for r in series] == [73.5, 72.0, 71.0]
    assert {r["metric"] for r in series} == {METRIC}
    # The decoy is really there — so "3 rows" isn't just "all the rows".
    assert len(await svc.query_trend(pool, DECOY)) == 1


async def test_trend_window_bounds_are_applied(pool):
    await _seed(pool)
    series = await svc.query_trend(
        pool, METRIC, since=NOW - timedelta(days=4), until=NOW - timedelta(days=2)
    )
    assert [r["value"] for r in series] == [72.0]


async def test_summarize_matches_hand_computed_stats(pool):
    await _seed(pool)

    out = await svc.summarize(pool, METRIC, window_days=30)

    assert out["count"] == 3
    assert out["min"] == 71.0
    assert out["max"] == 73.5
    assert out["avg"] == pytest.approx((71.0 + 73.5 + 72.0) / 3)
    # "latest" is the most recent by observed_at (1 day ago), not the last
    # row inserted (3 days ago).
    assert out["latest"] == 71.0
    assert out["latest_at"].date() == (NOW - timedelta(days=1)).date()


async def test_summarize_excludes_readings_outside_the_window(pool):
    """A 2-day window must ignore the 5-day-old reading — otherwise min/avg
    silently include history the caller asked to leave out."""
    await _seed(pool)

    out = await svc.summarize(pool, METRIC, window_days=2)

    assert out["count"] == 1
    assert out["min"] == 71.0
    assert out["max"] == 71.0


async def test_summarize_earlier_window_via_until(pool):
    """`until` is how the chat tool asks for the window before this one."""
    await _seed(pool)

    # Window [NOW-7d, NOW-4d): the 5-day-old reading only, not the 3-day one
    # that the default (until=now) window would have included.
    out = await svc.summarize(pool, METRIC, window_days=3, until=NOW - timedelta(days=4))

    assert out["count"] == 1
    assert out["avg"] == 73.5


async def test_summarize_of_an_unrecorded_metric_keeps_the_populated_shape(pool):
    """The no-rows answer must carry the same keys as a populated one —
    callers (the chat tool) index `count`/`avg`/`window_days` unconditionally,
    so a short-circuit that returns a smaller dict is a KeyError in prod."""
    out = await svc.summarize(pool, f"{PREFIX}NEVER_recorded", window_days=7)
    assert out["count"] == 0
    assert out["avg"] is None and out["latest"] is None and out["latest_at"] is None
    assert out["min"] is None and out["max"] is None
    assert out["metric"] == f"{PREFIX}never_recorded"
    assert out["window_days"] == 7
    assert (out["until"] - out["since"]).days == 7


async def test_metric_and_source_are_normalised_on_write_and_lookup(pool):
    """Stored stripped+lowercased on write, lowercased on read — otherwise
    'Weight' from a chat tool and 'weight' from a sensor are two series."""
    stored = await svc.record_observation(
        pool, f"  {SOURCE.upper()} ", f"  {METRIC.upper()}  ", 70.0, observed_at=NOW
    )
    assert stored["metric"] == METRIC
    assert stored["source"] == SOURCE

    # Lookup in yet another casing still finds it.
    assert len(await svc.query_trend(pool, METRIC.title())) == 1
    assert (await svc.summarize(pool, METRIC.upper(), window_days=1))["count"] == 1


async def test_numeric_value_round_trips_as_a_python_float(pool):
    """`value` is a Postgres `numeric`, which asyncpg hands back as a
    `Decimal` unless the read casts it — and a Decimal blows up json.dumps in
    any JSON-returning caller, and compares oddly with the float that went
    in. Every read path must select `value::float8`."""
    stored = await svc.record_observation(pool, SOURCE, METRIC, 71.25, observed_at=NOW)
    assert isinstance(stored["value"], float)
    assert stored["value"] == 71.25

    fetched = await svc.query_trend(pool, METRIC)
    assert isinstance(fetched[0]["value"], float)
    assert fetched[0]["value"] == 71.25


async def test_null_value_observations_are_stored_and_ignored_by_aggregates(pool):
    """A location-ping style row: no number, everything in metadata."""
    stored = await svc.record_observation(
        pool, SOURCE, METRIC, None, observed_at=NOW, metadata={"lat": 1.5}
    )
    assert stored["value"] is None
    # jsonb comes back as a Python dict (pool codec), not a string.
    assert stored["metadata"] == {"lat": 1.5}

    out = await svc.summarize(pool, METRIC, window_days=30)
    assert out["count"] == 1
    assert out["avg"] is None


async def test_blank_metric_or_source_is_refused_before_the_database(pool):
    for blank in ("", "   ", None):
        with pytest.raises(ValueError, match="metric is required"):
            await svc.record_observation(pool, SOURCE, blank, 1.0)
        with pytest.raises(ValueError, match="source is required"):
            await svc.record_observation(pool, blank, METRIC, 1.0)


async def test_blank_metric_reads_never_reach_the_database():
    """Asserting only `== []` / `count == 0` would be vacuous — the SQL
    returns nothing for an empty metric anyway. Hand the readers a pool that
    explodes on use, so removing the guard fails the test."""

    class ExplodingPool:
        async def fetch(self, *args, **kwargs):
            raise AssertionError("query_trend queried the database for a blank metric")

        async def fetchrow(self, *args, **kwargs):
            raise AssertionError("summarize queried the database for a blank metric")

        async def fetchval(self, *args, **kwargs):
            raise AssertionError("summarize queried the database for a blank metric")

    for blank in ("", "   ", None):
        assert await svc.query_trend(ExplodingPool(), blank) == []
        assert (await svc.summarize(ExplodingPool(), blank))["count"] == 0


# The companion guard — that life.observations IS registered in both cleanup
# retention maps — lives in tests/worker/test_cleanup_activity.py, because
# CI's core job installs only `core[dev]` and cannot import aegis_worker.
