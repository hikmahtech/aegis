"""The watchdog seam of the problem hub: findings in, fresh problems out.

A watchdog (flow health, stuck social posts, the comms probe, service drift)
re-evaluates the world every tick and produces the *current* set of things
that are wrong. Before the hub each one kept its own ledger to answer "is
this new?" — three copies of the same resolved-aware `audit_log` query and a
mute table with four key namespaces. :func:`reconcile_findings` is that
question asked once:

* every finding is an occurrence on its problem (new or attached);
* every live problem of the watchdog's classes that is **not** among the
  findings any more is resolved;
* a finding is *fresh* — worth a card — only when the hub says so
  (`IngestResult.investigate`: a new or returning problem, not suppressed,
  not muted).

The watchdog keeps its own notification (its Slack card) and sends it for the
fresh findings; the projector gives the problem its task; the sweep and the
digest read the same rows.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import asyncpg
import structlog

from aegis.services import hub_project
from aegis.services.hub import Event, ingest_event, slug

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

    Each finding is ``{"klass", "subject", "title", "severity"?, "payload"?}``.
    ``classes`` names every class this watchdog can produce, so a problem of
    one of them with no current finding is what "recovered" means.

    Returns ``fresh`` (the findings that earned a card, each with its
    ``problem_id``), the counts of ``attached`` / ``muted`` / ``suppressed``
    findings, and ``resolved`` (the subjects that recovered, with their ids).
    """
    now = now or datetime.now(UTC)
    fresh: list[dict[str, Any]] = []
    attached = muted = suppressed = 0
    seen: set[tuple[str, str]] = set()
    to_project: list[str] = []

    for f in findings:
        klass, subject = slug(str(f.get("klass") or "")), slug(str(f.get("subject") or ""))
        if not klass or not subject:
            logger.warning("hub_watch_finding_skipped", source=source, finding=str(f)[:200])
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
        "SELECT id::text AS id, class, subject FROM problems "
        "WHERE closed_at IS NULL AND status NOT IN ('resolved', 'closed') "
        "  AND subject_kind = $1 AND class = ANY($2::text[])",
        slug(subject_kind) or "service",
        [slug(c) for c in classes],
    )
    for row in rows:
        if (row["class"], row["subject"]) in seen:
            continue
        result = await ingest_event(
            pool,
            Event(
                source=source,
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
            resolved.append({"subject": row["subject"], "klass": row["class"], "problem_id": row["id"]})
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
        resolved=len(resolved),
    )
    return {
        "fresh": fresh,
        "attached": attached,
        "muted": muted,
        "suppressed": suppressed,
        "resolved": resolved,
    }


def mute_hint(problem_ids: list[str]) -> str:
    """The one-liner a card carries so an operator can silence a problem
    until the admin Problems page exists."""
    ids = ", ".join(f"'{p}'" for p in problem_ids if p)
    return (
        "Silence: UPDATE problems SET muted_until = now() + interval '2 days' "
        f"WHERE id IN ({ids});"
        if ids
        else ""
    )
