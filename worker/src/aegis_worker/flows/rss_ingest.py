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

            feed_ingested = 0
            # Track the highest entry timestamp that has a DEFINITE
            # outcome — either `process_content` succeeded, or the
            # entry was a known dup (idempotency claim already held).
            # Failed `process_content` entries DO NOT advance the
            # cursor: leaving them inside the next-tick window gives
            # KS another shot. Earlier code blindly advanced to
            # `result.latest_published`, which silently dropped failed
            # entries on the floor.
            latest_resolved_published: str | None = None
            for entry in result.entries:
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

                    feed_ingested += 1
                    if entry_ok:
                        resolved_published = entry.get("published") or None

                if resolved_published and (
                    latest_resolved_published is None
                    or resolved_published > latest_resolved_published
                ):
                    latest_resolved_published = resolved_published

            total_entries += len(result.entries)
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

            per_feed.append(
                {
                    "feed": identifier,
                    "entries": len(result.entries),
                    "ingested": feed_ingested,
                }
            )

        return {
            "entries": total_entries,
            "ingested": total_ingested,
            "errors": errors,
            "feeds": per_feed,
        }
