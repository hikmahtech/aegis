"""Observations service — write + query `life.observations` (migration 017).

A generic life-metrics store: one row per reading (weight, sleep hours, a
sensor temperature, a location ping), with anything non-numeric in
`metadata`. Ingest paths call `record_observation`; readers call
`query_trend` (ordered series) or `summarize` (min/max/avg/latest over a
window).

Shaped after `services/people.py`: plain dicts in and out over an asyncpg
pool, no ORM, aggregates in SQL (no pandas).

Two things every caller depends on:

* `metric` and `source` are stored **stripped and lowercased**
  (`normalize_key`). Every write and every lookup goes through it, so
  "Weight", " weight " and "WEIGHT" are the same series. Skipping it on
  either side silently splits a series in two.
* `value` is a Postgres `numeric`, and asyncpg hands a numeric column back as
  a `Decimal` — which `json.dumps` chokes on, and which compares awkwardly
  with the floats every caller has. So every read selects `value::float8`
  (`_SELECT_COLS` and the aggregates in `summarize`): floats out, JSON-safe.
  Writes need no cast — asyncpg 0.31 accepts a Python float for a numeric
  parameter (verified against this repo's asyncpg).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
import structlog

logger = structlog.get_logger()

_SELECT_COLS = (
    "id, source, metric, value::float8 AS value, observed_at, metadata, created_at"
)

# Upper bound on rows returned by a single trend query — a year of one-minute
# sensor readings is half a million rows and no caller wants them all.
_MAX_TREND_ROWS = 5000


def normalize_key(value: Any) -> str:
    """Strip + lowercase a metric or source name. '' for None/blank."""
    return str(value or "").strip().lower()


async def record_observation(
    pool: asyncpg.Pool,
    source: str,
    metric: str,
    value: float | int | None = None,
    observed_at: datetime | None = None,
    metadata: dict | None = None,
) -> dict:
    """Insert one reading. Returns the stored row.

    `observed_at` defaults to now (UTC). Raises ValueError on a blank source
    or metric — an unlabelled reading can never be queried back, so refusing
    it is better than storing garbage.
    """
    src = normalize_key(source)
    met = normalize_key(metric)
    if not src:
        raise ValueError("source is required")
    if not met:
        raise ValueError("metric is required")
    num = None if value is None else float(value)
    row = await pool.fetchrow(
        "INSERT INTO life.observations (source, metric, value, observed_at, metadata) "
        "VALUES ($1, $2, $3, $4, $5) "
        f"RETURNING {_SELECT_COLS}",
        src,
        met,
        num,
        observed_at or datetime.now(UTC),
        metadata or {},
    )
    return dict(row)


async def query_trend(
    pool: asyncpg.Pool,
    metric: str,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = _MAX_TREND_ROWS,
) -> list[dict]:
    """Readings for `metric` in [since, until), oldest first.

    Both bounds are optional. Returns [] for a blank metric without touching
    the database.
    """
    met = normalize_key(metric)
    if not met:
        return []
    clauses = ["metric = $1"]
    params: list[Any] = [met]
    if since is not None:
        params.append(since)
        clauses.append(f"observed_at >= ${len(params)}")
    if until is not None:
        params.append(until)
        clauses.append(f"observed_at < ${len(params)}")
    rows = await pool.fetch(
        f"SELECT {_SELECT_COLS} FROM life.observations "
        f"WHERE {' AND '.join(clauses)} "
        f"ORDER BY observed_at ASC LIMIT {max(1, min(int(limit), _MAX_TREND_ROWS))}",
        *params,
    )
    return [dict(r) for r in rows]


async def summarize(
    pool: asyncpg.Pool,
    metric: str,
    window_days: int = 30,
    until: datetime | None = None,
) -> dict:
    """min/max/avg/latest for `metric` over the `window_days` before `until`.

    `until` defaults to now, so the default window is "the last 30 days".
    Passing an explicit `until` is how a caller asks for an earlier window
    (the chat tool compares one window against the one before it).

    `count` counts rows; the numeric aggregates ignore NULL values, so a
    metric that only carries metadata reports count>0 with avg=None.
    """
    met = normalize_key(metric)
    days = max(1, int(window_days or 1))
    end = until or datetime.now(UTC)
    start = end - timedelta(days=days)
    empty = {
        "metric": met,
        "window_days": days,
        "since": start,
        "until": end,
        "count": 0,
        "min": None,
        "max": None,
        "avg": None,
        "latest": None,
        "latest_at": None,
    }
    if not met:
        return empty

    row = await pool.fetchrow(
        "SELECT count(*) AS count, min(value)::float8 AS min, max(value)::float8 AS max, "
        "avg(value)::float8 AS avg, max(observed_at) AS latest_at "
        "FROM life.observations "
        "WHERE metric = $1 AND observed_at >= $2 AND observed_at < $3",
        met,
        start,
        end,
    )
    if not row or not row["count"]:
        return empty
    latest = await pool.fetchval(
        "SELECT value::float8 FROM life.observations "
        "WHERE metric = $1 AND observed_at >= $2 AND observed_at < $3 "
        "ORDER BY observed_at DESC LIMIT 1",
        met,
        start,
        end,
    )
    return {
        **empty,
        "count": row["count"],
        "min": row["min"],
        "max": row["max"],
        "avg": row["avg"],
        "latest": latest,
        "latest_at": row["latest_at"],
    }
