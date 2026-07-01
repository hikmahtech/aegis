"""Notification budget gate (Phase 5)."""

from __future__ import annotations

import pytest_asyncio
from aegis.services.notifications import (
    budget_status,
    count_today,
    record_notification,
    should_send,
)


@pytest_asyncio.fixture(loop_scope="function")
async def clean_notif(db_pool):
    await db_pool.execute("DELETE FROM notification_log")
    yield db_pool
    await db_pool.execute("DELETE FROM notification_log")


async def test_record_and_count_only_sent(clean_notif):
    await record_notification(clean_notif, "sebas", "notify_drift", True)
    await record_notification(clean_notif, "sebas", "notify_drift", False)  # deferred
    assert await count_today(clean_notif) == 1


async def test_disabled_always_allows(clean_notif):
    for _ in range(20):
        await record_notification(clean_notif, "x", "e", True)
    allow, n = await should_send(clean_notif, enabled=False, daily_budget=5)
    assert allow is True and n == 20


async def test_enabled_caps_at_budget(clean_notif):
    for _ in range(5):
        await record_notification(clean_notif, "x", "e", True)
    allow, n = await should_send(clean_notif, enabled=True, daily_budget=5)
    assert allow is False and n == 5
    allow2, _ = await should_send(clean_notif, enabled=True, daily_budget=10)
    assert allow2 is True


async def test_budget_status(clean_notif):
    await record_notification(clean_notif, "x", "e", True)
    await record_notification(clean_notif, "x", "e", False)
    s = await budget_status(clean_notif, enabled=True, daily_budget=8)
    assert s == {"enabled": True, "daily_budget": 8, "sent_today": 1, "deferred_today": 1}
