"""safe_send_message honours the notification budget (Phase 5)."""

from __future__ import annotations

import inspect

import pytest_asyncio
from aegis_worker.activities.delivery import DeliveryActivities, safe_send_message

from tests.delivery_stub import FakeDelivery


def test_fake_delivery_matches_the_real_class():
    """The shared `tests/delivery_stub.py` fake must expose what
    safe_send_message actually reads off the real DeliveryActivities: a
    `channel` attribute, a `db_pool` attribute and send_message(agent_id,
    message, chat_id).

    One pin for one shared class — every file that used to carry its own copy
    of this test now imports that class, so a drift here fails everywhere.
    """
    real = inspect.signature(DeliveryActivities.send_message).parameters
    fake = inspect.signature(FakeDelivery.send_message).parameters
    for name in ("agent_id", "message", "chat_id"):
        assert name in real, f"DeliveryActivities.send_message lost {name}"
        assert name in fake, f"FakeDelivery.send_message lost {name}"
    fields = set(DeliveryActivities.__dataclass_fields__)
    assert {"channel", "db_pool"} <= fields
    assert hasattr(FakeDelivery, "channel") and hasattr(FakeDelivery, "db_pool")


@pytest_asyncio.fixture(loop_scope="function")
async def clean_notif(db_pool):
    await db_pool.execute("DELETE FROM notification_log")
    yield db_pool
    await db_pool.execute("DELETE FROM notification_log")


async def test_defers_when_over_budget(clean_notif):
    d = FakeDelivery(db_pool=clean_notif, budget_enabled=True, daily_budget=0)  # cap 0 → always over
    await safe_send_message(d, agent_id="sebas", message="hi", log_event="e")
    assert d.sent == []  # deferred, not sent
    assert await clean_notif.fetchval("SELECT count(*) FROM notification_log WHERE NOT sent") == 1


async def test_sends_and_records_when_under_budget(clean_notif):
    d = FakeDelivery(db_pool=clean_notif, budget_enabled=True, daily_budget=5)
    await safe_send_message(d, agent_id="sebas", message="hi", log_event="e")
    assert d.sent == ["hi"]
    assert await clean_notif.fetchval("SELECT count(*) FROM notification_log WHERE sent") == 1


async def test_disabled_always_sends(clean_notif):
    d = FakeDelivery(db_pool=clean_notif, budget_enabled=False, daily_budget=0)
    await safe_send_message(d, agent_id="sebas", message="hi", log_event="e")
    assert d.sent == ["hi"]


class _OkFalseDelivery:
    """Comms responded, but the send itself failed (`{"ok": False, ...}`)."""

    def __init__(self, pool):
        self.db_pool = pool
        self.budget_enabled = False
        self.daily_budget = 8
        self.channel = "slack"

    async def send_message(self, *, agent_id, message, chat_id):
        return {"ok": False, "error": "comms 500"}


class _RaisingDelivery:
    def __init__(self, pool):
        self.db_pool = pool
        self.budget_enabled = False
        self.daily_budget = 8
        self.channel = "slack"

    async def send_message(self, *, agent_id, message, chat_id):
        raise RuntimeError("connection refused")


async def test_ok_false_records_as_not_sent(clean_notif):
    """The recorded row must reflect the ACTUAL outcome — an `{"ok": False}`
    result must never land as `sent=true` (issue: it used to, unconditionally)."""
    d = _OkFalseDelivery(clean_notif)
    await safe_send_message(d, agent_id="sebas", message="hi", log_event="renewal_notify_failed")
    row = await clean_notif.fetchrow(
        "SELECT sent, log_event FROM notification_log ORDER BY id DESC LIMIT 1"
    )
    assert row["sent"] is False
    assert row["log_event"] == "renewal_notify_failed_ok_false"


async def test_raised_exception_records_as_not_sent(clean_notif):
    """A raised exception must also be recorded (as a failure) — previously
    the raise path returned before recording anything at all, leaving the
    failure invisible in notification_log."""
    d = _RaisingDelivery(clean_notif)
    await safe_send_message(d, agent_id="sebas", message="hi", log_event="renewal_notify_failed")
    row = await clean_notif.fetchrow(
        "SELECT sent, log_event FROM notification_log ORDER BY id DESC LIMIT 1"
    )
    assert row["sent"] is False
    assert row["log_event"] == "renewal_notify_failed_raised"


async def test_success_records_unsuffixed_log_event(clean_notif):
    """A true success keeps the caller's log_event name as-is — only failure
    paths get a distinguishing suffix."""
    d = FakeDelivery(db_pool=clean_notif, budget_enabled=False, daily_budget=0)
    await safe_send_message(d, agent_id="sebas", message="hi", log_event="renewal_notify_failed")
    row = await clean_notif.fetchrow(
        "SELECT sent, log_event FROM notification_log ORDER BY id DESC LIMIT 1"
    )
    assert row["sent"] is True
    assert row["log_event"] == "renewal_notify_failed"
