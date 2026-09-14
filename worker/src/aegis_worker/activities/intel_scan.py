"""IntelScanActivities — searxng queries for HN / news / finance intel scans."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx
import structlog
from aegis.errors import error_text
from temporalio import activity

logger = structlog.get_logger()


# The built-in query per source, `{topic}` standing for the term. A scan row
# carries its own in `activities.config.query_template` (the seed rows spell
# these out); these are the fallback for a row that sets none.
DEFAULT_QUERY_TEMPLATES: dict[str, str] = {
    "hn": "site:news.ycombinator.com {topic}",
    "news": "{topic}",
    "finance": "{topic} site:ft.com OR site:reuters.com OR site:bloomberg.com",
}


def render_query(template: str, source: str, topic: str) -> str:
    """The searxng query for `topic`: `template` with `{topic}` filled in, the
    source's built-in query when the template is blank, and the bare topic
    when the template names no `{topic}` at all (a template that could never
    vary by topic would search the same thing N times)."""
    tpl = (template or "").strip() or DEFAULT_QUERY_TEMPLATES.get(source, "{topic}")
    if "{topic}" not in tpl:
        return f"{tpl} {topic}".strip()
    return tpl.replace("{topic}", topic)


def interleave(per_topic: list[list[dict]], limit: int, start: int = 0) -> list[dict]:
    """Up to `limit` items taken one topic at a time, round-robin from topic
    `start`, skipping a URL another topic already gave.

    The scan used to keep the first `limit` results in the order it collected
    them, so the topics searched first filled every slot: with three configured
    topics ahead of the tracked ones, no tracked topic's result was ever scored
    (#585). Taking turns gives every topic that returned something a share.
    When there are more topics than slots, the ones after the cut get nothing
    this run — which is why `start` rotates from run to run."""
    n = len(per_topic)
    if n == 0 or limit <= 0:
        return []
    order = [per_topic[(start + i) % n] for i in range(n)]
    cursors = [0] * n
    seen: set[str] = set()
    out: list[dict] = []
    while len(out) < limit:
        took = False
        for i, items in enumerate(order):
            if len(out) >= limit:
                break
            while cursors[i] < len(items):
                item = items[cursors[i]]
                cursors[i] += 1
                if item["url"] in seen:
                    continue
                seen.add(item["url"])
                out.append(item)
                took = True
                break
        if not took:
            break
    return out


@dataclass
class SearchSourceInput:
    source: str  # 'hn' | 'news' | 'finance'
    topics: list[str] = field(default_factory=list)
    max_results: int = 20
    query_template: str = ""
    # Which topic the round-robin starts at. None (every scheduled run) means
    # today's day number, so when there are more topics than result slots a
    # different few miss out each day, not the same last-added ones every
    # night. Tests pin it.
    rotation: int | None = None


@dataclass
class SearchSourceResult:
    items: list[dict] = field(default_factory=list)
    source: str = ""
    # Topics whose searxng query failed. Non-empty means this is a PARTIAL
    # result — the flow surfaces it so a degraded scan is distinguishable
    # from a genuinely quiet one.
    failed_topics: list[str] = field(default_factory=list)


@dataclass
class IntelScanActivities:
    searxng_url: str
    http_client: httpx.AsyncClient | None = None

    def _build_query(self, source: str, topic: str, template: str = "") -> dict[str, str]:
        """Return query params dict for a given source + topic."""
        params = {"q": render_query(template, source, topic), "format": "json"}
        # HN is a site search across every category; news and finance ask
        # searxng's news engines.
        if source != "hn":
            params["categories"] = "news"
        return params

    @activity.defn
    async def search_source(self, input: SearchSourceInput) -> SearchSourceResult:
        """One searxng query per topic, then a fair share of the results for
        each topic (`interleave`), trimmed to `max_results`."""
        if not self.searxng_url:
            logger.warning("searxng_url_missing")
            return SearchSourceResult(items=[], source=input.source)

        client = self.http_client or httpx.AsyncClient()
        per_topic: list[list[dict]] = []
        failed_topics: list[str] = []

        try:
            for topic in input.topics:
                params = self._build_query(input.source, topic, input.query_template)
                try:
                    resp = await client.get(f"{self.searxng_url}/search", params=params)
                    resp.raise_for_status()
                    data = resp.json()
                except Exception as exc:  # noqa: BLE001 — one topic must not sink the scan
                    # A single bad topic query (searxng 4xx/5xx, connect error,
                    # non-JSON body) used to abort the whole activity, which
                    # ACT_RETRY then replayed 3x into a hard workflow failure —
                    # throwing away every OTHER topic's results for that source.
                    # Degrade the topic, keep the scan (issue #136).
                    failed_topics.append(topic)
                    logger.warning(
                        "intel_scan_topic_failed",
                        source=input.source,
                        topic=topic,
                        error=error_text(exc),
                    )
                    continue
                per_topic.append(
                    [
                        {
                            "title": r.get("title", ""),
                            "url": r.get("url", ""),
                            "snippet": (r.get("content", "") or "")[:500],
                            "source": input.source,
                            "published": r.get("publishedDate", ""),
                        }
                        for r in data.get("results", [])
                    ]
                )
        finally:
            if self.http_client is None:
                await client.aclose()

        # Every topic failed => searxng itself is unreachable, not one bad
        # query. Raise so the run is honestly recorded as failed instead of
        # completing green with "0 results" — the per-topic guard above must
        # degrade a partial outage, never hide a total one.
        if input.topics and len(failed_topics) == len(input.topics):
            raise RuntimeError(
                f"search_source: all {len(input.topics)} topic queries failed "
                f"for source={input.source}"
            )

        rotation = input.rotation
        if rotation is None:
            rotation = datetime.now(UTC).date().toordinal()
        trimmed = interleave(per_topic, input.max_results, rotation)
        logger.info(
            "intel_scan_done",
            source=input.source,
            topics=input.topics,
            total=len(trimmed),
            failed_topics=failed_topics,
        )
        return SearchSourceResult(
            items=trimmed, source=input.source, failed_topics=failed_topics
        )
