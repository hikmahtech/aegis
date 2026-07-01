"""Tests for CaptureActivities.capture_to_inbox."""

from __future__ import annotations

import pytest
from aegis_worker.activities.capture import CaptureActivities
from temporalio.testing import ActivityEnvironment


class _OkConnector:
    """Stub connector that always succeeds and returns a real id."""

    def __init__(self):
        self.commands_calls: list[list[dict]] = []

    async def commands(self, cmds: list[dict]) -> dict:
        self.commands_calls.append(cmds)
        return {
            "ok": True,
            "data": {
                "sync_status": {c["uuid"]: "ok" for c in cmds},
                "temp_id_mapping": {c["temp_id"]: f"real-{i}" for i, c in enumerate(cmds)},
            },
            "error": None,
            "retryable": False,
        }


class _FailRetryableConnector:
    async def commands(self, cmds):
        return {"ok": False, "data": None, "error": "http_503", "retryable": True}


@pytest.mark.asyncio
async def test_happy_path_creates_inbox_task(db_pool):
    """Capture succeeds, idempotency row written, real Todoist id stored."""
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM todoist_capture_idempotency WHERE external_id = 'gmail-msg-1'"
        )
        # Seed the inbox project id so the helper can find it.
        await conn.execute(
            "INSERT INTO settings (key, value) VALUES "
            "('todoist_managed_project_ids', '{\"inbox\": \"inbox-1\"}'::jsonb) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
        )
        await conn.execute(
            "INSERT INTO settings (key, value) VALUES "
            "('todoist_capture_enabled', 'true'::jsonb) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
        )
    connector = _OkConnector()
    activities = CaptureActivities(db_pool=db_pool, connector=connector)
    env = ActivityEnvironment()
    result = await env.run(
        activities.capture_to_inbox,
        "#email",
        "gmail-msg-1",
        "Re: Invoice",
        "From: alice@example.com",
    )
    assert result is not None
    assert result.startswith("real-")
    # idempotency row recorded with the real id
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT todoist_task_ref FROM todoist_capture_idempotency "
            "WHERE source_tag = '#email' AND external_id = 'gmail-msg-1'"
        )
    assert row is not None and row["todoist_task_ref"] == result
    # connector got one item_add command with the source_tag as a label
    assert len(connector.commands_calls) == 1
    cmd = connector.commands_calls[0][0]
    assert cmd["type"] == "item_add"
    assert cmd["args"]["project_id"] == "inbox-1"
    assert cmd["args"]["content"] == "Re: Invoice"
    assert "#email" in cmd["args"]["labels"]


@pytest.mark.asyncio
async def test_kill_switch_skips_emit(db_pool):
    """settings.todoist_capture_enabled=false → helper returns None, no API call."""
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO settings (key, value) VALUES "
            "('todoist_capture_enabled', 'false'::jsonb) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
        )
    connector = _OkConnector()
    activities = CaptureActivities(db_pool=db_pool, connector=connector)
    env = ActivityEnvironment()
    result = await env.run(activities.capture_to_inbox, "#email", "gmail-msg-2", "ignored", None)
    assert result is None
    assert connector.commands_calls == []


@pytest.mark.asyncio
async def test_dedup_hit_returns_existing_ref(db_pool):
    """Second call with same (source_tag, external_id) is a no-op and returns the recorded ref."""
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM todoist_capture_idempotency WHERE external_id = 'gmail-msg-3'"
        )
        await conn.execute(
            "INSERT INTO settings (key, value) VALUES "
            "('todoist_managed_project_ids', '{\"inbox\": \"inbox-1\"}'::jsonb), "
            "('todoist_capture_enabled', 'true'::jsonb) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
        )
    connector = _OkConnector()
    activities = CaptureActivities(db_pool=db_pool, connector=connector)
    env = ActivityEnvironment()
    first = await env.run(activities.capture_to_inbox, "#email", "gmail-msg-3", "First", None)
    second = await env.run(activities.capture_to_inbox, "#email", "gmail-msg-3", "First", None)
    assert first == second
    # connector only invoked once
    assert len(connector.commands_calls) == 1


@pytest.mark.asyncio
async def test_missing_inbox_id_returns_none_and_warns(db_pool):
    """If todoist_managed_project_ids is empty, capture skips and returns None."""
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM settings WHERE key = 'todoist_managed_project_ids'")
        await conn.execute(
            "INSERT INTO settings (key, value) VALUES "
            "('todoist_capture_enabled', 'true'::jsonb) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
        )
    connector = _OkConnector()
    activities = CaptureActivities(db_pool=db_pool, connector=connector)
    env = ActivityEnvironment()
    result = await env.run(activities.capture_to_inbox, "#email", "gmail-msg-4", "no inbox", None)
    assert result is None
    assert connector.commands_calls == []


@pytest.mark.asyncio
async def test_outbox_fallback_on_retryable_failure(db_pool):
    """Retryable connector failure stages an outbox row and returns the temp_id."""
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM todoist_capture_idempotency WHERE external_id = 'alert-fingerprint-1'"
        )
        await conn.execute(
            "INSERT INTO settings (key, value) VALUES "
            "('todoist_managed_project_ids', '{\"inbox\": \"inbox-1\"}'::jsonb), "
            "('todoist_capture_enabled', 'true'::jsonb) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
        )
        await conn.execute("DELETE FROM todoist_outbox")
    connector = _FailRetryableConnector()
    activities = CaptureActivities(db_pool=db_pool, connector=connector)
    env = ActivityEnvironment()
    result = await env.run(
        activities.capture_to_inbox, "#alert", "alert-fingerprint-1", "PG down", None
    )
    # The helper returns the temp_id of the command it staged
    assert result is not None and result.startswith("item-")
    # Outbox has one pending row
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT temp_id, status FROM todoist_outbox WHERE status = 'pending'"
        )
    assert len(rows) == 1
    assert rows[0]["temp_id"] == result
    # idempotency row recorded with the temp_id
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT todoist_task_ref FROM todoist_capture_idempotency "
            "WHERE source_tag = '#alert' AND external_id = 'alert-fingerprint-1'"
        )
    assert row["todoist_task_ref"] == result


@pytest.mark.asyncio
async def test_extra_labels_merged_into_item_add(db_pool):
    """extra_labels are appended to the [source_tag] label set on item_add."""
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM todoist_capture_idempotency WHERE external_id = 'alert-with-labels-1'"
        )
        await conn.execute(
            "INSERT INTO settings (key, value) VALUES "
            "('todoist_managed_project_ids', '{\"inbox\": \"inbox-1\"}'::jsonb) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
        )
        await conn.execute(
            "INSERT INTO settings (key, value) VALUES "
            "('todoist_capture_enabled', 'true'::jsonb) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
        )
    connector = _OkConnector()
    acts = CaptureActivities(db_pool=db_pool, connector=connector)
    env = ActivityEnvironment()
    ref = await env.run(
        acts.capture_to_inbox,
        "#alert",
        "alert-with-labels-1",
        "Production CI failure",
        "Some description",
        ["@pandora"],
    )
    assert ref is not None
    # Inspect the item_add command actually sent to the connector.
    sent = connector.commands_calls[0][0]
    assert sent["type"] == "item_add"
    labels = sent["args"]["labels"]
    assert "#alert" in labels
    assert "@pandora" in labels


@pytest.mark.asyncio
async def test_extra_labels_dedup_when_overlap_with_source_tag(db_pool):
    """Passing the source_tag again in extra_labels does not duplicate it."""
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM todoist_capture_idempotency WHERE external_id = 'alert-dedup-1'"
        )
        await conn.execute(
            "INSERT INTO settings (key, value) VALUES "
            "('todoist_managed_project_ids', '{\"inbox\": \"inbox-1\"}'::jsonb) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
        )
        await conn.execute(
            "INSERT INTO settings (key, value) VALUES "
            "('todoist_capture_enabled', 'true'::jsonb) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
        )
    connector = _OkConnector()
    acts = CaptureActivities(db_pool=db_pool, connector=connector)
    env = ActivityEnvironment()
    await env.run(
        acts.capture_to_inbox,
        "#alert",
        "alert-dedup-1",
        "Production CI failure",
        None,
        ["#alert", "@pandora"],
    )
    sent = connector.commands_calls[0][0]
    labels = sent["args"]["labels"]
    assert labels.count("#alert") == 1
