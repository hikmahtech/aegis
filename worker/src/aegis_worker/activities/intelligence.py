"""Activities for world intelligence research pipeline."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from aegis.llm import parse_llm_json
from aegis.services.content_extract import fetch_and_extract
from aegis.services.knowledge import _content_id_for
from temporalio import activity

from aegis_worker.activities.content import _MIN_CONTENT_LENGTH, detect_content_type

# The settings row the `track_topic` chat tool writes (services/chat.py).
TRACKED_TOPICS_SETTING = "intelligence_topics"
# A page read in place of a missing snippet is stored in full as raw text, but
# its summary is cut to about what a snippet would have been.
_FETCHED_SUMMARY_CHARS = 500


@dataclass
class IntelligenceActivities:
    """Activities for intelligence research: dedup, scoring, synthesis, ingestion."""

    knowledge_connector: Any = None
    llm_client: Any = None
    # Scoring model. NOTE: this default is only used by direct construction —
    # __main__.py passes `model_light=model_balanced`, so in a real worker the
    # significance score runs on whatever the BALANCED tier resolves to, not on
    # gemma4:e2b. An earlier comment here claimed the fast tier; it was never
    # wired that way, and that mismatch is what made issue #137 read the
    # 2026-07-22 balanced-tier remap (gemma4:e2b/gpt-oss:20b -> kimi-k2.5) as
    # "fast-tier calls went invisible". Change the wiring in __main__.py if you
    # want a different tier — changing this default alone does nothing.
    model_light: str = "gemma4:e2b"
    db_pool: Any = None
    # Owning agent — matches IntelligenceScanFlow's config default. Threaded
    # into llm_calls rows so intel_score_significance stops recording NULL
    # agent_id (same pattern as MoneyActivities.agent_id).
    agent_id: str = "raphael"

    @activity.defn
    async def dedup_items(self, items: list[dict]) -> list[dict]:
        """Filter out items already covered in the knowledge graph.

        Identity, not similarity. This used to ask `search(title, limit=1)` and
        skip on `similarity >= 0.85`, which is the wrong instrument twice over:
        a vector search answers "what is this like", and pgvector's HNSW scan
        only ever considers `hnsw.ef_search` candidates — so against a corpus
        where intelligence is a fraction of a percent, a genuine duplicate
        usually was not in the window and the check quietly passed everything.
        A dedupe that fails open is not a dedupe.

        The knowledge store already keys content on `sha1(url)` (see
        `KnowledgeStore._content_id_for`), so an exact lookup is both correct
        and cheaper. Items without a URL keep the old fail-open behaviour —
        there is nothing to key on, and re-ingesting beats dropping.
        """
        if not self.knowledge_connector or not items:
            return items

        novel = []
        for item in items:
            url = (item.get("url") or item.get("link") or "").strip()
            if not url:
                novel.append(item)
                continue
            try:
                status = await self.knowledge_connector.get_content_status(
                    _content_id_for(url)
                )
                if (status or {}).get("status") == "completed":
                    activity.logger.info(
                        "intel_dedup_skip", extra={"title": item.get("title", "")}
                    )
                    continue
            except Exception as exc:
                # KS down → fall back to "treat as novel" (we'd rather
                # re-ingest on the next tick than silently drop a candidate).
                # Logging means operators see KS flakiness instead of inferring
                # from missing analyses downstream.
                activity.logger.warning(
                    "intel_dedup_lookup_failed url=%s err=%s",
                    url[:120],
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
        # db_pool + purpose ⇒ think() writes the llm_calls row itself for every
        # outcome. Truncation included: without that row a model that truncates
        # every scan reads as a model nobody called (issue #137). It still
        # propagates, so the flow's graceful-degrade guard fires as before.
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
            agent_id=self.agent_id,
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
    async def load_tracked_topics(self) -> list[str]:
        """Search terms for the topics tracked from chat, in the order added.

        `track_topic` writes them to the settings row `intelligence_topics`.
        Until #508 nothing read that row, so the tool answered "added" and no
        scan ever changed. A missing or malformed row means no tracked topics;
        a failed read raises, and the flow falls back to its configured topics.
        """
        if not self.db_pool:
            return []
        value = await self.db_pool.fetchval(
            "SELECT value FROM settings WHERE key = $1", TRACKED_TOPICS_SETTING
        )
        return tracked_search_terms(value)

    async def _read_page(self, url: str) -> str:
        """The readable text at `url`, or "" when there is too little to keep.

        The same fetch and extraction an RSS entry gets (`process_content`),
        minus media and images: this path has no transcription and no OCR.
        """
        content_type = detect_content_type(url)
        if content_type in ("media", "image"):
            return ""
        try:
            text, _title = await fetch_and_extract(url, content_type)
        except Exception as exc:  # noqa: BLE001 — one unreadable page must not sink the batch
            activity.logger.warning(
                "intel_page_read_failed url=%s err=%s", url[:120], str(exc)[:200]
            )
            return ""
        return text if len(text) >= _MIN_CONTENT_LENGTH else ""

    @activity.defn
    async def ingest_intelligence(self, analyses: list[dict]) -> dict:
        """Batch ingest synthesized intelligence into knowledge-service."""
        if not self.knowledge_connector or not analyses:
            return {"ingested": 0}

        # ponytail: intel items are captured as content chunks in the loop below;
        # the graph-claim extraction path is gone (no knowledge graph).
        ingested_ok = 0
        ingest_failures = 0
        skipped_no_text = 0
        fetched = 0
        for a in analyses:
            # The intel-scan pipeline (activities/intel_scan.py::search_source)
            # emits items keyed `snippet`, not `summary` — gating on `summary`
            # alone silently ingested 0 worthy items into KS. Fall back across
            # the fields the various producers use.
            text = a.get("summary") or a.get("snippet") or a.get("body")
            summary = text
            url = (a.get("url") or "").strip()
            if not text and url:
                # A news result often arrives as a title and a link with an
                # empty snippet. Those were about half of every news scan's
                # worthy items (84 of 154 in the 30 days to 2026-09-11), and
                # every one was dropped here as skipped_no_text (#508). Read
                # the page instead, the way an RSS entry is read.
                text = await self._read_page(url)
                if text:
                    fetched += 1
                    summary = text[:_FETCHED_SUMMARY_CHARS]
            if text:
                try:
                    title = a.get("title") or "intelligence item"
                    raw_text = f"{title}\n\n{text}"
                    await self.knowledge_connector.ingest_content(
                        url=url or f"aegis://intelligence/{a.get('topic', 'item')}",
                        title=title,
                        source_type="intelligence",
                        summary=summary,
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
                        url[:120],
                        str(exc)[:200],
                    )
            else:
                # The third outcome, and until now the invisible one: the item
                # carried no text at all, and its page (if it had one) gave
                # too little to keep, so it was never even attempted. Without
                # its own counter a scored-worthy item that vanishes here is
                # indistinguishable from one that failed to ingest.
                skipped_no_text += 1

        # Observability: a silent 0-ingest despite worthy items is exactly the
        # bug that hid the snippet/summary mismatch for weeks — surface it.
        if analyses and ingested_ok == 0 and ingest_failures == 0:
            activity.logger.warning(
                "intel_ingest_zero_despite_candidates candidates=%d (no item carried "
                "summary/snippet/body text or a readable page?)",
                len(analyses),
            )

        return {
            "ingested": ingested_ok,
            "failed": ingest_failures,
            "candidates": len(analyses),
            "skipped_no_text": skipped_no_text,
            "fetched": fetched,
        }


def tracked_search_terms(value: Any) -> list[str]:
    """The search terms in an `intelligence_topics` settings value.

    Each topic gives its queries, or its name when it has none. Lenient on
    purpose: a hand-edited or half-written row yields what it can and never
    raises, because a scan must not fail on a config read.
    """
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    if not isinstance(value, dict):
        return []
    terms: list[str] = []
    for topic in value.get("topics") or []:
        if not isinstance(topic, dict):
            continue
        raw = topic.get("queries")
        queries = [q for q in raw if isinstance(q, str)] if isinstance(raw, list) else []
        name = topic.get("name")
        for term in queries or ([name] if isinstance(name, str) else []):
            if term.strip():
                terms.append(term.strip())
    return terms
