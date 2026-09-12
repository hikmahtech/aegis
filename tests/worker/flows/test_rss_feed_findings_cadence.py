"""How often a broken feed speaks to the hub (#511 validation).

A failing feed used to add an occurrence every hourly run, and each posted
"N more occurrences" on its task: 24 comments a day for one dead feed. It
now records on crossing the threshold and at the daily review, and only keeps
its problem open in between. A feed that never gave a dated entry is measured
from when polling began, so it can be reported stale at all."""

from __future__ import annotations

import pytest

from tests.worker.flows.test_rss_feeds_flow import Rec, _run, _stubs


def _failing(rec: Rec) -> list[dict]:
    return next(r for r in rec.reconciles if r["classes"] == ["feed_failing"])["findings"]


@pytest.mark.asyncio
async def test_crossing_the_threshold_records_an_occurrence():
    rec = Rec()
    await _run(_stubs(rec, config={"label": "Dead"}, fetch_error="HTTP 404", failures_before=2), "rc-cross")
    assert [f["record"] for f in _failing(rec)] == [True]


@pytest.mark.asyncio
async def test_a_feed_that_keeps_failing_records_no_hourly_occurrence():
    rec = Rec()
    await _run(_stubs(rec, config={"label": "Dead"}, fetch_error="HTTP 404", failures_before=3), "rc-hourly")
    findings = _failing(rec)
    assert [f["klass"] for f in findings] == ["feed_failing"], "it must stay found, or it resolves"
    assert findings[0]["record"] is False


@pytest.mark.asyncio
async def test_a_feed_that_keeps_failing_records_once_at_the_daily_review():
    rec = Rec()
    await _run(
        _stubs(rec, config={"label": "Dead"}, fetch_error="HTTP 404", failures_before=5),
        "rc-review",
        stale_review_hour=-1,
    )
    assert [f["record"] for f in _failing(rec)] == [True]


@pytest.mark.asyncio
async def test_a_feed_with_no_dated_entry_is_measured_from_when_polling_began():
    rec = Rec()
    await _run(
        _stubs(rec, config={"label": "Undated", "tracking_since": "2025-01-01T00:00:00+00:00"}, entries=[]),
        "rc-undated",
        stale_review_hour=-1,
    )
    stale = next(r for r in rec.reconciles if r["classes"] == ["feed_stale"])["findings"]
    assert [f["klass"] for f in stale] == ["feed_stale"]
    assert "began polling" in stale[0]["title"]


@pytest.mark.asyncio
async def test_a_young_feed_with_no_dated_entry_is_not_yet_stale():
    from datetime import UTC, datetime

    rec = Rec()
    await _run(
        _stubs(rec, config={"label": "New", "tracking_since": datetime.now(UTC).isoformat()}, entries=[]),
        "rc-young",
        stale_review_hour=-1,
    )
    stale = next(r for r in rec.reconciles if r["classes"] == ["feed_stale"])["findings"]
    assert stale == []
