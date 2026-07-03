"""Cleanup activities — prune old rows from observability/audit tables."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import httpx
import structlog
from temporalio import activity

logger = structlog.get_logger()

# Per-table timestamp column. Most tables use created_at; ops/observation
# tables under pandoras_actor.* and a handful of others store their "row
# became stale" timestamp under a different column. Schema-qualified keys
# (e.g. "pandoras_actor.homelab_drift") are passed through to the DELETE
# statement verbatim — see `prune_old_records` and `preview_retention`.
_TIMESTAMP_COLUMNS: dict[str, str] = {
    "audit_log": "created_at",
    "llm_calls": "created_at",
    "connector_calls": "created_at",
    "chat_tool_calls": "created_at",
    "chat_history": "created_at",
    "governance_decision_log": "created_at",
    "ingest_idempotency": "created_at",
    "workflow_runs": "started_at",
    "gtd_clarify_log": "created_at",
    "alert_dedup_index": "last_seen_at",
    # alert_mutes: `muted_until` is the row's "dead by" timestamp. Once the
    # mute is past, the row has no remaining purpose, so we prune by it
    # rather than by created_at (which would keep dead mutes alive for the
    # full retention window past their actual expiry).
    "alert_mutes": "muted_until",
    "pending_prs": "created_at",
    # pandoras_actor.* — per migration 003 the timestamp columns are
    # `detected_at` (homelab_drift) and `checked_at` (everything else).
    "pandoras_actor.homelab_drift": "detected_at",
    "pandoras_actor.backup_health": "checked_at",
    "pandoras_actor.schedule_health": "checked_at",
    "pandoras_actor.cert_expiry": "checked_at",
}

# Tables we are willing to prune. Anything outside this allowlist is ignored —
# prevents a misconfigured settings row from targeting the wrong table.
_ALLOWED_TABLES = frozenset(_TIMESTAMP_COLUMNS.keys())

# Batch size for each DELETE statement. Small enough to keep locks short,
# large enough that we don't spend all our time round-tripping.
_BATCH_SIZE = 10_000


def _parse_rowcount(status: str) -> int:
    """Parse asyncpg execute() status string like 'DELETE 42' → 42."""
    if not status:
        return 0
    parts = status.split()
    try:
        return int(parts[-1])
    except (ValueError, IndexError):
        return 0


@dataclass
class CleanupActivities:
    """Activities for pruning old rows from unbounded ops tables."""

    db_pool: Any = None
    comms_url: str = ""
    api_key: str = ""

    @activity.defn
    async def archive_orphan_interactions(self, threshold_days: int = 7) -> dict:
        """Archive `interactions` rows still in `pending` past the threshold,
        treating them as orphaned by a vanished parent workflow.

        How they get orphaned: an `InteractionFlow` calls `insert_interaction`
        to create the row, then enters `workflow.wait_condition(timeout=…)`.
        If the workflow vanishes before timeout fires (worker killed
        mid-wait, Temporal retention purged the workflow, parent terminated
        with TERMINATE close-policy), `apply_interaction_timeout` never
        runs and the DB row stays `pending` forever. Pre-PR-220 deployments
        left 178 such rows behind (cleaned up via one-shot SQL 2026-05-21);
        this janitor prevents recurrence by sweeping anything older than
        the threshold on each CleanupFlow tick.

        Returns `{archived: int, threshold_days: int}`.
        """
        if not self.db_pool:
            logger.warning("archive_orphan_interactions_no_db_pool")
            return {"archived": 0, "threshold_days": threshold_days}
        try:
            days_int = int(threshold_days)
        except (TypeError, ValueError):
            days_int = 7
        if days_int <= 0:
            logger.warning(
                "archive_orphan_interactions_bad_threshold",
                threshold_days=threshold_days,
            )
            return {"archived": 0, "threshold_days": threshold_days}

        status = await self.db_pool.execute(
            """
            UPDATE interactions
            SET status = 'archived',
                resolved_at = now(),
                response = COALESCE(response, '{}'::jsonb)
                           || '{"auto_archived": "janitor"}'::jsonb
            WHERE status = 'pending'
              AND resolved_at IS NULL
              AND created_at < now() - make_interval(days => $1)
            """,
            days_int,
        )
        archived = _parse_rowcount(status)
        if archived:
            logger.info(
                "archive_orphan_interactions_done",
                archived=archived,
                threshold_days=days_int,
            )
        return {"archived": archived, "threshold_days": days_int}

    @activity.defn
    async def cleanup_old_dispatches(self, days: int = 30) -> dict:
        """Prune chat_history rows older than `days` that carry a delivery_ref,
        calling the comms service DELETE endpoint for each so the message is
        removed from the channel too.

        Guard: if comms_url is not set, abort (mirrors the old no-token abort shape).
        """
        if not self.db_pool:
            logger.warning("cleanup_dispatches_no_db_pool")
            return {"candidates": 0, "deleted_from_db": 0}
        if not self.comms_url:
            logger.error("cleanup_dispatches_no_comms_url_abort")
            return {
                "candidates": 0,
                "deleted_from_channel": 0,
                "deleted_from_db": 0,
                "skipped_no_ref": 0,
                "channel_errors": 0,
                "status": "aborted_no_comms_url",
            }
        try:
            days_int = max(1, int(days))
        except (TypeError, ValueError):
            days_int = 30

        rows = await self.db_pool.fetch(
            """
            SELECT id, metadata
            FROM chat_history
            WHERE created_at < now() - make_interval(days => $1)
              AND metadata ? 'delivery_ref'
            ORDER BY created_at ASC
            LIMIT 5000
            """,
            days_int,
        )
        candidates = len(rows)
        deleted_channel = 0
        skipped_no_ref = 0
        channel_errors = 0
        to_delete_ids: list[Any] = []

        headers: dict[str, str] = {}
        if self.api_key:
            headers["X-API-Key"] = self.api_key

        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0)) as client:
            for row in rows:
                meta = row["metadata"] or {}
                ref_dict = meta.get("delivery_ref")
                if ref_dict is None:
                    skipped_no_ref += 1
                    to_delete_ids.append(row["id"])
                    continue
                try:
                    resp = await client.post(
                        f"{self.comms_url}/api/comms/delete",
                        json={"delivery_ref": ref_dict},
                        headers=headers,
                    )
                    if resp.status_code == 200 and resp.json().get("ok"):
                        deleted_channel += 1
                        to_delete_ids.append(row["id"])
                    else:
                        channel_errors += 1
                        logger.info(
                            "cleanup_channel_delete_skipped",
                            status=resp.status_code,
                            body=resp.text[:160],
                        )
                except Exception as exc:
                    channel_errors += 1
                    logger.warning("cleanup_channel_delete_error", error=str(exc)[:160])
                await asyncio.sleep(0.05)
                activity.heartbeat(f"dispatches:{deleted_channel}/{candidates}")

        deleted_db = 0
        if to_delete_ids:
            status = await self.db_pool.execute(
                "DELETE FROM chat_history WHERE id = ANY($1::uuid[])",
                to_delete_ids,
            )
            deleted_db = _parse_rowcount(status)

        summary = {
            "candidates": candidates,
            "deleted_from_channel": deleted_channel,
            "deleted_from_db": deleted_db,
            "skipped_no_ref": skipped_no_ref,
            "channel_errors": channel_errors,
            "retention_days": days_int,
        }
        logger.info("cleanup_dispatches_done", **summary)
        return summary

    @activity.defn
    async def prune_old_records(self, config: dict) -> dict:
        """Delete rows older than the configured retention window, per table.

        Config shape::

            {"retentions": {"audit_log": 90, "llm_calls": 90, ...}}

        Returns a dict of ``{table: rows_deleted}``. Tables missing from the
        schema (e.g. ``governance_decision_log`` is reserved for future use)
        are skipped silently and recorded as ``-1`` so the caller can tell
        skip-apart from zero-deleted.
        """
        if not self.db_pool:
            logger.warning("cleanup_no_db_pool")
            return {}

        retentions: dict[str, int] = (config or {}).get("retentions", {}) or {}
        summary: dict[str, int] = {}

        for table, days in retentions.items():
            if table not in _ALLOWED_TABLES:
                logger.warning("cleanup_table_not_allowed", table=table)
                continue
            try:
                days_int = int(days)
            except (TypeError, ValueError):
                logger.warning("cleanup_bad_days", table=table, days=days)
                continue
            if days_int <= 0:
                logger.warning("cleanup_non_positive_days", table=table, days=days_int)
                continue

            ts_col = _TIMESTAMP_COLUMNS[table]

            # Skip tables that aren't present in this deployment.
            exists = await self.db_pool.fetchval("SELECT to_regclass($1) IS NOT NULL", table)
            if not exists:
                logger.info("cleanup_table_missing", table=table)
                summary[table] = -1
                continue

            total_deleted = 0
            sql = (
                f"DELETE FROM {table} WHERE ctid IN ("
                f"  SELECT ctid FROM {table} "
                f"  WHERE {ts_col} < now() - make_interval(days => $1) "
                f"  LIMIT {_BATCH_SIZE}"
                f")"
            )

            while True:
                activity.heartbeat(f"{table}:{total_deleted}")
                status = await self.db_pool.execute(sql, days_int)
                deleted = _parse_rowcount(status)
                total_deleted += deleted
                logger.info(
                    "cleanup_batch",
                    table=table,
                    batch_deleted=deleted,
                    total_deleted=total_deleted,
                )
                if deleted == 0:
                    break

            summary[table] = total_deleted
            logger.info(
                "cleanup_table_done",
                table=table,
                retention_days=days_int,
                rows_deleted=total_deleted,
            )

        logger.info("cleanup_summary", summary=summary)
        return summary

    async def preview_retention(self, table: str, retention_days: int) -> int:
        """Return the count of rows that WOULD be deleted from `table` with a
        retention window of `retention_days` days, without deleting anything.

        Intended for ops to dry-run a retention change before adding the
        table to `_DEFAULT_RETENTIONS` or to size a backlog before changing
        the window. Honors the same allowlist + table-existence checks as
        `prune_old_records`.

        Returns:
            int: row count, or -1 if the table is unknown / not in the
            allowlist / missing from this deployment / pool is unset / the
            retention window is non-positive.
        """
        if not self.db_pool:
            logger.warning("preview_retention_no_db_pool", table=table)
            return -1
        if table not in _ALLOWED_TABLES:
            logger.warning("preview_retention_table_not_allowed", table=table)
            return -1
        try:
            days_int = int(retention_days)
        except (TypeError, ValueError):
            logger.warning(
                "preview_retention_bad_days", table=table, days=retention_days
            )
            return -1
        if days_int <= 0:
            return -1

        ts_col = _TIMESTAMP_COLUMNS[table]
        exists = await self.db_pool.fetchval(
            "SELECT to_regclass($1) IS NOT NULL", table
        )
        if not exists:
            return -1

        # f-string interpolation is safe here: `table` is gated by
        # _ALLOWED_TABLES and `ts_col` comes from the same in-module dict.
        count = await self.db_pool.fetchval(
            f"SELECT COUNT(*) FROM {table} "
            f"WHERE {ts_col} < now() - make_interval(days => $1)",
            days_int,
        )
        return int(count or 0)
