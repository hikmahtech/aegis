"""Due-today/overdue Todoist signal from the local Postgres mirror (no API call)."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# How far back an overdue task still counts as "someone is working on this".
# Unbounded overdue latches the guard open forever: a task due 2026-07-25
# ("Rotate + move plaintext secrets … homelab-gitops") suppressed EVERY
# homelab-gitops investigation indefinitely, because overdue never expires.
# ponytail: fixed 7-day window; make it a settings key if it ever needs tuning.
_OVERDUE_GRACE_DAYS = 7


async def due_today_or_overdue(db_pool, today_iso: str) -> list[dict]:
    if not db_pool:
        return []
    try:
        today_date = datetime.fromisoformat(today_iso).date()
        rows = await db_pool.fetch(
            "SELECT content, labels, due_date FROM todoist_tasks "
            "WHERE NOT is_completed AND due_date IS NOT NULL "
            "AND due_date <= $1 AND due_date >= $2",
            today_date,
            today_date - timedelta(days=_OVERDUE_GRACE_DAYS),
        )
    except Exception as exc:  # noqa: BLE001 — best-effort signal
        logger.warning("activework_todoist_error err=%s", str(exc)[:200])
        return []
    return [
        {"content": r["content"], "labels": list(r["labels"] or []), "due_date": r["due_date"]}
        for r in rows
    ]
