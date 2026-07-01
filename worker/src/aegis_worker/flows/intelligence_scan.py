"""IntelligenceScanFlow — scheduled feed scanner (HN, news, finance).

One flow class, three scheduled activity rows (intel-scan-hn, intel-scan-news,
intel-scan-finance) differentiated by config.source.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    import hashlib

    from aegis_worker.activities.capture import CaptureActivities
    from aegis_worker.activities.intel_scan import (
        SearchSourceInput,
        SearchSourceResult,
    )
    from aegis_worker.shared.retry import ACT_RETRY, NO_RETRY, RETRY_ONCE, TIMEOUT_FAST


_ACT_TIMEOUT = timedelta(seconds=60)
_SCAN_TIMEOUT = timedelta(seconds=120)
_SCORE_TIMEOUT = timedelta(seconds=180)


@dataclass
class IntelligenceScanInput:
    agent_id: str = "raphael"
    source: str = "hn"  # hn | news | finance
    topics: list[str] = field(default_factory=list)
    max_results: int = 20
    # schedule_sync always passes config's threshold; this default is the
    # fallback for direct/admin-trigger construction.
    significance_threshold: int = 5


@workflow.defn(name="IntelligenceScanFlow")
class IntelligenceScanFlow:
    @workflow.run
    async def run(self, input: IntelligenceScanInput) -> dict:
        if not input.topics:
            workflow.logger.warning("intel_scan_no_topics source=%s", input.source)
            return {"source": input.source, "raw": 0, "novel": 0, "ingested": 0}

        # 1. Search
        scan_result: SearchSourceResult = await workflow.execute_activity(
            "search_source",
            SearchSourceInput(
                source=input.source,
                topics=input.topics,
                max_results=input.max_results,
            ),
            result_type=SearchSourceResult,
            start_to_close_timeout=_SCAN_TIMEOUT,
            retry_policy=ACT_RETRY,
        )
        items = scan_result.items
        raw_count = len(items)

        if not items:
            return {"source": input.source, "raw": 0, "novel": 0, "ingested": 0}

        # 2. Dedup against KG
        novel = await workflow.execute_activity(
            "dedup_items",
            items,
            start_to_close_timeout=_ACT_TIMEOUT,
            retry_policy=ACT_RETRY,
        )
        novel_count = len(novel)

        if not novel:
            return {
                "source": input.source,
                "raw": raw_count,
                "novel": 0,
                "ingested": 0,
            }

        # 3. Score — graceful-degrade guard. score_significance runs on
        # gpt-oss:20b, which intermittently hangs past its 180s ceiling under
        # LiteLLM proxy load (07:00-08:00 scan window); with 3 retries that's a
        # ~9-min hard workflow failure and no intel that run. Fail fast
        # (RETRY_ONCE) and degrade to "nothing worthy this run" instead of
        # letting the ActivityError kill the scan — same pattern as
        # alert_investigation's assess guard (PR #282).
        topics_arg = [{"name": t} for t in input.topics]
        try:
            scored = await workflow.execute_activity(
                "score_significance",
                args=[novel, topics_arg],
                start_to_close_timeout=_SCORE_TIMEOUT,
                retry_policy=RETRY_ONCE,
            )
        except Exception as exc:
            workflow.logger.warning(
                "intel_score_degraded source=%s novel=%d err=%s",
                input.source,
                novel_count,
                str(exc)[:200],
            )
            return {
                "source": input.source,
                "raw": raw_count,
                "novel": novel_count,
                "ingested": 0,
                "score_degraded": True,
            }

        # 4. Filter by threshold
        worthy = [it for it in scored if it.get("significance", 0) >= input.significance_threshold]
        if not worthy:
            return {
                "source": input.source,
                "raw": raw_count,
                "novel": novel_count,
                "ingested": 0,
            }

        # 5. Capture worthy items to Todoist Inbox
        for item in worthy:
            url = item.get("url") or item.get("link") or ""
            if not url:
                continue
            ext_id = f"research-{hashlib.sha256(url.encode()).hexdigest()[:16]}"
            title = item.get("title") or "(untitled research item)"
            summary = (item.get("summary") or item.get("body") or "")[:200]
            reason = item.get("significance_reason") or ""
            description = (
                f"[Read]({url})\n\n"
                f"{summary}"
                + (f"\n\nWhy: {reason}" if reason else "")
            )
            try:
                await workflow.execute_activity_method(
                    CaptureActivities.capture_to_inbox,
                    args=["#research", ext_id, title[:120], description],
                    start_to_close_timeout=TIMEOUT_FAST,
                    retry_policy=NO_RETRY,
                )
            except Exception as exc:
                workflow.logger.warning(
                    "intel_capture_failed url=%s err=%s", url, str(exc)[:200]
                )

        # 6. Ingest. KS's `/api/content` is wrapped in a 600s server-side
        # httpx ceiling (see knowledge.py:_ensure_client / ingest_content);
        # the activity ceiling must match so the worker doesn't bail
        # mid-request and burn the retry budget. See lesson on KS ingestion
        # timeout cascade.
        ingest_result = await workflow.execute_activity(
            "ingest_intelligence",
            worthy,
            start_to_close_timeout=timedelta(seconds=600),
            retry_policy=ACT_RETRY,
        )
        ingested = (
            ingest_result.get("ingested", 0) if isinstance(ingest_result, dict) else len(worthy)
        )

        return {
            "source": input.source,
            "raw": raw_count,
            "novel": novel_count,
            "scored_worthy": len(worthy),
            "ingested": ingested,
        }
