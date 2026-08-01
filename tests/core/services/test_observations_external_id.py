"""`record_external_observation` + migration 021's unique index.

Real Postgres: the dedup is the index, not any Python check, so a fake pool
would test nothing. Every row is prefixed so a co-located test file's cleanup
cannot satisfy or break these assertions.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from aegis.db import run_migrations
from aegis.services import observations as svc

PREFIX = "zzb7ext-"
SOURCE = f"{PREFIX}oura"
METRIC = f"{PREFIX}sleep_score"
NOW = datetime(2026, 7, 30, 7, 30, tzinfo=UTC)


@pytest_asyncio.fixture(loop_scope="function")
async def pool(db_pool):
    await run_migrations(db_pool)
    await db_pool.execute("DELETE FROM life.observations WHERE source LIKE $1", f"{PREFIX}%")
    yield db_pool
    await db_pool.execute("DELETE FROM life.observations WHERE source LIKE $1", f"{PREFIX}%")


async def _count(pool) -> int:
    return await pool.fetchval(
        "SELECT count(*) FROM life.observations WHERE source LIKE $1", f"{PREFIX}%"
    )


async def test_second_write_of_the_same_external_id_returns_none(pool):
    first = await svc.record_external_observation(
        pool, SOURCE, METRIC, "vendor-1", 78.0, observed_at=NOW
    )
    assert first is not None and first["value"] == 78.0
    second = await svc.record_external_observation(
        pool, SOURCE, METRIC, "vendor-1", 99.0, observed_at=NOW
    )
    assert second is None, "duplicate insert was not deduped"
    assert await _count(pool) == 1
    # First write wins — a re-poll never rewrites an already-stored reading.
    assert (
        await pool.fetchval(
            "SELECT value::float8 FROM life.observations WHERE source = $1", SOURCE
        )
    ) == 78.0


async def test_concurrent_writers_of_the_same_id_produce_exactly_one_row(pool):
    """Two overlapping polls racing on the same record. A SELECT-then-INSERT
    check would let both through; the unique index serialises them."""
    results = await asyncio.gather(
        *[
            svc.record_external_observation(
                pool, SOURCE, METRIC, "race-1", 70.0 + i, observed_at=NOW
            )
            for i in range(5)
        ]
    )
    assert sum(1 for r in results if r is not None) == 1
    assert await _count(pool) == 1


async def test_dedup_is_scoped_by_metric_and_source(pool):
    """The key is the triple: one vendor record fanning out to two metrics, and
    two vendors that happen to mint the same id, must not collide."""
    await svc.record_external_observation(pool, SOURCE, METRIC, "shared", 1.0, observed_at=NOW)
    other_metric = await svc.record_external_observation(
        pool, SOURCE, f"{PREFIX}steps", "shared", 2.0, observed_at=NOW
    )
    other_source = await svc.record_external_observation(
        pool, f"{PREFIX}whoop", METRIC, "shared", 3.0, observed_at=NOW
    )
    assert other_metric is not None and other_source is not None
    assert await _count(pool) == 3


async def test_plain_record_observation_is_never_deduped(pool):
    """Sensor/manual readings carry no external id and legitimately repeat —
    the partial index must leave them alone."""
    for _ in range(3):
        await svc.record_observation(pool, SOURCE, METRIC, 70.0, observed_at=NOW)
    assert await _count(pool) == 3
    assert (
        await pool.fetchval(
            "SELECT count(*) FROM life.observations "
            "WHERE source LIKE $1 AND external_id IS NULL",
            f"{PREFIX}%",
        )
    ) == 3


async def test_blank_external_id_is_refused(pool):
    with pytest.raises(ValueError):
        await svc.record_external_observation(pool, SOURCE, METRIC, "  ", 1.0)
    assert await _count(pool) == 0


async def test_source_and_metric_are_normalized_like_plain_writes(pool):
    """`record_observation` lowercases both; if the external path did not, the
    same series would silently split in two."""
    await svc.record_external_observation(
        pool, SOURCE.upper(), METRIC.upper(), "norm-1", 78.0, observed_at=NOW
    )
    row = await pool.fetchrow(
        "SELECT source, metric FROM life.observations WHERE external_id = $1", "norm-1"
    )
    assert row["source"] == SOURCE and row["metric"] == METRIC
    # ...and the dedup therefore also matches across casings.
    assert (
        await svc.record_external_observation(pool, SOURCE, METRIC, "norm-1", 1.0)
    ) is None
