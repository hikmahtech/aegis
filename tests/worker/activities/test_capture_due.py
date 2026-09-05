"""CaptureActivities.capture_due / complete_captured_task (spec §7.1)."""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

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


def _today(acts: CaptureActivities):
    """"Today" as `capture_due` computes it — in the activity's own `home_tz`.

    `date.today()` is the RUNNER's timezone, so this test passed locally (IST)
    and failed in CI (UTC) for the ~5.5 hours a day the two disagree on the
    date. The floor being asserted is the activity's, so read its clock.
    """
    return datetime.now(ZoneInfo(acts.home_tz)).date()


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
        "due_on": (_today(acts) - timedelta(days=3)).isoformat(), "entity": "hikmah",
    }
    await ActivityEnvironment().run(acts.capture_due, ev, "arshad-hikmah", "m2")
    cmd = connector.commands.await_args.args[0][0]
    assert cmd["args"]["content"] == "Fix payment: Medium ₹199.00"
    assert cmd["args"]["due"] == {"date": _today(acts).isoformat()}
    assert "project_id" in cmd["args"]  # Inbox fallback when the entity has no project


@pytest.mark.asyncio
async def test_capture_due_ignores_non_dues(db_pool):
    acts, connector = _acts(db_pool)
    non_due = {**DUE, "kind": "transaction"}
    assert await ActivityEnvironment().run(acts.capture_due, non_due, "m", "x") is None
    no_date = {**DUE, "due_on": None}
    assert await ActivityEnvironment().run(acts.capture_due, no_date, "m", "x") is None
    no_amount = {**DUE, "amount": None}
    assert await ActivityEnvironment().run(acts.capture_due, no_amount, "m", "x") is None
    connector.commands.assert_not_awaited()


@pytest.mark.asyncio
async def test_capture_due_ignores_a_zero_invoice(db_pool):
    """A ₹0 invoice is not a bill.

    Cloudflare, Google Workspace and AWS all send zero invoices routinely, and
    "Pay Cloudflare $0.00" is the noise this lane exists to remove. Seen live
    on 2026-09-05: a $0.00 Cloudflare invoice produced a real Todoist task.
    The event is still indexed and still reaches the brief.
    """
    acts, connector = _acts(db_pool)
    for zero in ("0.00", "0", 0):
        ev = {**DUE, "amount": zero, "payee": "Cloudflare", "payee_key": "cloudflare"}
        assert await ActivityEnvironment().run(acts.capture_due, ev, "m", "x") is None
    connector.commands.assert_not_awaited()


@pytest.mark.asyncio
async def test_capture_due_skips_a_bill_another_sender_already_tasked(db_pool, monkeypatch):
    """One bill, one task, even when two senders announce it.

    Seen live 2026-09-05: Google Pay's "New bill from Axis Bank Credit Card cc"
    and the Axis statement's own "Axis credit card XX13" are the same
    ₹95,301.29 due 2026-08-07, but the payee-keyed dedupe saw two names and
    made two tasks. Money and date identify the obligation; the name does not.
    """
    await db_pool.execute("DELETE FROM todoist_capture_idempotency WHERE source_tag = '#bill'")
    await db_pool.execute("DELETE FROM finance.journal_index WHERE mailbox = 'dup-t'")
    await db_pool.execute(
        "INSERT INTO finance.journal_index "
        "  (message_id, mailbox, entity, kind, amount, currency, payee, payee_key, "
        "   due_on, parser, source_class, todoist_ref) "
        "VALUES ('dup-t/first', 'dup-t', 'personal', 'due', 95301.29, 'INR', "
        "        'Axis credit card XX13', 'axis credit card xx13', '2026-08-07', "
        "        'axis_cc_statement', 'bank', 'task-already-there')"
    )
    acts, connector = _acts(db_pool)
    twin = {
        **DUE, "payee": "Axis Bank Credit Card cc", "payee_key": "axis bank credit card cc",
        "amount": "95301.29", "currency": "INR", "due_on": "2026-08-07", "parser": "gpay_bill",
    }
    assert await ActivityEnvironment().run(acts.capture_due, twin, "m", "x") is None
    connector.commands.assert_not_awaited()

    # A genuinely different bill on the same date still gets its own task.
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
    other = {**twin, "payee": "Someone Else", "payee_key": "someone else", "amount": "42.00"}
    await ActivityEnvironment().run(acts.capture_due, other, "m", "y")
    connector.commands.assert_awaited()
    await db_pool.execute("DELETE FROM finance.journal_index WHERE mailbox = 'dup-t'")


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
