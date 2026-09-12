"""RSS/Atom feed fetch, plus the per-feed record (#511, #512).

A feed URL is a third party's: the feed is fetched with an httpx client whose
`url_guard` hook checks the first request and every redirect, bounded in time
and size, and only the bytes go to feedparser. feedparser used to fetch the URL
itself, over urllib, following any redirect wherever it led — a public feed
could bounce the poll onto the overlay network. feedparser is sync, so the
parse runs in `asyncio.to_thread`.

Besides fetching, this module keeps what `RssIngestFlow` learns about each
feed: which knowledge row every entry produced (`feed_entries`), whether the
fetch worked (`channels.config.fetch_failures` and friends), and the topic
terms the relevance gate matches entries against.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog
from aegis.services import feeds
from aegis.services.url_guard import UnsafeURLError, guarded_hooks
from temporalio import activity

from aegis_worker.activities.channels import _decode_config
from aegis_worker.activities.intelligence import TRACKED_TOPICS_SETTING, tracked_search_terms

logger = structlog.get_logger()


@dataclass
class FetchFeedInput:
    url: str
    since_cursor: str | None = None  # ISO timestamp


@dataclass
class FetchFeedResult:
    entries: list[dict] = field(default_factory=list)
    # Why a fetch produced nothing, when it did not simply have nothing new: an
    # HTTP error, a refused or failed request, or a body that is not a feed.
    # Before #511 all of these came back as an empty parse, which is how
    # Miniflux's dead feeds and a moved feed could both read as "quiet". ""
    # = the fetch itself worked.
    error: str = ""


# The fetch's bounds. A feed is one document: arXiv's daily burst, the largest
# the feeds carry, is a few MB, so a body past the cap is not a feed worth
# parsing.
_FEED_TIMEOUT = httpx.Timeout(30.0)
_FEED_MAX_BYTES = 20 * 1024 * 1024
# The headers feedparser sent when it fetched the feed itself, so a feed that
# served feedparser still serves this.
_FEED_HEADERS = {
    "User-Agent": "feedparser/6.0 +https://github.com/kurtmckee/feedparser/",
    "Accept": (
        "application/atom+xml,application/rdf+xml,application/rss+xml,"
        "application/x-netcdf,application/xml;q=0.9,text/xml;q=0.2,*/*;q=0.1"
    ),
}


def gate_pattern(terms: list[str]) -> re.Pattern[str] | None:
    """One case-insensitive pattern matching any topic term as a whole word.

    Lookarounds rather than `\\b`, so a term that starts or ends with a
    non-word character ("c++", ".net") still matches. None when there are no
    terms — the gate then has nothing to go on and lets everything through.
    """
    cleaned = sorted({t.strip() for t in terms if t and t.strip()}, key=len, reverse=True)
    if not cleaned:
        return None
    return re.compile(
        r"(?<!\w)(?:" + "|".join(re.escape(t) for t in cleaned) + r")(?!\w)", re.IGNORECASE
    )


def passes_gate(pattern: re.Pattern[str] | None, entry: dict) -> bool:
    """Whether an entry's title or summary names a topic. Permissive on
    purpose: a wasted full fetch is cheaper than a relevant paper kept as an
    abstract."""
    if pattern is None:
        return True
    return bool(pattern.search(f"{entry.get('title') or ''} {entry.get('summary') or ''}"))


async def _download_feed(url: str) -> tuple[bytes, dict[str, str], str]:
    """`(body, headers for feedparser, error)`: the feed's bytes, or why there
    are none. Never raises. An HTTP error, a refused hop, a network failure and
    a body past `_FEED_MAX_BYTES` are all a failed fetch."""
    try:
        async with (
            httpx.AsyncClient(
                timeout=_FEED_TIMEOUT,
                follow_redirects=True,
                event_hooks=guarded_hooks(),
                headers=_FEED_HEADERS,
            ) as client,
            client.stream("GET", url) as resp,
        ):
            if resp.status_code >= 400:
                return b"", {}, f"HTTP {resp.status_code}"
            body = bytearray()
            async for chunk in resp.aiter_bytes():
                body += chunk
                if len(body) > _FEED_MAX_BYTES:
                    return b"", {}, f"the response is larger than {_FEED_MAX_BYTES // 2**20} MB"
            # The final URL after redirects, so relative links resolve against
            # where the feed actually lives; the content type for the charset.
            headers = {
                "content-location": str(resp.url),
                "content-type": resp.headers.get("content-type", ""),
            }
            return bytes(body), headers, ""
    except UnsafeURLError as exc:
        return b"", {}, str(exc)[:200]
    except httpx.HTTPError as exc:
        return b"", {}, (str(exc) or type(exc).__name__)[:200]


def _fetch_error(parsed: Any) -> str:
    """Why an empty parse of a fetched body is a failed fetch, or "" when the
    feed is just empty. (An HTTP error never gets this far: `_download_feed`
    reports it.)"""
    if parsed.entries:
        return ""
    if getattr(parsed, "bozo", 0):
        # feedparser sets `version` ("rss20", "atom10", ...) when it recognised
        # a feed. A recognised feed that is merely empty, with a benign
        # complaint such as CharacterEncodingOverride, is a quiet feed, not a
        # failed fetch; three of those in a row used to raise `feed_failing`.
        version = getattr(parsed, "version", "")
        if isinstance(version, str) and version:
            return ""
        exc = getattr(parsed, "bozo_exception", None)
        return (str(exc) if exc else "the response is not a feed")[:200]
    return ""


def _as_uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


@dataclass
class RssActivities:
    db_pool: Any

    @activity.defn
    async def fetch_feed(self, input: FetchFeedInput) -> FetchFeedResult:
        """Fetch and parse a feed. Entry dicts: {id, title, link, summary, published}."""
        body, headers, error = await _download_feed(input.url)
        if error:
            return FetchFeedResult(error=error)

        def _sync() -> FetchFeedResult:
            import feedparser

            parsed = feedparser.parse(body, response_headers=headers)
            entries: list[dict] = []
            for e in parsed.entries:
                # Prefer published_parsed (9-tuple) -> ISO; fall back to raw
                # published string. Cursor comparisons elsewhere (Raindrop's
                # `last_cursor`) are tz-aware (Z suffix), so build tz-aware
                # ISO strings here too — naive vs tz-aware lexicographic
                # compare is silently broken otherwise.
                published_iso = ""
                if getattr(e, "published_parsed", None):
                    import datetime as _dt

                    published_iso = (
                        _dt.datetime(*e.published_parsed[:6])
                        .replace(tzinfo=_dt.UTC)
                        .isoformat()
                    )
                elif getattr(e, "updated_parsed", None):
                    import datetime as _dt

                    published_iso = (
                        _dt.datetime(*e.updated_parsed[:6])
                        .replace(tzinfo=_dt.UTC)
                        .isoformat()
                    )
                else:
                    published_iso = getattr(e, "published", "") or getattr(e, "updated", "")

                if input.since_cursor and published_iso and published_iso <= input.since_cursor:
                    continue

                entries.append(
                    {
                        "id": getattr(e, "id", "") or getattr(e, "link", ""),
                        "title": getattr(e, "title", ""),
                        "link": getattr(e, "link", ""),
                        "summary": getattr(e, "summary", "")[:500]
                        if getattr(e, "summary", "")
                        else "",
                        "published": published_iso,
                    }
                )

            return FetchFeedResult(entries=entries, error=_fetch_error(parsed))

        return await asyncio.to_thread(_sync)

    @activity.defn
    async def load_gate_terms(self) -> list[str]:
        """The relevance gate's topic terms (#512): every active intel scan's
        configured topics, then the topics tracked from chat. Deduplicated
        whatever their case, in that order."""
        if not self.db_pool:
            return []
        rows = await self.db_pool.fetch(
            "SELECT config FROM activities WHERE workflow_type = 'IntelligenceScanFlow' AND active"
        )
        terms: list[str] = []
        for r in rows:
            topics = _decode_config(r["config"]).get("topics") or []
            terms += [t for t in topics if isinstance(t, str)] if isinstance(topics, list) else []
        value = await self.db_pool.fetchval(
            "SELECT value FROM settings WHERE key = $1", TRACKED_TOPICS_SETTING
        )
        terms += tracked_search_terms(value)
        seen: set[str] = set()
        out: list[str] = []
        for t in terms:
            key = t.strip().lower()
            if key and key not in seen:
                seen.add(key)
                out.append(t.strip())
        return out

    @activity.defn
    async def record_feed_entries(self, channel_id: str, rows: list[dict]) -> int:
        """Record what one run did with a feed's entries: one `feed_entries`
        row each, carrying the knowledge row it produced. A retried entry
        updates its row, so a failure that later succeeds reads as stored."""
        cid = _as_uuid(channel_id)
        if not self.db_pool or cid is None or not rows:
            return 0
        values = [
            (
                cid,
                str(r.get("external_id") or "")[:1000],
                str(r.get("link") or "")[:2000],
                r.get("content_id") or None,
                r.get("mode") if r.get("mode") in ("full", "abstract", "failed") else "failed",
                str(r.get("published") or "")[:64],
            )
            for r in rows
            if r.get("external_id")
        ]
        async with self.db_pool.acquire() as conn:
            await conn.executemany(
                "INSERT INTO feed_entries (channel_id, external_id, link, content_id, mode, published) "
                "VALUES ($1, $2, $3, $4, $5, $6) "
                "ON CONFLICT (channel_id, external_id) DO UPDATE SET "
                "  mode = EXCLUDED.mode, "
                "  content_id = COALESCE(EXCLUDED.content_id, feed_entries.content_id), "
                "  link = EXCLUDED.link, published = EXCLUDED.published",
                values,
            )
        return len(values)

    @activity.defn
    async def record_feed_run(self, channel_id: str, outcome: dict) -> dict:
        """Fold one run's fetch into the channel's config, and say what the
        feed's record holds now. `outcome` carries `ok`, `error` and `backlog`.

        * `fetch_failures` / `fetch_successes`: fetches in a row that failed /
          worked; each resets the other. A failing feed resolves on
          `feeds.RECOVERED_AFTER` good fetches in a row, not on the first.
        * `last_stored_at`: the newest entry the store kept for the feed
          (`feed_entries.seen_at`, failed entries left out) — what staleness is
          measured from.
        * `tracking_since`: `feeds.tracking_since`, the one definition the
          feed stats use too.
        """
        ok = bool(outcome.get("ok"))
        cid = _as_uuid(channel_id)
        if not self.db_pool or cid is None:
            return {
                "fetch_failures": 0 if ok else 1,
                "fetch_successes": 1 if ok else 0,
                "last_stored_at": None,
                "tracking_since": None,
            }
        now = datetime.now(UTC).isoformat()
        patch: dict[str, Any] = {
            "last_fetch_at": now,
            "last_fetch_error": "" if ok else str(outcome.get("error") or "fetch failed")[:300],
            "backlog": int(outcome.get("backlog") or 0),
        }
        config = await self.db_pool.fetchval(
            "UPDATE channels SET config = config || jsonb_build_object("
            "  'fetch_failures', CASE WHEN $2 THEN 0 ELSE "
            "    (CASE WHEN config->>'fetch_failures' ~ '^[0-9]+$' "
            "          THEN (config->>'fetch_failures')::int ELSE 0 END) + 1 END, "
            "  'fetch_successes', CASE WHEN $2 THEN "
            "    (CASE WHEN config->>'fetch_successes' ~ '^[0-9]+$' "
            "          THEN (config->>'fetch_successes')::int ELSE 0 END) + 1 ELSE 0 END, "
            # When AEGIS first polled the feed, set once: when tracking began
            # for a feed that has recorded no entry (`feeds.tracking_since`).
            "  'tracking_since', COALESCE(config->>'tracking_since', $4::text)"
            ") || $3::text::jsonb "
            "WHERE id = $1 RETURNING config",
            cid,
            ok,
            json.dumps(patch),
            now,
        )
        config = _decode_config(config) if config is not None else {}
        seen = await self.db_pool.fetchrow(
            "SELECT min(seen_at) AS first_seen, "
            "       max(seen_at) FILTER (WHERE mode <> 'failed') AS last_stored "
            "FROM feed_entries WHERE channel_id = $1",
            cid,
        )
        last_stored = seen["last_stored"] if seen else None
        return {
            "fetch_failures": int(config.get("fetch_failures") or 0),
            "fetch_successes": int(config.get("fetch_successes") or 0),
            "last_stored_at": last_stored.isoformat() if last_stored else None,
            "tracking_since": feeds.tracking_since(seen["first_seen"] if seen else None, config),
        }
