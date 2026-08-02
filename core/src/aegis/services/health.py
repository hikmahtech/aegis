"""Apple Health pushes (B6) — normalise a Health Auto Export batch to observations.

DATA-MINIMISATION MODEL — read this before adding a metric.

HealthKit holds well over a hundred sample types, and Health Auto Export will
happily post every one of them. This module ingests **five** and drops the
rest, because a store of everything the phone knows about the owner's body is a
liability that only grows: it can be subpoenaed, leaked or mis-inferred from,
and nothing in AEGIS reads it. The five are the ones a briefing or a Theme-C
correlation actually consumes:

* ``sleep_minutes`` — how the night went, the single strongest predictor of
  how the day will go.
* ``hrv_ms`` and ``resting_hr`` — the two standard recovery/strain signals.
* ``steps`` — activity volume.
* ``active_energy_kcal`` — activity intensity, which steps alone misses.

Anything else (glucose, weight, menstrual cycle, blood oxygen, ECG, mindful
minutes, …) is **counted and discarded**, never stored — so a mis-configured
export automation cannot quietly turn this into a full health record. Widening
the list is a deliberate act: add the mapping here and say what reads it.

What is never persisted or logged, beyond the value itself:

* the per-sample ``source`` device name Health Auto Export attaches,
* the raw payload, in whole or in part,
* any individual value — log lines carry counts only, plus (on the unreadable-
  unit warning) the metric name and the unit string, which are export
  configuration rather than readings. The one place a value is ever rendered is
  the owner's own briefing.

Ingest is inline (a service called from the webhook), NOT a Temporal flow, for
the same reason B5's location lane is: a workflow's argument is persisted in
Temporal's workflow history, so handing a health batch to a flow would copy the
owner's body data into a second store that has its own retention and its own
UI. Normalising a batch is pure arithmetic plus N inserts — there is nothing
here worth durable-retrying at that price.
"""

from __future__ import annotations

import math
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
import structlog

from aegis.services.observations import record_external_observation

logger = structlog.get_logger()

# `life.observations.source` for every row this module writes.
HEALTH_SOURCE = "health"

# THE ALLOWLIST: Health Auto Export metric name -> the canonical metric stored.
# A name absent from this mapping is counted in `skipped` and dropped.
# The canonical names carry their unit, so a reader never has to consult
# `metadata` to know what a number means.
_METRIC_ALLOWLIST: dict[str, str] = {
    "sleep_analysis": "sleep_minutes",
    "heart_rate_variability": "hrv_ms",
    "resting_heart_rate": "resting_hr",
    "step_count": "steps",
    "active_energy": "active_energy_kcal",
}

# What the store may contain from this source — used by the briefing so it
# cannot drift from what ingest actually writes.
HEALTH_METRICS: tuple[str, ...] = tuple(sorted(set(_METRIC_ALLOWLIST.values())))

# Where the number lives inside one data point. Health Auto Export uses `qty`
# for simple metrics and named fields for aggregates; sleep is split across
# stages, so the whole-night total is preferred over any single stage.
_VALUE_KEYS: dict[str, tuple[str, ...]] = {
    "sleep_minutes": ("totalSleep", "asleep", "inBed", "qty"),
}
_DEFAULT_VALUE_KEYS: tuple[str, ...] = ("qty", "Avg", "avg")

# Runaway guard, mirroring `activities/raindrop.py`'s `_MAX_PAGES`: an export
# window the owner widened by accident must cost a bounded number of inserts,
# not a request that holds a connection for a minute. The 64 KiB body cap on
# the webhook already bounds this loosely; this bounds it exactly.
_MAX_SAMPLES = 500

# Timestamp plausibility, same reasoning as `services/places.py`: a sample from
# a dead clock lands outside every trend query yet survives retention for a
# year, so it is dropped rather than believed.
_MIN_OBSERVED_AT = datetime(2015, 1, 1, tzinfo=UTC)
_MAX_CLOCK_SKEW = timedelta(days=1)

# Health Auto Export writes "2026-07-30 00:00:00 +0530" — an ISO timestamp with
# a space before the offset, which `datetime.fromisoformat` rejects.
_OFFSET_SPACE = re.compile(r"\s+([+-]\d{2}:?\d{2})$")

_KJ_PER_KCAL = 4.184

# THE UNIT ALLOWLIST, per metric whose value depends on its unit. A unit outside
# its metric's sets is NOT assumed away — `convert` returns None and the sample
# is skipped, because a stored reading is effectively permanent:
# `record_external_observation` dedups on (source, metric, instant) with
# ON CONFLICT DO NOTHING, so a corrected re-export can never overwrite a
# wrong-unit value. A skipped sample, by contrast, is re-offered by the very
# next overlapping export once the phone is set right.
#
# Only what Health Auto Export is known to emit is listed. A plausible-looking
# unit it has never been observed to send (`Cal`, `kilojoules`) skips loudly so
# the operator confirms what it means before it becomes an unfixable row.
_SLEEP_MINUTES = frozenset({"min", "mins", "minute", "minutes"})
_SLEEP_SECONDS = frozenset({"s", "sec", "secs", "second", "seconds"})
_SLEEP_HOURS = frozenset({"h", "hr", "hrs", "hour", "hours"})
_ENERGY_KCAL = frozenset({"kcal"})
_ENERGY_KJ = frozenset({"kj"})


def parse_timestamp(raw: Any, now: datetime | None = None) -> datetime | None:
    """When a sample was taken, or ``None`` if that cannot be established.

    Naive timestamps are read as UTC. A value outside
    ``[2015-01-01, now + 1 day]`` is treated as a broken device clock and
    rejected — unlike the location lane there is no sensible "use arrival
    time" fallback here, because a health batch is explicitly historical and
    stamping it "now" would corrupt the series it belongs to.
    """
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        stamp = datetime.fromisoformat(_OFFSET_SPACE.sub(r"\1", text).replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    current = now or datetime.now(UTC)
    if not (_MIN_OBSERVED_AT <= stamp <= current + _MAX_CLOCK_SKEW):
        return None
    return stamp


def convert(metric: str, units: Any, value: float) -> float | None:
    """Value in the canonical unit named by `metric`, or ``None`` if unreadable.

    The unit is read from the export rather than assumed, because Health Auto
    Export follows the phone's locale — energy arrives as kJ or kcal depending
    on the owner's settings, and treating one as the other is a silent 4x
    error.

    An unknown or absent unit on a metric whose value depends on it returns
    ``None`` — "this sample cannot be read" — rather than falling back to Health
    Auto Export's default. The fallback used to turn a mislabeled export into a
    silent 60x (sleep, hours assumed) or 4x (energy, kcal assumed) error that
    landed in `life.observations` and poisoned every downstream trend with no
    signal at all. The caller counts a ``None`` into `skipped` and names the
    offending unit in a warning.

    Metrics that need no conversion (steps, HRV, resting HR) are returned as
    given: they have no unit-dependent branch to get wrong.
    """
    unit = str(units or "").strip().lower()
    if metric == "sleep_minutes":
        if unit in _SLEEP_MINUTES:
            return value
        if unit in _SLEEP_SECONDS:
            return value / 60.0
        if unit in _SLEEP_HOURS:
            return value * 60.0
        return None
    if metric == "active_energy_kcal":
        if unit in _ENERGY_KCAL:
            return value
        if unit in _ENERGY_KJ:
            return value / _KJ_PER_KCAL
        return None
    return value


def _first_value(point: dict, metric: str) -> float | None:
    for key in _VALUE_KEYS.get(metric, _DEFAULT_VALUE_KEYS):
        if key not in point:
            continue
        try:
            num = float(point[key])
        except (TypeError, ValueError):
            return None
        return num if math.isfinite(num) else None
    return None


def parse_health_export(
    payload: dict, now: datetime | None = None
) -> tuple[list[tuple[str, float, datetime]], int, bool]:
    """``([(metric, value, observed_at), ...], skipped, truncated)``.

    Raises ``ValueError`` when the envelope itself is malformed (no metrics
    list) — the webhook turns that into a 422, because it is a client
    configuration error and no amount of retrying will fix it. A *well-formed*
    export containing only metrics outside the allowlist is not an error: it
    returns no samples and a non-zero `skipped`.
    """
    data = payload.get("data")
    if not isinstance(data, dict):
        data = payload
    metrics = data.get("metrics") if isinstance(data, dict) else None
    if not isinstance(metrics, list):
        raise ValueError("payload carries no metrics list")

    samples: list[tuple[str, float, datetime]] = []
    skipped = 0
    truncated = False
    # canonical metric -> the unreadable unit strings seen for it. Collected
    # rather than logged per sample: one mislabeled export is hundreds of
    # samples carrying the same wrong unit.
    unreadable: dict[str, set[str]] = {}
    for entry in metrics:
        if truncated:
            break
        if not isinstance(entry, dict):
            skipped += 1
            continue
        canonical = _METRIC_ALLOWLIST.get(str(entry.get("name") or "").strip().lower())
        points = entry.get("data")
        if canonical is None or not isinstance(points, list):
            # Outside the allowlist, or shaped wrong: counted, never stored.
            skipped += len(points) if isinstance(points, list) else 1
            continue
        units = entry.get("units")
        for point in points:
            if len(samples) >= _MAX_SAMPLES:
                truncated = True
                break
            if not isinstance(point, dict):
                skipped += 1
                continue
            value = _first_value(point, canonical)
            observed_at = parse_timestamp(point.get("date"), now=now)
            if value is None or observed_at is None:
                skipped += 1
                continue
            converted = convert(canonical, units, value)
            if converted is None:
                # Unreadable unit — dropped rather than assumed. See `convert`.
                skipped += 1
                unreadable.setdefault(canonical, set()).add(str(units or "")[:32])
                continue
            samples.append((canonical, converted, observed_at))

    if unreadable:
        # WARNING, not info: this is a misconfigured export losing readings, and
        # it is the only signal that it happened. Metric names and unit strings
        # are configuration, not readings — no value is rendered, same rule as
        # `record_health_push`.
        logger.warning(
            "health_push_unreadable_unit",
            units={metric: sorted(seen) for metric, seen in sorted(unreadable.items())},
        )
    return samples, skipped, truncated


async def record_health_push(pool: asyncpg.Pool, payload: dict) -> dict[str, Any]:
    """Store one Health Auto Export batch. Returns counts, never rows.

    Dedup is per SAMPLE, on ``(source, metric, external_id)`` where
    ``external_id`` is the sample's instant in UTC. That triple is exactly
    "one reading of this metric at this instant", which is what a re-export
    repeats: Health Auto Export batches overlap heavily by design (a device
    that was offline re-sends its whole window), so the webhook's
    request-level `ingest_idempotency` claim — keyed on the whole body at one
    timestamp — does not help at all once a single new sample joins the batch.
    This is the load-bearing dedup; that one is the cheap short-circuit.

    Each sample is its own statement rather than one transaction on purpose:
    a batch is a set of independent readings, and one bad row rolling back a
    night's sleep and a day's steps would make the next re-export the only
    repair path. Partial progress is the correct outcome here.

    First write wins for a given (metric, instant): `ON CONFLICT DO NOTHING`
    never overwrites. That matters for same-day aggregates — steps exported at
    noon and again at midnight share the day's timestamp, and the noon value
    is the one kept. Export completed days (see docs) rather than partial ones.
    """
    samples, skipped, truncated = parse_health_export(payload)
    stored = 0
    duplicate = 0
    for metric, value, observed_at in samples:
        row = await record_external_observation(
            pool,
            source=HEALTH_SOURCE,
            metric=metric,
            external_id=observed_at.astimezone(UTC).replace(microsecond=0).isoformat(),
            value=value,
            observed_at=observed_at,
            # Deliberately empty: the canonical metric name already carries the
            # unit, and the export's per-sample device name is not stored.
            metadata={},
        )
        if row is None:
            duplicate += 1
        else:
            stored += 1
    # Counts only. No metric is named and no value is rendered — a log stream
    # of "resting_hr" values is a health record by another route.
    logger.info(
        "health_push_recorded",
        stored=stored,
        duplicate=duplicate,
        skipped=skipped,
        truncated=truncated,
    )
    return {
        "stored": stored,
        "duplicate": duplicate,
        "skipped": skipped,
        "truncated": truncated,
    }
