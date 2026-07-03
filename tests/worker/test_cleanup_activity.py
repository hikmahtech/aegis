"""Smoke tests for CleanupActivities.prune_old_records."""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import uuid4

import respx
from aegis_worker.activities.cleanup import (
    _ALLOWED_TABLES,
    _TIMESTAMP_COLUMNS,
    CleanupActivities,
    _parse_rowcount,
)
from aegis_worker.flows.cleanup import _DEFAULT_RETENTIONS
from httpx import Response
from temporalio.testing import ActivityEnvironment


def test_parse_rowcount_normal():
    assert _parse_rowcount("DELETE 42") == 42
    assert _parse_rowcount("DELETE 0") == 0


def test_parse_rowcount_garbage():
    assert _parse_rowcount("") == 0
    assert _parse_rowcount("BOGUS") == 0


async def test_prune_no_pool_returns_empty():
    activities = CleanupActivities(db_pool=None)
    env = ActivityEnvironment()
    result = await env.run(activities.prune_old_records, {"retentions": {"audit_log": 90}})
    assert result == {}


async def test_prune_skips_unknown_table():
    """Tables outside the allowlist must be silently skipped."""
    pool = AsyncMock()
    pool.fetchval = AsyncMock(return_value=True)
    pool.execute = AsyncMock(return_value="DELETE 0")
    activities = CleanupActivities(db_pool=pool)
    env = ActivityEnvironment()
    result = await env.run(
        activities.prune_old_records,
        {"retentions": {"some_random_table": 30, "audit_log": 90}},
    )
    # only audit_log was processed
    assert "some_random_table" not in result
    assert result["audit_log"] == 0


async def test_prune_loops_until_zero_and_uses_correct_sql():
    """Activity should batch-delete until rowcount == 0 and use ctid-based SQL."""
    pool = AsyncMock()
    pool.fetchval = AsyncMock(return_value=True)
    # First batch deletes 10000, second deletes 5, third deletes 0 → stop.
    pool.execute = AsyncMock(side_effect=["DELETE 10000", "DELETE 5", "DELETE 0"])
    activities = CleanupActivities(db_pool=pool)
    env = ActivityEnvironment()
    result = await env.run(activities.prune_old_records, {"retentions": {"audit_log": 90}})

    assert result == {"audit_log": 10005}
    # Three DELETE batches issued
    assert pool.execute.await_count == 3
    sql_used = pool.execute.await_args_list[0].args[0]
    assert "DELETE FROM audit_log" in sql_used
    assert "ctid IN" in sql_used
    assert "created_at" in sql_used
    assert "LIMIT 10000" in sql_used
    # Days passed as parameter
    days_arg = pool.execute.await_args_list[0].args[1]
    assert days_arg == 90


async def test_prune_skips_missing_table():
    """If to_regclass returns NULL, table is recorded as -1 and not deleted."""
    pool = AsyncMock()
    # governance_decision_log doesn't exist in this deployment
    pool.fetchval = AsyncMock(return_value=False)
    pool.execute = AsyncMock(return_value="DELETE 0")
    activities = CleanupActivities(db_pool=pool)
    env = ActivityEnvironment()
    result = await env.run(
        activities.prune_old_records,
        {"retentions": {"governance_decision_log": 90}},
    )
    assert result["governance_decision_log"] == -1
    pool.execute.assert_not_called()


async def test_prune_ignores_non_positive_days():
    pool = AsyncMock()
    pool.fetchval = AsyncMock(return_value=True)
    pool.execute = AsyncMock(return_value="DELETE 0")
    activities = CleanupActivities(db_pool=pool)
    env = ActivityEnvironment()
    result = await env.run(
        activities.prune_old_records,
        {"retentions": {"audit_log": 0, "llm_calls": -5}},
    )
    assert result == {}
    pool.execute.assert_not_called()


# ── archive_orphan_interactions (janitor for orphaned InteractionFlow rows) ──


async def test_archive_orphans_no_pool_returns_zero():
    activities = CleanupActivities(db_pool=None)
    env = ActivityEnvironment()
    result = await env.run(activities.archive_orphan_interactions, 7)
    assert result == {"archived": 0, "threshold_days": 7}


async def test_archive_orphans_happy_path():
    """Sweeps pending interactions older than threshold via UPDATE."""
    pool = AsyncMock()
    pool.execute = AsyncMock(return_value="UPDATE 17")
    activities = CleanupActivities(db_pool=pool)
    env = ActivityEnvironment()
    result = await env.run(activities.archive_orphan_interactions, 7)
    assert result == {"archived": 17, "threshold_days": 7}
    # Verify the SQL — UPDATE on interactions with pending+resolved_at IS NULL+age filter
    sql_used = pool.execute.await_args.args[0]
    assert "UPDATE interactions" in sql_used
    assert "status = 'archived'" in sql_used
    assert "resolved_at IS NULL" in sql_used
    assert "make_interval(days => $1)" in sql_used
    days_used = pool.execute.await_args.args[1]
    assert days_used == 7


async def test_archive_orphans_zero_threshold_returns_zero():
    """Threshold of 0 (or negative) disables the sweep — caller's opt-out."""
    pool = AsyncMock()
    pool.execute = AsyncMock(return_value="UPDATE 0")
    activities = CleanupActivities(db_pool=pool)
    env = ActivityEnvironment()
    result = await env.run(activities.archive_orphan_interactions, 0)
    assert result == {"archived": 0, "threshold_days": 0}
    pool.execute.assert_not_called()


async def test_archive_orphans_bad_input_falls_back_to_default():
    pool = AsyncMock()
    pool.execute = AsyncMock(return_value="UPDATE 3")
    activities = CleanupActivities(db_pool=pool)
    env = ActivityEnvironment()
    # Pass a string that can't be parsed — activity should still execute
    # using the default 7-day threshold rather than raising.
    result = await env.run(activities.archive_orphan_interactions, "not-an-int")
    assert result["archived"] == 3
    assert result["threshold_days"] == 7


# --- cleanup_old_dispatches ---

COMMS_URL = "http://comms-service:8081"


@respx.mock
async def test_cleanup_dispatches_delivery_ref_row_posts_ref_and_db_deletes():
    """A row with metadata.delivery_ref sends that exact ref dict to the comms
    delete endpoint; on ok=True the row is removed from chat_history."""
    import json

    row_id = uuid4()
    delivery_ref = {"adapter": "slack", "channel": "C100123", "ts": "4242.0"}
    pool = AsyncMock()
    pool.fetch = AsyncMock(
        return_value=[
            {"id": row_id, "metadata": {"delivery_ref": delivery_ref, "kind": "interaction_card"}},
        ]
    )
    pool.execute = AsyncMock(return_value="DELETE 1")

    delete_route = respx.post(f"{COMMS_URL}/api/comms/delete").mock(
        return_value=Response(200, json={"ok": True})
    )

    activities = CleanupActivities(db_pool=pool, comms_url=COMMS_URL, api_key="test-key")
    env = ActivityEnvironment()
    result = await env.run(activities.cleanup_old_dispatches, 30)

    assert delete_route.call_count == 1
    sent_ref = delete_route.calls[0].request.content
    assert json.loads(sent_ref)["delivery_ref"] == delivery_ref
    assert delete_route.calls[0].request.headers.get("x-api-key") == "test-key"
    assert result["candidates"] == 1
    assert result["deleted_from_channel"] == 1
    assert result["deleted_from_db"] == 1
    pool.execute.assert_awaited_once()


@respx.mock
async def test_cleanup_dispatches_channel_error_leaves_row_in_db():
    """When the comms service returns ok=False, the DB row is NOT deleted —
    preserving the audit trail for a retry on the next run."""
    row_id = uuid4()
    pool = AsyncMock()
    pool.fetch = AsyncMock(
        return_value=[
            {"id": row_id, "metadata": {"delivery_ref": {"adapter": "slack", "channel": "C1", "ts": "2.0"}}},
        ]
    )
    pool.execute = AsyncMock(return_value="DELETE 0")

    respx.post(f"{COMMS_URL}/api/comms/delete").mock(
        return_value=Response(200, json={"ok": False})
    )

    activities = CleanupActivities(db_pool=pool, comms_url=COMMS_URL)
    env = ActivityEnvironment()
    result = await env.run(activities.cleanup_old_dispatches, 30)

    assert result["channel_errors"] == 1
    assert result["deleted_from_db"] == 0
    pool.execute.assert_not_called()


async def test_cleanup_dispatches_aborts_when_comms_url_missing():
    """Without comms_url we cannot call the delete endpoint, so abort and
    return status='aborted_no_comms_url' without touching the DB."""
    pool = AsyncMock()
    pool.fetch = AsyncMock(return_value=[{"id": uuid4(), "metadata": {"delivery_ref": {"adapter": "slack"}}}])
    pool.execute = AsyncMock(return_value="DELETE 0")

    activities = CleanupActivities(db_pool=pool, comms_url="")
    env = ActivityEnvironment()
    result = await env.run(activities.cleanup_old_dispatches, 30)

    assert result["status"] == "aborted_no_comms_url"
    assert result["deleted_from_db"] == 0
    pool.fetch.assert_not_called()
    pool.execute.assert_not_called()


async def test_cleanup_dispatches_query_filters_on_delivery_ref():
    """The fetch query matches rows with metadata.delivery_ref — not
    role=dispatch."""
    pool = AsyncMock()
    pool.fetch = AsyncMock(return_value=[])
    pool.execute = AsyncMock(return_value="DELETE 0")

    activities = CleanupActivities(db_pool=pool, comms_url=COMMS_URL)
    env = ActivityEnvironment()
    await env.run(activities.cleanup_old_dispatches, 30)

    sql = pool.fetch.await_args.args[0]
    assert "metadata ? 'delivery_ref'" in sql
    assert "role = 'dispatch'" not in sql


# --- expanded retention coverage (Bundle H) ---


_EXPANDED_TABLES = [
    "workflow_runs",
    "ingest_idempotency",
    "gtd_clarify_log",
    "alert_dedup_index",
    "alert_mutes",
    "pending_prs",
    "pandoras_actor.homelab_drift",
    "pandoras_actor.backup_health",
    "pandoras_actor.schedule_health",
    "pandoras_actor.cert_expiry",
]


def test_expanded_tables_present_in_defaults_and_allowlist():
    """Every table added in Bundle H must be in BOTH _DEFAULT_RETENTIONS
    (so CleanupFlow actually targets it) and _ALLOWED_TABLES (so
    prune_old_records doesn't silently skip it)."""
    for table in _EXPANDED_TABLES:
        assert table in _DEFAULT_RETENTIONS, f"missing from _DEFAULT_RETENTIONS: {table}"
        assert table in _ALLOWED_TABLES, f"missing from _ALLOWED_TABLES: {table}"
        assert table in _TIMESTAMP_COLUMNS, f"missing from _TIMESTAMP_COLUMNS: {table}"


def test_expanded_tables_use_correct_timestamp_columns():
    """The timestamp column chosen per table must match the migration:
    workflow_runs → started_at, alert_dedup_index → last_seen_at,
    alert_mutes → muted_until (dead-by), pandoras_actor.* → detected_at
    (homelab_drift) or checked_at (everything else)."""
    expected = {
        "workflow_runs": "started_at",
        "ingest_idempotency": "created_at",
        "gtd_clarify_log": "created_at",
        "alert_dedup_index": "last_seen_at",
        "alert_mutes": "muted_until",
        "pending_prs": "created_at",
        "pandoras_actor.homelab_drift": "detected_at",
        "pandoras_actor.backup_health": "checked_at",
        "pandoras_actor.schedule_health": "checked_at",
        "pandoras_actor.cert_expiry": "checked_at",
    }
    for table, ts_col in expected.items():
        assert _TIMESTAMP_COLUMNS[table] == ts_col, (
            f"{table}: expected ts col {ts_col!r}, got {_TIMESTAMP_COLUMNS[table]!r}"
        )


async def test_prune_uses_started_at_for_workflow_runs():
    """workflow_runs has no `created_at` — pruning must target `started_at`."""
    pool = AsyncMock()
    pool.fetchval = AsyncMock(return_value=True)
    pool.execute = AsyncMock(return_value="DELETE 0")
    activities = CleanupActivities(db_pool=pool)
    env = ActivityEnvironment()
    await env.run(activities.prune_old_records, {"retentions": {"workflow_runs": 90}})
    sql = pool.execute.await_args.args[0]
    assert "DELETE FROM workflow_runs" in sql
    assert "started_at <" in sql
    assert "created_at" not in sql


async def test_prune_uses_muted_until_for_alert_mutes():
    """alert_mutes prunes by `muted_until` so expired mutes drop as soon
    as they're past, not after the full retention window from creation."""
    pool = AsyncMock()
    pool.fetchval = AsyncMock(return_value=True)
    pool.execute = AsyncMock(return_value="DELETE 0")
    activities = CleanupActivities(db_pool=pool)
    env = ActivityEnvironment()
    await env.run(activities.prune_old_records, {"retentions": {"alert_mutes": 30}})
    sql = pool.execute.await_args.args[0]
    assert "DELETE FROM alert_mutes" in sql
    assert "muted_until <" in sql


async def test_prune_handles_schema_qualified_table_names():
    """`pandoras_actor.homelab_drift` is schema-qualified. Postgres accepts
    `DELETE FROM schema.table` natively; the query builder must pass the
    qualified name through verbatim AND use the right timestamp column
    (`detected_at`, not `created_at`)."""
    pool = AsyncMock()
    pool.fetchval = AsyncMock(return_value=True)
    pool.execute = AsyncMock(return_value="DELETE 0")
    activities = CleanupActivities(db_pool=pool)
    env = ActivityEnvironment()
    await env.run(
        activities.prune_old_records,
        {"retentions": {"pandoras_actor.homelab_drift": 60}},
    )
    sql = pool.execute.await_args.args[0]
    assert "DELETE FROM pandoras_actor.homelab_drift" in sql
    assert "detected_at <" in sql
    # to_regclass must also receive the qualified name.
    regclass_arg = pool.fetchval.await_args.args[1]
    assert regclass_arg == "pandoras_actor.homelab_drift"


async def test_prune_uses_checked_at_for_pandoras_actor_backup_health():
    """backup_health / schedule_health / cert_expiry all use `checked_at`
    per migration 003."""
    pool = AsyncMock()
    pool.fetchval = AsyncMock(return_value=True)
    pool.execute = AsyncMock(return_value="DELETE 0")
    activities = CleanupActivities(db_pool=pool)
    env = ActivityEnvironment()
    await env.run(
        activities.prune_old_records,
        {"retentions": {"pandoras_actor.backup_health": 60}},
    )
    sql = pool.execute.await_args.args[0]
    assert "DELETE FROM pandoras_actor.backup_health" in sql
    assert "checked_at <" in sql


# --- preview_retention ---


async def test_preview_retention_returns_count_without_deleting():
    """preview_retention SELECTs COUNT(*) — it must NOT issue a DELETE."""
    pool = AsyncMock()
    pool.fetchval = AsyncMock(side_effect=[True, 42])  # to_regclass, then count
    activities = CleanupActivities(db_pool=pool)
    result = await activities.preview_retention("audit_log", 90)
    assert result == 42
    # Both fetchval calls happened; no execute (no DELETE) issued.
    assert pool.fetchval.await_count == 2
    pool.execute.assert_not_called()
    count_sql = pool.fetchval.await_args_list[1].args[0]
    assert "SELECT COUNT(*) FROM audit_log" in count_sql
    assert "created_at <" in count_sql


async def test_preview_retention_unknown_table_returns_minus_one():
    pool = AsyncMock()
    activities = CleanupActivities(db_pool=pool)
    result = await activities.preview_retention("not_a_real_table", 30)
    assert result == -1


async def test_preview_retention_no_pool_returns_minus_one():
    activities = CleanupActivities(db_pool=None)
    result = await activities.preview_retention("audit_log", 90)
    assert result == -1


async def test_preview_retention_handles_schema_qualified_table():
    pool = AsyncMock()
    pool.fetchval = AsyncMock(side_effect=[True, 7])
    activities = CleanupActivities(db_pool=pool)
    result = await activities.preview_retention(
        "pandoras_actor.homelab_drift", 60
    )
    assert result == 7
    count_sql = pool.fetchval.await_args_list[1].args[0]
    assert "FROM pandoras_actor.homelab_drift" in count_sql
    assert "detected_at <" in count_sql
