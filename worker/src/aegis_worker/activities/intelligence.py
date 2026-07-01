"""Activities for world intelligence research pipeline."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from aegis.llm import parse_llm_json
from aegis.observability import record_llm_call
from temporalio import activity


@dataclass
class IntelligenceActivities:
    """Activities for intelligence research: dedup, scoring, synthesis, ingestion."""

    knowledge_connector: Any = None
    llm_client: Any = None
    # Significance scoring runs on the FAST tier (gemma4:e2b), not balanced —
    # gpt-oss:20b intermittently hangs >180s under proxy load (PR #319) and a
    # 1-5 significance score is a lightweight judgment a small model handles.
    model_light: str = "gemma4:e2b"
    db_pool: Any = None

    @activity.defn
    async def dedup_items(self, items: list[dict]) -> list[dict]:
        """Filter out items already covered in the knowledge graph."""
        if not self.knowledge_connector or not items:
            return items

        novel = []
        for item in items:
            title = item.get("title", "")
            try:
                results = await self.knowledge_connector.search(title, limit=1)
                if results and any(r.get("similarity", 0) >= 0.85 for r in results):
                    activity.logger.info("intel_dedup_skip", extra={"title": title})
                    continue
            except Exception as exc:
                # KS search down → fall back to "treat as novel" (we'd rather
                # re-ingest on the next tick than silently drop a candidate).
                # Logging means operators see KS flakiness instead of inferring
                # from missing analyses downstream.
                activity.logger.warning(
                    "intel_dedup_search_failed title=%s err=%s",
                    title[:80],
                    str(exc)[:200],
                )
            novel.append(item)
        return novel

    @activity.defn
    async def score_significance(self, items: list[dict], topics: list[dict]) -> list[dict]:
        """Rate each item 1-5 for significance using LLM."""
        if not items:
            return []
        if not self.llm_client:
            return [{**item, "significance": 3} for item in items]

        topic_desc = ", ".join(t.get("name", "") for t in topics) if topics else "general"
        items_text = "\n".join(
            f"[{i}] {item.get('title', '')} — {item.get('snippet', '')[:200]}"
            for i, item in enumerate(items)
        )
        _t0 = time.monotonic()
        result = await self.llm_client.think(
            prompt=items_text,
            model=self.model_light,
            system_prompt=(
                "Rate each news item 1-5 for significance to a user interested in: "
                f"{topic_desc}. Consider: relevance, novelty, potential impact on financial/life decisions. "
                'Return JSON array: [{"index": 0, "score": 4, "reason": "<max 10 words>"}]'
            ),
            # gemma4:e2b returns EMPTY content below ~900 tokens for this task and
            # is more verbose than gpt-oss (markdown-fenced, pretty-printed), so it
            # needs generous headroom to emit the full scored array (validated live).
            max_tokens=1500,
            db_pool=self.db_pool,
            purpose="intel_score_significance",
        )
        await record_llm_call(
            self.db_pool,
            model=result.get("model", self.model_light),
            prompt_tokens=result.get("prompt_tokens", 0),
            completion_tokens=result.get("completion_tokens", 0),
            latency_ms=int((time.monotonic() - _t0) * 1000),
            purpose="intel_score_significance",
        )
        scores = parse_llm_json(result["response"])
        try:
            score_map = (
                {s["index"]: s for s in scores if isinstance(s, dict)}
                if isinstance(scores, list)
                else {}
            )
        except KeyError:
            score_map = {}

        return [
            {
                **item,
                "significance": score_map.get(i, {}).get("score", 3),
                "significance_reason": score_map.get(i, {}).get("reason", ""),
            }
            for i, item in enumerate(items)
        ]

    @activity.defn
    async def ingest_intelligence(self, analyses: list[dict]) -> dict:
        """Batch ingest synthesized intelligence into knowledge-service."""
        if not self.knowledge_connector or not analyses:
            return {"ingested": 0}

        # ponytail: intel items are captured as content chunks in the loop below;
        # the graph-claim extraction path is gone (no knowledge graph).
        ingested_ok = 0
        ingest_failures = 0
        for a in analyses:
            # The intel-scan pipeline (activities/intel_scan.py::search_source)
            # emits items keyed `snippet`, not `summary` — gating on `summary`
            # alone silently ingested 0 worthy items into KS. Fall back across
            # the fields the various producers use.
            text = a.get("summary") or a.get("snippet") or a.get("body")
            if text:
                try:
                    title = a.get("title") or "intelligence item"
                    raw_text = f"{title}\n\n{text}"
                    await self.knowledge_connector.ingest_content(
                        url=a.get("url") or f"aegis://intelligence/{a.get('topic', 'item')}",
                        title=title,
                        source_type="intelligence",
                        summary=text,
                        raw_text=raw_text,
                        metadata={
                            "topic": a.get("topic", ""),
                            "significance": a.get("significance", 3),
                        },
                    )
                    ingested_ok += 1
                except Exception as exc:
                    ingest_failures += 1
                    activity.logger.warning(
                        "intel_ingest_content_failed url=%s err=%s",
                        (a.get("url") or "")[:120],
                        str(exc)[:200],
                    )

        # Observability: a silent 0-ingest despite worthy items is exactly the
        # bug that hid the snippet/summary mismatch for weeks — surface it.
        if analyses and ingested_ok == 0 and ingest_failures == 0:
            activity.logger.warning(
                "intel_ingest_zero_despite_candidates candidates=%d (no item carried "
                "summary/snippet/body text?)",
                len(analyses),
            )

        return {
            "ingested": ingested_ok,
            "failed": ingest_failures,
            "candidates": len(analyses),
        }
