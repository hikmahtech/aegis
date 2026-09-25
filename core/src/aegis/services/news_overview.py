"""What Raphael's news lane did, for the admin News page, so none of it lives
only in Slack or a table.

- **Stories**: every area story the brief showed (`area_stories`), newest
  first, with the owner's verdict.
- **Watchers**: every scheduled source that files items onto a tracked topic —
  any `activities` row whose config names a ``topic`` (the world watch, the
  rising repos, the tender watch). For each: on or off, its last run, the
  topic and the area that holds it, what it filed lately, and the problems
  that would keep its items from ever reaching the brief.

Read-only. A new watcher appears here by giving its row a ``topic``.
"""

from __future__ import annotations

import json
from typing import Any

from aegis.services import research_topics
from aegis.services.hub import TOPIC_CLASS, slug


async def stories(pool: Any, *, area: str = "", limit: int = 100) -> list[dict[str, Any]]:
    rows = await pool.fetch(
        "SELECT area, title, url, why, shown_at, verdict, verdict_at, ts IS NOT NULL AS posted "
        "FROM area_stories WHERE ($1 = '' OR area = $1) ORDER BY shown_at DESC, id DESC LIMIT $2",
        area,
        max(1, min(limit, 500)),
    )
    return [dict(r) for r in rows]


def _json(v: Any) -> dict:
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            return {}
    return v if isinstance(v, dict) else {}


def problems_for(watcher: dict[str, Any]) -> list[str]:
    """Why this watcher's items would not reach the user. Pure."""
    out: list[str] = []
    if not watcher["active"]:
        out.append("switched off")
    if not watcher["topic_tracked"]:
        out.append(f"its topic {watcher['topic']!r} is not tracked, so it files nothing")
    elif not watcher["area"]:
        out.append(f"its topic {watcher['topic']!r} is in no area, so nothing reaches the brief")
    run = watcher.get("last_run") or {}
    summary = run.get("summary") or {}
    if run and (run.get("status") == "failed" or run.get("error")):
        out.append(f"last run failed: {str(run.get('error') or 'failed')[:160]}")
    elif summary.get("error"):
        out.append(f"last run: {str(summary['error'])[:160]}")
    elif summary.get("skipped"):
        out.append(f"last run skipped: {summary['skipped']}")
    if summary.get("failed_topics"):
        out.append(f"searches failed for: {', '.join(summary['failed_topics'])}")
    return out


async def watchers(pool: Any) -> list[dict[str, Any]]:
    rows = await pool.fetch(
        """
        SELECT a.slug, a.workflow_type, a.agent_id, a.active, a.schedule_cron,
               a.config->>'topic' AS topic,
               r.status, r.started_at, r.completed_at, r.result_summary, r.error
        FROM activities a
        LEFT JOIN LATERAL (
            SELECT status, started_at, completed_at, result_summary, error
            FROM workflow_runs w WHERE w.workflow_type = a.workflow_type
            ORDER BY started_at DESC LIMIT 1
        ) r ON true
        WHERE jsonb_typeof(a.config->'topic') = 'string'
        ORDER BY a.slug
        """
    )
    tracked = {t.slug for t in await research_topics.load_topics(pool)}
    area_of = {slug(t): a.name for a in await research_topics.load_areas(pool) for t in a.topics}
    out: list[dict[str, Any]] = []
    for r in rows:
        topic = r["topic"] or ""
        items = await pool.fetch(
            "SELECT e.payload->>'title' AS title, e.payload->>'url' AS url, e.occurred_at "
            "FROM problem_events e JOIN problems p ON p.id = e.problem_id "
            "WHERE p.class = $1 AND p.subject = $2 AND e.payload->>'item' = 'true' "
            "ORDER BY e.occurred_at DESC, e.id DESC LIMIT 5",
            TOPIC_CLASS,
            slug(topic),
        )
        week = await pool.fetchval(
            "SELECT count(*) FROM problem_events e JOIN problems p ON p.id = e.problem_id "
            "WHERE p.class = $1 AND p.subject = $2 AND e.payload->>'item' = 'true' "
            "AND e.occurred_at > now() - interval '7 days'",
            TOPIC_CLASS,
            slug(topic),
        )
        watcher = {
            "slug": r["slug"],
            "workflow_type": r["workflow_type"],
            "agent_id": r["agent_id"],
            "active": r["active"],
            "schedule": r["schedule_cron"],
            "topic": topic,
            "topic_tracked": slug(topic) in tracked,
            "area": area_of.get(slug(topic)),
            "items_7d": int(week or 0),
            "recent_items": [dict(i) for i in items],
            "last_run": (
                {
                    "status": r["status"],
                    "started_at": r["started_at"],
                    "completed_at": r["completed_at"],
                    "summary": _json(r["result_summary"]),
                    "error": r["error"],
                }
                if r["started_at"]
                else None
            ),
        }
        watcher["problems"] = problems_for(watcher)
        out.append(watcher)
    return out
