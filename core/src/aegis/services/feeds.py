"""The RSS feed list and what each feed is worth (#511, #512).

AEGIS owns the feed list: `channels(kind='rss')` is what `RssIngestFlow` polls,
and nothing else seeds it. (A Miniflux instance used to seed the list once at
core startup. It sat dead for five and a half months with nobody reading in it,
and its health check only asked whether `/v1/feeds` answered — so it went.)

Everything here is shared by the admin route, Raphael's chat tools and the
worker, so there is one answer to "what is this feed worth?":

* :func:`feed_stats` — per feed, measured from `feed_entries` (migration 046):
  entries seen, stored, stored as an abstract only, documents a prompt used in
  the last 30 and 90 days, the last entry, fetch failures and the backlog.
  "Used" means a document was retrieved into a prompt — a chat turn or a
  research run (`knowledge_injection_log`, source `chat` or `research`). A
  briefing or a rollup does not log its reads, so it is still a floor.
* :func:`unused_feeds` — active feeds with 90 days of history and no use.
* :func:`recent_items` — the newest entries across the feeds, newest first,
  with an excerpt and whether a prompt used each: the reading list Miniflux
  used to be (Admin → Channels → Recent items).
* :func:`subscribe` / :func:`unsubscribe` — add a feed after checking it is
  one; stop one without deleting its history.
* :func:`retention_preview` — how much a "PDFs nobody used shrink to their
  first chunk" rule would free. Read-only by construction: it counts.
"""

from __future__ import annotations

import html
import re
from datetime import datetime
from typing import Any
from urllib.parse import urljoin, urlparse

import asyncpg
import httpx
import structlog

from aegis.services.url_guard import UnsafeURLError, public_url_problem

logger = structlog.get_logger()

# `channels.config.ingest` for an rss row (#512). `full` fetches the page (or
# PDF) and stores it; `abstract` stores the title and summary the feed already
# carries and fetches nothing; `gate` does `full` for an entry that matches a
# topic term and `abstract` for one that does not.
INGEST_MODES = ("full", "abstract", "gate")
# `full` is today's behaviour. Measured on 2026-09-12 against the last 30 days,
# the topic gate would have kept the full text of only 2 of the 10 non-arXiv
# documents a prompt actually used, so gating is opt-in per feed. arXiv is the
# feed to set to `abstract` (90% of all RSS chunks, 14 of 1,889 papers used).
DEFAULT_INGEST_MODE = "full"
# Fetches that must fail in a row before the feed is a hub finding. The flow
# runs hourly, so three is three hours — past a blip, well inside a day.
FAILING_AFTER = 3
# Days without a new entry before a feed is reported stale; per feed
# `channels.config.stale_after_days` overrides it.
DEFAULT_STALE_AFTER_DAYS = 30
# A feed needs this much history before it can be called unused.
UNUSED_AFTER_DAYS = 90
# `knowledge_chunks.embedding` is vector(768) of float4.
_VECTOR_BYTES = 768 * 4
# How much of a response a feed check reads.
_SNIFF_BYTES = 2_000_000
_FETCH_TIMEOUT = httpx.Timeout(15.0, connect=5.0)
_USER_AGENT = "Mozilla/5.0 (compatible; AegisBot/2.0; feed check)"

_FEED_ROOT_RE = re.compile(r"<(rss|feed|rdf:rdf)[\s>]", re.IGNORECASE)
_ITEM_RE = re.compile(r"<(item|entry)[\s>]", re.IGNORECASE)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_ALTERNATE_RE = re.compile(
    r"<link[^>]+type=[\"']application/(?:rss|atom)\+xml[\"'][^>]*>", re.IGNORECASE
)
_HREF_RE = re.compile(r"href=[\"']([^\"']+)[\"']", re.IGNORECASE)
_CDATA_RE = re.compile(r"<!\[CDATA\[(.*?)\]\]>", re.DOTALL)


def ingest_mode(config: dict | None) -> str:
    """The feed's ingest mode; anything unrecognised is the default."""
    mode = str((config or {}).get("ingest") or "").strip().lower()
    return mode if mode in INGEST_MODES else DEFAULT_INGEST_MODE


def stale_after_days(config: dict | None) -> int:
    try:
        days = int((config or {}).get("stale_after_days") or DEFAULT_STALE_AFTER_DAYS)
    except (TypeError, ValueError):
        return DEFAULT_STALE_AFTER_DAYS
    return days if days > 0 else DEFAULT_STALE_AFTER_DAYS


def feed_label(identifier: str, config: dict | None) -> str:
    """What a person calls the feed: its label, else its host."""
    label = str((config or {}).get("label") or "").strip()
    return label or (urlparse(identifier).hostname or identifier)


def _iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


# --------------------------------------------------------------------------
# What each feed is worth
# --------------------------------------------------------------------------

_STATS_SQL = """
WITH used AS (
    SELECT u.cid, max(l.created_at) AS last_used
    FROM knowledge_injection_log l, unnest(l.content_ids) AS u(cid)
    WHERE l.created_at > now() - interval '90 days'
    GROUP BY u.cid
),
per AS (
    SELECT fe.channel_id,
           min(fe.seen_at) AS tracking_since,
           count(*) FILTER (WHERE fe.seen_at > now() - interval '30 days') AS entries_30d,
           count(*) FILTER (WHERE fe.seen_at > now() - interval '90 days') AS entries_90d,
           count(*) FILTER (WHERE fe.seen_at > now() - interval '30 days'
                              AND fe.mode <> 'failed') AS stored_30d,
           count(*) FILTER (WHERE fe.seen_at > now() - interval '90 days'
                              AND fe.mode <> 'failed') AS stored_90d,
           count(*) FILTER (WHERE fe.seen_at > now() - interval '30 days'
                              AND fe.mode = 'abstract') AS abstract_30d,
           count(DISTINCT fe.content_id)
               FILTER (WHERE u.last_used > now() - interval '30 days') AS used_30d,
           count(DISTINCT fe.content_id) FILTER (WHERE u.last_used IS NOT NULL) AS used_90d,
           max(u.last_used) AS last_used_at
    FROM feed_entries fe
    LEFT JOIN used u ON u.cid = fe.content_id
    GROUP BY fe.channel_id
)
SELECT ch.id::text AS id, ch.identifier, ch.config, ch.active,
       p.tracking_since, p.entries_30d, p.entries_90d, p.stored_30d, p.stored_90d,
       p.abstract_30d, p.used_30d, p.used_90d, p.last_used_at
FROM channels ch
LEFT JOIN per p ON p.channel_id = ch.id
WHERE ch.kind = 'rss'
ORDER BY ch.active DESC, ch.identifier
"""


async def feed_stats(pool: asyncpg.Pool) -> list[dict[str, Any]]:
    """Every rss channel with its measured worth. One query; cheap on the full store."""
    out: list[dict[str, Any]] = []
    for r in await pool.fetch(_STATS_SQL):
        config = r["config"] if isinstance(r["config"], dict) else {}
        out.append(
            {
                "id": r["id"],
                "identifier": r["identifier"],
                "label": feed_label(r["identifier"], config),
                "active": r["active"],
                "agent_id": config.get("agent_id") or "",
                "ingest": ingest_mode(config),
                "tracking_since": _iso(r["tracking_since"]),
                "entries_30d": _int(r["entries_30d"]),
                "entries_90d": _int(r["entries_90d"]),
                "stored_30d": _int(r["stored_30d"]),
                "stored_90d": _int(r["stored_90d"]),
                "abstract_30d": _int(r["abstract_30d"]),
                "used_30d": _int(r["used_30d"]),
                "used_90d": _int(r["used_90d"]),
                "last_used_at": _iso(r["last_used_at"]),
                "last_entry_at": config.get("last_cursor"),
                "last_fetch_at": config.get("last_fetch_at"),
                "fetch_failures": _int(config.get("fetch_failures")),
                "last_fetch_error": config.get("last_fetch_error") or "",
                "backlog": _int(config.get("backlog")),
            }
        )
    return out


async def unused_feeds(pool: asyncpg.Pool) -> list[dict[str, Any]]:
    """Active feeds with at least 90 days of history that no prompt used in 90 days.

    A feed younger than that is not judged: "nothing used yet" from a feed
    added last week is not evidence.
    """
    now = datetime.now().astimezone()
    out = []
    for f in await feed_stats(pool):
        if not f["active"] or f["used_90d"]:
            continue
        since = f["tracking_since"]
        if not since:
            continue
        if (now - datetime.fromisoformat(since)).days < UNUSED_AFTER_DAYS:
            continue
        out.append(f)
    return out


# --------------------------------------------------------------------------
# What came in
# --------------------------------------------------------------------------

# `feed_entries.mode`: what happened to one entry (migration 046).
ENTRY_MODES = ("full", "abstract", "failed")
RECENT_ITEMS_MAX = 200
_EXCERPT_CHARS = 280
_WS_RE = re.compile(r"\s+")

# Newest first, keyset-paged on (seen_at, external_id): an entry that arrives
# between two pages can neither shift the next page nor repeat an item. The
# excerpt's first chunk is one indexed lookup per row (knowledge_chunks has an
# index on content_id), and a failed entry has no content row at all.
_RECENT_SQL = """
SELECT fe.channel_id::text AS channel_id, ch.identifier, ch.config,
       fe.external_id, fe.link, fe.mode, fe.published, fe.seen_at,
       c.title, c.summary,
       (SELECT left(k.chunk_text, 600) FROM knowledge_chunks k
         WHERE k.content_id = fe.content_id
         ORDER BY k.chunk_index LIMIT 1) AS first_chunk,
       EXISTS (SELECT 1 FROM knowledge_injection_log l
                WHERE fe.content_id = ANY(l.content_ids)) AS used
FROM feed_entries fe
JOIN channels ch ON ch.id = fe.channel_id
LEFT JOIN knowledge_content c ON c.content_id = fe.content_id
WHERE ch.kind = 'rss'
  AND ($1::uuid IS NULL OR fe.channel_id = $1::uuid)
  AND ($2::text IS NULL OR fe.mode = $2::text)
  AND ($3::timestamptz IS NULL OR (fe.seen_at, fe.external_id) < ($3::timestamptz, $4::text))
ORDER BY fe.seen_at DESC, fe.external_id DESC
LIMIT $5
"""


def _safe_link(link: str) -> str:
    """The entry's link if it is a web address, else "". A feed is untrusted
    input, and the admin page turns this into a clickable `href`."""
    link = (link or "").strip()
    return link if urlparse(link).scheme in ("http", "https") else ""


def _excerpt(summary: str | None, chunk: str | None, title: str) -> str:
    """A couple of lines to read under the title: the feed's summary, else the
    start of the stored page. A stored page usually opens with its own title,
    which says nothing a second time, so that is dropped."""
    text = _WS_RE.sub(" ", (summary or "").strip() or (chunk or "").strip())
    if title and text.lower().startswith(title.lower()):
        text = text[len(title) :].lstrip(" -:|—")
    if len(text) > _EXCERPT_CHARS:
        return text[:_EXCERPT_CHARS].rstrip() + "…"
    return text


def _cursor(seen_at: datetime, external_id: str) -> str:
    return f"{seen_at.isoformat()}|{external_id}"


def _parse_cursor(cursor: str) -> tuple[datetime, str]:
    seen, sep, external_id = (cursor or "").partition("|")
    try:
        when = datetime.fromisoformat(seen)
    except ValueError as exc:
        raise ValueError(f"bad cursor {cursor!r}") from exc
    if not sep or when.tzinfo is None:
        raise ValueError(f"bad cursor {cursor!r}")
    return when, external_id


async def recent_items(
    pool: asyncpg.Pool,
    *,
    channel_id: Any = None,
    mode: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> dict[str, Any]:
    """The newest RSS entries across every feed (or one), newest first — the
    reading list Miniflux used to be. Each item carries its feed, title, link,
    when it came in, how it was stored (`full` / `abstract` / `failed`), an
    excerpt and whether a prompt ever used it. Page with `next_cursor`.
    Read-only."""
    if mode is not None and mode not in ENTRY_MODES:
        raise ValueError(f"mode must be one of {ENTRY_MODES}, not {mode!r}")
    limit = max(1, min(int(limit), RECENT_ITEMS_MAX))
    after_seen, after_id = _parse_cursor(cursor) if cursor else (None, None)
    rows = await pool.fetch(
        _RECENT_SQL,
        str(channel_id) if channel_id else None,
        mode,
        after_seen,
        after_id,
        limit + 1,
    )
    page = rows[:limit]
    items = []
    for r in page:
        config = r["config"] if isinstance(r["config"], dict) else {}
        link = _safe_link(r["link"])
        title = (r["title"] or "").strip() or link or r["external_id"]
        items.append(
            {
                "channel_id": r["channel_id"],
                "feed": feed_label(r["identifier"], config),
                "feed_url": r["identifier"],
                "external_id": r["external_id"],
                "title": title,
                "link": link,
                "mode": r["mode"],
                "published": r["published"] or None,
                "seen_at": _iso(r["seen_at"]),
                "excerpt": _excerpt(r["summary"], r["first_chunk"], title),
                "used": bool(r["used"]),
            }
        )
    more = len(rows) > limit
    next_cursor = _cursor(page[-1]["seen_at"], page[-1]["external_id"]) if more else None
    return {"items": items, "next_cursor": next_cursor}


# --------------------------------------------------------------------------
# Subscribing and unsubscribing
# --------------------------------------------------------------------------


def _clean_title(raw: str) -> str:
    raw = _CDATA_RE.sub(r"\1", raw or "")
    return html.unescape(re.sub(r"<[^>]+>", " ", raw)).strip()[:120]


async def inspect_feed(url: str) -> dict[str, Any]:
    """Fetch `url` and say whether it is an RSS or Atom feed.

    Returns ``{"ok": True, "title", "entries"}`` or ``{"ok": False, "error"}``.
    An HTML page that advertises a feed says where (``"suggest"``), because
    "follow simonwillison.net" usually means the site, not its feed URL.
    """
    problem = await public_url_problem(url)
    if problem:
        return {"ok": False, "error": problem}

    async def _public_only(request: httpx.Request) -> None:
        # Every hop, the redirects included: a public page that redirects
        # inward would otherwise be fetched from inside the stack. Looked up
        # at call time, so each hop is checked by the same function the URL was.
        hop = await public_url_problem(str(request.url))
        if hop:
            raise UnsafeURLError(f"refused {request.url.host or request.url}: {hop}")

    body_bytes = bytearray()
    try:
        async with (
            httpx.AsyncClient(
                timeout=_FETCH_TIMEOUT,
                follow_redirects=True,
                headers={"User-Agent": _USER_AGENT},
                event_hooks={"request": [_public_only]},
            ) as client,
            client.stream("GET", url) as resp,
        ):
            status = resp.status_code
            content_type = resp.headers.get("content-type", "unknown type")
            final_url = str(resp.url)
            if status < 400:
                async for chunk in resp.aiter_bytes():
                    body_bytes += chunk[: _SNIFF_BYTES - len(body_bytes)]
                    if len(body_bytes) >= _SNIFF_BYTES:
                        break
    except UnsafeURLError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 — an unreachable URL is an answer
        return {"ok": False, "error": f"could not fetch it: {str(exc)[:200]}"}
    if status >= 400:
        return {"ok": False, "error": f"the server answered HTTP {status}"}
    body = bytes(body_bytes).decode("utf-8", errors="replace")
    if not _FEED_ROOT_RE.search(body[:4000]):
        out: dict[str, Any] = {
            "ok": False,
            "error": f"that is not an RSS or Atom feed ({content_type})",
        }
        link = _ALTERNATE_RE.search(body)
        href = _HREF_RE.search(link.group(0)) if link else None
        if href:
            out["suggest"] = urljoin(final_url, html.unescape(href.group(1)))
        return out
    title = _TITLE_RE.search(body)
    return {
        "ok": True,
        "title": _clean_title(title.group(1)) if title else "",
        "entries": len(_ITEM_RE.findall(body)),
    }


async def subscribe(
    pool: asyncpg.Pool, url: str, *, label: str = "", agent_id: str | None = None
) -> dict[str, Any]:
    """Follow a feed: check it parses as one, then add (or re-activate) its channel."""
    url = (url or "").strip()
    if not url:
        return {"error": "url is required"}
    existing = await pool.fetchrow(
        "SELECT id::text AS id, active, config FROM channels WHERE kind = 'rss' AND identifier = $1",
        url,
    )
    if existing and existing["active"]:
        return {
            "status": "already_subscribed",
            "id": existing["id"],
            "label": feed_label(url, existing["config"]),
        }
    check = await inspect_feed(url)
    if not check["ok"]:
        return {"error": check["error"], **({"suggest": check["suggest"]} if "suggest" in check else {})}
    if existing:
        await pool.execute("UPDATE channels SET active = true WHERE id = $1::uuid", existing["id"])
        return {
            "status": "resubscribed",
            "id": existing["id"],
            "label": feed_label(url, existing["config"]),
            "entries_in_feed": check["entries"],
        }
    config: dict[str, Any] = {
        "label": (label or "").strip() or check["title"] or feed_label(url, None),
        "last_cursor": None,
        "ingest": DEFAULT_INGEST_MODE,
    }
    if agent_id:
        config["agent_id"] = agent_id
    try:
        row = await pool.fetchrow(
            "INSERT INTO channels (kind, identifier, config, active) "
            "VALUES ('rss', $1, $2, true) RETURNING id::text AS id",
            url,
            config,
        )
    except asyncpg.UniqueViolationError:
        return {"status": "already_subscribed", "label": config["label"]}
    logger.info("feed_subscribed", url=url[:200], label=config["label"])
    return {
        "status": "subscribed",
        "id": row["id"],
        "label": config["label"],
        "entries_in_feed": check["entries"],
    }


async def unsubscribe(pool: asyncpg.Pool, feed: str) -> dict[str, Any]:
    """Stop following a feed, by URL or label. The row and its history stay."""
    feed = (feed or "").strip()
    if not feed:
        return {"error": "say which feed: its URL or its label"}
    rows = await pool.fetch(
        "SELECT id::text AS id, identifier, config FROM channels "
        "WHERE kind = 'rss' AND active "
        "  AND (identifier = $1 OR lower(config->>'label') = lower($1))",
        feed,
    )
    if not rows:
        active = await pool.fetch(
            "SELECT identifier, config FROM channels WHERE kind = 'rss' AND active ORDER BY identifier"
        )
        return {
            "error": f"no active feed matches {feed!r}",
            "feeds": [feed_label(r["identifier"], r["config"]) for r in active],
        }
    if len(rows) > 1:
        return {
            "error": f"{feed!r} matches {len(rows)} feeds; give the URL",
            "feeds": [r["identifier"] for r in rows],
        }
    row = rows[0]
    await pool.execute("UPDATE channels SET active = false WHERE id = $1::uuid", row["id"])
    logger.info("feed_unsubscribed", url=row["identifier"][:200])
    return {
        "status": "unsubscribed",
        "identifier": row["identifier"],
        "label": feed_label(row["identifier"], row["config"]),
    }


# --------------------------------------------------------------------------
# Retention, as a dry run only (#512)
# --------------------------------------------------------------------------

_RETENTION_SQL = """
WITH used AS (SELECT DISTINCT unnest(content_ids) AS cid FROM knowledge_injection_log),
per AS (
    SELECT c.content_id,
           count(k.id) AS chunks,
           coalesce(sum(octet_length(k.chunk_text)), 0) AS text_bytes,
           coalesce(sum(octet_length(k.chunk_text)) FILTER (WHERE k.chunk_index = 0), 0)
               AS kept_bytes
    FROM knowledge_content c
    JOIN knowledge_chunks k ON k.content_id = c.content_id
    WHERE c.source_type = 'pdf'
      AND c.ingested_at < now() - make_interval(days => $1)
      AND NOT EXISTS (SELECT 1 FROM used u WHERE u.cid = c.content_id)
    GROUP BY c.content_id
)
SELECT count(*) AS documents,
       coalesce(sum(chunks), 0) AS chunks,
       coalesce(sum(text_bytes), 0) AS text_bytes,
       coalesce(sum(kept_bytes), 0) AS kept_bytes
FROM per
"""


async def retention_preview(pool: asyncpg.Pool, older_than_days: int = 30) -> dict[str, Any]:
    """What "a PDF no prompt used, older than N days, keeps only its first
    chunk" would remove. Counts only — nothing is deleted or changed."""
    days = max(1, int(older_than_days))
    r = await pool.fetchrow(_RETENTION_SQL, days)
    documents, chunks = _int(r["documents"]), _int(r["chunks"])
    removed = max(0, chunks - documents)
    return {
        "dry_run": True,
        "rule": f"PDF, never injected into a prompt, ingested more than {days} days ago: "
        "keep its first chunk, drop the rest",
        "older_than_days": days,
        "documents": documents,
        "chunks": chunks,
        "chunks_removed": removed,
        "text_bytes_freed": max(0, _int(r["text_bytes"]) - _int(r["kept_bytes"])),
        "vector_bytes_freed": removed * _VECTOR_BYTES,
        "note": "Use is counted from knowledge_injection_log, which chat turns and "
        "research runs write; a document only a briefing or rollup read counts as unused.",
    }
