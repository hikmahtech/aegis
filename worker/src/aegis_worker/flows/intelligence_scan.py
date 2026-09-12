"""IntelligenceScanFlow — scheduled feed scanner (HN, news, finance).

One flow class, three scheduled activity rows (intel-scan-hn, intel-scan-news,
intel-scan-finance) differentiated by config.source.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from aegis_worker.activities.intel_scan import (
        SearchSourceInput,
        SearchSourceResult,
    )
    from aegis_worker.shared.retry import ACT_RETRY, RETRY_ONCE


_ACT_TIMEOUT = timedelta(seconds=60)
_SCAN_TIMEOUT = timedelta(seconds=120)
_SCORE_TIMEOUT = timedelta(seconds=180)

# Deprecated patches (#533): the load_tracked_topics call added by #508 and
# step 5's switch from an Inbox capture per item to the topic hub (#513).
# Every scan that started before them has finished, so only the new path is
# left. The markers stay one more deploy, so a scan started on the patched
# code still replays; then the calls and these ids go.
_PATCH_TRACKED_TOPICS = "intel-tracked-topics"
_PATCH_TOPICS = "research-hub-513"
# Attaching can raise a topic's task, which is a Todoist round trip.
_ATTACH_TIMEOUT = timedelta(seconds=120)


def merge_topics(configured: list[str], tracked: list[str]) -> list[str]:
    """Configured topics first, then each tracked one not already there (any case)."""
    seen = {t.strip().lower() for t in configured}
    merged = list(configured)
    for term in tracked:
        key = term.strip().lower()
        if key and key not in seen:
            seen.add(key)
            merged.append(term.strip())
    return merged


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
        # What a run reports beyond its counts — a partial search, a degraded
        # step, how many tracked topics it added — threaded into every return
        # below, so a scan that lost something is never indistinguishable from
        # a quiet one.
        notes: dict = {}
        topics = list(input.topics)

        # 0. Topics tracked from chat (#508). `track_topic` wrote them to a
        # settings row that nothing read, so "added" changed no scan. A failed
        # read is not a failed scan: it runs on its configured topics and says so.
        workflow.deprecate_patch(_PATCH_TRACKED_TOPICS)
        try:
            tracked = await workflow.execute_activity(
                "load_tracked_topics",
                start_to_close_timeout=_ACT_TIMEOUT,
                retry_policy=RETRY_ONCE,
            )
        except Exception as exc:
            workflow.logger.warning(
                "intel_tracked_topics_degraded source=%s err=%s",
                input.source,
                str(exc)[:200],
            )
            tracked = []
            notes["tracked_topics_degraded"] = True
        merged = merge_topics(topics, list(tracked or []))
        if len(merged) > len(topics):
            notes["tracked_topics"] = len(merged) - len(topics)
        topics = merged

        if not topics:
            workflow.logger.warning("intel_scan_no_topics source=%s", input.source)
            return {"source": input.source, "raw": 0, "novel": 0, "ingested": 0, **notes}

        # 1. Search
        scan_result: SearchSourceResult = await workflow.execute_activity(
            "search_source",
            SearchSourceInput(
                source=input.source,
                topics=topics,
                max_results=input.max_results,
            ),
            result_type=SearchSourceResult,
            start_to_close_timeout=_SCAN_TIMEOUT,
            retry_policy=ACT_RETRY,
        )
        items = scan_result.items
        raw_count = len(items)
        if scan_result.failed_topics:
            notes["search_degraded"] = True
            notes["failed_topics"] = list(scan_result.failed_topics)

        if not items:
            return {
                "source": input.source,
                "raw": 0,
                "novel": 0,
                "ingested": 0,
                **notes,
            }

        # 2. Dedup against KG — graceful-degrade guard. dedup_items is a
        # best-effort filter: its own per-item handler already treats an
        # unreachable knowledge store as "novel". But when the store got slow
        # the ACTIVITY blew its 60s ceiling, and ACT_RETRY's 3 attempts turned
        # that into a ~183s hard workflow failure — the exact signature of the
        # 9 timed-out runs in issue #136 (duration_ms 186-189k on 07-16/17/21,
        # = 60+1+60+2+60 plus the search). Retrying is actively harmful here:
        # Temporal does not cancel a non-heartbeating activity when
        # start_to_close fires, so each retry stacks another in-flight scan on
        # the store that was already too slow to answer. Fail fast (RETRY_ONCE)
        # and fall back to "everything is novel" — the same answer dedup gives
        # per-item when the store is down. The pgvector plan fix in
        # services/knowledge.py::search removed the known cause; this guard
        # stops the next slow-store episode from failing the scan at all.
        try:
            novel = await workflow.execute_activity(
                "dedup_items",
                items,
                start_to_close_timeout=_ACT_TIMEOUT,
                retry_policy=RETRY_ONCE,
            )
        except Exception as exc:
            workflow.logger.warning(
                "intel_dedup_degraded source=%s raw=%d err=%s",
                input.source,
                raw_count,
                str(exc)[:200],
            )
            novel = items
            notes["dedup_degraded"] = True
        novel_count = len(novel)

        if not novel:
            return {
                "source": input.source,
                "raw": raw_count,
                "novel": 0,
                "ingested": 0,
                **notes,
            }

        # 3. Score — graceful-degrade guard. score_significance runs on
        # gpt-oss:20b, which intermittently hangs past its 180s ceiling under
        # LiteLLM proxy load (07:00-08:00 scan window); with 3 retries that's a
        # ~9-min hard workflow failure and no intel that run. Fail fast
        # (RETRY_ONCE) and degrade to "nothing worthy this run" instead of
        # letting the ActivityError kill the scan — same pattern as
        # alert_investigation's assess guard (PR #282).
        topics_arg = [{"name": t} for t in topics]
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
                **notes,
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
                **notes,
            }

        # 5. Worthy items attach to the tracked topics they name (#513). This
        # used to capture every worthy item as a `#research` Inbox task — 277
        # in 30 days, each closed by clarify on arrival. The hub keeps them
        # now and a topic raises ONE task when its round earns it; the
        # knowledge store and the briefing still get every item (step 6).
        workflow.deprecate_patch(_PATCH_TOPICS)
        try:
            attached = await workflow.execute_activity(
                "attach_topic_items",
                args=[worthy, f"intel:{input.source}"],
                start_to_close_timeout=_ATTACH_TIMEOUT,
                retry_policy=RETRY_ONCE,
            )
            if isinstance(attached, dict) and attached.get("attached"):
                notes["topic_items"] = attached["attached"]
        except Exception as exc:
            workflow.logger.warning(
                "intel_topic_attach_degraded source=%s err=%s", input.source, str(exc)[:200]
            )
            notes["topics_degraded"] = True

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
        # Record WHY worthy items didn't land, not just how many did. Prod ran
        # days of `scored_worthy: 5, ingested: 0` that were undiagnosable from
        # the summary alone — "knowledge-store ingest is failing" and "the
        # items carried no text" are different bugs and these two counters are
        # what tells them apart. The activity already counted them; the flow
        # was dropping them on the floor. Zeros are omitted so a healthy run
        # reads exactly as it did before (`candidates` is skipped entirely —
        # it is just len(worthy), which `scored_worthy` already reports).
        # `fetched` counts items whose text came from reading the page because
        # the search result had no snippet (#508).
        ingest_detail = (
            {
                k: ingest_result[k]
                for k in ("failed", "skipped_no_text", "fetched")
                if ingest_result.get(k)
            }
            if isinstance(ingest_result, dict)
            else {}
        )

        return {
            "source": input.source,
            "raw": raw_count,
            "novel": novel_count,
            "scored_worthy": len(worthy),
            "ingested": ingested,
            **ingest_detail,
            **notes,
        }
