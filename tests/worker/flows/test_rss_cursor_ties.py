"""A capped feed whose entries share a timestamp drains the whole batch (#584).

arXiv cs.AI publishes a day as ONE burst of 270-750 entries, and every entry
carries the same `published` timestamp. The cap (`max_entries_per_run`, 30 on
arXiv) took the oldest 30 and moved the cursor to their timestamp, which is the
whole burst's timestamp; `fetch_feed` then dropped every entry `<=` the cursor,
so the rest of the burst was gone for good. Prod stored exactly 30 on every
announcement day, and every later poll that day said `entries: 0`.

The cursor is now a `(published, external id)` pair. These tests drive the
REAL `fetch_feed` (only the download and the parse are faked), because the
filter that dropped the burst lives there, and poll the flow several times
against one channel whose config, claims and store persist between polls.
"""

from __future__ import annotations

import random
import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest
from temporalio import activity, workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

with workflow.unsafe.imports_passed_through():
    from aegis_worker.activities import rss as rss_mod
    from aegis_worker.activities.rss import FetchFeedInput, FetchFeedResult, RssActivities
    from aegis_worker.flows.rss_ingest import RssIngestFlow, RssIngestInput

FEED = "https://arxiv.org/rss/cs.AI"
DAY = (2026, 9, 12, 4, 0, 0)
DAY_ISO = "2026-09-12T04:00:00+00:00"


def _paper(n: int, stamp: tuple = DAY) -> SimpleNamespace:
    """One feedparser entry, as `fetch_feed` reads it."""
    return SimpleNamespace(
        id=f"oai:arXiv.org:2609.{n:05d}v1",
        title=f"Paper {n}",
        link=f"https://arxiv.org/abs/2609.{n:05d}",
        summary="s",
        published_parsed=(*stamp, 0, 0, 0),
        updated_parsed=None,
    )


def _iso(stamp: tuple) -> str:
    y, mo, d, h, mi, s = stamp
    return f"{y:04d}-{mo:02d}-{d:02d}T{h:02d}:{mi:02d}:{s:02d}+00:00"


def _shuffled(papers: list, seed: int = 584) -> list:
    """A feed's own order, which is not the id order."""
    out = list(papers)
    random.Random(seed).shuffle(out)
    return out


class Feed:
    """One channel: its feed document, config, claims and store, kept across polls."""

    def __init__(self, papers, *, config: dict, claimed=(), fail_once=()):
        self.papers = list(papers)
        self.config = dict(config)
        self.claims: set[str] = set(claimed)
        self.fail_once: set[str] = set(fail_once)  # links whose first store fails
        self.stored: list[str] = []  # links, in the order they were stored
        self.tried: list[list[str]] = []  # per poll, the links process_content saw
        self.cursors: list[tuple] = []  # per poll, (last_cursor, last_cursor_id) after it


def _activities(feed: Feed, monkeypatch) -> list:
    async def download(url: str, user_agent: str = ""):
        return b"<rss/>", {}, ""

    def parse(body, response_headers=None):
        return SimpleNamespace(entries=list(feed.papers), bozo=0, version="rss20")

    monkeypatch.setattr(rss_mod, "_download_feed", download)
    monkeypatch.setattr("feedparser.parse", parse)

    @activity.defn(name="list_active_channels")
    async def channels(kind: str) -> list[dict]:
        return [
            {"id": "c1", "kind": "rss", "identifier": FEED, "config": dict(feed.config), "active": True}
        ]

    @activity.defn(name="load_feeds_config")
    async def feeds_config() -> dict:
        return {}

    @activity.defn(name="load_gate_terms")
    async def terms() -> list[str]:
        return []

    @activity.defn(name="ingest_idempotency_claim")
    async def claim(source_type: str, external_id: str) -> bool:
        if external_id in feed.claims:
            return False
        feed.claims.add(external_id)
        return True

    @activity.defn(name="ingest_idempotency_release")
    async def release(source_type: str, external_id: str) -> bool:
        feed.claims.discard(external_id)
        return True

    @activity.defn(name="process_content")
    async def content(url, title, category, fallback_text="", extra_tags=None) -> dict:
        feed.tried[-1].append(url)
        if url in feed.fail_once:
            feed.fail_once.discard(url)
            return {"status": "error"}
        feed.stored.append(url)
        return {"status": "ok", "content_id": f"k-{url}"}

    @activity.defn(name="record_feed_entries")
    async def record_entries(channel_id: str, rows: list[dict]) -> int:
        return len(rows)

    @activity.defn(name="record_feed_run")
    async def record_run(channel_id: str, outcome: dict) -> dict:
        return {"fetch_failures": 0, "fetch_successes": 1}

    @activity.defn(name="reconcile_findings")
    async def reconcile(inp: dict) -> dict:
        return {}

    @activity.defn(name="attach_topic_items")
    async def attach(items: list[dict], origin: str) -> dict:
        return {"attached": 0}

    @activity.defn(name="update_channel_config_key")
    async def write(kind, identifier, key, value) -> None:
        feed.config[key] = value

    return [
        channels, feeds_config, terms, claim, release, content, record_entries,
        record_run, reconcile, attach, write, RssActivities(db_pool=None).fetch_feed,
    ]


async def _poll(feed: Feed, monkeypatch, polls: int) -> list[dict]:
    """Run the flow `polls` times against the same channel."""
    results = []
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue="tq",
            workflows=[RssIngestFlow],
            activities=_activities(feed, monkeypatch),
        ),
    ):
        for _ in range(polls):
            feed.tried.append([])
            results.append(
                await env.client.execute_workflow(
                    RssIngestFlow.run,
                    RssIngestInput(stale_review_hour=99),
                    id=f"rss-ties-{uuid.uuid4()}",
                    task_queue="tq",
                )
            )
            feed.cursors.append((feed.config.get("last_cursor"), feed.config.get("last_cursor_id")))
    return results


def _entries(results: list[dict]) -> list[int]:
    return [r["entries"] for r in results]


def _ingested(results: list[dict]) -> list[int]:
    return [r["ingested"] for r in results]


@pytest.mark.asyncio
async def test_a_burst_sharing_one_timestamp_drains_over_four_polls(monkeypatch):
    """The issue's own test: 100 entries, one timestamp, a cap of 30. Four
    polls store all 100 and the fifth stores nothing. The old cursor stored
    the first 30 and then said `entries: 0` for ever."""
    papers = _shuffled([_paper(n) for n in range(100)])
    feed = Feed(papers, config={"last_cursor": None, "max_entries_per_run": 30})

    results = await _poll(feed, monkeypatch, 5)

    assert _ingested(results) == [30, 30, 30, 10, 0]
    assert _entries(results) == [30, 30, 30, 10, 0]
    assert sorted(feed.stored) == sorted(p.link for p in papers)
    assert len(feed.stored) == len(set(feed.stored)) == 100
    # The cursor ends on the burst's last entry in the pair order.
    assert feed.cursors[-1] == (DAY_ISO, max(p.id for p in papers))
    # A throttled poll still says how deep the queue is.
    assert results[0]["feeds"][0]["backlog"] == 70


@pytest.mark.asyncio
async def test_mixed_timestamps_with_ties_drain_in_order_without_a_gap(monkeypatch):
    """Groups of tied entries between single ones, capped at 4: every entry is
    stored exactly once, and across all the polls they are stored in the
    cursor's own `(published, id)` order, so no poll ever stepped back or
    skipped ahead."""
    sizes = {1: 1, 2: 7, 3: 1, 4: 6, 5: 2, 6: 8}  # hour -> entries at that hour
    papers, n = [], 0
    for hour, size in sizes.items():
        for _ in range(size):
            # Ids NOT in time order, so the id alone would sort them wrongly.
            papers.append(_paper((n * 37) % 101, (2026, 9, 12, hour, 0, 0)))
            n += 1
    papers = _shuffled(papers, seed=7)
    place = {p.link: (_iso(p.published_parsed[:6]), p.id) for p in papers}
    feed = Feed(papers, config={"last_cursor": None, "max_entries_per_run": 4})

    results = await _poll(feed, monkeypatch, 8)

    assert _entries(results) == [4, 4, 4, 4, 4, 4, 1, 0]
    assert len(feed.stored) == len(set(feed.stored)) == 25
    assert [place[link] for link in feed.stored] == sorted(place.values())


@pytest.mark.asyncio
async def test_a_failure_inside_a_tied_batch_is_offered_again(monkeypatch):
    """Ten tied entries, a cap of 5, and the third (in the cursor's order)
    fails once. The cursor stops on the second, the last resolved entry
    before the failure, so the next poll offers the failed one again and it
    is stored; nothing is stepped over."""
    papers = _shuffled([_paper(n) for n in range(10)])
    ordered = sorted(papers, key=lambda p: p.id)
    feed = Feed(
        papers,
        config={"last_cursor": None, "max_entries_per_run": 5},
        fail_once={ordered[2].link},
    )

    results = await _poll(feed, monkeypatch, 4)

    first = results[0]["feeds"][0]
    assert first["failed"] == 1
    assert first["cursor_held"] is False
    assert feed.cursors[0] == (DAY_ISO, ordered[1].id)
    assert feed.tried[1][0] == ordered[2].link, "the failed entry was not offered again"
    assert _ingested(results) == [4, 3, 3, 0]
    assert sorted(feed.stored) == sorted(p.link for p in papers)
    assert len(feed.stored) == 10


@pytest.mark.asyncio
async def test_a_legacy_cursor_drains_the_rest_of_its_batch_without_looping(monkeypatch):
    """A channel from before the fix: only `last_cursor`, sitting on the
    burst's timestamp, with the 30 entries the old cap took already stored.
    A missing id is the lowest id, so the whole burst is offered again. The
    30 resolve as known duplicates and the cursor walks past them, so the
    other 70 are stored and the feed then goes quiet instead of re-offering
    the same 30 on every poll."""
    papers = _shuffled([_paper(n) for n in range(100)])
    # What the old cap took: the first 30 in the feed's own order.
    already = papers[:30]
    feed = Feed(
        papers,
        config={"last_cursor": DAY_ISO, "max_entries_per_run": 30},
        claimed={p.id for p in already},
    )

    results = await _poll(feed, monkeypatch, 6)

    assert _entries(results) == [30, 30, 30, 10, 0, 0]
    assert sum(_ingested(results)) == 70
    assert sorted(feed.stored) == sorted(p.link for p in papers[30:])
    assert not {p.link for p in already} & {link for poll in feed.tried for link in poll}
    assert feed.cursors[-1] == (DAY_ISO, max(p.id for p in papers))


# --------------------------------------------------------------------------
# Replay. RssIngestFlow runs hourly and a capped arXiv run makes 30 content
# calls of up to 180 s each, so a deploy can land mid-run. The new cursor
# write is behind `workflow.patched("rss-cursor-ties")`.
# --------------------------------------------------------------------------


@workflow.defn(name="RssIngestFlow")
class _RssIngestBeforeTies:
    """The activities RssIngestFlow scheduled before #584, for one feed whose
    entries are all new and all store: `last_cursor` written, no
    `last_cursor_id`. Kept so a history it wrote can be replayed against
    today's flow."""

    @workflow.run
    async def run(self, input: RssIngestInput) -> dict:
        t = timedelta(seconds=60)
        channels = await workflow.execute_activity(
            "list_active_channels", "rss", start_to_close_timeout=t
        )
        await workflow.execute_activity("load_feeds_config", start_to_close_timeout=t)
        await workflow.execute_activity("load_gate_terms", start_to_close_timeout=t)
        ch = channels[0]
        result = await workflow.execute_activity(
            "fetch_feed",
            FetchFeedInput(url=ch["identifier"], since_cursor=None),
            result_type=FetchFeedResult,
            start_to_close_timeout=t,
        )
        rows = []
        for e in result.entries:
            await workflow.execute_activity(
                "ingest_idempotency_claim", args=["rss", e["id"]], start_to_close_timeout=t
            )
            await workflow.execute_activity(
                "process_content",
                args=[e["link"], e["title"], "rss", e["summary"]],
                start_to_close_timeout=t,
            )
            rows.append({"external_id": e["id"], "link": e["link"], "mode": "full"})
        await workflow.execute_activity(
            "record_feed_entries", args=[ch["id"], rows], start_to_close_timeout=t
        )
        await workflow.execute_activity(
            "update_channel_config_key",
            args=["rss", ch["identifier"], "last_cursor", result.entries[0]["published"]],
            start_to_close_timeout=t,
        )
        await workflow.execute_activity(
            "record_feed_run", args=[ch["id"], {"ok": True, "backlog": 0}], start_to_close_timeout=t
        )
        await workflow.execute_activity(
            "attach_topic_items", args=[[], "rss"], start_to_close_timeout=t
        )
        await workflow.execute_activity("reconcile_findings", args=[{}], start_to_close_timeout=t)
        return {}


async def _history(flow, feed: Feed, monkeypatch):
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue="tq",
            workflows=[flow],
            activities=_activities(feed, monkeypatch),
        ),
    ):
        feed.tried.append([])
        handle = await env.client.start_workflow(
            flow.run,
            RssIngestInput(stale_review_hour=99),
            id=f"rss-ties-replay-{uuid.uuid4()}",
            task_queue="tq",
        )
        await handle.result()
        return await handle.fetch_history()


@pytest.mark.asyncio
async def test_a_run_started_before_the_change_replays_on_the_new_flow(monkeypatch):
    """Falsifiable: write `last_cursor_id` without the `patched` guard and
    this replay raises a nondeterminism error, because the old history has
    `record_feed_run` where the new flow schedules its second cursor write."""
    feed = Feed([_paper(1), _paper(2)], config={"last_cursor": None})
    history = await _history(_RssIngestBeforeTies, feed, monkeypatch)
    await Replayer(workflows=[RssIngestFlow]).replay_workflow(history)


@pytest.mark.asyncio
async def test_the_new_flow_replays_its_own_history(monkeypatch):
    papers = [_paper(n) for n in range(6)]
    feed = Feed(papers, config={"last_cursor": DAY_ISO, "max_entries_per_run": 4})
    history = await _history(RssIngestFlow, feed, monkeypatch)
    assert feed.config["last_cursor_id"] == sorted(p.id for p in papers)[3]
    await Replayer(workflows=[RssIngestFlow]).replay_workflow(history)
