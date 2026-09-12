"""Tracked research topics (#513): the registry, one hub problem per round, and
matching items to topics.

Spec: docs/superpowers/specs/2026-09-12-research-hub-design.md.

**The registry is the `intelligence_topics` settings row** —
``{"topics": [{"name", "queries", "priority"}]}``, which `track_topic` has
always written and which the intel scans (#508) and the RSS gate (#512) read
for their search terms. It stays the registry because a topic's terms must
outlive any one problem. Every change to it is a read-modify-write under one
advisory lock, so `track_topic` from chat and a curiosity "yes" landing
together cannot lose one of the two.

**A topic's activity is a hub problem.** Class `topic`, subject the topic's
slug, kind `topic`, source `research` — so Raphael owns it (`hub_project`).
Every article that names one of the topic's terms is an occurrence, keyed on
the article's URL, so the same story reaching us from a scan and a feed
attaches once. The problem earns a Todoist task only when its current round
holds enough items (`ATTENTION_ITEMS`); until then it lives in the hub and the
briefing.

**A round ends when the user ticks the task off.** The problem resolves and
closes at once (`hub_project.reconcile_completed_tasks` → :func:`close_round`),
so the next matching article opens a fresh round — a new problem, and a new
task only when that round crosses the threshold again. The hub's usual reopen
window would have reopened the task on the next day's article.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import asyncpg
import structlog

from aegis.services.hub import (
    TOPIC_CLASS,
    Event,
    correlation_key,
    ingest_event,
    set_status,
    slug,
)

logger = structlog.get_logger()

TOPICS_SETTING = "intelligence_topics"
SOURCE = "research"
TOPIC_KIND = "topic"
PRIORITIES = ("high", "medium", "low")
# Items a round must hold before the topic interrupts the user with a task.
# One new article is news, not a chore; a high-priority topic asks sooner.
ATTENTION_ITEMS = {"high": 2, "medium": 3, "low": 5}
# What a round's task lists, newest first.
_DIGEST_ITEMS = 10
# The advisory lock every registry read-modify-write takes.
_REGISTRY_LOCK = "research_topics:registry"


@dataclass(frozen=True)
class Topic:
    name: str
    queries: tuple[str, ...]
    priority: str = "medium"

    @property
    def slug(self) -> str:
        return slug(self.name)

    @property
    def terms(self) -> tuple[str, ...]:
        """What an article must name to belong to the topic: its queries, or
        its name when it has none — the same rule the scans search by."""
        return self.queries or (self.name,)

    @property
    def threshold(self) -> int:
        return ATTENTION_ITEMS.get(self.priority, ATTENTION_ITEMS["medium"])


def parse_topics(value: Any) -> list[Topic]:
    """The topics in an `intelligence_topics` value. Lenient: a hand-edited or
    half-written row yields what it can and never raises."""
    import json

    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    if not isinstance(value, dict):
        return []
    out: list[Topic] = []
    seen: set[str] = set()
    for raw in value.get("topics") or []:
        if not isinstance(raw, dict):
            continue
        name = raw.get("name")
        if not isinstance(name, str) or not name.strip() or not slug(name):
            continue
        qs = raw.get("queries")
        queries = tuple(q.strip() for q in qs if isinstance(q, str) and q.strip()) if isinstance(qs, list) else ()
        priority = raw.get("priority") if raw.get("priority") in PRIORITIES else "medium"
        topic = Topic(name.strip(), queries, priority)
        if topic.slug in seen:
            continue
        seen.add(topic.slug)
        out.append(topic)
    return out


async def load_topics(pool: asyncpg.Pool) -> list[Topic]:
    return parse_topics(
        await pool.fetchval("SELECT value FROM settings WHERE key = $1", TOPICS_SETTING)
    )


def _pattern(topic: Topic) -> re.Pattern[str]:
    alts = "|".join(re.escape(t) for t in sorted(topic.terms, key=len, reverse=True))
    return re.compile(rf"(?<![\w])(?:{alts})(?![\w])", re.IGNORECASE)


def match_topics(topics: list[Topic], text: str) -> list[Topic]:
    """The topics whose terms ``text`` names, as whole words in any case. No
    model: the same kind of cheap, permissive test the RSS gate uses."""
    if not text:
        return []
    return [t for t in topics if _pattern(t).search(text)]


def _event(topic: Topic, **kw: Any) -> Event:
    return Event(
        source=SOURCE,
        klass=TOPIC_CLASS,
        subject=topic.slug,
        subject_kind=TOPIC_KIND,
        severity="info",
        **kw,
    )


async def live_problem(pool: asyncpg.Pool, topic: Topic) -> dict[str, Any] | None:
    """The topic's current round, if one is open."""
    key = correlation_key(_event(topic, external_id="-", kind="occurrence", title="-"))
    row = await pool.fetchrow(
        "SELECT id::text AS id, status, metadata, todoist_task_id FROM problems "
        "WHERE correlation_key = $1 AND closed_at IS NULL",
        key,
    )
    return dict(row) if row else None


async def ensure_round(
    pool: asyncpg.Pool, topic: Topic, *, now: datetime | None = None
) -> str | None:
    """The live problem for ``topic``, opening a round when there is none. The
    opening occurrence names the round; it is not an item, so it never counts
    towards the threshold."""
    now = now or datetime.now(UTC)
    current = await live_problem(pool, topic)
    if current is not None:
        return current["id"]
    result = await ingest_event(
        pool,
        _event(
            topic,
            external_id=f"topic:{topic.slug}@{now.isoformat()}",
            kind="occurrence",
            title=f"{topic.name}: new items worth a look",
            payload={"opened": True, "topic": topic.name},
            occurred_at=now,
        ),
        now=now,
    )
    if not result.problem_id:
        return None
    await pool.execute(
        "UPDATE problems SET metadata = metadata || $2::jsonb WHERE id = $1::uuid",
        result.problem_id,
        {"topic": topic.name},
    )
    return result.problem_id


async def _save_registry(db: Any, topics: list[dict[str, Any]]) -> None:
    await db.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES ($1, $2, NOW()) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()",
        TOPICS_SETTING,
        {"topics": topics},
    )


async def _raw_registry(db: Any) -> list[dict[str, Any]]:
    value = await db.fetchval("SELECT value FROM settings WHERE key = $1", TOPICS_SETTING)
    if isinstance(value, dict) and isinstance(value.get("topics"), list):
        return [t for t in value["topics"] if isinstance(t, dict)]
    return []


async def track(
    pool: asyncpg.Pool,
    name: str,
    queries: list[str],
    priority: str = "medium",
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Track a topic: write it to the registry the scans read, and make sure it
    has a live round in the hub. Raises ValueError on an empty name or no
    queries. Re-tracking a topic (any case) updates its queries and priority."""
    name = (name or "").strip()
    queries = [q.strip() for q in (queries or []) if isinstance(q, str) and q.strip()]
    if not name or not queries or not slug(name):
        raise ValueError("topic_name and queries are required")
    priority = priority if priority in PRIORITIES else "medium"
    entry = {"name": name, "queries": queries, "priority": priority}

    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", _REGISTRY_LOCK)
        existing = await _raw_registry(conn)
        status = "added"
        updated: list[dict[str, Any]] = []
        for t in existing:
            if slug(str(t.get("name") or "")) == slug(name):
                if status == "added":
                    updated.append(entry)
                status = "updated"
            else:
                updated.append(t)
        if status == "added":
            updated.append(entry)
        await _save_registry(conn, updated)

    problem_id = await ensure_round(pool, Topic(name, tuple(queries), priority), now=now)
    logger.info("research_topic_tracked", topic=name, status=status, problem_id=problem_id)
    return {
        "status": status,
        "topic": name,
        "query_count": len(queries),
        "total_topics": len(updated),
        "problem_id": problem_id,
        "task_after_items": ATTENTION_ITEMS[priority],
    }


async def untrack(
    pool: asyncpg.Pool, name: str, *, now: datetime | None = None
) -> dict[str, Any]:
    """Stop tracking a topic: drop it from the registry, so the scans stop
    searching it, and close its live round (retiring the round's task).

    The removal is what the call reports on; retiring the task is a Todoist
    round trip after it, so a failure there is `task_retired: False`, never an
    error for a removal that already happened."""
    now = now or datetime.now(UTC)
    target = slug(name or "")
    if not target:
        raise ValueError("topic_name is required")
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", _REGISTRY_LOCK)
        existing = await _raw_registry(conn)
        kept = [t for t in existing if slug(str(t.get("name") or "")) != target]
        if len(kept) == len(existing):
            return {"status": "not_found", "topic": name, "total_topics": len(existing)}
        await _save_registry(conn, kept)
    out: dict[str, Any] = {"status": "removed", "topic": name, "total_topics": len(kept)}
    closed = False
    current = await live_problem(pool, Topic(name.strip(), ()))
    if current is not None:
        closed = await close_round(
            pool, current["id"], reason="the topic is no longer tracked", now=now
        )
        if closed and current.get("todoist_task_id"):
            from aegis.services import hub_project

            try:
                out["task_retired"] = bool(
                    await hub_project.retire_task(
                        pool, current["todoist_task_id"], f"No longer tracking {name.strip()}."
                    )
                )
            except Exception as exc:  # noqa: BLE001 — the topic is untracked either way
                logger.warning(
                    "research_topic_task_retire_failed", topic=name, error=str(exc)[:200]
                )
                out["task_retired"] = False
    out["round_closed"] = closed
    logger.info("research_topic_untracked", topic=name, round_closed=closed)
    return out


async def close_round(
    pool: asyncpg.Pool,
    problem_id: str,
    *,
    reason: str,
    source: str = SOURCE,
    now: datetime | None = None,
) -> bool:
    """End a topic's round: resolve it, then close it at once, so the next item
    opens a fresh round instead of reopening this one. True when it closed.

    Both steps run under the advisory lock `ingest_event` takes for the
    round's correlation key. Without it an item attached between the resolve
    and the close reopened the round, and with it the task the user had just
    ticked off."""
    from aegis.services.hub import close_problem

    now = now or datetime.now(UTC)
    async with pool.acquire() as conn, conn.transaction():
        key = await conn.fetchval(
            "SELECT correlation_key FROM problems WHERE id = $1::uuid", problem_id
        )
        if key:
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", key)
        await set_status(pool, problem_id, "resolved", reason=reason, source=source, now=now)
        return await close_problem(pool, problem_id, now=now)


def _item_id(url: str, topic: Topic) -> str:
    return f"item:{hashlib.sha1(url.encode()).hexdigest()[:20]}:{topic.slug}"


async def round_items(
    pool: asyncpg.Pool, problem_id: str, *, limit: int = _DIGEST_ITEMS
) -> list[dict[str, Any]]:
    """The round's articles, newest first."""
    rows = await pool.fetch(
        "SELECT payload, occurred_at FROM problem_events "
        "WHERE problem_id = $1::uuid AND kind = 'occurrence' AND payload->>'item' = 'true' "
        "ORDER BY occurred_at DESC, id DESC LIMIT $2",
        problem_id,
        limit,
    )
    return [{**dict(r["payload"] or {}), "occurred_at": r["occurred_at"]} for r in rows]


async def _item_count(pool: asyncpg.Pool, problem_id: str) -> int:
    return int(
        await pool.fetchval(
            "SELECT count(*) FROM problem_events "
            "WHERE problem_id = $1::uuid AND kind = 'occurrence' AND payload->>'item' = 'true'",
            problem_id,
        )
        or 0
    )


async def _check_attention(
    pool: asyncpg.Pool, problem_id: str, topic: Topic, now: datetime
) -> bool:
    """Mark the round as worth a task once it holds ``topic.threshold`` items.
    True only on the call that crosses the threshold."""
    count = await _item_count(pool, problem_id)
    if count < topic.threshold:
        return False
    tag = await pool.execute(
        "UPDATE problems SET metadata = metadata || $2::jsonb "
        "WHERE id = $1::uuid AND closed_at IS NULL "
        "  AND COALESCE(metadata->>'attention', '') <> 'true'",
        problem_id,
        {"attention": True, "attention_at": now.isoformat(), "attention_items": count},
    )
    return str(tag).endswith(" 1")


async def attach_items(
    pool: asyncpg.Pool,
    items: list[dict[str, Any]],
    *,
    origin: str,
    now: datetime | None = None,
    project: bool = True,
) -> dict[str, Any]:
    """Attach every item that names a tracked topic to that topic's round.

    An item is ``{title, url|link, summary|snippet, significance?}``. Returns
    counts: ``matched`` item-topic pairs, ``attached`` (new ones — an article
    seen before, by any path, is not attached twice), ``topics`` touched and
    ``tasks`` raised by a round crossing its threshold.
    """
    now = now or datetime.now(UTC)
    topics = await load_topics(pool)
    if not topics or not items:
        return {"topics": 0, "matched": 0, "attached": 0, "tasks": 0}
    rounds: dict[str, str] = {}
    by_slug = {t.slug: t for t in topics}
    matched = attached = 0
    for item in items:
        url = str(item.get("url") or item.get("link") or "").strip()
        title = str(item.get("title") or "").strip()
        if not url or not title:
            continue
        summary = str(item.get("summary") or item.get("snippet") or "")
        for topic in match_topics(topics, f"{title}\n{summary}"):
            matched += 1
            if topic.slug not in rounds:
                pid = await ensure_round(pool, topic, now=now)
                if not pid:
                    continue
                rounds[topic.slug] = pid
            result = await ingest_event(
                pool,
                _event(
                    topic,
                    external_id=_item_id(url, topic),
                    kind="occurrence",
                    title=title[:500],
                    payload={
                        "item": True,
                        "topic": topic.name,
                        "url": url,
                        "title": title[:300],
                        "summary": summary[:300],
                        "origin": origin,
                        **(
                            {"significance": item["significance"]}
                            if item.get("significance") is not None
                            else {}
                        ),
                    },
                    occurred_at=now,
                ),
                now=now,
            )
            if result.action != "duplicate":
                attached += 1
    tasks = 0
    for topic_slug, pid in rounds.items():
        if await _check_attention(pool, pid, by_slug[topic_slug], now):
            tasks += 1
            if project:
                from aegis.services.hub_project import project as project_problem

                try:
                    await project_problem(pool, pid, now=now)
                except Exception as exc:  # noqa: BLE001 — the hub sweep retries projection
                    logger.warning("research_topic_project_failed", problem_id=pid, error=str(exc)[:200])
    logger.info(
        "research_topic_items_attached",
        origin=origin,
        matched=matched,
        attached=attached,
        topics=len(rounds),
        tasks=tasks,
    )
    return {"topics": len(rounds), "matched": matched, "attached": attached, "tasks": tasks}
