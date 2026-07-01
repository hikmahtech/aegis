"""End-to-end: capture_to_inbox with real DB and a stubbed TodoistConnector.

Verifies:
- A successful capture results in:
  - one idempotency row
  - one connector.commands() call with the right shape
  - the recorded todoist_task_ref
- A second call with the same (source_tag, external_id) is a no-op
  and returns the stored ref.
- An outbox-staged capture (retryable connector failure) records the
  temp_id and inserts a pending outbox row.
"""

from __future__ import annotations

import pytest
from aegis_worker.activities.capture import CaptureActivities
from temporalio.testing import ActivityEnvironment


class _OkConnector:
    def __init__(self):
        self.calls = []

    async def commands(self, cmds):
        self.calls.append(cmds)
        return {
            "ok": True,
            "data": {
                "sync_status": {c["uuid"]: "ok" for c in cmds},
                "temp_id_mapping": {c["temp_id"]: "real-from-e2e" for c in cmds},
            },
            "error": None,
            "retryable": False,
        }


class _RetryableFailConnector:
    async def commands(self, cmds):
        return {"ok": False, "data": None, "error": "http_503", "retryable": True}


async def _setup(db_pool):
    """Seed settings + clean tables. Called from each test."""
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO settings (key, value) VALUES "
            "('todoist_managed_project_ids', '{\"inbox\": \"inbox-test\"}'::jsonb), "
            "('todoist_capture_enabled', 'true'::jsonb) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
        )
        await conn.execute("DELETE FROM todoist_capture_idempotency")
        await conn.execute("DELETE FROM todoist_outbox")


@pytest.mark.asyncio
async def test_capture_creates_idempotency_and_calls_connector(db_pool):
    await _setup(db_pool)
    connector = _OkConnector()
    activities = CaptureActivities(db_pool=db_pool, connector=connector)
    env = ActivityEnvironment()
    ref = await env.run(
        activities.capture_to_inbox,
        "#email",
        "gmail-e2e-1",
        "Phase 2 e2e test",
        "description here",
    )
    assert ref == "real-from-e2e"
    assert len(connector.calls) == 1
    cmd = connector.calls[0][0]
    assert cmd["args"]["project_id"] == "inbox-test"
    assert cmd["args"]["content"] == "Phase 2 e2e test"
    assert "#email" in cmd["args"]["labels"]
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT todoist_task_ref FROM todoist_capture_idempotency "
            "WHERE source_tag = '#email' AND external_id = 'gmail-e2e-1'"
        )
    assert row["todoist_task_ref"] == "real-from-e2e"


@pytest.mark.asyncio
async def test_second_capture_dedups(db_pool):
    await _setup(db_pool)
    connector = _OkConnector()
    activities = CaptureActivities(db_pool=db_pool, connector=connector)
    env = ActivityEnvironment()
    first = await env.run(
        activities.capture_to_inbox, "#alert", "alert-dedup-1", "first", None
    )
    second = await env.run(
        activities.capture_to_inbox, "#alert", "alert-dedup-1", "first", None
    )
    assert first == second
    assert len(connector.calls) == 1  # connector NOT called for the second


@pytest.mark.asyncio
async def test_outbox_fallback_on_retryable(db_pool):
    await _setup(db_pool)
    connector = _RetryableFailConnector()
    activities = CaptureActivities(db_pool=db_pool, connector=connector)
    env = ActivityEnvironment()
    ref = await env.run(
        activities.capture_to_inbox, "#calendar", "calendar-e2e-1", "RSVP test", None
    )
    assert ref is not None and ref.startswith("item-")
    async with db_pool.acquire() as conn:
        outbox = await conn.fetch("SELECT temp_id, status FROM todoist_outbox WHERE status = 'pending'")
    assert len(outbox) == 1
    assert outbox[0]["temp_id"] == ref
