"""safe_send_message honours the notification budget (Phase 5)."""

from __future__ import annotations

import pytest_asyncio
from aegis_worker.activities.delivery import safe_send_message


class _FakeDelivery:
    def __init__(self, pool, *, enabled, budget):
        self.db_pool = pool
        self.budget_enabled = enabled
        self.daily_budget = budget
        self.channel = "slack"  # budget gate only applies on the slack push path
        self.sent: list[str] = []

    async def send_message(self, *, agent_id, message, chat_id):
        self.sent.append(message)
        return {"ok": True}


@pytest_asyncio.fixture(loop_scope="function")
async def clean_notif(db_pool):
    await db_pool.execute("DELETE FROM notification_log")
    yield db_pool
    await db_pool.execute("DELETE FROM notification_log")


async def test_defers_when_over_budget(clean_notif):
    d = _FakeDelivery(clean_notif, enabled=True, budget=0)  # cap 0 → always over
    await safe_send_message(d, agent_id="sebas", message="hi", log_event="e")
    assert d.sent == []  # deferred, not sent
    assert await clean_notif.fetchval("SELECT count(*) FROM notification_log WHERE NOT sent") == 1


async def test_sends_and_records_when_under_budget(clean_notif):
    d = _FakeDelivery(clean_notif, enabled=True, budget=5)
    await safe_send_message(d, agent_id="sebas", message="hi", log_event="e")
    assert d.sent == ["hi"]
    assert await clean_notif.fetchval("SELECT count(*) FROM notification_log WHERE sent") == 1


async def test_disabled_always_sends(clean_notif):
    d = _FakeDelivery(clean_notif, enabled=False, budget=0)
    await safe_send_message(d, agent_id="sebas", message="hi", log_event="e")
    assert d.sent == ["hi"]
