"""RssIngestFlow — hourly RSS feed poll across all kind='rss' channels.

Since #511/#512 each run also:

* stores an entry the way its feed's `channels.config.ingest` says: `full`
  (fetch and store the page or PDF — the default), `abstract` (the title and
  summary the feed carries, nothing fetched) or `gate` (full when the entry
  names a topic term, abstract otherwise);
* records every entry it stored or failed in `feed_entries`, so a feed's worth
  can be measured, and each fetch's outcome in the channel's config;
* reports a feed that failed `failing_after` fetches in a row (every run) or
  stored nothing for `stale_after_days` (once a day) to the problem hub as a
  `feeds` finding. A failing feed's finding resolves after `recovered_after`
  good fetches in a row; a stale one when the feed stores an entry again.

The thresholds are the `feeds_config` settings row (`services/feeds_config.py`,
Admin → Research → Feed health), read through the `load_feeds_config`
activity at the start of every run; a failed read runs on the code defaults
and says so.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from aegis.errors import error_text
    from aegis.services import feeds, feeds_config

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

# Attaching can raise a topic's task, which is a Todoist round trip.
_TOPICS_TIMEOUT = timedelta(seconds=120)
# Statuses of process_content / store_feed_abstract that settle an entry
# without storing it: an empty extraction, a URL the store already had, content
# extraction switched off, a link off the public internet (refused for good).
# Nothing to retry, nothing new stored.
_SETTLED_UNSTORED = frozenset({"empty", "duplicate", "disabled", "refused"})

# Retired `workflow.patched` ids. The old branches are gone; the markers
# stay one release longer as `workflow.deprecate_patch`, because a run that
# RECORDED one is wedged by a worker whose code no longer mentions it at all
# ("[TMPRL1100] Non-deprecated patch marker encountered"). Drop the calls and
# these ids in the release after next — see #614.
# A capped arXiv run makes 30 content calls of up to 180s each, so a deploy
# lands mid-run often.
_CURSOR_TIES_PATCH = "rss-cursor-ties"


def _external_id(entry: dict) -> str:
    """The id an entry is claimed, recorded and placed by."""
    return entry.get("id") or entry.get("link", "") or ""


def _place(entry: dict) -> tuple[str, str]:
    """Where an entry sits in its feed's order: `(published, external id)`.

    arXiv publishes a day as ONE burst of 270-750 entries that all carry the
    same timestamp, so a timestamp alone cannot say where a capped run
    stopped: the cursor landed on the shared timestamp and `fetch_feed`
    dropped the rest of the burst for good (#584, exactly 30 stored per
    announcement day). The id breaks the tie."""
    return (entry.get("published") or "", _external_id(entry))


def _parse_stamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)


def _stale_finding(
    now: datetime,
    identifier: str,
    config: dict,
    label: str,
    run: dict,
    default_days: int | None = None,
) -> dict | None:
    """A `feed_stale` finding when the last entry the store kept for the feed
    is older than its limit.

    Measured from `feed_entries` (`run["last_stored_at"]`, which
    `record_feed_run` reports), not from the cursor: the cursor also moves past
    duplicates and entries that settled without being stored, so a feed whose
    every entry was a duplicate looked alive. A feed that never stored an
    entry is measured from when AEGIS began tracking it
    (`run["tracking_since"]`, `feeds.tracking_since`) — the date the feed
    stats show too."""
    last_stored = run.get("last_stored_at")
    last = _parse_stamp(last_stored)
    stored = last is not None
    if last is None:
        last = _parse_stamp(run.get("tracking_since"))
    if last is None:
        return None
    limit = feeds.stale_after_days(config, default_days)
    if (now - last).days < limit:
        return None
    title = (
        f"RSS feed {label} has stored no new entry since {last.date().isoformat()}"
        if stored
        else f"RSS feed {label} has stored nothing since AEGIS began polling it on "
        f"{last.date().isoformat()}"
    )
    return {
        "klass": "feed_stale",
        "subject": identifier,
        "title": title,
        "severity": "info",
        "payload": {
            "url": identifier,
            "last_stored_at": last_stored,
            "tracking_since": run.get("tracking_since"),
            "stale_after_days": limit,
        },
    }


def _held(klass: str, identifier: str, label: str) -> dict:
    """A finding that keeps the feed's open problem of `klass` open and records
    nothing. It opens nothing on its own (`hub_watch.reconcile_findings`), so
    it is safe for a feed that has no problem."""
    return {"klass": klass, "subject": identifier, "title": f"RSS feed {label}", "record": False}


def _after_good_fetch(
    now: datetime,
    identifier: str,
    config: dict,
    label: str,
    run: dict | None,
    cfg: dict | None = None,
) -> tuple[list[dict], dict | None]:
    """What a fetch that worked leaves: `(held findings, stale finding)`.

    One good fetch does not end a failure: until `recovered_after` in a row
    (`feeds_config`), the feed's `feed_failing` problem is kept open, so a feed
    that fails every other hour is one problem rather than one opened and
    resolved all day. And a record that could not be written says nothing
    about the feed either way, so both its findings are kept as they are."""
    cfg = cfg or feeds_config.merge(None)
    if run is None:
        return [_held("feed_failing", identifier, label), _held("feed_stale", identifier, label)], None
    held = []
    if int(run.get("fetch_successes") or 0) < int(cfg["recovered_after"]):
        held.append(_held("feed_failing", identifier, label))
    return held, _stale_finding(now, identifier, config, label, run, int(cfg["stale_after_days"]))


@dataclass
class RssIngestInput:
    # The scheduled row's agent; "" (a hand-started run) records no agent.
    agent_id: str = ""
    # The UTC hour whose run reconciles the stale findings; negative = every
    # run (tests and a manual trigger); None = the `feeds_config` row's
    # `stale_review_hour`. Staleness is measured in days, so once a day is
    # enough, and it keeps the hub from recording 24 occurrences a day of a
    # feed that is merely quiet.
    stale_review_hour: int | None = None


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
        notes: dict = {}
        # The deployment's thresholds (`feeds_config`). A failed read runs on
        # the code defaults, which are what the row's defaults are too.
        cfg = feeds_config.merge(None)
        try:
            loaded = await workflow.execute_activity(
                "load_feeds_config",
                start_to_close_timeout=_ACT_TIMEOUT,
                retry_policy=RETRY_ONCE,
            )
            cfg = feeds_config.merge(loaded)
        except Exception as exc:
            workflow.logger.warning("rss_feeds_config_degraded err=%s", error_text(exc))
            notes["feeds_config_degraded"] = True
        pattern = None
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
            workflow.logger.warning("rss_gate_terms_degraded err=%s", error_text(exc))
            notes["gate_terms_degraded"] = True

        total_entries = 0
        total_ingested = 0
        total_failed = 0
        total_abstract = 0
        errors = 0
        per_feed: list[dict] = []
        failing: list[dict] = []
        stale: list[dict] = []
        # Findings kept open without a new occurrence (`_held`).
        held: list[dict] = []
        # Every stored entry, for the tracked-topic match at the end (#513).
        topic_items: list[dict] = []
        now = workflow.now()
        # The daily review: the run whose hour is `stale_review_hour` (every
        # run when it is negative — tests and a manual trigger). Stale feeds
        # are reconciled then, and a feed that is still failing records its
        # daily occurrence then.
        review_hour = (
            input.stale_review_hour
            if input.stale_review_hour is not None
            else int(cfg["stale_review_hour"])
        )
        review = review_hour < 0 or now.hour == review_hour
        failing_after = int(cfg["failing_after"])
        # Once per run, where the old `ties = workflow.patched(...)` read was.
        # deprecate_patch: remove after the next release, see #614
        workflow.deprecate_patch(_CURSOR_TIES_PATCH)
        for ch in channels:
            identifier = ch["identifier"]
            config = ch.get("config") or {}
            since = config.get("last_cursor")
            # The second half of the cursor. A channel whose cursor predates
            # it has only `last_cursor`: "" is the lowest id, so the entries AT
            # that timestamp are offered once more. The ones already stored
            # resolve as known duplicates and the cursor moves past them.
            since_id = str(config.get("last_cursor_id") or "")
            label = feeds.feed_label(identifier, config)
            mode = feeds.ingest_mode(config, cfg["default_ingest"])

            fetch_error = ""
            result: FetchFeedResult | None = None
            try:
                result = await workflow.execute_activity(
                    "fetch_feed",
                    FetchFeedInput(url=identifier, since_cursor=since, since_cursor_id=since_id),
                    result_type=FetchFeedResult,
                    start_to_close_timeout=_FETCH_TIMEOUT,
                    retry_policy=NO_RETRY,
                )
            except Exception as exc:
                fetch_error = error_text(exc)
            else:
                # An empty parse that says why is a failed fetch too. Before
                # #511 a dead or moved feed read as a quiet one here.
                if result.error and not result.entries:
                    fetch_error = result.error
            if fetch_error:
                workflow.logger.warning(
                    "rss_fetch_failed url=%s err=%s", identifier, fetch_error[:200]
                )
                errors += 1
                per_feed.append({"feed": identifier, "status": "fetch_failed"})
                run = await self._record_run(ch, {"ok": False, "error": fetch_error})
                if run is None:
                    # The count is unknown: keep what the hub has open for the
                    # feed, and open and resolve nothing.
                    held += [_held("feed_failing", identifier, label),
                             _held("feed_stale", identifier, label)]
                    continue
                failures = int(run.get("fetch_failures") or 0)
                per_feed[-1]["fetch_failures"] = failures
                if failures >= failing_after:
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
                            "record": failures == failing_after or review,
                        }
                    )
                else:
                    # Under the threshold a failure opens nothing, but it never
                    # resolves a failing feed either: that takes good fetches
                    # (`recovered_after` in a row).
                    held.append(_held("feed_failing", identifier, label))
                finding = _stale_finding(
                    now, identifier, config, label, run, int(cfg["stale_after_days"])
                )
                if finding:
                    stale.append(finding)
                continue

            if not result.entries:
                per_feed.append({"feed": identifier, "entries": 0})
                run = await self._record_run(ch, {"ok": True, "backlog": 0})
                kept, finding = _after_good_fetch(now, identifier, config, label, run, cfg)
                held += kept
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
            #
            # "Oldest" is the `(published, external id)` order, not the
            # timestamp alone: a burst shares ONE timestamp, and a cursor on
            # that timestamp left nothing of the burst above it (#584).
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
                entries = sorted(result.entries, key=lambda e: _place(e))[:cap]
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
            # store another shot. Earlier code blindly advanced to the feed's
            # newest entry, which silently dropped failed entries on the
            # floor.
            #
            # Taking the MAX of resolved entries was not enough, because a
            # batch is not ordered by outcome. If entry A (10:00) fails and
            # entry B (11:00) resolves, the max is 11:00 and `fetch_feed`'s
            # `published_iso <= since_cursor` filter then excludes A for good.
            # `earliest_failed` is the real ceiling: the cursor may only move
            # to the newest resolved entry OLDER than the oldest failure.
            # Measured cost of not doing this: 553 of 3835 arXiv entries (14%)
            # lost over 14 days, in two large overnight batches.
            #
            # Both are `_place` pairs, the cursor's own order (#584), so a
            # failure fences the entries that share its timestamp too: the
            # cursor stops at the last resolved entry before it in that order.
            latest_resolved: tuple[str, str] | None = None
            earliest_failed: tuple[str, str] | None = None
            # A failure we cannot place in time can't be fenced by a timestamp
            # comparison, so the whole feed holds its cursor for this run
            # rather than risk stepping over it. Costs a re-fetch, never a drop.
            saw_untimed_failure = False
            resolved_all: list[tuple[str, str]] = []
            for entry in entries:
                external_id = _external_id(entry)
                if not external_id:
                    continue

                new = await workflow.execute_activity(
                    "ingest_idempotency_claim",
                    args=["rss", external_id],
                    start_to_close_timeout=_ACT_TIMEOUT,
                    retry_policy=ACT_RETRY,
                )
                resolved = False
                if not new:
                    # Known dup → no retry needed, cursor may advance.
                    resolved = True
                else:
                    use_mode = mode
                    if mode == "gate":
                        use_mode = "full" if passes_gate(pattern, entry) else "abstract"
                    # An exception is a failure, and so is a returned
                    # `status: error`: process_content swallows a
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
                        status = str(res.get("status") or "ok")
                        content_id = res.get("content_id")
                    except Exception as exc:
                        workflow.logger.warning(
                            "rss_process_content_failed url=%s err=%s",
                            entry.get("link", ""),
                            error_text(exc),
                        )

                    entry_ok = status == "ok" or status in _SETTLED_UNSTORED
                    if entry_ok:
                        resolved = True
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
                        failed_at = _place(entry)
                        if not failed_at[0]:
                            saw_untimed_failure = True
                        elif earliest_failed is None or failed_at < earliest_failed:
                            earliest_failed = failed_at

                # An entry with no timestamp has no place to move the cursor to.
                resolved_at = _place(entry)
                if resolved and resolved_at[0]:
                    resolved_all.append(resolved_at)

            # Cursor ceiling: newest resolved entry strictly older than the
            # oldest failure. Computed after the loop because a failure can
            # appear after the resolved entry it has to fence.
            if saw_untimed_failure:
                latest_resolved = None
            else:
                eligible = [
                    p for p in resolved_all if earliest_failed is None or p < earliest_failed
                ]
                latest_resolved = max(eligible) if eligible else None

            total_entries += len(entries)
            total_ingested += feed_ingested
            total_abstract += feed_abstract

            if entry_rows:
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
                        "rss_record_entries_failed feed=%s err=%s", identifier, error_text(exc)
                    )

            # Cursor advances only past entries with a DEFINITE outcome
            # (stored OR known dup). Failed entries stay inside the next-tick
            # window for retry.
            if latest_resolved:
                cursor_at, cursor_id = latest_resolved
                # The id BEFORE the timestamp. A run that stops between the
                # two writes leaves the new id beside the old timestamp, which
                # only offers entries again (they resolve as duplicates) or
                # passes over ones this run resolved. The other order could
                # pair the new timestamp with an old, larger id and pass over
                # entries nothing ever stored.
                await workflow.execute_activity(
                    "update_channel_config_key",
                    args=["rss", identifier, "last_cursor_id", cursor_id],
                    start_to_close_timeout=_ACT_TIMEOUT,
                    retry_policy=ACT_RETRY,
                )
                await workflow.execute_activity(
                    "update_channel_config_key",
                    args=[
                        "rss",
                        identifier,
                        "last_cursor",
                        cursor_at,
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
                entry_summary["cursor_held"] = latest_resolved is None
            per_feed.append(entry_summary)
            total_failed += feed_failed

            # After `record_feed_entries`, so this run's stored entries count
            # towards the feed's last stored one.
            run = await self._record_run(ch, {"ok": True, "backlog": available - len(entries)})
            kept, finding = _after_good_fetch(now, identifier, config, label, run, cfg)
            held += kept
            if finding:
                stale.append(finding)

        # Stored entries that name a tracked topic join its round in the hub
        # (#513), so a story from a feed and the same story from an intel scan
        # are one item there. One call per run, never per entry.
        if topic_items:
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
                workflow.logger.warning("rss_topic_attach_degraded err=%s", error_text(exc))
                notes["topics_degraded"] = True

        held_failing = [h for h in held if h["klass"] == "feed_failing"]
        found: dict = {"failing": len(failing)}
        if held_failing:
            found["held"] = len(held_failing)
        if not await self._reconcile(["feed_failing"], failing + held_failing):
            notes["hub_degraded"] = True
        if review:
            found["stale"] = len(stale)
            held_stale = [h for h in held if h["klass"] == "feed_stale"]
            if not await self._reconcile(["feed_stale"], stale + held_stale):
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

    async def _record_run(self, ch: dict, outcome: dict) -> dict | None:
        """Record one fetch's outcome and return what the feed's record says
        now: `fetch_failures`, `fetch_successes`, `last_stored_at` and
        `tracking_since`. None when the record could not be written — the
        caller then keeps the feed's findings as they are, because a count it
        does not have must not make a failing feed look healthy."""
        try:
            out = await workflow.execute_activity(
                "record_feed_run",
                args=[ch["id"], outcome],
                start_to_close_timeout=_ACT_TIMEOUT,
                retry_policy=RETRY_ONCE,
            )
        except Exception as exc:
            workflow.logger.warning(
                "rss_record_run_failed feed=%s err=%s", ch.get("identifier"), error_text(exc)
            )
            return None
        return out if isinstance(out, dict) else {}

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
            workflow.logger.warning("rss_hub_reconcile_failed err=%s", error_text(exc))
            return False
        return True
