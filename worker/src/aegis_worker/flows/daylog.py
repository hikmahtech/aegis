"""DayLogFlow — file one dated episodic entry per night.

The knowledge store has plenty of *topics* and no *timeline*: nothing answers
"what happened on 2026-07-14". This flow gathers the day out of the tables
that already record it, distils one narrative, and files it as a single
`source_type='daylog'` knowledge entry keyed `aegis://daylog/<date>` — the
natural key, so a re-run of the same date updates rather than duplicates
(`KnowledgeStore.ingest_content` upserts on `_content_id_for(url)`).

A quiet day is still filed (`metadata.quiet = true`): "nothing happened" is
data, and A9's rollups need every date present to reason about a week.

Scheduled nightly at 19:00 UTC = 00:30 IST — after the IST day closes, so
the run's own UTC date IS the IST day being logged.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from aegis_worker.activities.content import ContentActivities
    from aegis_worker.activities.daylog import DayLogActivities
    from aegis_worker.shared.retry import (
        NO_RETRY,
        RETRY_ONCE,
        TIMEOUT_FAST,
        TIMEOUT_LLM,
        TIMEOUT_STANDARD,
    )


@dataclass
class DayLogConfig:
    """Configuration for DayLogFlow.

    NOTE (deviation from the A8 sketch, which proposed `lookback_hours: int
    = 24`): the entry's identity is a calendar DATE, not a rolling window, so
    an hours knob can only ever be converted back into a date — and at the
    19:00 UTC cron a literal 24h lookback lands on the PREVIOUS date, i.e.
    the wrong day. `day_offset` says the same thing without the off-by-one:
    0 = the date the run starts on (the IST day that just closed).
    """

    agent_id: str = "raphael"
    day_offset: int = 0


@workflow.defn
class DayLogFlow:
    """Gather → distil → ingest → commit cursor, for one day."""

    @workflow.run
    async def run(self, config: DayLogConfig) -> dict:
        target_date = (workflow.now() - timedelta(days=config.day_offset)).strftime("%Y-%m-%d")
        workflow.logger.info("daylog_starting date=%s", target_date)

        try:
            events = await workflow.execute_activity_method(
                DayLogActivities.gather_day_events,
                args=[target_date],
                start_to_close_timeout=TIMEOUT_STANDARD,
                retry_policy=RETRY_ONCE,
            )
        except Exception:
            workflow.logger.warning("daylog_gather_failed date=%s", target_date)
            return {"status": "skipped", "reason": "gather_failed", "date": target_date}

        # distil_daylog swallows LLM failure internally; this guard covers the
        # activity-level failures (timeout, worker loss) so an LLM problem can
        # only ever produce a SKIPPED run, never a failed one.
        try:
            narrative = await workflow.execute_activity_method(
                DayLogActivities.distil_daylog,
                args=[events, target_date, config.agent_id],
                start_to_close_timeout=TIMEOUT_LLM,
                retry_policy=NO_RETRY,
            )
        except Exception:
            workflow.logger.warning("daylog_distil_failed date=%s", target_date)
            return {"status": "skipped", "reason": "distil_failed", "date": target_date}

        if not (narrative or "").strip():
            return {"status": "skipped", "reason": "empty_narrative", "date": target_date}

        url = f"aegis://daylog/{target_date}"
        try:
            ingested = await workflow.execute_activity_method(
                ContentActivities.ingest_content,
                args=[
                    {
                        "url": url,
                        "title": f"Day Log {target_date}",
                        "source_type": "daylog",
                        "raw_text": narrative,
                        "tags": ["daylog"],
                        "metadata": {
                            "date": target_date,
                            "quiet": bool(events.get("quiet")),
                            "counts": events.get("counts") or {},
                        },
                    }
                ],
                start_to_close_timeout=TIMEOUT_STANDARD,
                retry_policy=RETRY_ONCE,
            )
        except Exception:
            workflow.logger.warning("daylog_ingest_failed date=%s", target_date)
            return {"status": "skipped", "reason": "ingest_failed", "date": target_date}

        # Allow-list, not a deny-list: ingest_content answers "disabled" with
        # no knowledge connector, "skipped" on a bad item and "empty" when the
        # body embedded to nothing. Only "ok" is a filed entry, so only "ok"
        # may move the cursor (same discipline as commit_briefing_state).
        status = (ingested or {}).get("status")
        if status != "ok":
            workflow.logger.warning("daylog_not_ingested date=%s status=%s", target_date, status)
            return {"status": "skipped", "reason": f"ingest_{status or 'no_result'}",
                    "date": target_date}

        try:
            await workflow.execute_activity_method(
                DayLogActivities.commit_daylog_state,
                args=[{"last_date": target_date, "url": url}],
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=RETRY_ONCE,
            )
        except Exception:
            workflow.logger.warning("daylog_state_commit_failed date=%s", target_date)

        return {
            "status": "ingested",
            "date": target_date,
            "url": url,
            "quiet": bool(events.get("quiet")),
            "content_id": (ingested or {}).get("content_id"),
        }
