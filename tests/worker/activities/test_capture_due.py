"""CaptureActivities.capture_due / complete_captured_task (spec §7.1)."""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from aegis_worker.activities.capture import CaptureActivities
from temporalio.testing import ActivityEnvironment

DUE = {
    "kind": "due", "direction": "out", "amount": "100308.53", "currency": "INR",
    "payee": "Axis credit card XX13", "payee_key": "axis credit card xx13", "channel": "statement",
    "instrument": "axis-cc-13", "due_on": "2099-09-07", "entity": "personal",
    "parser": "axis_cc_statement", "confidence": 1.0, "source_class": "bank",
}


@pytest_asyncio.fixture(autouse=True, loop_scope="function")
async def _capture_settings(db_pool):
    """`_capture` reads both rows; sibling files flip the kill switch off."""
    await db_pool.execute(
        "INSERT INTO settings (key, value) VALUES "
        "('todoist_managed_project_ids', '{\"inbox\": \"inbox-1\"}'::jsonb) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
    )
    await db_pool.execute(
        "INSERT INTO settings (key, value) VALUES ('todoist_capture_enabled', 'true'::jsonb) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
    )


def _acts(db_pool, projects=None):
    connector = AsyncMock()
    connector.commands = AsyncMock(
        return_value={"sync_status": {}, "data": {"temp_id_mapping": {}}}
    )
    acts = CaptureActivities(
        db_pool=db_pool, connector=connector, todoist_projects=projects or {}
    )
    return acts, connector


@pytest.mark.asyncio
async def test_capture_due_builds_a_dated_task_in_the_entity_project(db_pool, monkeypatch):
    await db_pool.execute("DELETE FROM todoist_capture_idempotency WHERE source_tag = '#bill'")
    acts, connector = _acts(db_pool, {"personal": "proj-personal", "hikmah": "proj-hikmah"})
    monkeypatch.setattr(
        "aegis.connectors.todoist.TodoistConnector.check_sync_status",
        staticmethod(
            lambda result, uuids: {
                "ok": True, "retryable": False, "rejected_retryable": False,
                "rejected": {}, "envelope_error": None,
            }
        ),
    )
    connector.commands = AsyncMock(return_value={"data": {"temp_id_mapping": {}}})
    ref = await ActivityEnvironment().run(acts.capture_due, DUE, "arshad-personal", "1a06")
    cmd = connector.commands.await_args.args[0][0]
    assert cmd["type"] == "item_add"
    assert cmd["args"]["project_id"] == "proj-personal"
    assert cmd["args"]["content"] == "Pay Axis credit card XX13 ₹1,00,308.53"
    assert cmd["args"]["due"] == {"date": "2099-09-06"}
    assert cmd["args"]["labels"] == ["#bill"]
    assert "Due 2099-09-07" in cmd["args"]["description"]
    assert "gmail 1a06" in cmd["args"]["description"]
    assert ref is None or isinstance(ref, str)
    # dedupe on (payee_key, due_on): second call never hits Todoist
    connector.commands.reset_mock()
    await ActivityEnvironment().run(acts.capture_due, DUE, "arshad-personal", "1a06-dup")
    connector.commands.assert_not_awaited()


@pytest.mark.asyncio
async def test_capture_due_failed_kind_and_past_due_date(db_pool, monkeypatch):
    await db_pool.execute("DELETE FROM todoist_capture_idempotency WHERE source_tag = '#bill'")
    acts, connector = _acts(db_pool)
    monkeypatch.setattr(
        "aegis.connectors.todoist.TodoistConnector.check_sync_status",
        staticmethod(
            lambda result, uuids: {
                "ok": True, "retryable": False, "rejected_retryable": False,
                "rejected": {}, "envelope_error": None,
            }
        ),
    )
    connector.commands = AsyncMock(return_value={"data": {"temp_id_mapping": {}}})
    ev = {
        **DUE, "kind": "failed", "payee": "Medium", "payee_key": "medium", "amount": "199.00",
        "due_on": (date.today() - timedelta(days=3)).isoformat(), "entity": "hikmah",
    }
    await ActivityEnvironment().run(acts.capture_due, ev, "arshad-hikmah", "m2")
    cmd = connector.commands.await_args.args[0][0]
    assert cmd["args"]["content"] == "Fix payment: Medium ₹199.00"
    assert cmd["args"]["due"] == {"date": date.today().isoformat()}
    assert "project_id" in cmd["args"]  # Inbox fallback when the entity has no project


@pytest.mark.asyncio
async def test_capture_due_ignores_non_dues(db_pool):
    acts, connector = _acts(db_pool)
    non_due = {**DUE, "kind": "transaction"}
    assert await ActivityEnvironment().run(acts.capture_due, non_due, "m", "x") is None
    no_date = {**DUE, "due_on": None}
    assert await ActivityEnvironment().run(acts.capture_due, no_date, "m", "x") is None
    connector.commands.assert_not_awaited()


@pytest.mark.asyncio
async def test_complete_captured_task(db_pool, monkeypatch):
    acts, connector = _acts(db_pool)
    monkeypatch.setattr(
        "aegis.connectors.todoist.TodoistConnector.check_sync_status",
        staticmethod(
            lambda result, uuids: {
                "ok": True, "retryable": False, "rejected_retryable": False,
                "rejected": {}, "envelope_error": None,
            }
        ),
    )
    assert await ActivityEnvironment().run(acts.complete_captured_task, "task-123") is True
    cmd = connector.commands.await_args.args[0][0]
    assert cmd["type"] == "item_complete" and cmd["args"]["id"] == "task-123"
    assert await ActivityEnvironment().run(acts.complete_captured_task, "item-temp") is False
    assert await ActivityEnvironment().run(acts.complete_captured_task, "") is False
