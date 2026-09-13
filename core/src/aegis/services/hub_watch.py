"""The watchdog seam of the problem hub: findings in, fresh problems out.

A watchdog (flow health, stuck social posts, the comms probe, service drift)
re-evaluates the world every tick and produces the *current* set of things
that are wrong. Before the hub each one kept its own ledger to answer "is
this new?" — three copies of the same resolved-aware `audit_log` query and a
mute table with four key namespaces. :func:`reconcile_findings` is that
question asked once:

* every finding is an occurrence on its problem (new or attached);
* a finding marked ``"record": False`` is still *found* — its problem stays
  live — but adds no occurrence this tick, for a watchdog that looks hourly
  but should only speak when something changes (a dead feed posted "N more
  occurrences" on its task 24 times a day). It opens nothing on its own;
* every live problem of the watchdog's classes that is **not** among the
  findings any more is resolved — except a GROUP problem, which stands for a
  whole class rather than one subject and so recovers only when the watchdog
  stops finding ANY member of that class;
* a finding is *fresh* — worth a card — only when the hub says so
  (`IngestResult.investigate`: a new or returning problem, not suppressed,
  not muted).

The watchdog keeps its own notification (its Slack card) and sends it for the
fresh findings; the projector gives the problem its task; the sweep and the
digest read the same rows.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
import structlog

from aegis.services import hub_project
from aegis.services.hub import LIVE_STATUSES, Event, ingest_event, slug

logger = structlog.get_logger()


async def reconcile_findings(
    pool: asyncpg.Pool,
    *,
    source: str,
    subject_kind: str,
    classes: list[str],
    findings: list[dict[str, Any]],
    now: datetime | None = None,
    project: bool = True,
) -> dict[str, Any]:
    """Record ``findings`` and resolve what is no longer found.

    Each finding is ``{"klass", "subject", "title", "severity"?, "payload"?,
    "record"?}``. ``classes`` names every class this watchdog can produce, so a
    problem of one of them with no current finding is what "recovered" means.
    ``"record": False`` keeps a finding's problem live without adding an
    occurrence.

    Returns ``fresh`` (the findings that earned a card, each with its
    ``problem_id``), the counts of ``attached`` / ``muted`` / ``suppressed`` /
    ``ongoing`` findings, and ``resolved`` (the subjects that recovered, with
    their ids).
    """
    now = now or datetime.now(UTC)
    fresh: list[dict[str, Any]] = []
    attached = muted = suppressed = ongoing = 0
    seen: set[tuple[str, str]] = set()
    to_project: list[str] = []

    for f in findings:
        klass, subject = slug(str(f.get("klass") or "")), slug(str(f.get("subject") or ""))
        if not klass or not subject:
            logger.warning("hub_watch_finding_skipped", source=source, finding=str(f)[:200])
            continue
        if f.get("record") is False:
            seen.add((klass, subject))
            ongoing += 1
            continue
        result = await ingest_event(
            pool,
            Event(
                source=source,
                external_id=f"{source}:{klass}:{subject}@{now.isoformat()}",
                kind="occurrence",
                title=str(f.get("title") or f"{klass}: {subject}")[:500],
                subject=subject,
                subject_kind=subject_kind,
                klass=klass,
                severity=str(f.get("severity") or "warning"),
                payload=dict(f.get("payload") or {}),
                occurred_at=now,
            ),
            now=now,
        )
        seen.add((klass, subject))
        if result.muted:
            muted += 1
        elif result.suppressed:
            suppressed += 1
        elif result.investigate:
            fresh.append({**f, "problem_id": result.problem_id})
            to_project.append(result.problem_id)
        else:
            attached += 1

    resolved: list[dict[str, Any]] = []
    rows = await pool.fetch(
        "SELECT id::text AS id, class, subject, title, group_key FROM problems "
        "WHERE closed_at IS NULL AND status NOT IN ('resolved', 'closed') "
        "  AND subject_kind = $1 AND class = ANY($2::text[])",
        slug(subject_kind) or "service",
        [slug(c) for c in classes],
    )
    still_failing = {klass for klass, _ in seen}
    for row in rows:
        if row["group_key"]:
            # A group's subject is `*` and can never be among the findings. It
            # recovers when its class does: one post publishing does not mean
            # the queue drained.
            if row["class"] in still_failing:
                continue
        elif (row["class"], row["subject"]) in seen:
            continue
        result = await ingest_event(
            pool,
            Event(
                source=source,
                # Name the problem outright rather than re-deriving it from a
                # subject: a group's subject does not correlate back to it.
                problem_id=row["id"],
                external_id=f"{source}:{row['class']}:{row['subject']}@{now.isoformat()}@resolved",
                kind="resolved",
                title=f"{row['class']} recovered: {row['subject']}",
                subject=row["subject"],
                subject_kind=subject_kind,
                klass=row["class"],
                occurred_at=now,
            ),
            now=now,
        )
        if result.action == "resolved":
            resolved.append(
                {
                    # `label` is what a recovery card should print: a group's
                    # subject is `*`, which names nothing to a reader.
                    "subject": row["subject"],
                    "label": row["title"] if row["group_key"] else row["subject"],
                    "klass": row["class"],
                    "problem_id": row["id"],
                    "group": bool(row["group_key"]),
                }
            )
            to_project.append(row["id"])

    if project:
        for pid in to_project:
            try:
                await hub_project.project(pool, pid, now=now)
            except Exception as exc:  # noqa: BLE001 — the sweep retries projection
                logger.warning("hub_watch_project_failed", problem_id=pid, error=str(exc)[:200])

    logger.info(
        "hub_watch_reconciled",
        source=source,
        fresh=len(fresh),
        attached=attached,
        muted=muted,
        suppressed=suppressed,
        ongoing=ongoing,
        resolved=len(resolved),
    )
    return {
        "fresh": fresh,
        "attached": attached,
        "muted": muted,
        "suppressed": suppressed,
        "ongoing": ongoing,
        "resolved": resolved,
    }


def mute_hint(problem_ids: list[str]) -> str:
    """The line a card carries on how to silence what it reports. It points at
    the Problems page's Mute button, which goes through `hub.mute_problem` and
    so records the mute on the timeline; the raw SQL it used to carry did not."""
    ids = [p for p in problem_ids if p]
    if not ids:
        return ""
    return (
        "Silence: admin Problems page → open the problem → Mute 24h "
        f"(problem{'s' if len(ids) > 1 else ''} {', '.join(ids)})."
    )


# Alertmanager holds its firing alerts in memory only, so a restart makes it
# forget every one it was holding and their `resolved` webhooks are never sent.
# Until this existed, that stranded the problem AND its Todoist task for good:
# the alertmanager lane was the only producer with no reconciliation, so a
# single lost webhook was permanent. Seen live on 2026-09-13 (#551) — a repaired
# overlay fault sat `waiting_human` for 15 hours with zero resolution events
# while alertmanager reported no active alerts at all.
#
# The same hole swallows a resolve sent during an ingress outage, which is
# exactly the outage the ingress canary exists to catch (#492).
_ALERTMANAGER_SOURCE = "alertmanager"


async def reconcile_alertmanager(
    pool: asyncpg.Pool,
    *,
    active_fingerprints: set[str],
    now: datetime | None = None,
    grace_minutes: float = 10.0,
) -> dict[str, Any]:
    """Resolve every live alertmanager problem whose alert it no longer lists.

    ``active_fingerprints`` is what alertmanager currently holds. The caller
    reads it and MUST NOT call this at all when that read failed or when
    alertmanager has only just started — an empty set from a monitoring stack
    that cannot be reached, or that has forgotten everything, would otherwise
    read as "the whole estate recovered". The resolve says alertmanager stopped
    listing the alert, not that the alert cleared, because only the first of
    those is evidenced here.

    Two carve-outs. A problem younger than ``grace_minutes`` is left alone, so
    one raised seconds ago is never resolved before alertmanager has grouped it.
    A GROUP problem is left alone too: its subject is ``*``, it stands for a
    whole class rather than one alert, and no single fingerprint speaks for it —
    the same reason :func:`reconcile_findings` treats groups separately.
    """
    now = now or datetime.now(UTC)
    rows = await pool.fetch(
        "SELECT p.id::text AS id, p.class, p.subject, p.subject_kind, p.title, f.external_id "
        "FROM problems p JOIN LATERAL ("
        "  SELECT e.external_id, e.source FROM problem_events e"
        "  WHERE e.problem_id = p.id AND e.kind = 'occurrence' ORDER BY e.id LIMIT 1"
        ") f ON TRUE "
        "WHERE p.closed_at IS NULL AND p.status = ANY($1::text[]) "
        "  AND p.group_key IS NULL "
        "  AND f.source = $2 "
        "  AND p.first_seen_at < $3 "
        "ORDER BY p.first_seen_at",
        sorted(LIVE_STATUSES),
        _ALERTMANAGER_SOURCE,
        now - timedelta(minutes=max(0.0, grace_minutes)),
    )
    resolved: list[dict[str, Any]] = []
    for row in rows:
        # `external_id` is `<fingerprint>@<startsAt>`; a synthesised fingerprint
        # (`alertmanager:<alertname>:<instance>`) carries colons but never an @.
        fingerprint = str(row["external_id"] or "").split("@", 1)[0]
        if not fingerprint or fingerprint in active_fingerprints:
            continue
        result = await ingest_event(
            pool,
            Event(
                source=_ALERTMANAGER_SOURCE,
                problem_id=row["id"],
                external_id=f"alertmanager-reconcile:{row['id']}@{now.isoformat()}",
                kind="resolved",
                title=f"{row['class']} is no longer firing: {row['subject']}",
                subject=row["subject"],
                subject_kind=row["subject_kind"],
                klass=row["class"],
                payload={
                    "reason": (
                        "alertmanager no longer lists this alert. Reconciled by the hub "
                        "sweep because a resolution webhook never arrived — alertmanager "
                        "keeps its alerts in memory, so a restart loses them (#551)."
                    ),
                    "fingerprint": fingerprint,
                },
                occurred_at=now,
            ),
            now=now,
        )
        if result.action == "resolved":
            resolved.append(
                {
                    "problem_id": row["id"],
                    "klass": row["class"],
                    "subject": row["subject"],
                    "fingerprint": fingerprint,
                }
            )
    if resolved:
        logger.info(
            "hub_alertmanager_reconciled",
            resolved=len(resolved),
            checked=len(rows),
            problems=[r["problem_id"] for r in resolved],
        )
    return {"checked": len(rows), "resolved": resolved}
