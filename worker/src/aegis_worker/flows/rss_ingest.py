"""RssIngestFlow — hourly RSS feed poll across all kind='rss' channels.

Since #511/#512 each run also:

* stores an entry the way its feed's `channels.config.ingest` says: `full`
  (fetch and store the page or PDF — the default), `abstract` (the title and
  summary the feed carries, nothing fetched) or `gate` (full when the entry
  names a topic term, abstract otherwise);
* records every entry it stored or failed in `feed_entries`, so a feed's worth
  can be measured, and each fetch's outcome in the channel's config;
* reports a feed that failed `feeds.FAILING_AFTER` fetches in a row (every
  run) or published nothing for `stale_after_days` (once a day) to the problem
  hub as a `feeds` finding, which resolves itself when the feed recovers.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from aegis.services import feeds

    from aegis_worker.activities.rss import (
        FetchFeedInput,
        FetchFeedResult,
        gate_pattern,
        passes_gate,
    )
    from aegis_worker.shared.retry import ACT_RETRY, NO_RETRY, RETRY_ONCE


_ACT_TIMEOUT = timedelta(seconds=60)
_FETCH_TIMEOUT = timedelta(seconds=120)
_HUB_TIMEOUT = timedelta(seconds=120)

# A feed whose last known entry is older than this is almost certainly dead
# (moved, discontinued, or was never a real feed) rather than just quiet —
# surface it instead of polling it hourly forever (issue #120).
_STALE_FEED_DAYS = 90

# Guards every activity call #511/#512 added, so a run that started on the old
# code replays without them. A run lasts minutes: `workflow.deprecate_patch`
# one deploy later.
_PATCH_FEEDS = "rss-feeds-511"
# Guards the tracked-topic attach (#513). Same deprecation note.
_PATCH_TOPICS = "research-hub-513"
# Attaching can raise a topic's task, which is a Todoist round trip.
_TOPICS_TIMEOUT = timedelta(seconds=120)
# The UTC hour whose run reconciles the stale findings. Staleness is measured
# in days, so once a day is enough, and it keeps the hub from recording 24
# occurrences a day of a feed that is merely quiet.
_STALE_REVIEW_HOUR = 3
# Statuses of process_content / store_feed_abstract that settle an entry
# without storing it: an empty extraction, a URL the store already had, content
# extraction switched off, a link off the public internet (refused for good).
# Nothing to retry, nothing new stored.
_SETTLED_UNSTORED = frozenset({"empty", "duplicate", "disabled", "refused"})


def _newest(*stamps: str | None) -> str | None:
    present = [s for s in stamps if s]
    return max(present) if present else None


def _parse_stamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)


def _stale_finding(
    now: datetime, identifier: str, config: dict, label: str, last_entry: str | None
) -> dict | None:
    """A `feed_stale` finding when the feed's newest entry is older than its limit.

    A feed that never gave a dated entry (no cursor, or one that is not a
    timestamp) is measured from when AEGIS began polling it
    (`config.tracking_since`, set by `record_feed_run`). Before, such a feed
    could never be reported stale at all."""
    last = _parse_stamp(last_entry)
    dated = last is not None
    if last is None:
        last = _parse_stamp(config.get("tracking_since"))
    if last is None:
        return None
    limit = feeds.stale_after_days(config)
    if (now - last).days < limit:
        return None
    title = (
        f"RSS feed {label} has published nothing since {last.date().isoformat()}"
        if dated
        else f"RSS feed {label} has given no dated entry since AEGIS began polling it on "
        f"{last.date().isoformat()}"
    )
    return {
        "klass": "feed_stale",
        "subject": identifier,
        "title": title,
        "severity": "info",
        "payload": {
            "url": identifier,
            "last_entry_at": last_entry,
            "tracking_since": config.get("tracking_since"),
            "stale_after_days": limit,
        },
    }


@dataclass
class RssIngestInput:
    agent_id: str = "raphael"
    # The UTC hour whose run reconciles the stale findings; negative = every
    # run (tests and a manual trigger). Scheduled runs use the default.
    stale_review_hour: int = _STALE_REVIEW_HOUR


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
        v2 = workflow.patched(_PATCH_FEEDS)
        notes: dict = {}
        pattern = None
        if v2:
            try:
                terms = await workflow.execute_activity(
                    "load_gate_terms",
                    start_to_close_timeout=_ACT_TIMEOUT,
                    retry_policy=RETRY_ONCE,
                )
                pattern = gate_pattern(list(terms or []))
            except Exception as exc:
                # No terms means the gate lets everything through: a failed
                # config read costs full fetches, never a lost entry.
                workflow.logger.warning("rss_gate_terms_degraded err=%s", str(exc)[:200])
                notes["gate_terms_degraded"] = True

        total_entries = 0
        total_ingested = 0
        total_failed = 0
        total_abstract = 0
        errors = 0
        per_feed: list[dict] = []
        failing: list[dict] = []
        stale: list[dict] = []
        # Every stored entry, for the tracked-topic match at the end (#513).
        topic_items: list[dict] = []
        now = workflow.now()
        # The daily review: the run whose hour is `stale_review_hour` (every
        # run when it is negative — tests and a manual trigger). Stale feeds
        # are reconciled then, and a feed that is still failing records its
        # daily occurrence then.
        review = input.stale_review_hour < 0 or now.hour == input.stale_review_hour

        for ch in channels:
            identifier = ch["identifier"]
            config = ch.get("config") or {}
            since = config.get("last_cursor")
            label = feeds.feed_label(identifier, config)
            mode = feeds.ingest_mode(config) if v2 else "full"

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

            fetch_error = ""
            result: FetchFeedResult | None = None
            try:
                result = await workflow.execute_activity(
                    "fetch_feed",
                    FetchFeedInput(url=identifier, since_cursor=since),
                    result_type=FetchFeedResult,
                    start_to_close_timeout=_FETCH_TIMEOUT,
                    retry_policy=NO_RETRY,
                )
            except Exception as exc:
                fetch_error = str(exc)[:200] or "fetch failed"
            else:
                # An empty parse that says why is a failed fetch too. Before
                # #511 a dead or moved feed read as a quiet one here.
                if v2 and result.error and not result.entries:
                    fetch_error = result.error
            if fetch_error:
                workflow.logger.warning(
                    "rss_fetch_failed url=%s err=%s", identifier, fetch_error[:200]
                )
                errors += 1
                per_feed.append({"feed": identifier, "status": "fetch_failed"})
                if v2:
                    failures = await self._record_run(ch, {"ok": False, "error": fetch_error})
                    per_feed[-1]["fetch_failures"] = failures
                    if failures >= feeds.FAILING_AFTER:
                        failing.append(
                            {
                                "klass": "feed_failing",
                                "subject": identifier,
                                "title": f"RSS feed {label} failed {failures} fetches in a row",
                                "severity": "warning",
                                "payload": {
                                    "url": identifier,
                                    "error": fetch_error,
                                    "fetch_failures": failures,
                                },
                                # An occurrence on crossing the threshold and
                                # at the daily review; every other hour it only
                                # keeps the problem open. Hourly occurrences
                                # posted "N more occurrences" on the task 24
                                # times a day for one dead feed.
                                "record": failures == feeds.FAILING_AFTER or review,
                            }
                        )
                    finding = _stale_finding(now, identifier, config, label, since)
                    if finding:
                        stale.append(finding)
                continue

            if not result.entries:
                per_feed.append({"feed": identifier, "entries": 0})
                if v2:
                    await self._record_run(ch, {"ok": True, "backlog": 0})
                    finding = _stale_finding(now, identifier, config, label, since)
                    if finding:
                        stale.append(finding)
                continue

            # Per-feed throttle. arxiv cs.AI publishes its whole day in ONE
            # burst — 270-750 entries — and every entry is fully fetched and
            # chunked, which is how PDFs came to be 94.7% of the knowledge
            # corpus and how one feed came to crowd every other source out of
            # retrieval. Volume is one knob, and it lives in the DB:
            # `channels.config.max_entries_per_run`, absent or 0 meaning
            # unlimited. The other is the ingest mode (#512): an `abstract`
            # feed stores one short row per entry and fetches nothing.
            #
            # OLDEST-first, not newest-first. A cap that took the newest N and
            # then advanced the cursor past the rest would silently drop them —
            # the exact data-loss shape the cursor logic below exists to
            # prevent. Taking the oldest N leaves the remainder ABOVE the
            # cursor, so the next poll picks them up: a burst drains over
            # several hours instead of being lost or ingested all at once.
            available = len(result.entries)
            cap = config.get("max_entries_per_run") or 0
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
            feed_abstract = 0
            entry_rows: list[dict] = []
            # Track the highest entry timestamp that has a DEFINITE
            # outcome — either the entry was stored, or it was a known dup
            # (idempotency claim already held). Failed entries DO NOT advance
            # the cursor: leaving them inside the next-tick window gives the
            # store another shot. Earlier code blindly advanced to
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
                    use_mode = mode
                    if mode == "gate":
                        use_mode = "full" if passes_gate(pattern, entry) else "abstract"
                    # An exception is a failure. On the new path a returned
                    # `status: error` is one too: process_content swallows a
                    # store failure into that status, and counting it as
                    # ingested is how a batch could report everything landed.
                    status = "error"
                    content_id = None
                    try:
                        if use_mode == "abstract":
                            res = await workflow.execute_activity(
                                "store_feed_abstract",
                                args=[
                                    entry.get("link", ""),
                                    entry.get("title", ""),
                                    entry.get("summary", ""),
                                ],
                                start_to_close_timeout=_ACT_TIMEOUT,
                                retry_policy=NO_RETRY,
                            )
                        else:
                            # Kick off async content extraction. Thread
                            # `summary` through as fallback raw_text so KS
                            # still ingests something useful when its own
                            # scrape fails (dead link, paywall, JS-rendered
                            # SPA).
                            #
                            # 180s ceiling: RSS entries are usually fast, but
                            # PDFs (arxiv etc.) can take longer; lining up
                            # close to the KS server-side ingestion budget
                            # without paying the full 600s on every
                            # well-behaved feed.
                            res = await workflow.execute_activity(
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
                        res = res if isinstance(res, dict) else {}
                        status = str(res.get("status") or "ok") if v2 else "ok"
                        content_id = res.get("content_id")
                    except Exception as exc:
                        workflow.logger.warning(
                            "rss_process_content_failed url=%s err=%s",
                            entry.get("link", ""),
                            str(exc)[:200],
                        )

                    entry_ok = status == "ok" or status in _SETTLED_UNSTORED
                    if entry_ok:
                        resolved_published = entry.get("published") or None
                        if status == "ok":
                            # Only a real store counts. This used to increment
                            # unconditionally, so `ingested` counted failures
                            # as successes.
                            feed_ingested += 1
                            if use_mode == "abstract":
                                feed_abstract += 1
                            entry_rows.append(
                                {
                                    "external_id": external_id,
                                    "link": entry.get("link", ""),
                                    "content_id": content_id,
                                    "mode": use_mode,
                                    "published": entry.get("published") or "",
                                }
                            )
                            # A stored entry may name a tracked topic (#513).
                            topic_items.append(
                                {
                                    "title": entry.get("title", ""),
                                    "url": entry.get("link", ""),
                                    "summary": (entry.get("summary") or "")[:500],
                                }
                            )
                    else:
                        feed_failed += 1
                        entry_rows.append(
                            {
                                "external_id": external_id,
                                "link": entry.get("link", ""),
                                "content_id": None,
                                "mode": "failed",
                                "published": entry.get("published") or "",
                            }
                        )
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
            total_abstract += feed_abstract

            if v2 and entry_rows:
                try:
                    await workflow.execute_activity(
                        "record_feed_entries",
                        args=[ch["id"], entry_rows],
                        start_to_close_timeout=_ACT_TIMEOUT,
                        retry_policy=RETRY_ONCE,
                    )
                except Exception as exc:
                    # The stats lose a run; the entries themselves are stored.
                    workflow.logger.warning(
                        "rss_record_entries_failed feed=%s err=%s", identifier, str(exc)[:200]
                    )

            # Cursor advances only past entries with a DEFINITE outcome
            # (stored OR known dup). Failed entries stay inside the next-tick
            # window for retry.
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

            entry_summary: dict = {
                "feed": identifier,
                "entries": len(entries),
                "ingested": feed_ingested,
            }
            if mode != "full":
                entry_summary["mode"] = mode
            if feed_abstract:
                entry_summary["abstract"] = feed_abstract
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

            if v2:
                await self._record_run(ch, {"ok": True, "backlog": available - len(entries)})
                finding = _stale_finding(
                    now, identifier, config, label, _newest(since, latest_resolved_published)
                )
                if finding:
                    stale.append(finding)

        # Stored entries that name a tracked topic join its round in the hub
        # (#513), so a story from a feed and the same story from an intel scan
        # are one item there. One call per run, never per entry.
        if topic_items and workflow.patched(_PATCH_TOPICS):
            try:
                attached = await workflow.execute_activity(
                    "attach_topic_items",
                    args=[topic_items, "rss"],
                    start_to_close_timeout=_TOPICS_TIMEOUT,
                    retry_policy=RETRY_ONCE,
                )
                if isinstance(attached, dict) and attached.get("attached"):
                    notes["topic_items"] = attached["attached"]
            except Exception as exc:  # noqa: BLE001 — the entries are stored either way
                workflow.logger.warning("rss_topic_attach_degraded err=%s", str(exc)[:200])
                notes["topics_degraded"] = True

        if v2:
            found: dict = {"failing": len(failing)}
            if not await self._reconcile(["feed_failing"], failing):
                notes["hub_degraded"] = True
            if review:
                found["stale"] = len(stale)
                if not await self._reconcile(["feed_stale"], stale):
                    notes["hub_degraded"] = True
            notes["findings"] = found

        out: dict = {
            "entries": total_entries,
            "ingested": total_ingested,
            "failed": total_failed,
            "errors": errors,
            "feeds": per_feed,
        }
        if total_abstract:
            out["abstract"] = total_abstract
        out.update(notes)
        return out

    async def _record_run(self, ch: dict, outcome: dict) -> int:
        """Record one fetch's outcome; the consecutive failure count, 0 when unknown."""
        try:
            out = await workflow.execute_activity(
                "record_feed_run",
                args=[ch["id"], outcome],
                start_to_close_timeout=_ACT_TIMEOUT,
                retry_policy=RETRY_ONCE,
            )
        except Exception as exc:
            workflow.logger.warning(
                "rss_record_run_failed feed=%s err=%s", ch.get("identifier"), str(exc)[:200]
            )
            return 0
        return int((out or {}).get("fetch_failures") or 0)

    async def _reconcile(self, classes: list[str], findings: list[dict]) -> bool:
        """Hand the hub this run's feed findings; False when the hub could not take them."""
        try:
            await workflow.execute_activity(
                "reconcile_findings",
                args=[
                    {
                        "source": "feeds",
                        "subject_kind": "feed",
                        "classes": classes,
                        "findings": findings,
                    }
                ],
                start_to_close_timeout=_HUB_TIMEOUT,
                retry_policy=NO_RETRY,
            )
        except Exception as exc:  # noqa: BLE001 — the feeds were polled either way
            workflow.logger.warning("rss_hub_reconcile_failed err=%s", str(exc)[:200])
            return False
        return True
