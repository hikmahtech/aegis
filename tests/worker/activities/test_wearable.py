"""WearableActivities — vendor poll (stubbed HTTP) + observation write (real DB).

No test in this file touches the network: every HTTP interaction is a `respx`
route, and the two "not configured" tests assert that a route registered for
the vendor was never called at all — an empty token must not produce a request,
not merely an ignored response.

The write half runs against the session's real, migrated Postgres, because the
whole point of `record_external_observation` is the unique index from migration
021; a fake pool could not enforce it.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import asyncpg
import pytest
import pytest_asyncio
import respx
import structlog.testing
from aegis.db import run_migrations
from aegis_worker.activities.wearable import (
    _MAX_LOOKBACK_DAYS,
    _MAX_PAGES,
    PollWearableInput,
    PollWearableResult,
    RecordWearableInput,
    WearableActivities,
    _window,
)
from httpx import Response
from temporalio.testing import ActivityEnvironment

_BASE = "https://api.ouraring.com/v2/usercollection"

# Prefix every fixture row so assertions can't be satisfied (or broken) by rows
# another test file left behind in the shared database.
PREFIX = "zzb7-"
SOURCE = f"{PREFIX}oura"

TOKEN = "zzb7-not-a-real-token"


def _oura_page(items: list[dict], next_token=None) -> Response:
    return Response(200, json={"data": items, "next_token": next_token})


def _sleep_item(day: str, score: int, item_id: str) -> dict:
    return {
        "id": item_id,
        "day": day,
        "score": score,
        "timestamp": f"{day}T07:30:00+00:00",
    }


def _mock_all(sleep=None, readiness=None, activity=None) -> None:
    respx.get(f"{_BASE}/daily_sleep").mock(return_value=_oura_page(sleep or []))
    respx.get(f"{_BASE}/daily_readiness").mock(return_value=_oura_page(readiness or []))
    respx.get(f"{_BASE}/daily_activity").mock(return_value=_oura_page(activity or []))


@pytest.fixture
def acts():
    return WearableActivities(oura_api_token=TOKEN)


# --------------------------------------------------------------------------
# Fail closed: unconfigured is an observable status and issues NO request.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_missing_token_reports_status_and_never_calls_the_api():
    route = respx.get(f"{_BASE}/daily_sleep").mock(return_value=_oura_page([]))
    acts = WearableActivities(oura_api_token="")
    env = ActivityEnvironment()
    with structlog.testing.capture_logs() as logs:
        result = await env.run(acts.poll_wearable, PollWearableInput(vendor="oura"))
    assert isinstance(result, PollWearableResult)
    assert result.status == "token_missing"
    assert result.records == []
    assert "oura_api_token" in result.detail
    assert route.call_count == 0, "polled the vendor with an empty token"
    assert any(e.get("event") == "wearable_token_missing" for e in logs)


@pytest.mark.asyncio
@respx.mock
async def test_unsupported_vendor_reports_status_and_never_calls_the_api(acts):
    route = respx.get(f"{_BASE}/daily_sleep").mock(return_value=_oura_page([]))
    env = ActivityEnvironment()
    with structlog.testing.capture_logs() as logs:
        result = await env.run(acts.poll_wearable, PollWearableInput(vendor="whoop"))
    assert result.status == "unsupported_vendor"
    assert result.records == []
    assert route.call_count == 0
    assert any(e.get("event") == "wearable_vendor_unsupported" for e in logs)


# --------------------------------------------------------------------------
# Happy path + payload mapping.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_poll_maps_every_endpoint_to_its_metrics(acts):
    _mock_all(
        sleep=[_sleep_item("2026-07-30", 78, "sleep-1")],
        readiness=[_sleep_item("2026-07-30", 85, "ready-1")],
        activity=[
            {
                "id": "act-1",
                "day": "2026-07-30",
                "score": 90,
                "steps": 11500,
                "timestamp": "2026-07-30T23:59:00+00:00",
            }
        ],
    )
    env = ActivityEnvironment()
    result = await env.run(acts.poll_wearable, PollWearableInput(vendor="oura"))
    assert result.status == "ok" and result.errors == 0
    by_metric = {r["metric"]: r for r in result.records}
    assert set(by_metric) == {"sleep_score", "readiness_score", "activity_score", "steps"}
    assert by_metric["sleep_score"]["value"] == 78.0
    assert by_metric["sleep_score"]["external_id"] == "sleep-1"
    assert by_metric["steps"]["value"] == 11500.0
    # One vendor record fans out to two metrics under the SAME external id —
    # the dedup key is (source, metric, external_id), not external_id alone.
    assert by_metric["steps"]["external_id"] == "act-1"
    assert by_metric["activity_score"]["external_id"] == "act-1"
    assert all(r["day"] == "2026-07-30" for r in result.records)


@pytest.mark.asyncio
@respx.mock
async def test_poll_sends_bearer_token_and_the_cursor_window(acts):
    route = respx.get(f"{_BASE}/daily_sleep").mock(return_value=_oura_page([]))
    respx.get(f"{_BASE}/daily_readiness").mock(return_value=_oura_page([]))
    respx.get(f"{_BASE}/daily_activity").mock(return_value=_oura_page([]))
    env = ActivityEnvironment()
    await env.run(
        acts.poll_wearable,
        PollWearableInput(vendor="oura", since_cursor="2026-07-25"),
    )
    req = route.calls.last.request
    assert req.headers["authorization"] == f"Bearer {TOKEN}"
    # Cursor is INCLUSIVE — the cursor day is re-requested so a partial day heals.
    assert req.url.params["start_date"] == "2026-07-25"


@pytest.mark.asyncio
@respx.mock
async def test_poll_drops_records_without_a_usable_id_day_or_value(acts):
    _mock_all(
        sleep=[
            _sleep_item("2026-07-30", 70, "ok-1"),
            {"id": "no-day", "score": 60},
            {"day": "2026-07-29", "score": 60},  # no id
            {"id": "null-score", "day": "2026-07-28", "score": None},
            "not-a-dict",
        ]
    )
    env = ActivityEnvironment()
    result = await env.run(acts.poll_wearable, PollWearableInput(vendor="oura"))
    assert result.status == "ok"
    assert [r["external_id"] for r in result.records] == ["ok-1"]


# --------------------------------------------------------------------------
# Degrade, never fail.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_rate_limited_endpoint_degrades_to_an_error_count(acts):
    respx.get(f"{_BASE}/daily_sleep").mock(return_value=Response(429, json={"detail": "slow"}))
    respx.get(f"{_BASE}/daily_readiness").mock(
        return_value=_oura_page([_sleep_item("2026-07-30", 85, "ready-1")])
    )
    respx.get(f"{_BASE}/daily_activity").mock(return_value=_oura_page([]))
    env = ActivityEnvironment()
    result = await env.run(acts.poll_wearable, PollWearableInput(vendor="oura"))
    # Not an exception, not a lost run: the healthy endpoint still returns data
    # and the failure is reported so the flow can hold the cursor.
    assert result.status == "ok"
    assert result.errors == 1
    assert [r["metric"] for r in result.records] == ["readiness_score"]
    assert "daily_sleep" in result.detail


@pytest.mark.asyncio
@respx.mock
async def test_all_endpoints_failing_is_fetch_failed_not_an_exception(acts):
    for ep in ("daily_sleep", "daily_readiness", "daily_activity"):
        respx.get(f"{_BASE}/{ep}").mock(return_value=Response(500, text="boom"))
    env = ActivityEnvironment()
    result = await env.run(acts.poll_wearable, PollWearableInput(vendor="oura"))
    assert result.status == "fetch_failed"
    assert result.errors == 3
    assert result.records == []


@pytest.mark.asyncio
@respx.mock
async def test_malformed_payload_shape_degrades_to_an_error(acts):
    # 200 OK, valid JSON, wrong shape — `data` is an object, not a list.
    respx.get(f"{_BASE}/daily_sleep").mock(
        return_value=Response(200, json={"data": {"score": 70}})
    )
    respx.get(f"{_BASE}/daily_readiness").mock(return_value=Response(200, text="<html>nope"))
    respx.get(f"{_BASE}/daily_activity").mock(return_value=_oura_page([]))
    env = ActivityEnvironment()
    result = await env.run(acts.poll_wearable, PollWearableInput(vendor="oura"))
    assert result.status == "fetch_failed"
    assert result.errors == 2
    assert result.records == []


@pytest.mark.asyncio
@respx.mock
async def test_pagination_follows_next_token_and_stops_at_the_page_cap(acts):
    def _endless(request):
        return _oura_page(
            [_sleep_item("2026-07-30", 70, f"id-{request.url.params.get('next_token', '0')}")],
            next_token="more",
        )

    route = respx.get(f"{_BASE}/daily_sleep").mock(side_effect=_endless)
    respx.get(f"{_BASE}/daily_readiness").mock(return_value=_oura_page([]))
    respx.get(f"{_BASE}/daily_activity").mock(return_value=_oura_page([]))
    env = ActivityEnvironment()
    result = await env.run(acts.poll_wearable, PollWearableInput(vendor="oura"))
    # Literal, not `_MAX_PAGES`: an assertion whose expected value is the
    # constant under test passes no matter what that constant becomes.
    assert route.call_count == 5, "followed next_token the wrong number of times"
    assert _MAX_PAGES == 5, "page cap changed — update the expected call count"
    assert result.status == "ok"


def test_window_clamps_a_stale_cursor_to_the_lookback_floor():
    """A cursor the flow has been holding back for months must not grow the
    request window without bound."""
    today = date(2026, 7, 31)
    start, end = _window("2019-01-01", 7, today)
    assert end == today
    assert start == today - timedelta(days=_MAX_LOOKBACK_DAYS)
    # No cursor → the configured lookback.
    assert _window(None, 7, today)[0] == today - timedelta(days=7)
    # Garbage cursor → falls back to the lookback rather than raising.
    assert _window("not-a-date", 7, today)[0] == today - timedelta(days=7)


# --------------------------------------------------------------------------
# The write half — real Postgres, real unique index.
# --------------------------------------------------------------------------


async def _wipe(pool: asyncpg.Pool) -> None:
    await pool.execute("DELETE FROM life.observations WHERE source LIKE $1", f"{PREFIX}%")


@pytest_asyncio.fixture(loop_scope="function")
async def pool(db_pool):
    await run_migrations(db_pool)
    await _wipe(db_pool)
    yield db_pool
    await _wipe(db_pool)


def _rec(metric: str, value: float, day: str, external_id: str) -> dict:
    return {
        "external_id": external_id,
        "metric": f"{PREFIX}{metric}",
        "value": value,
        "day": day,
        "observed_at": f"{day}T07:30:00+00:00",
        "metadata": {"vendor": SOURCE},
    }


@pytest.mark.asyncio
async def test_repeated_poll_of_the_same_window_creates_no_duplicate_rows(pool):
    records = [
        _rec("sleep_score", 78, "2026-07-29", "sleep-29"),
        _rec("sleep_score", 81, "2026-07-30", "sleep-30"),
        _rec("steps", 11500, "2026-07-30", "act-30"),
    ]
    acts = WearableActivities(oura_api_token=TOKEN, db_pool=pool)
    env = ActivityEnvironment()

    first = await env.run(
        acts.record_wearable_observations,
        RecordWearableInput(source=SOURCE, records=records),
    )
    assert (first.written, first.duplicates, first.failed) == (3, 0, 0)
    assert first.latest_resolved_day == "2026-07-30"

    # Exactly the poll a 6-hourly schedule makes: the same window again.
    second = await env.run(
        acts.record_wearable_observations,
        RecordWearableInput(source=SOURCE, records=records),
    )
    assert (second.written, second.duplicates, second.failed) == (0, 3, 0)
    assert second.latest_resolved_day == "2026-07-30"

    total = await pool.fetchval(
        "SELECT count(*) FROM life.observations WHERE source = $1", SOURCE
    )
    assert total == 3, "repeated poll duplicated observation rows"


@pytest.mark.asyncio
async def test_same_external_id_under_two_metrics_is_two_rows(pool):
    """One vendor record fans out to several metrics; the dedup key is the
    triple, so they must not collide with each other."""
    acts = WearableActivities(oura_api_token=TOKEN, db_pool=pool)
    env = ActivityEnvironment()
    await env.run(
        acts.record_wearable_observations,
        RecordWearableInput(
            source=SOURCE,
            records=[
                _rec("activity_score", 90, "2026-07-30", "act-30"),
                _rec("steps", 11500, "2026-07-30", "act-30"),
            ],
        ),
    )
    assert (
        await pool.fetchval("SELECT count(*) FROM life.observations WHERE source = $1", SOURCE)
    ) == 2


@pytest.mark.asyncio
async def test_a_failed_write_holds_the_cursor_below_its_day(pool):
    """A record that cannot be written keeps its day inside the next tick's
    window — the cursor may not step over it, nor over any later day."""
    acts = WearableActivities(oura_api_token=TOKEN, db_pool=pool)
    env = ActivityEnvironment()
    result = await env.run(
        acts.record_wearable_observations,
        RecordWearableInput(
            source=SOURCE,
            records=[
                _rec("sleep_score", 70, "2026-07-28", "sleep-28"),
                # Blank metric — record_external_observation raises ValueError.
                {
                    "external_id": "sleep-29",
                    "metric": "",
                    "value": 71,
                    "day": "2026-07-29",
                    "observed_at": "2026-07-29T07:30:00+00:00",
                    "metadata": {},
                },
                _rec("sleep_score", 72, "2026-07-30", "sleep-30"),
            ],
        ),
    )
    assert result.written == 2 and result.failed == 1
    # 07-30 wrote fine, but 07-29 did not — stopping at 07-28 is what keeps
    # 07-29 re-covered next tick.
    assert result.latest_resolved_day == "2026-07-28"


@pytest.mark.asyncio
async def test_observed_at_comes_from_the_vendor_timestamp(pool):
    acts = WearableActivities(oura_api_token=TOKEN, db_pool=pool)
    env = ActivityEnvironment()
    await env.run(
        acts.record_wearable_observations,
        RecordWearableInput(source=SOURCE, records=[_rec("sleep_score", 78, "2026-07-30", "s1")]),
    )
    observed = await pool.fetchval(
        "SELECT observed_at FROM life.observations WHERE source = $1", SOURCE
    )
    assert observed == datetime(2026, 7, 30, 7, 30, tzinfo=UTC)
