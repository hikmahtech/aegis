"""WearableIngestFlow — cursor-based poll of wearable vendor APIs (B7).

One `channels` row per vendor (`kind='wearable'`, `identifier='oura'`), cursor
in `channels.config.last_cursor`, readings written to `life.observations`.

The two rules that make repeated polling safe:

* **The cursor only ever advances past a definite outcome.** A vendor endpoint
  that failed to read, or a record that failed to write, holds the cursor below
  its day so the next tick re-covers it. This is the bug `rss_ingest` documents
  at its own cursor update, applied one level up (per endpoint as well as per
  record) because a wearable poll fans out over several endpoints.
* **Re-covering costs nothing.** Every reading carries the vendor's own id, and
  `record_external_observation` deduplicates on it in the database. Overlapping
  windows produce `duplicates`, never a second row.

Unconfigured is a *reported* state, not silence: no channel row, an inactive
one, or a missing token each land in the returned summary (and therefore in
`workflow_runs.result_summary`) with a named status.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from aegis_worker.activities.wearable import (
        PollWearableInput,
        PollWearableResult,
        RecordWearableInput,
        RecordWearableResult,
    )
    from aegis_worker.shared.retry import ACT_RETRY, NO_RETRY


_ACT_TIMEOUT = timedelta(seconds=60)
_POLL_TIMEOUT = timedelta(seconds=180)
_WRITE_TIMEOUT = timedelta(seconds=120)


@dataclass
class WearableIngestInput:
    agent_id: str = "sebas"
    lookback_days: int = 7


@workflow.defn(name="WearableIngestFlow")
class WearableIngestFlow:
    @workflow.run
    async def run(self, input: WearableIngestInput) -> dict:
        channels = await workflow.execute_activity(
            "list_active_channels",
            "wearable",
            start_to_close_timeout=_ACT_TIMEOUT,
            retry_policy=ACT_RETRY,
        )
        if not channels:
            # Fail closed and SAY SO. A wearable channel is opt-in (the seed
            # row ships inactive), so this is the normal state on a fresh
            # install — but it must read as "not configured", never as a
            # successful run that happened to find nothing.
            workflow.logger.info("wearable_no_active_channel")
            return {
                "status": "no_channel",
                "vendors": 0,
                "written": 0,
                "duplicates": 0,
                "per_vendor": [],
            }

        written = 0
        duplicates = 0
        failed = 0
        skipped = 0
        per_vendor: list[dict] = []

        for ch in channels:
            vendor = ch["identifier"]
            since = (ch.get("config") or {}).get("last_cursor")

            try:
                poll: PollWearableResult = await workflow.execute_activity(
                    "poll_wearable",
                    PollWearableInput(
                        vendor=vendor,
                        since_cursor=since,
                        lookback_days=input.lookback_days,
                    ),
                    result_type=PollWearableResult,
                    start_to_close_timeout=_POLL_TIMEOUT,
                    # The activity already absorbs per-endpoint failures; a
                    # throw here is unexpected, and retrying a vendor API that
                    # just rate-limited us is the wrong reflex.
                    retry_policy=NO_RETRY,
                )
            except Exception as exc:
                workflow.logger.warning(
                    "wearable_poll_failed vendor=%s err=%s", vendor, str(exc)[:200]
                )
                skipped += 1
                per_vendor.append({"vendor": vendor, "status": "poll_failed"})
                continue

            if poll.status != "ok":
                # token_missing / unsupported_vendor / fetch_failed — all
                # non-fatal, all named in the summary.
                workflow.logger.warning(
                    "wearable_poll_not_ok vendor=%s status=%s detail=%s",
                    vendor,
                    poll.status,
                    poll.detail[:200],
                )
                skipped += 1
                per_vendor.append(
                    {"vendor": vendor, "status": poll.status, "detail": poll.detail[:200]}
                )
                continue

            if not poll.records:
                per_vendor.append({"vendor": vendor, "status": "ok", "records": 0})
                continue

            write: RecordWearableResult = await workflow.execute_activity(
                "record_wearable_observations",
                RecordWearableInput(source=vendor, records=poll.records),
                result_type=RecordWearableResult,
                start_to_close_timeout=_WRITE_TIMEOUT,
                retry_policy=ACT_RETRY,
            )
            written += write.written
            duplicates += write.duplicates
            failed += write.failed

            # Cursor advances only when EVERY endpoint was read (poll.errors
            # == 0) and only up to the newest fully-written day. An endpoint
            # that 429'd carries days we never saw, so advancing past them
            # would drop that vendor's data on the floor permanently.
            cursor_advanced = None
            if poll.errors == 0 and write.latest_resolved_day:
                await workflow.execute_activity(
                    "update_channel_config_key",
                    args=["wearable", vendor, "last_cursor", write.latest_resolved_day],
                    start_to_close_timeout=_ACT_TIMEOUT,
                    retry_policy=ACT_RETRY,
                )
                cursor_advanced = write.latest_resolved_day

            per_vendor.append(
                {
                    "vendor": vendor,
                    "status": "ok",
                    "records": len(poll.records),
                    "written": write.written,
                    "duplicates": write.duplicates,
                    "failed": write.failed,
                    "endpoint_errors": poll.errors,
                    "cursor": cursor_advanced,
                }
            )

        return {
            "status": "ok" if written or duplicates else "no_data",
            "vendors": len(channels),
            "written": written,
            "duplicates": duplicates,
            "failed": failed,
            "skipped": skipped,
            "per_vendor": per_vendor,
        }
