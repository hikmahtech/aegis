"""Tests for AlertActivities signature-dedup helpers + build_alert_signature.

Covers the SQL-level dedup gate added in migration 017:

- find_open_task_for_signature: respects is_completed + missing-task fall-through
- record_signature_new_task: insert + upsert (resets stats)
- record_signature_recurrence: bumps counter; no-op on missing row
- build_alert_signature: Sentry-only with required fields
"""

from __future__ import annotations

import uuid

import pytest
from aegis_worker.activities.alerts import AlertActivities, build_alert_signature

# ---------------------------------------------------------------------------
# build_alert_signature — pure function, no DB
# ---------------------------------------------------------------------------


def test_build_signature_sentry_with_metadata_type_and_service():
    alert = {
        "source": "sentry",
        "service": "acme-data",
        "raw_payload": {"metadata": {"type": "IncompatiblePeer", "value": "..."}},
    }
    assert build_alert_signature(alert) == "sentry-class:acme-data:IncompatiblePeer"


def test_build_signature_non_sentry_returns_empty():
    alert = {
        "source": "github",
        "service": "repo/x",
        "raw_payload": {"metadata": {"type": "Whatever"}},
    }
    assert build_alert_signature(alert) == ""


def test_build_signature_missing_service_returns_empty():
    alert = {
        "source": "sentry",
        "service": "",
        "raw_payload": {"metadata": {"type": "IncompatiblePeer"}},
    }
    assert build_alert_signature(alert) == ""


def test_build_signature_missing_error_class_returns_empty():
    alert = {
        "source": "sentry",
        "service": "acme-data",
        "raw_payload": {"metadata": {"value": "no type field"}},
    }
    assert build_alert_signature(alert) == ""


def test_build_signature_non_dict_payload_returns_empty():
    alert = {"source": "sentry", "service": "x", "raw_payload": "not a dict"}
    assert build_alert_signature(alert) == ""


# ---------------------------------------------------------------------------
# DB-backed activity helpers
# ---------------------------------------------------------------------------


async def _seed_task(db_pool, task_id: str, *, is_completed: bool) -> None:
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO todoist_projects (id, name, is_managed, raw) "
            "VALUES ('P_INBOX', 'Inbox', true, '{}'::jsonb) "
            "ON CONFLICT (id) DO NOTHING"
        )
        await conn.execute("DELETE FROM todoist_tasks WHERE id = $1", task_id)
        await conn.execute(
            """
            INSERT INTO todoist_tasks
                (id, project_id, content, labels, is_completed, raw)
            VALUES ($1, 'P_INBOX', 'test task', $2, $3, '{}'::jsonb)
            """,
            task_id,
            ["@pandora", "#alert"],
            is_completed,
        )


async def _clear_dedup(db_pool, signature: str) -> None:
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM alert_dedup_index WHERE signature = $1", signature)


@pytest.mark.asyncio
async def test_find_open_task_returns_none_when_no_binding(db_pool):
    acts = AlertActivities(db_pool=db_pool)
    sig = f"sentry-class:svc:{uuid.uuid4().hex}"
    await _clear_dedup(db_pool, sig)
    assert await acts.find_open_task_for_signature(sig) is None


@pytest.mark.asyncio
async def test_find_open_task_returns_id_for_open_task(db_pool):
    acts = AlertActivities(db_pool=db_pool)
    sig = f"sentry-class:svc:{uuid.uuid4().hex}"
    task_id = f"task-{uuid.uuid4().hex[:12]}"
    await _seed_task(db_pool, task_id, is_completed=False)
    await _clear_dedup(db_pool, sig)
    await acts.record_signature_new_task(sig, task_id)
    assert await acts.find_open_task_for_signature(sig) == task_id


@pytest.mark.asyncio
async def test_find_open_task_returns_none_when_task_completed(db_pool):
    acts = AlertActivities(db_pool=db_pool)
    sig = f"sentry-class:svc:{uuid.uuid4().hex}"
    task_id = f"task-{uuid.uuid4().hex[:12]}"
    await _seed_task(db_pool, task_id, is_completed=True)
    await _clear_dedup(db_pool, sig)
    await acts.record_signature_new_task(sig, task_id)
    assert await acts.find_open_task_for_signature(sig) is None


@pytest.mark.asyncio
async def test_find_open_task_returns_none_when_task_missing_from_projection(db_pool):
    acts = AlertActivities(db_pool=db_pool)
    sig = f"sentry-class:svc:{uuid.uuid4().hex}"
    missing_task_id = f"missing-{uuid.uuid4().hex[:12]}"
    await _clear_dedup(db_pool, sig)
    # Binding exists, but no row in todoist_tasks → JOIN drops it.
    await acts.record_signature_new_task(sig, missing_task_id)
    assert await acts.find_open_task_for_signature(sig) is None


@pytest.mark.asyncio
async def test_record_signature_new_task_upsert_resets_stats(db_pool):
    acts = AlertActivities(db_pool=db_pool)
    sig = f"sentry-class:svc:{uuid.uuid4().hex}"
    first_id = f"task-{uuid.uuid4().hex[:12]}"
    second_id = f"task-{uuid.uuid4().hex[:12]}"
    await _seed_task(db_pool, first_id, is_completed=False)
    await _seed_task(db_pool, second_id, is_completed=False)
    await _clear_dedup(db_pool, sig)

    await acts.record_signature_new_task(sig, first_id)
    await acts.record_signature_recurrence(sig)
    await acts.record_signature_recurrence(sig)

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT task_id, occurrence_count FROM alert_dedup_index WHERE signature = $1",
            sig,
        )
    assert row["task_id"] == first_id
    assert row["occurrence_count"] == 3

    # Upsert with a new task id → counter resets, task rebinds
    await acts.record_signature_new_task(sig, second_id)
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT task_id, occurrence_count FROM alert_dedup_index WHERE signature = $1",
            sig,
        )
    assert row["task_id"] == second_id
    assert row["occurrence_count"] == 1


@pytest.mark.asyncio
async def test_record_signature_recurrence_no_op_on_missing_row(db_pool):
    acts = AlertActivities(db_pool=db_pool)
    sig = f"sentry-class:svc:{uuid.uuid4().hex}"
    await _clear_dedup(db_pool, sig)
    # Should not raise.
    await acts.record_signature_recurrence(sig)
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT 1 FROM alert_dedup_index WHERE signature = $1", sig)
    assert row is None


@pytest.mark.asyncio
async def test_find_open_task_returns_none_when_signature_empty(db_pool):
    acts = AlertActivities(db_pool=db_pool)
    assert await acts.find_open_task_for_signature("") is None


@pytest.mark.asyncio
async def test_record_signature_new_task_noop_when_args_empty(db_pool):
    acts = AlertActivities(db_pool=db_pool)
    # Should not raise nor write a row with empty key.
    await acts.record_signature_new_task("", "task-1")
    await acts.record_signature_new_task("sig-x", "")
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT 1 FROM alert_dedup_index WHERE signature IN ('', 'sig-x')"
        )
    assert row is None
