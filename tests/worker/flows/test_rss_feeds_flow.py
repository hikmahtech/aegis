"""RssIngestFlow since #511/#512: ingest modes, the gate, the feed record and
the hub findings."""

from __future__ import annotations

import pytest
from temporalio import activity, workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

with workflow.unsafe.imports_passed_through():
    from aegis_worker.activities.rss import FetchFeedInput, FetchFeedResult
    from aegis_worker.flows.rss_ingest import RssIngestFlow, RssIngestInput

CH_ID = "0b7c1f7e-5d3a-4a52-9c9e-000000000001"
FEED = "https://feeds.example/rss"


class Rec:
    """What the stubs saw, reset per test."""

    def __init__(self):
        self.content: list[str] = []
        self.abstract: list[str] = []
        self.rows: list[dict] = []
        self.runs: list[dict] = []
        self.reconciles: list[dict] = []
        self.released: list[str] = []
        self.cursor: list[str] = []


def _entry(n: int, title: str, summary: str = "") -> dict:
    return {
        "id": f"{FEED}#{n}",
        "title": title,
        "link": f"{FEED}/{n}",
        "summary": summary,
        "published": f"2026-09-10T{n:02d}:00:00+00:00",
    }


def _stubs(
    rec: Rec,
    *,
    config: dict,
    entries: list[dict] | None = None,
    fetch_error: str = "",
    terms: list[str] | None = None,
    terms_fail: bool = False,
    content_status: str = "ok",
    failures_before: int = 0,
):
    state = {"failures": failures_before}

    @activity.defn(name="list_active_channels")
    async def list_channels(kind: str) -> list[dict]:
        return [{"id": CH_ID, "kind": "rss", "identifier": FEED, "config": config, "active": True}]

    @activity.defn(name="fetch_feed")
    async def fetch(inp: FetchFeedInput) -> FetchFeedResult:
        return FetchFeedResult(entries=list(entries or []), error=fetch_error)

    @activity.defn(name="load_gate_terms")
    async def load_terms() -> list[str]:
        if terms_fail:
            raise RuntimeError("settings unreadable")
        return list(terms or [])

    @activity.defn(name="ingest_idempotency_claim")
    async def claim(source_type: str, external_id: str) -> bool:
        return True

    @activity.defn(name="ingest_idempotency_release")
    async def release(source_type: str, external_id: str) -> bool:
        rec.released.append(external_id)
        return True

    @activity.defn(name="process_content")
    async def content(url, title, category, fallback_text="", extra_tags=None) -> dict:
        rec.content.append(url)
        return {"status": content_status, "content_id": f"full-{url}"}

    @activity.defn(name="store_feed_abstract")
    async def abstract(url, title, summary, extra_tags=None) -> dict:
        rec.abstract.append(url)
        return {"status": "ok", "content_id": f"abs-{url}"}

    @activity.defn(name="record_feed_entries")
    async def record_entries(channel_id: str, rows: list[dict]) -> int:
        rec.rows.extend(rows)
        return len(rows)

    @activity.defn(name="record_feed_run")
    async def record_run(channel_id: str, outcome: dict) -> dict:
        rec.runs.append(outcome)
        state["failures"] = 0 if outcome.get("ok") else state["failures"] + 1
        return {"fetch_failures": state["failures"]}

    @activity.defn(name="reconcile_findings")
    async def reconcile(inp: dict) -> dict:
        rec.reconciles.append(inp)
        return {"fresh": [], "attached": 0, "muted": 0, "suppressed": 0, "resolved": []}

    @activity.defn(name="update_channel_config_key")
    async def cursor(kind, identifier, key, value) -> None:
        rec.cursor.append(value)

    return [
        list_channels, fetch, load_terms, claim, release, content, abstract,
        record_entries, record_run, reconcile, cursor,
    ]


async def _run(stubs, wf_id: str, stale_review_hour: int = 99) -> dict:
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(env.client, task_queue="tq", workflows=[RssIngestFlow], activities=stubs),
    ):
        return await env.client.execute_workflow(
            RssIngestFlow.run,
            RssIngestInput(stale_review_hour=stale_review_hour),
            id=wf_id,
            task_queue="tq",
        )


@pytest.mark.asyncio
async def test_an_abstract_feed_fetches_nothing_and_records_each_entry():
    rec = Rec()
    entries = [_entry(1, "Paper one", "abstract one"), _entry(2, "Paper two", "abstract two")]
    result = await _run(
        _stubs(rec, config={"label": "arxiv", "ingest": "abstract"}, entries=entries), "rf-abs"
    )
    assert rec.content == []
    assert rec.abstract == [f"{FEED}/1", f"{FEED}/2"]
    assert result["ingested"] == 2
    assert result["abstract"] == 2
    assert result["feeds"][0]["mode"] == "abstract"
    assert [r["mode"] for r in rec.rows] == ["abstract", "abstract"]
    assert rec.rows[0]["content_id"] == f"abs-{FEED}/1"
    assert rec.runs[-1] == {"ok": True, "backlog": 0}


@pytest.mark.asyncio
async def test_the_gate_fetches_what_names_a_topic_and_abstracts_the_rest():
    rec = Rec()
    entries = [
        _entry(1, "Agents that plan", "a new AI method"),
        _entry(2, "Soil chemistry", "nothing on topic"),
    ]
    result = await _run(
        _stubs(rec, config={"ingest": "gate"}, entries=entries, terms=["ai", "crypto"]), "rf-gate"
    )
    assert rec.content == [f"{FEED}/1"]
    assert rec.abstract == [f"{FEED}/2"]
    assert {r["external_id"]: r["mode"] for r in rec.rows} == {
        f"{FEED}#1": "full",
        f"{FEED}#2": "abstract",
    }
    assert result["abstract"] == 1


@pytest.mark.asyncio
async def test_without_gate_terms_the_gate_lets_everything_through():
    rec = Rec()
    entries = [_entry(1, "Soil chemistry", "")]
    result = await _run(
        _stubs(rec, config={"ingest": "gate"}, entries=entries, terms_fail=True), "rf-noterms"
    )
    assert rec.content == [f"{FEED}/1"]
    assert result["gate_terms_degraded"] is True


@pytest.mark.asyncio
async def test_a_returned_error_status_is_a_failure_that_holds_the_cursor():
    """process_content swallows a store failure into `status: error`; counting
    that as ingested is how a batch could report everything landed."""
    rec = Rec()
    entries = [_entry(1, "One"), _entry(2, "Two")]
    result = await _run(
        _stubs(rec, config={}, entries=entries, content_status="error"), "rf-error"
    )
    assert result["ingested"] == 0
    assert result["failed"] == 2
    assert rec.released == [f"{FEED}#1", f"{FEED}#2"]
    assert rec.cursor == []
    assert {r["mode"] for r in rec.rows} == {"failed"}


@pytest.mark.asyncio
async def test_a_settled_but_unstored_entry_advances_the_cursor_without_counting():
    rec = Rec()
    result = await _run(
        _stubs(rec, config={}, entries=[_entry(1, "One")], content_status="empty"), "rf-empty"
    )
    assert result["ingested"] == 0
    assert result["failed"] == 0
    assert rec.cursor == ["2026-09-10T01:00:00+00:00"]
    assert rec.rows == []


@pytest.mark.asyncio
async def test_a_failed_fetch_becomes_a_hub_finding_on_the_third_in_a_row():
    rec = Rec()
    result = await _run(
        _stubs(rec, config={"label": "Dead"}, fetch_error="HTTP 404", failures_before=2), "rf-fail"
    )
    assert result["errors"] == 1
    assert result["feeds"][0]["fetch_failures"] == 3
    assert rec.runs == [{"ok": False, "error": "HTTP 404"}]
    failing = next(r for r in rec.reconciles if r["classes"] == ["feed_failing"])
    assert failing["source"] == "feeds"
    assert failing["subject_kind"] == "feed"
    assert [f["klass"] for f in failing["findings"]] == ["feed_failing"]
    assert failing["findings"][0]["subject"] == FEED
    assert "Dead" in failing["findings"][0]["title"]
    assert result["findings"]["failing"] == 1


@pytest.mark.asyncio
async def test_a_first_failed_fetch_is_not_yet_a_finding_but_is_still_reconciled():
    """The failing reconcile runs every time: an empty list is what resolves a
    feed that recovered."""
    rec = Rec()
    await _run(_stubs(rec, config={}, fetch_error="HTTP 503"), "rf-fail-1")
    failing = next(r for r in rec.reconciles if r["classes"] == ["feed_failing"])
    assert failing["findings"] == []


@pytest.mark.asyncio
async def test_a_quiet_feed_past_its_limit_is_reported_stale_at_the_review_hour():
    rec = Rec()
    config = {"label": "Quiet", "last_cursor": "2020-01-01T00:00:00+00:00", "stale_after_days": 30}
    result = await _run(_stubs(rec, config=config, entries=[]), "rf-stale", stale_review_hour=-1)
    stale = next(r for r in rec.reconciles if r["classes"] == ["feed_stale"])
    assert [f["klass"] for f in stale["findings"]] == ["feed_stale"]
    assert "2020-01-01" in stale["findings"][0]["title"]
    assert result["findings"]["stale"] == 1


@pytest.mark.asyncio
async def test_stale_findings_wait_for_the_review_hour():
    rec = Rec()
    config = {"last_cursor": "2020-01-01T00:00:00+00:00"}
    await _run(_stubs(rec, config=config, entries=[]), "rf-stale-wait", stale_review_hour=99)
    assert [r["classes"] for r in rec.reconciles] == [["feed_failing"]]
