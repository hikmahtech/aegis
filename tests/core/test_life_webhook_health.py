"""B6 — the `health` lane of POST /api/webhooks/life/{source}.

B4's file pins the signature / replay / body-bound machinery, which is
source-agnostic. What is new here is what the health lane *does* with an
authenticated Health Auto Export batch: keep five metrics, drop everything
else, normalise units, dedup per sample, and never let a value out through a
log line.

Real Postgres, httpx ASGITransport (TestClient's separate event loop would
InterfaceError on the shared pool).

Isolation: `life.observations.source` is the constant "health" for this
feature and nothing else in the suite writes it, exactly as B5's file owns
"location" — so deletes scope on the source. Sample `external_id`s are
timestamps chosen by the export, not by us, so there is no prefix to scope on.
Idempotency claims scope on the `life:health` source_type.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from unittest.mock import AsyncMock, MagicMock

import pytest_asyncio
from aegis.api.app import create_app
from aegis.api.deps import get_settings
from aegis.api.routes.interactions import get_workflow_client
from aegis.config import Settings
from httpx import ASGITransport, AsyncClient
from structlog.testing import capture_logs

SECRET = "zzb6-test-life-secret"
SOURCE = "health"

_TEST_SETTINGS = {
    "database_url": "postgresql://test:test@localhost:5432/test",
    "litellm_url": "https://litellm.example.com/v1",
    "temporal_ui_url": "https://temporal.example.com",
    "n8n_ui_url": "https://n8n.example.com",
    "admin_username": "admin",
    "admin_password": "admin",
    "life_webhook_secret": SECRET,
}


def _push(body: dict | bytes) -> tuple[str, bytes, dict[str, str]]:
    """(url, raw body, headers) signed with B4's literal wire scheme."""
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    stamp = str(int(time.time()))
    sig = hmac.new(
        SECRET.encode(), f"{SOURCE}.{stamp}.".encode() + raw, hashlib.sha256
    ).hexdigest()
    return (
        f"/api/webhooks/life/{SOURCE}",
        raw,
        {"X-Aegis-Timestamp": stamp, "X-Aegis-Signature": sig},
    )


def _metric(name: str, units: str, points: list[dict]) -> dict:
    return {"name": name, "units": units, "data": points}


def _export(*metrics: dict) -> dict:
    """The envelope Health Auto Export actually posts."""
    return {"data": {"metrics": list(metrics)}}


async def _cleanup(pool) -> None:
    await pool.execute("DELETE FROM life.observations WHERE source = $1", SOURCE)
    await pool.execute(
        "DELETE FROM ingest_idempotency WHERE source_type = $1", f"life:{SOURCE}"
    )
    await pool.execute("DELETE FROM audit_log WHERE actor = $1", f"life:{SOURCE}")


@pytest_asyncio.fixture(loop_scope="function")
async def client(db_pool):
    await _cleanup(db_pool)
    app = create_app(run_lifespan=False)
    app.state.db_pool = db_pool
    app.dependency_overrides[get_settings] = lambda: Settings(**_TEST_SETTINGS)
    temporal = MagicMock()
    temporal.start_workflow = AsyncMock()
    app.dependency_overrides[get_workflow_client] = lambda: temporal
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        c.temporal = temporal
        yield c
    await _cleanup(db_pool)


async def _rows(pool) -> list[dict]:
    rows = await pool.fetch(
        "SELECT metric, value::float8 AS value, observed_at, metadata, external_id "
        "FROM life.observations WHERE source = $1 ORDER BY metric",
        SOURCE,
    )
    return [dict(r) for r in rows]


async def test_a_realistic_export_stores_only_the_five_allowlisted_metrics(client, db_pool):
    """The whole point of the lane: HealthKit offers everything, we keep five."""
    url, raw, headers = _push(
        _export(
            _metric("step_count", "count", [
                {"date": "2026-07-30 00:00:00 +0530", "qty": 8421, "source": "iPhone"},
            ]),
            _metric("sleep_analysis", "hr", [
                {"date": "2026-07-30 00:00:00 +0530", "totalSleep": 7.5, "inBed": 8.2,
                 "deep": 1.1, "source": "Apple Watch"},
            ]),
            _metric("heart_rate_variability", "ms", [
                {"date": "2026-07-30 03:12:00 +0530", "qty": 46.5},
            ]),
            _metric("resting_heart_rate", "count/min", [
                {"date": "2026-07-30 00:00:00 +0530", "qty": 54},
            ]),
            _metric("active_energy", "kJ", [
                {"date": "2026-07-30 00:00:00 +0530", "qty": 2092.0},
            ]),
            # Everything below is real HealthKit and must never be stored.
            _metric("blood_glucose", "mg/dL", [
                {"date": "2026-07-30 08:00:00 +0530", "qty": 96},
            ]),
            _metric("sexual_activity", "count", [
                {"date": "2026-07-30 23:00:00 +0530", "qty": 1},
            ]),
            _metric("blood_oxygen_saturation", "%", [
                {"date": "2026-07-30 04:00:00 +0530", "qty": 97},
            ]),
        )
    )
    resp = await client.post(url, content=raw, headers=headers)
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["stored"] == 5
    assert body["skipped"] == 3

    rows = await _rows(db_pool)
    assert sorted(r["metric"] for r in rows) == [
        "active_energy_kcal", "hrv_ms", "resting_hr", "sleep_minutes", "steps",
    ]
    by_metric = {r["metric"]: r for r in rows}
    # Units normalised: 7.5 hours of sleep is 450 minutes, 2092 kJ is 500 kcal.
    assert by_metric["sleep_minutes"]["value"] == 450.0
    assert by_metric["active_energy_kcal"]["value"] == 500.0
    assert by_metric["steps"]["value"] == 8421.0
    assert by_metric["hrv_ms"]["value"] == 46.5
    # The device name Health Auto Export attaches is not kept.
    assert all(r["metadata"] == {} for r in rows)
    # Inline lane: a health batch starts no durable workflow.
    client.temporal.start_workflow.assert_not_awaited()


async def test_an_export_of_only_unknown_metrics_stores_nothing_and_does_not_raise(
    client, db_pool
):
    url, raw, headers = _push(
        _export(
            _metric("blood_glucose", "mg/dL", [
                {"date": "2026-07-30 08:00:00 +0530", "qty": 96},
                {"date": "2026-07-30 12:00:00 +0530", "qty": 104},
            ]),
            _metric("menstrual_flow", "count", [
                {"date": "2026-07-30 08:00:00 +0530", "qty": 1},
            ]),
        )
    )
    resp = await client.post(url, content=raw, headers=headers)
    assert resp.status_code == 202, resp.text
    assert resp.json()["stored"] == 0
    assert resp.json()["skipped"] == 3
    assert await _rows(db_pool) == []


async def test_an_overlapping_re_export_stores_only_the_genuinely_new_sample(
    client, db_pool
):
    """Health Auto Export re-sends its whole window. Per-sample dedup is what
    stops a device that was offline for a week from writing seven copies."""
    day_one = _metric("step_count", "count", [
        {"date": "2026-07-28 00:00:00 +0530", "qty": 6000},
        {"date": "2026-07-29 00:00:00 +0530", "qty": 7000},
    ])
    url, raw, headers = _push(_export(day_one))
    first = await client.post(url, content=raw, headers=headers)
    assert first.status_code == 202
    assert first.json()["stored"] == 2

    # Same window, one day wider — exactly what the next scheduled export sends.
    wider = _metric("step_count", "count", [
        {"date": "2026-07-28 00:00:00 +0530", "qty": 6000},
        {"date": "2026-07-29 00:00:00 +0530", "qty": 7000},
        {"date": "2026-07-30 00:00:00 +0530", "qty": 8000},
    ])
    url, raw, headers = _push(_export(wider))
    second = await client.post(url, content=raw, headers=headers)
    assert second.status_code == 202, second.text
    assert second.json()["stored"] == 1
    assert second.json()["duplicate"] == 2

    rows = await _rows(db_pool)
    assert sorted(r["value"] for r in rows) == [6000.0, 7000.0, 8000.0]


async def test_the_same_reading_in_a_different_timezone_spelling_still_dedups(
    client, db_pool
):
    """The dedup key is the instant, not the string the phone happened to send."""
    url, raw, headers = _push(
        _export(_metric("resting_heart_rate", "count/min", [
            {"date": "2026-07-30 05:30:00 +0530", "qty": 54},
        ]))
    )
    assert (await client.post(url, content=raw, headers=headers)).status_code == 202

    url, raw, headers = _push(
        _export(_metric("resting_heart_rate", "count/min", [
            {"date": "2026-07-30T00:00:00Z", "qty": 54},
        ]))
    )
    resp = await client.post(url, content=raw, headers=headers)
    assert resp.status_code == 202, resp.text
    assert resp.json()["stored"] == 0
    assert resp.json()["duplicate"] == 1
    assert len(await _rows(db_pool)) == 1


async def test_a_malformed_envelope_is_422_and_releases_its_claim(client, db_pool):
    """`metrics` not being a list is a client misconfiguration, not a 500."""
    bad = _push({"id": "zzb6-batch-1", "data": {"metrics": "not-a-list"}})
    resp = await client.post(bad[0], content=bad[1], headers=bad[2])
    assert resp.status_code == 422
    assert resp.json()["detail"] == "invalid_observation"
    assert await _rows(db_pool) == []

    # And the same batch id may be retried once the export is fixed.
    good = _push({
        "id": "zzb6-batch-1",
        "data": {"metrics": [_metric("step_count", "count", [
            {"date": "2026-07-30 00:00:00 +0530", "qty": 8421},
        ])]},
    })
    retry = await client.post(good[0], content=good[1], headers=good[2])
    assert retry.status_code == 202, retry.text
    assert retry.json()["stored"] == 1


async def test_junk_samples_are_skipped_without_poisoning_the_batch(client, db_pool):
    url, raw, headers = _push(
        _export(_metric("step_count", "count", [
            {"date": "2026-07-30 00:00:00 +0530", "qty": 8421},
            {"date": "1970-01-01 00:00:00 +0000", "qty": 5},  # dead device clock
            {"date": "not-a-date", "qty": 5},
            {"date": "2026-07-29 00:00:00 +0530", "qty": "NaN"},
            {"date": "2026-07-29 00:00:00 +0530"},  # no value at all
            "not-an-object",
        ]))
    )
    resp = await client.post(url, content=raw, headers=headers)
    assert resp.status_code == 202, resp.text
    assert resp.json()["stored"] == 1
    assert resp.json()["skipped"] == 5
    assert [r["value"] for r in await _rows(db_pool)] == [8421.0]


async def test_an_unreadable_unit_is_skipped_and_warned_never_assumed(client, db_pool):
    """The unit is load-bearing, so an unknown one drops the sample loudly.

    Assuming Health Auto Export's default (hours for sleep, kcal for energy) on
    an export that does not say so is a silent 60x / 4x error, and the first
    write for a (metric, instant) wins — so a corrected re-export could never
    overwrite it. Skipping keeps the store repairable.

    Controls, so this cannot pass by the lane simply storing nothing: the same
    batch carries a `sleep_analysis` entry that DOES name its unit and a
    `step_count` entry, and both must still land.
    """
    url, raw, headers = _push(
        _export(
            # No `units` key at all — the "absent" half of the bug.
            {"name": "sleep_analysis", "data": [
                {"date": "2026-07-29 00:00:00 +0530", "totalSleep": 7.0},
            ]},
            # Present but not a unit this lane has ever seen HAE send.
            _metric("active_energy", "kilojoules", [
                {"date": "2026-07-29 00:00:00 +0530", "qty": 2092.0},
            ]),
            # Controls: identical shape, unit named, must still be stored.
            _metric("sleep_analysis", "hr", [
                {"date": "2026-07-30 00:00:00 +0530", "totalSleep": 7.5},
            ]),
            _metric("step_count", "count", [
                {"date": "2026-07-30 00:00:00 +0530", "qty": 8421},
            ]),
        )
    )
    with capture_logs() as captured:
        resp = await client.post(url, content=raw, headers=headers)
    assert resp.status_code == 202, resp.text
    assert resp.json()["stored"] == 2
    assert resp.json()["skipped"] == 2

    rows = await _rows(db_pool)
    # The unit-bearing sleep sample landed; the unit-less one did not, and
    # emphatically not as `7.0 * 60`, the hours the old fallback assumed.
    assert [r["value"] for r in rows if r["metric"] == "sleep_minutes"] == [450.0]
    # 2092 kJ is 500 kcal; the old fallback would have banked it as 2092 kcal.
    assert [r["metric"] for r in rows] == ["sleep_minutes", "steps"]
    assert [r["value"] for r in rows if r["metric"] == "steps"] == [8421.0]

    # The operator can tell it happened, and from what.
    warnings = [e for e in captured if e.get("event") == "health_push_unreadable_unit"]
    assert len(warnings) == 1, captured
    assert warnings[0]["log_level"] == "warning"
    assert warnings[0]["units"] == {
        "active_energy_kcal": ["kilojoules"],
        "sleep_minutes": [""],
    }
    # Non-vacuity: the lane logged normally too, so the capture really works.
    assert any(e.get("event") == "health_push_recorded" for e in captured)


async def test_a_batch_beyond_the_sample_cap_is_truncated_not_run_to_completion(
    client, db_pool
):
    """Runaway guard: an export window widened by accident costs a bounded
    number of inserts. 500 is `_MAX_SAMPLES` — bump both together."""
    points = [
        {"date": f"2026-07-30 00:{m // 60:02d}:{m % 60:02d} +0530", "qty": m}
        for m in range(600)
    ]
    url, raw, headers = _push(_export(_metric("step_count", "count", points)))
    resp = await client.post(url, content=raw, headers=headers)
    assert resp.status_code == 202, resp.text
    assert resp.json()["stored"] == 500
    assert resp.json()["truncated"] is True
    assert len(await _rows(db_pool)) == 500


async def test_an_export_over_the_body_cap_is_refused_whole(client, db_pool):
    """64 KiB is a security control, not a tuning knob. A per-sample export of
    a wide window blows through it — the operator narrows the export."""
    points = [
        {"date": "2026-07-30 00:00:00 +0530", "qty": 1, "source": "Arshad's Apple Watch"}
    ] * 900
    fat = json.dumps(_export(_metric("heart_rate_variability", "ms", points))).encode()
    assert len(fat) > 64 * 1024
    url, raw, headers = _push(fat)
    resp = await client.post(url, content=raw, headers=headers)
    assert resp.status_code == 413
    assert resp.json()["detail"] == "body_too_large"
    assert await _rows(db_pool) == []


async def test_no_sample_value_reaches_a_log_line(client):
    """Structured logs go to disk and to OTel. A stream of resting heart rates
    there is a health record by another route."""
    steps, hrv, sleep_hours = 13579, 24680.5, 6.25
    url, raw, headers = _push(
        _export(
            _metric("step_count", "count", [
                {"date": "2026-07-30 00:00:00 +0530", "qty": steps},
            ]),
            _metric("heart_rate_variability", "ms", [
                {"date": "2026-07-30 03:12:00 +0530", "qty": hrv},
            ]),
            _metric("sleep_analysis", "hr", [
                {"date": "2026-07-30 00:00:00 +0530", "totalSleep": sleep_hours},
            ]),
        )
    )
    with capture_logs() as captured:
        resp = await client.post(url, content=raw, headers=headers)
    assert resp.status_code == 202, resp.text

    # Non-vacuity: the lane really did log, so "nothing found" means "nothing
    # leaked", not "nothing captured".
    assert any(e.get("event") == "health_push_recorded" for e in captured)

    haystack = json.dumps(captured, default=str)
    for leak in (str(steps), str(hrv), str(sleep_hours), "375.0"):
        assert leak not in haystack, f"{leak} leaked into a log line"
    # The 202 body a proxy log keeps is counts only, too.
    assert str(steps) not in resp.text
