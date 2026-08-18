"""RssIngestFlow — hourly RSS feed poll across all kind='rss' channels."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from aegis_worker.activities.rss import FetchFeedInput, FetchFeedResult
    from aegis_worker.shared.retry import ACT_RETRY, NO_RETRY


_ACT_TIMEOUT = timedelta(seconds=60)
_FETCH_TIMEOUT = timedelta(seconds=120)

# A feed whose last known entry is older than this is almost certainly dead
# (moved, discontinued, or was never a real feed) rather than just quiet —
# surface it instead of polling it hourly forever (issue #120).
_STALE_FEED_DAYS = 90


@dataclass
class RssIngestInput:
    agent_id: str = "raphael"


@workflow.defn(name="RssIngestFlow")
class RssIngestFlow:
    @workflow.run
    async def run(self, input: RssIngestInput) -> dict:
        channels = await workflow.execute_activity(
            "list_active_channels",
            "rss",
            start_to_close_timeout=_ACT_TIMEOUT,
            retry_policy=ACT_RETRY,
        )
        total_entries = 0
        total_ingested = 0
        total_failed = 0
        errors = 0
        per_feed: list[dict] = []

        for ch in channels:
            identifier = ch["identifier"]
            since = (ch.get("config") or {}).get("last_cursor")

            # Once per run: flag feeds that haven't yielded a new entry in a
            # long time so they surface instead of being polled silently
            # forever. `since` is the ISO timestamp of the last entry we
            # ever accepted (or None if the feed has never yielded one).
            if since:
                try:
                    last_entry_at = datetime.fromisoformat(since)
                    stale_days = (workflow.now() - last_entry_at).days
                except (ValueError, TypeError):
                    stale_days = 0
                if stale_days > _STALE_FEED_DAYS:
                    workflow.logger.warning(
                        "rss_feed_stale feed=%s days_since_last_entry=%d",
                        identifier,
                        stale_days,
                    )

            try:
                result: FetchFeedResult = await workflow.execute_activity(
                    "fetch_feed",
                    FetchFeedInput(url=identifier, since_cursor=since),
                    result_type=FetchFeedResult,
                    start_to_close_timeout=_FETCH_TIMEOUT,
                    retry_policy=NO_RETRY,
                )
            except Exception as exc:
                workflow.logger.warning(
                    "rss_fetch_failed url=%s err=%s", identifier, str(exc)[:200]
                )
                errors += 1
                per_feed.append({"feed": identifier, "status": "fetch_failed"})
                continue

            if not result.entries:
                per_feed.append({"feed": identifier, "entries": 0})
                continue

            # Per-feed throttle. arxiv cs.AI publishes its whole day in ONE
            # burst — 270-750 entries — and every entry is fully fetched and
            # chunked, which is how PDFs came to be 94.7% of the knowledge
            # corpus and how one feed came to crowd every other source out of
            # retrieval. There is no relevance filter here (unlike
            # IntelligenceScanFlow's significance gate), so volume is the only
            # knob, and it lives in the DB: `channels.config.max_entries_per_run`,
            # absent or 0 meaning unlimited.
            #
            # OLDEST-first, not newest-first. A cap that took the newest N and
            # then advanced the cursor past the rest would silently drop them —
            # the exact data-loss shape the cursor logic below exists to
            # prevent. Taking the oldest N leaves the remainder ABOVE the
            # cursor, so the next poll picks them up: a burst drains over
            # several hours instead of being lost or ingested all at once.
            available = len(result.entries)
            cap = (ch.get("config") or {}).get("max_entries_per_run") or 0
            entries = result.entries
            try:
                cap = int(cap)
            except (TypeError, ValueError):
                workflow.logger.warning(
                    "rss_bad_max_entries_per_run feed=%s value=%r", identifier, cap
                )
                cap = 0
            if cap > 0 and available > cap:
                # "" (no timestamp) sorts first and so is never starved.
                entries = sorted(result.entries, key=lambda e: e.get("published") or "")[:cap]
                workflow.logger.info(
                    "rss_throttled feed=%s took=%d of=%d", identifier, len(entries), available
                )

            feed_ingested = 0
            feed_failed = 0
            # Track the highest entry timestamp that has a DEFINITE
            # outcome — either `process_content` succeeded, or the
            # entry was a known dup (idempotency claim already held).
            # Failed `process_content` entries DO NOT advance the
            # cursor: leaving them inside the next-tick window gives
            # KS another shot. Earlier code blindly advanced to
            # `result.latest_published`, which silently dropped failed
            # entries on the floor.
            #
            # Taking the MAX of resolved entries was not enough, because a
            # batch is not ordered by outcome. If entry A (10:00) fails and
            # entry B (11:00) resolves, the max is 11:00 and `fetch_feed`'s
            # `published_iso <= since_cursor` filter then excludes A for good.
            # `earliest_failed_published` is the real ceiling: the cursor may
            # only move to the newest resolved entry OLDER than the oldest
            # failure. Measured cost of not doing this: 553 of 3835 arXiv
            # entries (14%) lost over 14 days, in two large overnight batches.
            latest_resolved_published: str | None = None
            earliest_failed_published: str | None = None
            # A failure we cannot place in time can't be fenced by a timestamp
            # comparison, so the whole feed holds its cursor for this run
            # rather than risk stepping over it. Costs a re-fetch, never a drop.
            saw_untimed_failure = False
            resolved_published_all: list[str] = []
            for entry in entries:
                external_id = entry.get("id") or entry.get("link", "")
                if not external_id:
                    continue

                new = await workflow.execute_activity(
                    "ingest_idempotency_claim",
                    args=["rss", external_id],
                    start_to_close_timeout=_ACT_TIMEOUT,
                    retry_policy=ACT_RETRY,
                )
                resolved_published: str | None = None
                if not new:
                    # Known dup → no retry needed, cursor may advance.
                    resolved_published = entry.get("published") or None
                else:
                    # Kick off async content extraction. Thread `summary`
                    # through as fallback raw_text so KS still ingests
                    # something useful when its own scrape fails (dead
                    # link, paywall, JS-rendered SPA).
                    #
                    # 180s ceiling: RSS entries are usually fast, but PDFs
                    # (arxiv etc.) can take longer; lining up close to the
                    # KS server-side ingestion budget without paying the
                    # full 600s on every well-behaved feed.
                    entry_ok = False
                    try:
                        await workflow.execute_activity(
                            "process_content",
                            args=[
                                entry.get("link", ""),
                                entry.get("title", ""),
                                "rss",
                                entry.get("summary", ""),
                            ],
                            start_to_close_timeout=timedelta(seconds=180),
                            retry_policy=NO_RETRY,
                        )
                        entry_ok = True
                    except Exception as exc:
                        workflow.logger.warning(
                            "rss_process_content_failed url=%s err=%s",
                            entry.get("link", ""),
                            str(exc)[:200],
                        )

                    if entry_ok:
                        # Only a real ingest counts. This used to increment
                        # unconditionally, so `ingested` in result_summary
                        # counted failures as successes — the one number an
                        # operator would have checked said everything landed.
                        feed_ingested += 1
                        resolved_published = entry.get("published") or None
                    else:
                        feed_failed += 1
                        # Hand the claim back, or the retry this cursor logic
                        # is protecting can never happen: the next poll would
                        # re-see the entry, get "not new", and treat it as a
                        # known dup — which is exactly how a batch can report
                        # 299 entries and 0 ingested.
                        await workflow.execute_activity(
                            "ingest_idempotency_release",
                            args=["rss", external_id],
                            start_to_close_timeout=_ACT_TIMEOUT,
                            retry_policy=ACT_RETRY,
                        )
                        failed_published = entry.get("published") or None
                        if not failed_published:
                            saw_untimed_failure = True
                        elif (
                            earliest_failed_published is None
                            or failed_published < earliest_failed_published
                        ):
                            earliest_failed_published = failed_published

                if resolved_published:
                    resolved_published_all.append(resolved_published)

            # Cursor ceiling: newest resolved entry strictly older than the
            # oldest failure. Computed after the loop because a failure can
            # appear after the resolved entry it has to fence.
            if saw_untimed_failure:
                latest_resolved_published = None
            else:
                eligible = [
                    p
                    for p in resolved_published_all
                    if earliest_failed_published is None or p < earliest_failed_published
                ]
                latest_resolved_published = max(eligible) if eligible else None

            total_entries += len(entries)
            total_ingested += feed_ingested

            # Cursor advances only past entries with a DEFINITE outcome
            # (success OR known dup). Failed `process_content` entries
            # stay inside the next-tick window for retry.
            if latest_resolved_published:
                await workflow.execute_activity(
                    "update_channel_config_key",
                    args=[
                        "rss",
                        identifier,
                        "last_cursor",
                        latest_resolved_published,
                    ],
                    start_to_close_timeout=_ACT_TIMEOUT,
                    retry_policy=ACT_RETRY,
                )

            entry_summary = {
                "feed": identifier,
                "entries": len(entries),
                "ingested": feed_ingested,
            }
            # A throttled feed has a backlog. Say so, or "entries: 30" on a
            # 300-entry burst reads as a quiet feed rather than a queue nine
            # hours deep.
            if len(entries) < available:
                entry_summary["available"] = available
                entry_summary["backlog"] = available - len(entries)
            # Report failures, and report a HELD cursor as its own fact. Both
            # were previously invisible: `entries: 299, ingested: 0` looked
            # like a quiet feed rather than a batch that lost everything.
            if feed_failed:
                entry_summary["failed"] = feed_failed
                entry_summary["cursor_held"] = latest_resolved_published is None
            per_feed.append(entry_summary)
            total_failed += feed_failed

        return {
            "entries": total_entries,
            "ingested": total_ingested,
            "failed": total_failed,
            "errors": errors,
            "feeds": per_feed,
        }
