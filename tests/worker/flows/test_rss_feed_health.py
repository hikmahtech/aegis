"""How RssIngestFlow judges a feed's health (#511, from the audit):

* stale is measured from the last entry the store kept (`feed_entries`), not
  from the cursor — the cursor also moves past duplicates and entries that
  settled without being stored;
* a feed record that could not be written says nothing about the feed, so the
  run keeps its finding rather than resolving it;
* a failing feed resolves only after two good fetches in a row;
* "tracking since" is what `record_feed_run` reports (the first stored entry,
  else the first poll), the same value the feed stats show."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tests.worker.flows.test_rss_feeds_flow import FEED, Rec, _run, _stubs


def _findings(rec: Rec, klass: str) -> list[dict]:
    return next(r for r in rec.reconciles if r["classes"] == [klass])["findings"]


@pytest.mark.asyncio
async def test_stale_is_measured_from_the_last_stored_entry_not_the_cursor():
    rec = Rec()
    config = {"label": "Dupes", "last_cursor": datetime.now(UTC).isoformat(), "stale_after_days": 30}
    await _run(
        _stubs(rec, config=config, entries=[], last_stored_at="2020-01-01T00:00:00+00:00"),
        "fh-stored",
        stale_review_hour=-1,
    )
    stale = _findings(rec, "feed_stale")
    assert [f["klass"] for f in stale] == ["feed_stale"]
    assert "2020-01-01" in stale[0]["title"]


@pytest.mark.asyncio
async def test_a_feed_that_stored_something_lately_is_not_stale_whatever_its_cursor_says():
    rec = Rec()
    config = {"label": "Old", "last_cursor": "2020-01-01T00:00:00+00:00", "stale_after_days": 30}
    await _run(
        _stubs(rec, config=config, entries=[], last_stored_at=datetime.now(UTC).isoformat()),
        "fh-fresh",
        stale_review_hour=-1,
    )
    assert _findings(rec, "feed_stale") == []


@pytest.mark.asyncio
async def test_a_failing_feed_whose_record_failed_stays_found():
    rec = Rec()
    await _run(
        _stubs(rec, config={"label": "Dead"}, fetch_error="HTTP 404", failures_before=5,
               record_fails=True),
        "fh-rec-fail",
    )
    failing = _findings(rec, "feed_failing")
    assert [(f["klass"], f["subject"], f["record"]) for f in failing] == [
        ("feed_failing", FEED, False)
    ]


@pytest.mark.asyncio
async def test_a_good_fetch_whose_record_failed_does_not_resolve_the_feed():
    rec = Rec()
    await _run(
        _stubs(rec, config={"label": "Flaky"}, entries=[], record_fails=True), "fh-rec-fail-ok"
    )
    assert [(f["subject"], f["record"]) for f in _findings(rec, "feed_failing")] == [(FEED, False)]


@pytest.mark.asyncio
async def test_one_good_fetch_does_not_end_a_failure():
    rec = Rec()
    await _run(
        _stubs(rec, config={"label": "Back"}, entries=[], failures_before=4, successes_before=0),
        "fh-one-ok",
    )
    assert [(f["subject"], f["record"]) for f in _findings(rec, "feed_failing")] == [(FEED, False)]


@pytest.mark.asyncio
async def test_the_second_good_fetch_in_a_row_resolves_it():
    rec = Rec()
    await _run(
        _stubs(rec, config={"label": "Back"}, entries=[], successes_before=1), "fh-two-ok"
    )
    assert _findings(rec, "feed_failing") == []


@pytest.mark.asyncio
async def test_tracking_since_comes_from_the_feed_record():
    rec = Rec()
    await _run(
        _stubs(rec, config={"label": "Undated"}, entries=[],
               tracking_since="2025-01-01T00:00:00+00:00"),
        "fh-tracking",
        stale_review_hour=-1,
    )
    stale = _findings(rec, "feed_stale")
    assert [f["klass"] for f in stale] == ["feed_stale"]
    assert "began polling" in stale[0]["title"]
