"""RSS/Atom feed fetch via feedparser, plus the per-feed record (#511, #512).

feedparser is sync, so the fetch runs in `asyncio.to_thread`.

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

import structlog
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
    latest_published: str | None = None
    # Why a fetch produced nothing, when it did not simply have nothing new.
    # feedparser never raises: a dead host, a 404 or an HTML page all come
    # back as an empty parse with `bozo` set, which is how Miniflux's dead
    # feeds and a moved feed could both read as "quiet" (#511). "" = the fetch
    # itself worked.
    error: str = ""


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


def _fetch_error(parsed: Any) -> str:
    """Why an empty parse is a failed fetch, or "" when it is just empty."""
    if parsed.entries:
        return ""
    status = getattr(parsed, "status", None)
    try:
        if status is not None and int(status) >= 400:
            return f"HTTP {int(status)}"
    except (TypeError, ValueError):
        pass
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
        """Parse a feed URL. Entry dicts: {id, title, link, summary, published}."""

        def _sync() -> FetchFeedResult:
            import feedparser

            parsed = feedparser.parse(input.url)
            entries: list[dict] = []
            latest: str | None = None
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
                if published_iso and (latest is None or published_iso > latest):
                    latest = published_iso

            return FetchFeedResult(
                entries=entries, latest_published=latest, error=_fetch_error(parsed)
            )

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
        """Fold one run's fetch into the channel's config and return the
        consecutive failure count. `outcome` carries `ok`, `error` and
        `backlog`. A fetch that works resets the count."""
        ok = bool(outcome.get("ok"))
        cid = _as_uuid(channel_id)
        if not self.db_pool or cid is None:
            return {"fetch_failures": 0 if ok else 1}
        now = datetime.now(UTC).isoformat()
        patch: dict[str, Any] = {
            "last_fetch_at": now,
            "last_fetch_error": "" if ok else str(outcome.get("error") or "fetch failed")[:300],
            "backlog": int(outcome.get("backlog") or 0),
        }
        if ok:
            patch["last_fetch_ok_at"] = now
        failures = await self.db_pool.fetchval(
            "UPDATE channels SET config = config || jsonb_build_object("
            "  'fetch_failures', CASE WHEN $2 THEN 0 ELSE "
            "    (CASE WHEN config->>'fetch_failures' ~ '^[0-9]+$' "
            "          THEN (config->>'fetch_failures')::int ELSE 0 END) + 1 END, "
            # When AEGIS first polled the feed, set once: what `feed_stale`
            # measures from for a feed that never gave a dated entry.
            "  'tracking_since', COALESCE(config->>'tracking_since', $4::text)"
            ") || $3::text::jsonb "
            "WHERE id = $1 RETURNING (config->>'fetch_failures')::int",
            cid,
            ok,
            json.dumps(patch),
            now,
        )
        return {"fetch_failures": int(failures or 0)}
