"""Groups: when the same failure keeps happening to different entities.

The hub keys a problem on ``{class}:{subject_kind}:{subject}``, which is right
for one broken service and wrong for six posts wedged in one queue. Those six
were six problems and six Todoist tasks, each asking you to look at the same
stalled Postiz worker.

A **group** is one problem that stands for all of them. It carries
``group_key = '{class}:{subject_kind}'``, and :func:`aegis.services.hub.ingest_event`
gives it every later occurrence of that class whose own key has no problem —
so the seventh stuck post joins the group rather than opening a seventh task.

Three rules keep the grouping honest:

* **One class, one subject kind.** A group never spans classes. The hub does
  not decide that two different failures are the same failure; it decides that
  one failure has more than one victim. The ``manual`` class — a hand-written
  task's problem — is never groupable at all: three ``@code`` tasks are three
  pieces of work, and folding them would move one task's sessions and PR links
  onto another.
* **A judge, not a rule.** The count alone is a candidate, not a verdict —
  three services crash-looping for three unrelated reasons must stay three
  problems. :func:`candidates` finds clusters; the caller (the sweep's LLM
  judge, or a person on the Problems page) says yes or no, and
  :func:`record_verdict` remembers a "no" so the same cluster is not re-priced
  every five minutes.
* **Nothing is destroyed.** Folding is `hub.merge_problems`: the members'
  events, links and sessions move onto the group, their tasks are retired with
  a note pointing at it, and the links read both ways. An operator can see
  every member the group swallowed and unpick it by hand.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
import structlog

from aegis.services.hub import (
    GROUP_SUBJECT,
    LIVE_STATUSES,
    group_correlation_key,
    group_key,
    merge_problems,
    normalize_severity,
    slug,
)

logger = structlog.get_logger()

# Two of a kind is a coincidence. Three is a pattern worth asking about — and
# it is only ever a question: the judge still has to agree.
MIN_MEMBERS = 3
# How far back a member may have been last seen and still count towards a
# cluster. Longer than the daily watchdogs so a once-a-day finding still
# accumulates; short enough that last month's incident does not.
WINDOW_HOURS = 72.0
# How long a "these are not the same thing" verdict stands, unless the cluster
# grows. Re-asking every sweep would spend a model call every five minutes to
# be told the same thing.
VERDICT_TTL_HOURS = 24.0
_VERDICT_KEY = "hub_group_verdicts"
_SEVERITY_ORDER = ("info", "warning", "error", "critical")


def _utcnow() -> datetime:
    return datetime.now(UTC)


def worst(severities: list[str]) -> str:
    """The most serious of several severities: a group is as bad as its worst
    member, never as bad as its average."""
    ranked = [normalize_severity(s) for s in severities if s]
    if not ranked:
        return "warning"
    return max(ranked, key=_SEVERITY_ORDER.index)


async def candidates(
    pool: asyncpg.Pool,
    *,
    min_members: int = MIN_MEMBERS,
    hours: float = WINDOW_HOURS,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Clusters worth asking about: live, ungrouped problems that share a class
    and a subject kind across at least ``min_members`` different subjects.

    Each cluster carries its members so a judge can read the titles rather
    than the count. Never decides anything.
    """
    now = now or _utcnow()
    since = now - timedelta(hours=max(float(hours), 0.0))
    floor = max(2, int(min_members))
    rows = await pool.fetch(
        "SELECT class, subject_kind, count(*) AS members FROM problems "
        "WHERE closed_at IS NULL AND status = ANY($1::text[]) AND group_key IS NULL "
        "  AND class <> '' AND class <> 'manual' AND subject <> '' AND subject_kind <> '' "
        "  AND last_seen_at >= $2 "
        "GROUP BY 1, 2 HAVING count(*) >= $3 ORDER BY count(*) DESC",
        sorted(LIVE_STATUSES),
        since,
        floor,
    )
    out: list[dict[str, Any]] = []
    for row in rows:
        members = [
            dict(m)
            for m in await pool.fetch(
                "SELECT id::text AS id, subject, title, severity, status, occurrences, "
                "       first_seen_at, last_seen_at, todoist_task_id FROM problems "
                "WHERE closed_at IS NULL AND status = ANY($1::text[]) AND group_key IS NULL "
                "  AND class = $2 AND subject_kind = $3 AND subject <> '' "
                "ORDER BY first_seen_at",
                sorted(LIVE_STATUSES),
                row["class"],
                row["subject_kind"],
            )
        ]
        if len(members) < floor:
            continue
        out.append(
            {
                "class": row["class"],
                "subject_kind": row["subject_kind"],
                "group_key": group_key(row["class"], row["subject_kind"]),
                "members": members,
                "member_count": len(members),
            }
        )
    return out


async def recent_verdict(
    pool: asyncpg.Pool, gkey: str, member_count: int, *, now: datetime | None = None
) -> dict[str, Any] | None:
    """A standing verdict for this cluster, or None when it is worth asking
    again.

    A verdict expires with :data:`VERDICT_TTL_HOURS`, and a cluster that has
    GROWN since the verdict is a new question — three unrelated crash-loops
    are three problems, but the tenth one is evidence of something shared.
    """
    now = now or _utcnow()
    row = await pool.fetchval("SELECT value FROM settings WHERE key = $1", _VERDICT_KEY)
    entry = row.get(gkey) if isinstance(row, dict) else None
    if not isinstance(entry, dict):
        return None
    try:
        decided_at = datetime.fromisoformat(str(entry.get("decided_at")))
    except (TypeError, ValueError):
        return None
    if decided_at.tzinfo is None:
        decided_at = decided_at.replace(tzinfo=UTC)
    if now - decided_at > timedelta(hours=VERDICT_TTL_HOURS):
        return None
    if int(entry.get("member_count") or 0) < int(member_count):
        return None
    return entry


async def record_verdict(
    pool: asyncpg.Pool,
    gkey: str,
    *,
    grouped: bool,
    member_count: int,
    reason: str = "",
    now: datetime | None = None,
) -> None:
    """Remember what the judge said about a cluster, so the next sweep does not
    pay to ask again."""
    now = now or _utcnow()
    entry = {
        "decided_at": now.isoformat(),
        "grouped": bool(grouped),
        "member_count": int(member_count),
        "reason": (reason or "")[:300],
    }
    await pool.execute(
        "INSERT INTO settings (key, value) VALUES ($1, jsonb_build_object($2::text, $3::jsonb)) "
        "ON CONFLICT (key) DO UPDATE SET value = settings.value || excluded.value, "
        "updated_at = now()",
        _VERDICT_KEY,
        gkey,
        entry,
    )


async def upgrade(
    pool: asyncpg.Pool,
    *,
    klass: str,
    subject_kind: str,
    title: str,
    member_ids: list[str],
    by: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Fold ``member_ids`` into one group problem for this class and kind.

    The oldest member becomes the group — it keeps its history, its task and
    its sessions, and takes the group's key, subject and title. The rest are
    merged into it (`hub.merge_problems`), which moves their events and links
    and closes them with a link back. Their tasks come back in ``merged`` for
    the caller to retire; the hub never touches Todoist.

    Re-runnable: called again with more members it folds them into the group
    that already exists. Raises ValueError when there is nothing to group or
    the class and kind cannot form a group key.
    """
    now = now or _utcnow()
    gkey = group_key(klass, subject_kind)
    if not gkey:
        raise ValueError("a group needs both a class and a subject kind")
    title = (title or "").strip()[:500]
    ids = [m for m in dict.fromkeys(member_ids) if m]
    if not ids:
        raise ValueError("no members to group")

    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", gkey)
        group = await conn.fetchrow(
            "SELECT id::text AS id, title, severity, todoist_task_id FROM problems "
            "WHERE group_key = $1 AND closed_at IS NULL FOR UPDATE",
            gkey,
        )
        rows = [
            dict(r)
            for r in await conn.fetch(
                "SELECT id::text AS id, subject, severity, todoist_task_id, first_seen_at "
                "FROM problems WHERE id = ANY($1::uuid[]) AND closed_at IS NULL "
                "  AND group_key IS NULL ORDER BY first_seen_at",
                ids,
            )
        ]
        if group is None:
            if len(rows) < 2:
                raise ValueError("a group needs at least two live members")
            keeper, rows = rows[0], rows[1:]
            keeper_id = keeper["id"]
            severity = worst([keeper["severity"], *[r["severity"] for r in rows]])
            await conn.execute(
                "UPDATE problems SET group_key = $2, correlation_key = $3, subject = $4, "
                "title = $5, severity = $6, "
                "metadata = jsonb_set(metadata, '{grouped_from}', to_jsonb($7::text)) "
                "WHERE id = $1::uuid",
                keeper_id,
                gkey,
                group_correlation_key(gkey),
                GROUP_SUBJECT,
                title or f"{klass} on several {subject_kind}s",
                severity,
                keeper["subject"],
            )
            folded = [keeper["subject"]]
        else:
            keeper_id = group["id"]
            severity = worst([group["severity"], *[r["severity"] for r in rows]])
            await conn.execute(
                "UPDATE problems SET severity = $2, title = COALESCE(NULLIF($3, ''), title) "
                "WHERE id = $1::uuid",
                keeper_id,
                severity,
                title,
            )
            folded = []

    merged: list[dict[str, Any]] = []
    for row in rows:
        result = await merge_problems(pool, keeper_id, row["id"], by=by, now=now)
        merged.append(
            {
                "problem_id": row["id"],
                "subject": row["subject"],
                "task_id": result.get("merged_task_id") or "",
            }
        )
        folded.append(row["subject"])

    # One readable event for the whole upgrade. `merge_problems` writes a row
    # per member, which says how it happened; this says why.
    await pool.execute(
        "INSERT INTO problem_events (problem_id, source, external_id, kind, severity, "
        "payload, occurred_at) VALUES ($1::uuid, 'hub', $2, 'state_change', $3, $4, $5) "
        "ON CONFLICT (source, external_id) DO NOTHING",
        keeper_id,
        f"group:{gkey}:{now.isoformat()}",
        severity,
        {
            "action": "grouped",
            "group_key": gkey,
            "by": by[:100],
            "title": title,
            "members": folded[:50],
            "member_count": len(folded),
        },
        now,
    )
    logger.info(
        "hub_problems_grouped",
        group_key=gkey,
        problem_id=keeper_id,
        merged=len(merged),
        by=by,
    )
    return {
        "problem_id": keeper_id,
        "group_key": gkey,
        "class": slug(klass),
        "subject_kind": slug(subject_kind),
        "title": title,
        "severity": severity,
        "merged": merged,
        "subjects": folded,
    }
