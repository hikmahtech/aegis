"""Submit Todoist commands, staging retryable failures in the outbox.

Extracted so the two writers that close a task AEGIS did not create —
`CaptureActivities.link_email_to_task` (an email said so) and
`JiraActivities.close_resolved_jira_tasks` (the issue tracker said so) — share
one behaviour instead of two copies that drift.

The distinction that matters and is easy to get wrong: a **transient** failure
(5xx, timeout, rate limit) goes to `todoist_outbox` for `drain_outbox` to retry,
while a **permanent** rejection (ITEM_NOT_FOUND, INVALID_ARGUMENT) must NOT —
replaying it just fails again and burns five attempts per call.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def submit_or_queue(pool: Any, connector: Any, cmds: list[dict], context: str) -> dict:
    """Send `cmds`; queue them for retry if the failure looks transient.

    Returns ``{"ok", "queued", "error"}``. Never raises — every caller is a
    best-effort side path that must not fail the run that triggered it.
    """
    from aegis.connectors.todoist import TodoistConnector

    if not cmds:
        return {"ok": True, "queued": False, "error": None}

    try:
        result = await connector.commands(cmds)
        status = TodoistConnector.check_sync_status(result, [c["uuid"] for c in cmds])
    except Exception as exc:  # network/transport — treat as retryable
        await _queue(pool, cmds)
        logger.warning("todoist_write_transport_failed ctx=%s err=%s", context, str(exc)[:200])
        return {"ok": False, "queued": True, "error": str(exc)[:200]}

    if status["ok"]:
        return {"ok": True, "queued": False, "error": None}

    err = status["envelope_error"] or str(status["rejected"])[:200]
    if status["retryable"] or status["rejected_retryable"]:
        await _queue(pool, cmds)
        logger.warning("todoist_write_outbox_staged ctx=%s err=%s", context, err)
        return {"ok": False, "queued": True, "error": err}

    logger.warning("todoist_write_rejected ctx=%s err=%s", context, err)
    return {"ok": False, "queued": False, "error": err}


async def _queue(pool: Any, cmds: list[dict]) -> None:
    """Stage commands in `todoist_outbox`, keyed on temp_id (uuid when absent).

    `item_complete` and `item_update` carry no temp_id — only `item_add` does —
    so the command's own uuid is the fallback key, which is equally unique and
    equally idempotent under the ON CONFLICT.
    """
    if pool is None:
        return
    try:
        async with pool.acquire() as conn:
            for cmd in cmds:
                await conn.execute(
                    "INSERT INTO todoist_outbox (temp_id, command, status) "
                    "VALUES ($1, $2, 'pending') ON CONFLICT (temp_id) DO NOTHING",
                    cmd.get("temp_id") or cmd["uuid"],
                    cmd,
                )
    except Exception as exc:
        logger.warning("todoist_write_queue_failed err=%s", str(exc)[:200])
