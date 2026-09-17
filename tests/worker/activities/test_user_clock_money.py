"""The money lane's "today" is the user's clock, not a hardcoded zone (#556).

`CaptureActivities` and `MoneyActivities` used to carry `home_tz =
"Asia/Kolkata"`. They now read the `user_timezone` settings row through
`services/user_time.py`. Two zones 26 hours apart (UTC+14 and UTC-12) never
share a calendar date, so each assertion below can only pass if the row is
what the activity read.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from aegis_worker.activities.capture import CaptureActivities
from aegis_worker.activities.money import MoneyActivities
from temporalio.testing import ActivityEnvironment

EAST = "Pacific/Kiritimati"  # UTC+14
WEST = "Etc/GMT+12"  # UTC-12


def _today(zone: str):
    return datetime.now(ZoneInfo(zone)).date()


@pytest_asyncio.fixture(loop_scope="function")
async def clock(db_pool):
    async def _set(zone: str | None):
        if zone is None:
            await db_pool.execute("DELETE FROM settings WHERE key = 'user_timezone'")
            return
        await db_pool.execute(
            "INSERT INTO settings (key, value) VALUES ('user_timezone', $1) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            zone,
        )

    await db_pool.execute(
        "INSERT INTO settings (key, value) VALUES "
        "('todoist_managed_project_ids', '{\"inbox\": \"inbox-1\"}'::jsonb) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
    )
    await db_pool.execute(
        "INSERT INTO settings (key, value) VALUES ('todoist_capture_enabled', 'true'::jsonb) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
    )
    await db_pool.execute("DELETE FROM todoist_capture_idempotency WHERE source_tag = '#bill'")
    yield _set
    await db_pool.execute("DELETE FROM settings WHERE key = 'user_timezone'")
    await db_pool.execute("DELETE FROM todoist_capture_idempotency WHERE source_tag = '#bill'")


def _due(payee_key: str, due_on) -> dict:
    return {
        "kind": "due", "direction": "out", "amount": "10.00", "currency": "INR",
        "payee": payee_key.title(), "payee_key": payee_key, "channel": "statement",
        "instrument": "x", "due_on": due_on.isoformat(), "entity": "personal",
        "parser": "t", "confidence": 1.0, "source_class": "bank",
    }


@pytest.mark.parametrize("zone", [EAST, WEST])
async def test_an_overdue_bill_is_due_today_on_the_users_clock(db_pool, clock, monkeypatch, zone):
    monkeypatch.setattr(
        "aegis.connectors.todoist.TodoistConnector.check_sync_status",
        staticmethod(
            lambda result, uuids: {
                "ok": True, "retryable": False, "rejected_retryable": False,
                "rejected": {}, "envelope_error": None,
            }
        ),
    )
    await clock(zone)
    connector = AsyncMock()
    connector.commands = AsyncMock(return_value={"data": {"temp_id_mapping": {}}})
    acts = CaptureActivities(db_pool=db_pool, connector=connector)
    ev = _due(f"clock-{zone.lower().replace('/', '-')}", _today(zone) - timedelta(days=10))
    await ActivityEnvironment().run(acts.capture_due, ev, "clock-t", f"m-{zone}")
    cmd = connector.commands.await_args.args[0][0]
    # "Never born overdue": the floor is today — the user's today.
    assert cmd["args"]["due"] == {"date": _today(zone).isoformat()}


async def test_the_money_brief_is_dated_on_the_users_clock(db_pool, clock):
    act = MoneyActivities(db_pool=db_pool, llm=None, delivery=None, books_cfg=None)
    assert _today(EAST) != _today(WEST)
    for zone in (EAST, WEST):
        await clock(zone)
        brief = await ActivityEnvironment().run(act.build_money_brief, 7)
        assert brief["as_of"] == _today(zone).isoformat()


async def test_no_row_is_utc(db_pool, clock):
    await clock(None)
    act = MoneyActivities(db_pool=db_pool, llm=None, delivery=None, books_cfg=None)
    brief = await ActivityEnvironment().run(act.build_money_brief, 7)
    assert brief["as_of"] == _today("UTC").isoformat()
