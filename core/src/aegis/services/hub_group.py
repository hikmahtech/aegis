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
  onto another. Whole sources opt out the same way, through
  :data:`NON_GROUPABLE_SOURCES`.
* **A judge, not a rule.** The count alone is a candidate, not a verdict —
  three services crash-looping for three unrelated reasons must stay three
  problems. :func:`candidates` finds clusters; the sweep's LLM judge says yes
  or no, and :func:`record_verdict` remembers the answer for
  :data:`VERDICT_TTL_HOURS` so the same cluster is not re-priced every five
  minutes. The Problems page has no grouping control: a person folds
  duplicates one at a time with a merge, or steers the judge through the
  verdict cache (`docs/infrastructure.md`).
* **Nothing is destroyed.** Folding is `hub.merge_problems`: the members'
  events and links move onto the group, their tasks are retired with a note
  pointing at it, and the links read both ways. An operator can see
  every member the group swallowed and unpick it by hand.

One fold needs no judge: a **stray**. `ingest_event` absorbs a new subject
into its class's group, but a subject whose own problem still exists — it
recovered while the group formed, then came back and reopened — keeps that
problem, and a single stray is never three of a kind again. :func:`candidates`
folds such strays into their group before it looks for clusters
(:func:`absorb_strays`). The group already stands for the class, so this is
the decision `ingest_event` would have made, not a new one.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
import structlog

from aegis.connectors.todoist import TodoistConnector
from aegis.services import hub_project
from aegis.services.hub import (
    GROUP_SUBJECT,
    LIVE_STATUSES,
    TASK_SUBJECT_KIND,
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
# Sources whose problems are never offered as a cluster, for the same reason
# `class = 'manual'` is not: each of their problems is individually actionable
# and folding would destroy that.
#
# `money`: a reconciliation finding is keyed on the account, so
# `unmatched_rows:instrument:axis-cc-1313` IS "214 unmatched rows on
# axis-cc-1313" and the 215th row joins it. The grouping is already done by the
# correlation key. `candidates` clusters on (class, subject_kind) alone and has
# no idea whose problems they are, so once three accounts carry `unmatched_rows`
# the sweep would offer them to the billed judge and a "yes" would fold every
# account into one `unmatched_rows:instrument:*` problem — one task for the
# whole backlog, which is the opposite of what the lane needs, and reached
# without the money lane doing anything at all.
#
# `problems` has no source column: the source lives on `problem_events`, so the
# rule is a NOT EXISTS rather than a migration. Naming a source rather than a
# list of class names keeps this from drifting every time the money lane adds
# a class.
#
# This changes nothing about how a group that DOES exist recovers: a group's
# subject is `*` and can never appear among a watchdog's findings, so
# `hub_watch.reconcile_findings` checks a group's membership by CLASS, not by
# subject. That rule is unchanged and still correct.
#
# `research` (#513): two tracked topics are two interests by construction, and
# a `#research` task's question is one person's request. Folding either would
# merge unrelated news, or one task's answer onto another.
NON_GROUPABLE_SOURCES = frozenset({"money", "research"})
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
# The `NON_GROUPABLE_SOURCES` rule as SQL. Both queries in `candidates` pass the
# source list as `$4`, so the fragment is shared rather than written twice.
_NOT_FROM_NON_GROUPABLE = (
    "NOT EXISTS (SELECT 1 FROM problem_events e "
    "WHERE e.problem_id = problems.id AND e.source = ANY($4::text[]))"
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def worst(severities: list[str]) -> str:
    """The most serious of several severities: a group is as bad as its worst
    member, never as bad as its average."""
    ranked = [normalize_severity(s) for s in severities if s]
    if not ranked:
        return "warning"
    return max(ranked, key=_SEVERITY_ORDER.index)


async def absorb_strays(
    pool: asyncpg.Pool,
    *,
    hours: float = WINDOW_HOURS,
    by: str = "hub-sweep",
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Fold every live, ungrouped problem into the live group of its class and
    subject kind, and retire its task (#474).

    Deterministic, with no judge: the group already stands for the class, and
    `ingest_event` gives it every NEW subject without asking. A stray is a
    subject that missed that only because its own problem was live, or came
    back, when the group formed. Leaving it is two open tasks for one
    condition.

    The fences are the ones the judge's :func:`candidates` keeps, and one more
    because nothing reviews this fold:

    * one class and one subject kind, never across either;
    * never class ``manual`` or kind ``task`` (hand-written tasks and reports
      about one task — `hub.group_key` refuses both);
    * never a problem from a :data:`NON_GROUPABLE_SOURCES` source (money);
    * only a stray seen inside the same window the judge looks at;
    * only into a group that is itself live and visible — never ``resolved``
      or ``suppressed``. The merge keeps the group's status, so folding a
      live stray into either would hide it.

    Returns one entry per group that took strays.
    """
    now = now or _utcnow()
    since = now - timedelta(hours=max(float(hours), 0.0))
    rows = await pool.fetch(
        "SELECT g.id::text AS group_id, g.class, g.subject_kind, g.title, "
        "       COALESCE(g.todoist_task_id, '') AS group_task, "
        "       array_agg(p.id::text ORDER BY p.first_seen_at) AS member_ids "
        "FROM problems g JOIN problems p "
        "  ON p.class = g.class AND p.subject_kind = g.subject_kind AND p.id <> g.id "
        "WHERE g.group_key IS NOT NULL AND g.closed_at IS NULL AND g.status = ANY($1::text[]) "
        "  AND p.group_key IS NULL AND p.closed_at IS NULL AND p.status = ANY($2::text[]) "
        "  AND p.class <> 'manual' AND p.subject_kind <> $3 AND p.subject <> '' "
        "  AND p.last_seen_at >= $4 "
        "  AND NOT EXISTS (SELECT 1 FROM problem_events e "
        "                  WHERE e.problem_id IN (p.id, g.id) AND e.source = ANY($5::text[])) "
        "GROUP BY 1, 2, 3, 4, 5 ORDER BY 1",
        sorted(LIVE_STATUSES - {"suppressed"}),
        sorted(LIVE_STATUSES),
        TASK_SUBJECT_KIND,
        since,
        sorted(NON_GROUPABLE_SOURCES),
    )
    out: list[dict[str, Any]] = []
    for row in rows:
        try:
            result = await upgrade(
                pool,
                klass=row["class"],
                subject_kind=row["subject_kind"],
                # Empty: the group keeps the title somebody already gave it.
                title="",
                member_ids=list(row["member_ids"]),
                by=by,
                reason="still live when the group formed, so it kept its own problem",
                now=now,
            )
        except ValueError as exc:  # a class the hub will not group: leave it
            logger.warning(
                "hub_group_absorb_refused", group_id=row["group_id"], error=str(exc)[:200]
            )
            continue
        # A stray that shares the group's own task (one task linked to two
        # problems, #472) must not close the task the group still needs.
        tasks = [
            m["task_id"] for m in result["merged"] if m["task_id"] not in ("", row["group_task"])
        ]
        retired = sum([await _retire(pool, t, row["title"]) for t in tasks])
        out.append({**result, "tasks_retired": retired})
        logger.info(
            "hub_group_strays_absorbed",
            problem_id=result["problem_id"],
            group_key=result["group_key"],
            absorbed=[m["problem_id"] for m in result["merged"]],
            tasks_retired=retired,
        )
    return out


async def _retire(pool: asyncpg.Pool, task_id: str, title: str) -> bool:
    """Retire a folded stray's task: a note saying where the work went, then
    the completion. True when the completion queued.

    Both go through the outbox rather than a live Todoist call, because this
    runs inside the sweep's candidate step, which has a 15-second budget and
    is meant to be a query. The note carries the hub's footer, so clarify's
    loop guard reads it as the hub talking, not a user. A task still known
    only by its outbox temp id cannot take a comment yet, so it is left for
    a person to close.
    """
    if task_id.startswith("item-"):
        logger.warning("hub_group_absorb_task_pending_outbox", task_id=task_id)
        return False
    note = (
        f"Folded into one problem: {title or 'the group for this class'}. Work it there. "
        "This one was still open when the group formed, so it had kept its own task."
    )
    await hub_project._queue(
        pool,
        f"problem-fold-{task_id}",
        TodoistConnector.build_note_add_command(task_id, note + hub_project.FOOTER),
    )
    return await hub_project._complete_task(pool, task_id)


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
    than the count. It decides nothing about them — but first it folds the
    strays of groups that already exist (:func:`absorb_strays`), a decision
    made when the group formed. That is also why a stray is never counted
    towards a new cluster of its own class.
    """
    now = now or _utcnow()
    await absorb_strays(pool, hours=hours, now=now)
    since = now - timedelta(hours=max(float(hours), 0.0))
    floor = max(2, int(min_members))
    rows = await pool.fetch(
        "SELECT class, subject_kind, count(*) AS members FROM problems "
        "WHERE closed_at IS NULL AND status = ANY($1::text[]) AND group_key IS NULL "
        "  AND class <> '' AND class <> 'manual' AND subject <> '' AND subject_kind <> '' "
        "  AND last_seen_at >= $2 "
        f"  AND {_NOT_FROM_NON_GROUPABLE} "
        "GROUP BY 1, 2 HAVING count(*) >= $3 ORDER BY count(*) DESC",
        sorted(LIVE_STATUSES),
        since,
        floor,
        sorted(NON_GROUPABLE_SOURCES),
    )
    out: list[dict[str, Any]] = []
    for row in rows:
        gkey = group_key(row["class"], row["subject_kind"])
        if not gkey:
            # A kind the hub never groups (`task`): no judge should be asked.
            continue
        members = [
            dict(m)
            for m in await pool.fetch(
                "SELECT id::text AS id, subject, title, severity, status, occurrences, "
                "       first_seen_at, last_seen_at, todoist_task_id FROM problems "
                "WHERE closed_at IS NULL AND status = ANY($1::text[]) AND group_key IS NULL "
                "  AND class = $2 AND subject_kind = $3 AND subject <> '' "
                f"  AND {_NOT_FROM_NON_GROUPABLE} "
                "ORDER BY first_seen_at",
                sorted(LIVE_STATUSES),
                row["class"],
                row["subject_kind"],
                sorted(NON_GROUPABLE_SOURCES),
            )
        ]
        if len(members) < floor:
            continue
        out.append(
            {
                "class": row["class"],
                "subject_kind": row["subject_kind"],
                "group_key": gkey,
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
    reason: str = "",
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
    the class and kind cannot form a group key. ``reason``, when given, is
    written on the `grouped` event beside who did it.
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
            # The keeper's own subject is overwritten with `*`; the `grouped`
            # event below keeps it, first in its `members`.
            await conn.execute(
                "UPDATE problems SET group_key = $2, correlation_key = $3, subject = $4, "
                "title = $5, severity = $6 WHERE id = $1::uuid",
                keeper_id,
                gkey,
                group_correlation_key(gkey),
                GROUP_SUBJECT,
                title or f"{klass} on several {subject_kind}s",
                severity,
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
            **({"reason": reason[:300]} if reason else {}),
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
