"""Close the `#alert` Todoist task an alert spawned, once that alert resolves.

Issue #279. Three individually-correct mechanisms dead-ended here:

  * `AgentTaskFlow` gets ONE pass per task, then `park_task` stamps `@waiting`
    — deliberately terminal, or the cooldown loops forever.
  * `InfraHeartbeatFlow` alerts only on TRANSITIONS, so a service that stays
    down (or later recovers) never re-triggers anything.
  * Recovery IS detected and audited, but only ever re-armed `check_dedup`.

Net effect: an alert task's last lifecycle event was the dedup index appending
"another occurrence" comments to something nobody would look at again. Six
tasks outlived their incidents by up to 9 days (`koyracloud_redis` resolved
2026-08-02 23:38, task open until 2026-08-11).

No new plumbing is needed to fix it: `AlertInvestigationFlow` captures the task
with `external_id = f"alert-{fingerprint}"`, `todoist_capture_idempotency` maps
`(source_tag, external_id) -> todoist_task_ref`, and both resolve paths already
hold that same fingerprint. This module is the one shared implementation they
both call — worker (`HomelabActivities.record_heartbeat_resolved`) and core
(`routes/webhooks.py`) — so the two packages cannot drift apart.
"""

from __future__ import annotations

import asyncpg
import structlog

from aegis.connectors.todoist import TodoistConnector

logger = structlog.get_logger()


async def close_task_for_resolved_alert(
    pool: asyncpg.Pool | None, fingerprint: str
) -> str | None:
    """Complete the alert task for `fingerprint`. Returns the task id if a
    close was queued, else None. Never raises — a resolve path must keep
    working even when Todoist bookkeeping does not.

    Only closes a task AEGIS itself opened (`source_tag = '#alert'`) and that
    the user has not claimed (`assignee_label` still an agent, not `@me`) —
    once it is theirs, closing it out from under them would be worse than
    leaving it stale.
    """
    if pool is None or not fingerprint:
        return None
    try:
        row = await pool.fetchrow(
            "SELECT t.id FROM todoist_capture_idempotency ci "
            "JOIN todoist_tasks t ON t.id = ci.todoist_task_ref "
            "WHERE ci.source_tag = '#alert' AND ci.external_id = 'alert-' || $1 "
            "  AND NOT t.is_completed "
            "  AND t.source_tag = '#alert' "
            "  AND t.assignee_label IS DISTINCT FROM '@me'",
            fingerprint,
        )
        if row is None:
            return None
        task_id = row["id"]
        # Same re-queue-on-terminal semantics as agent_task._queue_command: a
        # deterministic temp_id makes a flapping alert idempotent, but a row
        # already drained to 'committed'/'failed' must be re-armed or a later
        # close silently no-ops. An undrained 'pending' row is left alone.
        await pool.execute(
            "INSERT INTO todoist_outbox (temp_id, command, status) "
            "VALUES ($1, $2, 'pending') "
            "ON CONFLICT (temp_id) DO UPDATE "
            "SET command = EXCLUDED.command, status = 'pending', attempt_count = 0 "
            "WHERE todoist_outbox.status <> 'pending'",
            f"alert-resolved-close-{task_id}",
            TodoistConnector.build_item_complete_command(task_id),
        )
        # Optimistic local close so the task leaves every projection-backed
        # view immediately rather than at the next 5-min sync.
        await pool.execute(
            "UPDATE todoist_tasks SET is_completed = true, updated_at = now() WHERE id = $1",
            task_id,
        )
        logger.info("alert_task_closed_on_resolve", fingerprint=fingerprint, task_id=task_id)
        return task_id
    except Exception as exc:  # noqa: BLE001 — bookkeeping must never break resolve
        logger.warning(
            "alert_task_close_failed", fingerprint=fingerprint, error=str(exc)[:200]
        )
        return None
