"""RaindropIngestFlow — 2-hourly Raindrop bookmark poll → content extract → claims.

Single channel row with kind='raindrop', cursor in channels.config.last_cursor.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from aegis_worker.activities.capture import CaptureActivities
    from aegis_worker.activities.raindrop import (
        PollBookmarksInput,
        PollBookmarksResult,
    )
    from aegis_worker.shared.retry import ACT_RETRY, NO_RETRY


_ACT_TIMEOUT = timedelta(seconds=60)
_FETCH_TIMEOUT = timedelta(seconds=120)


@dataclass
class RaindropIngestInput:
    agent_id: str = "raphael"


@workflow.defn(name="RaindropIngestFlow")
class RaindropIngestFlow:
    @workflow.run
    async def run(self, input: RaindropIngestInput) -> dict:
        channels = await workflow.execute_activity(
            "list_active_channels",
            "raindrop",
            start_to_close_timeout=_ACT_TIMEOUT,
            retry_policy=ACT_RETRY,
        )
        if not channels:
            return {"bookmarks": 0, "ingested": 0, "no_channel": True}

        ch = channels[0]  # single row
        since = (ch.get("config") or {}).get("last_cursor")

        result: PollBookmarksResult = await workflow.execute_activity(
            "poll_bookmarks",
            PollBookmarksInput(since_cursor=since),
            result_type=PollBookmarksResult,
            start_to_close_timeout=_FETCH_TIMEOUT,
            retry_policy=ACT_RETRY,
        )

        bookmarks = result.bookmarks
        if not bookmarks:
            return {
                "bookmarks": 0,
                "ingested": 0,
                "todoist_committed": 0,
                "outbox_staged": 0,
                "capture_failed": 0,
            }

        ingested = 0
        todoist_committed = 0
        outbox_staged = 0
        capture_failed = 0
        for bm in bookmarks:
            # Idempotency
            new = await workflow.execute_activity(
                "ingest_idempotency_claim",
                args=["raindrop", bm["id"]],
                start_to_close_timeout=_ACT_TIMEOUT,
                retry_policy=ACT_RETRY,
            )
            if not new:
                continue

            # Content extract — fires off URL to knowledge-service (best-effort).
            # Thread the user's excerpt + tags through as fallback signal so
            # the KS ingest keeps the user's saved-summary even when a
            # downstream scrape fails (paywall, dead link, etc.).
            try:
                # process_content ultimately hits KS's `/api/content` which
                # carries a 600s server-side httpx ceiling. Match the
                # activity ceiling so the worker doesn't bail mid-request
                # while KS finishes happily on its side. Single attempt —
                # KS dedups by URL on retry anyway.
                await workflow.execute_activity(
                    "process_content",
                    args=[
                        bm["link"],
                        bm.get("title", ""),
                        "raindrop",
                        (bm.get("excerpt") or "").strip(),
                        list(bm.get("tags") or []),
                    ],
                    start_to_close_timeout=timedelta(seconds=600),
                    retry_policy=NO_RETRY,
                )
            except Exception as exc:
                workflow.logger.warning(
                    "raindrop_content_failed url=%s err=%s",
                    bm["link"],
                    str(exc)[:200],
                )

            # Phase 5: capture every new Raindrop to Inbox tagged #research
            # so the user sees what's coming in. ClarifyFlow's
            # skip_inbox('#research', 'reference') rule auto-routes to
            # 🔖 Reference without user interaction; ingest_reference_to_ks
            # then ensures it's in KS with source_type='reference' tags.
            # If KS already has the URL from process_content above, KS's
            # own dedup (409 on duplicate URL) handles it cleanly.
            #
            # capture_to_inbox returns one of:
            #   - real Todoist id (e.g. "6ghwHcgX2H9xH98v") → todoist_committed
            #   - temp_id starting with "item-" → outbox_staged (will retry)
            #   - None → skipped (kill switch, no inbox project, empty title)
            # Surface the distinction in result_summary so a "stuck" state
            # (everything outbox-staged + drain failing) is visible without
            # tailing worker logs.
            ref: str | None = None
            try:
                title = (bm.get("title") or "").strip() or bm.get("link", "")
                description = (f"{bm.get('link', '')}\n\n{bm.get('excerpt') or ''}").strip()
                ref = await workflow.execute_activity_method(
                    CaptureActivities.capture_to_inbox,
                    args=["#research", f"raindrop-{bm['id']}", title[:120], description],
                    start_to_close_timeout=timedelta(seconds=30),
                    retry_policy=NO_RETRY,
                )
            except Exception as exc:
                workflow.logger.warning(
                    "raindrop_capture_failed url=%s err=%s",
                    bm.get("link", ""),
                    str(exc)[:200],
                )

            if ref is None:
                capture_failed += 1
            elif ref.startswith("item-"):
                outbox_staged += 1
            else:
                todoist_committed += 1

            ingested += 1

        # Advance cursor if anything was fetched
        if result.latest_created:
            await workflow.execute_activity(
                "update_channel_config_key",
                args=["raindrop", ch["identifier"], "last_cursor", result.latest_created],
                start_to_close_timeout=_ACT_TIMEOUT,
                retry_policy=ACT_RETRY,
            )

        return {
            "bookmarks": len(bookmarks),
            "ingested": ingested,
            "todoist_committed": todoist_committed,
            "outbox_staged": outbox_staged,
            "capture_failed": capture_failed,
        }
