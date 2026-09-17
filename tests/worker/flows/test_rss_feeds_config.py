"""RssIngestFlow reads its thresholds from the `feeds_config` row (through
`load_feeds_config`); a non-default row changes what a run does, and a failed
read runs on the code defaults and says so."""

from __future__ import annotations

import pytest
from temporalio import activity

from tests.worker.flows.test_rss_feeds_flow import FEED, Rec, _entry, _run, _stubs


def _config_stub(value: dict | None, *, fail: bool = False):
    @activity.defn(name="load_feeds_config")
    async def load_feeds_config() -> dict:
        if fail:
            raise RuntimeError("settings unreadable")
        return dict(value or {})

    return load_feeds_config


def _findings(rec: Rec, klass: str) -> list[dict]:
    return [f for r in rec.reconciles for f in r["findings"] if f["klass"] == klass]


@pytest.mark.asyncio
async def test_failing_after_from_the_row_decides_when_one_failure_is_a_finding():
    rec = Rec()
    stubs = _stubs(rec, config={"label": "Dead"}, fetch_error="HTTP 503")
    out = await _run(stubs + [_config_stub({"failing_after": 1})], "rf-cfg-fail-1")
    found = _findings(rec, "feed_failing")
    assert len(found) == 1 and found[0]["record"] is True
    assert found[0]["payload"]["fetch_failures"] == 1
    assert "feeds_config_degraded" not in out

    rec = Rec()
    stubs = _stubs(rec, config={"label": "Dead"}, fetch_error="HTTP 503")
    await _run(stubs + [_config_stub({"failing_after": 5})], "rf-cfg-fail-5")
    assert [f for f in _findings(rec, "feed_failing") if f.get("record") is not False] == []


@pytest.mark.asyncio
async def test_default_ingest_from_the_row_applies_to_a_feed_that_sets_none():
    rec = Rec()
    entries = [_entry(1, "Paper one", "abstract one")]
    stubs = _stubs(rec, config={"label": "arxiv"}, entries=entries)
    out = await _run(stubs + [_config_stub({"default_ingest": "abstract"})], "rf-cfg-abs")
    assert rec.content == [] and rec.abstract == [f"{FEED}/1"]
    assert out["feeds"][0]["mode"] == "abstract"

    rec = Rec()
    stubs = _stubs(rec, config={"label": "arxiv", "ingest": "full"}, entries=entries)
    await _run(stubs + [_config_stub({"default_ingest": "abstract"})], "rf-cfg-abs-own")
    assert rec.content == [f"{FEED}/1"], "the feed's own mode still wins"


@pytest.mark.asyncio
async def test_recovered_after_from_the_row_decides_when_a_failing_feed_is_held():
    rec = Rec()
    stubs = _stubs(rec, config={"label": "Flaky"}, entries=[_entry(1, "Back")], successes_before=1)
    await _run(stubs + [_config_stub({"recovered_after": 1})], "rf-cfg-rec-1")
    assert _findings(rec, "feed_failing") == [], "two good fetches with recovered_after=1: resolved"

    rec = Rec()
    stubs = _stubs(rec, config={"label": "Flaky"}, entries=[_entry(1, "Back")], successes_before=1)
    await _run(stubs + [_config_stub({"recovered_after": 3})], "rf-cfg-rec-3")
    held = _findings(rec, "feed_failing")
    assert len(held) == 1 and held[0]["record"] is False


@pytest.mark.asyncio
async def test_a_failed_config_read_runs_on_the_defaults_and_says_so():
    rec = Rec()
    stubs = _stubs(rec, config={"label": "Dead"}, fetch_error="HTTP 503", failures_before=2)
    out = await _run(stubs + [_config_stub(None, fail=True)], "rf-cfg-degraded")
    assert out["feeds_config_degraded"] is True
    found = _findings(rec, "feed_failing")
    assert len(found) == 1 and found[0]["payload"]["fetch_failures"] == 3, "the default of 3"
