"""Tests for HomelabActivities.check_comms_inbound_health and
alert_comms_inbound_down (comms inbound watchdog via Todoist).

Uses ActivityEnvironment + respx per the worker testing convention.
DB-touching tests (dedup, audit_log write) run against real Postgres via the
db_pool fixture (tests/worker/conftest.py).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
import respx
from aegis_worker.activities.homelab import HomelabActivities
from httpx import Response
from temporalio.testing import ActivityEnvironment

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_act(db_pool=None, todoist_connector=None):
    delivery = AsyncMock()
    delivery.send_message = AsyncMock(return_value={"ok": True})
    return HomelabActivities(
        db_pool=db_pool,
        homelab=None,
        delivery=delivery,
        todoist_connector=todoist_connector,
    )


# ---------------------------------------------------------------------------
# check_comms_inbound_health — no-DB tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_health_unreachable_endpoint_returns_unknown():
    """HTTP error reaching the comms service → status='unknown' (backward compat)."""
    respx.get("http://comms.test/api/health").mock(side_effect=Exception("Connection refused"))
    act = _make_act()
    env = ActivityEnvironment()
    result = await env.run(act.check_comms_inbound_health, "http://comms.test")
    assert result == {"status": "unknown"}


@pytest.mark.asyncio
@respx.mock
async def test_health_no_inbound_field_returns_unknown():
    """No `inbound` block in the health body → status='unknown' (do nothing).

    Also covers the removed legacy `telegram_api` fallback: a body carrying
    only that block is treated as unknown now that Telegram is gone.
    """
    respx.get("http://comms.test/api/health").mock(
        return_value=Response(
            200,
            json={
                "status": "ok",
                "service": "aegis-comms",
                "telegram_api": {"reachable": True, "last_ok_seconds_ago": 30},
            },
        )
    )
    act = _make_act()
    env = ActivityEnvironment()
    result = await env.run(act.check_comms_inbound_health, "http://comms.test")
    assert result == {"status": "unknown"}


@pytest.mark.asyncio
@respx.mock
async def test_health_slack_inbound_healthy_returns_ok():
    """Slack body: generic `inbound` block healthy → status='ok'."""
    respx.get("http://comms.test/api/health").mock(
        return_value=Response(
            200,
            json={
                "status": "ok",
                "channel": "slack",
                "inbound": {
                    "channel": "slack",
                    "healthy": True,
                    "last_ok_seconds_ago": 30,
                    "last_error": None,
                },
            },
        )
    )
    act = _make_act()
    env = ActivityEnvironment()
    result = await env.run(act.check_comms_inbound_health, "http://comms.test")
    assert result == {"status": "ok"}


@pytest.mark.asyncio
@respx.mock
async def test_health_slack_inbound_unhealthy_returns_down():
    """Slack body: `inbound.healthy` False → status='down' with the inbound details."""
    respx.get("http://comms.test/api/health").mock(
        return_value=Response(
            200,
            json={
                "status": "ok",
                "channel": "slack",
                "inbound": {
                    "channel": "slack",
                    "healthy": False,
                    "last_ok_seconds_ago": 900,
                    "last_error": "socket_not_connected",
                },
            },
        )
    )
    act = _make_act()
    env = ActivityEnvironment()
    result = await env.run(act.check_comms_inbound_health, "http://comms.test")
    assert result["status"] == "down"
    assert result["last_ok_seconds_ago"] == 900
    assert result["last_error"] == "socket_not_connected"


@pytest.mark.asyncio
async def test_health_empty_url_returns_unknown():
    """No comms_url configured → status='unknown'."""
    act = _make_act()
    env = ActivityEnvironment()
    result = await env.run(act.check_comms_inbound_health, "")
    assert result == {"status": "unknown"}


# ---------------------------------------------------------------------------
# alert_comms_inbound_down — DB tests (real Postgres via db_pool fixture)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_alert_polling_down_no_dedup_creates_task(db_pool):
    """First alert in the dedup window creates a Todoist task and writes audit_log."""
    # Seed the settings the activity needs
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO settings (key, value) VALUES ('todoist_capture_enabled', 'true'::jsonb)"
            " ON CONFLICT (key) DO UPDATE SET value = 'true'::jsonb"
        )
        await conn.execute(
            "INSERT INTO settings (key, value) VALUES "
            "('todoist_managed_project_ids', '{\"inbox\": \"inbox-project-1\"}'::jsonb)"
            " ON CONFLICT (key) DO UPDATE SET value = "
            "'{\"inbox\": \"inbox-project-1\"}'::jsonb"
        )
        # Clean up any prior audit rows for this test
        await conn.execute(
            "DELETE FROM audit_log WHERE action = 'comms_inbound_alert'"
        )

    # Mock todoist connector that records the command
    todoist = MagicMock()
    commands_called = []

    async def fake_commands(cmds):
        commands_called.extend(cmds)
        return {"ok": True, "data": {"temp_id_mapping": {}}}

    todoist.commands = fake_commands

    act = _make_act(db_pool=db_pool, todoist_connector=todoist)
    env = ActivityEnvironment()
    created = await env.run(act.alert_comms_inbound_down, 900, "Network unreachable")

    assert created is True
    assert len(commands_called) == 1
    cmd = commands_called[0]
    assert cmd["type"] == "item_add"
    # Label @pandora must be present
    assert "@pandora" in cmd["args"]["labels"]
    # ...and #alert, which is what gives the task a source_tag. Without it
    # resolve_verb returns "unknown" and AgentTaskFlow parks the task unworked.
    assert "#alert" in cmd["args"]["labels"]
    # Content mentions the outage
    assert "DOWN" in cmd["args"]["content"] or "down" in cmd["args"]["content"].lower()

    # audit_log row must exist
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id FROM audit_log WHERE action = 'comms_inbound_alert'"
        )
    assert row is not None

    # Cleanup
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM audit_log WHERE action = 'comms_inbound_alert'")


@pytest.mark.asyncio
async def test_alert_polling_down_dedup_skips_second_task(db_pool):
    """Second alert within dedup window → deduped, no second task created."""
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO settings (key, value) VALUES ('todoist_capture_enabled', 'true'::jsonb)"
            " ON CONFLICT (key) DO UPDATE SET value = 'true'::jsonb"
        )
        await conn.execute(
            "INSERT INTO settings (key, value) VALUES "
            "('todoist_managed_project_ids', '{\"inbox\": \"inbox-project-1\"}'::jsonb)"
            " ON CONFLICT (key) DO UPDATE SET value = "
            "'{\"inbox\": \"inbox-project-1\"}'::jsonb"
        )
        # Seed an audit_log row representing a recent prior alert
        await conn.execute(
            "DELETE FROM audit_log WHERE action = 'comms_inbound_alert'"
        )
        await conn.execute(
            "INSERT INTO audit_log (actor, action, target_type, target_id, details) "
            "VALUES ('delivery-watchdog', 'comms_inbound_alert', 'comms', 'inbound', '{}')"
        )

    todoist = MagicMock()
    commands_called = []

    async def fake_commands(cmds):
        commands_called.extend(cmds)
        return {"ok": True, "data": {"temp_id_mapping": {}}}

    todoist.commands = fake_commands

    act = _make_act(db_pool=db_pool, todoist_connector=todoist)
    env = ActivityEnvironment()
    created = await env.run(act.alert_comms_inbound_down, 900, None)

    assert created is False
    assert len(commands_called) == 0

    # Cleanup
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM audit_log WHERE action = 'comms_inbound_alert'")


async def test_alert_rejected_create_does_not_write_dedup_audit(db_pool):
    """A rejected Todoist create must return False and NOT write the
    comms_inbound_alert audit row — otherwise the alert is silently
    deduped away for 12h after a single transient rejection."""
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM audit_log WHERE action = 'comms_inbound_alert'")

    todoist = MagicMock()

    async def fake_commands(cmds):
        return {
            "ok": True,
            "data": {"sync_status": {cmds[0]["uuid"]: {"error_tag": "INVALID_ARGUMENT"}}},
        }

    todoist.commands = fake_commands

    act = _make_act(db_pool=db_pool, todoist_connector=todoist)
    env = ActivityEnvironment()
    created = await env.run(act.alert_comms_inbound_down, 900, None)

    assert created is False
    async with db_pool.acquire() as conn:
        row = await conn.fetchval(
            "SELECT 1 FROM audit_log WHERE action = 'comms_inbound_alert' LIMIT 1"
        )
    assert row is None


# ---------------------------------------------------------------------------
# One-open-task-at-a-time guard + resolve
#
# Prod produced 8 identical open tasks over 4 days on the old 12h time window
# (2026-08-23..27). These pin the replacement: the alert is suppressed while its
# task is open, and re-arms only once that task is completed.
# ---------------------------------------------------------------------------


async def _seed_alert_settings(db_pool) -> None:
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO settings (key, value) VALUES ('todoist_capture_enabled', 'true'::jsonb)"
            " ON CONFLICT (key) DO UPDATE SET value = 'true'::jsonb"
        )
        await conn.execute(
            "INSERT INTO settings (key, value) VALUES "
            "('todoist_managed_project_ids', '{\"inbox\": \"inbox-project-1\"}'::jsonb)"
            " ON CONFLICT (key) DO UPDATE SET value = "
            "'{\"inbox\": \"inbox-project-1\"}'::jsonb"
        )
        await conn.execute("DELETE FROM audit_log WHERE action = 'comms_inbound_alert'")
        await conn.execute("DELETE FROM todoist_tasks WHERE id = 'alert-task-1'")


async def _seed_prior_alert(db_pool, *, task_ref: str, completed: bool) -> None:
    """A previous alert whose task exists in todoist_tasks."""
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO todoist_tasks (id, content, labels, is_completed) "
            "VALUES ($1, 'AEGIS inbound comms is DOWN', "
            "ARRAY['#alert','@pandora']::text[], $2)",
            task_ref,
            completed,
        )
        await conn.execute(
            "INSERT INTO audit_log (actor, action, target_type, target_id, details) "
            "VALUES ('delivery-watchdog', 'comms_inbound_alert', 'comms', 'polling', $1)",
            {"task_ref": task_ref},
        )


def _recording_todoist(calls: list):
    todoist = MagicMock()

    async def fake_commands(cmds):
        calls.extend(cmds)
        # item_complete carries no temp_id; only item_add gets a mapping back.
        temp_id = cmds[0].get("temp_id")
        mapping = {temp_id: "new-task-9"} if temp_id else {}
        return {"ok": True, "data": {"temp_id_mapping": mapping}}

    todoist.commands = fake_commands
    return todoist


async def test_alert_suppressed_while_previous_task_still_open(db_pool):
    """An open alert task suppresses a new one however long the outage lasts."""
    await _seed_alert_settings(db_pool)
    await _seed_prior_alert(db_pool, task_ref="alert-task-1", completed=False)

    calls: list = []
    act = _make_act(db_pool=db_pool, todoist_connector=_recording_todoist(calls))
    created = await ActivityEnvironment().run(act.alert_comms_inbound_down, 900, None)

    assert created is False
    assert calls == []


async def test_alert_rearms_once_previous_task_completed(db_pool):
    """Completing the task re-arms the alert — the guard must not be permanent."""
    await _seed_alert_settings(db_pool)
    await _seed_prior_alert(db_pool, task_ref="alert-task-1", completed=True)

    calls: list = []
    act = _make_act(db_pool=db_pool, todoist_connector=_recording_todoist(calls))
    created = await ActivityEnvironment().run(act.alert_comms_inbound_down, 900, None)

    assert created is True
    assert len(calls) == 1
    # The new task's id is recorded, or the next tick has nothing to check.
    async with db_pool.acquire() as conn:
        ref = await conn.fetchval(
            "SELECT details->>'task_ref' FROM audit_log "
            "WHERE action = 'comms_inbound_alert' ORDER BY created_at DESC LIMIT 1"
        )
    assert ref == "new-task-9"


async def test_resolve_completes_the_open_alert_task(db_pool):
    """Recovery completes the task, which is what re-arms the guard."""
    await _seed_alert_settings(db_pool)
    await _seed_prior_alert(db_pool, task_ref="alert-task-1", completed=False)

    calls: list = []
    act = _make_act(db_pool=db_pool, todoist_connector=_recording_todoist(calls))
    resolved = await ActivityEnvironment().run(act.resolve_comms_inbound_alert)

    assert resolved is True
    assert calls[0]["type"] == "item_complete"
    assert calls[0]["args"]["id"] == "alert-task-1"
    async with db_pool.acquire() as conn:
        done = await conn.fetchval(
            "SELECT is_completed FROM todoist_tasks WHERE id = 'alert-task-1'"
        )
    assert done is True


async def test_resolve_is_a_noop_when_no_task_is_open(db_pool):
    """No open alert task → nothing to complete, and no Todoist call."""
    await _seed_alert_settings(db_pool)
    await _seed_prior_alert(db_pool, task_ref="alert-task-1", completed=True)

    calls: list = []
    act = _make_act(db_pool=db_pool, todoist_connector=_recording_todoist(calls))
    resolved = await ActivityEnvironment().run(act.resolve_comms_inbound_alert)

    assert resolved is False
    assert calls == []
