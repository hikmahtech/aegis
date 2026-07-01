"""Web channel (OSS Phase C): interaction cards land in the admin inbox and
proactive FYIs no-op when channel != slack — no external chat service needed."""

from __future__ import annotations

from aegis_worker.activities.delivery import DeliveryActivities, safe_send_telegram


async def test_interaction_card_web_returns_web_ref():
    d = DeliveryActivities(channel="web", telegram_url="http://comms:8081")
    r = await d.send_interaction_card("iid", "sebas", "approval", "Approve?", {"yes": "Yes"})
    assert r == {"ok": True, "delivery_ref": {"adapter": "web"}}


async def test_interaction_card_web_when_no_comms_url():
    # Even configured for slack, a missing comms URL falls back to the web ref.
    d = DeliveryActivities(channel="slack", telegram_url="")
    r = await d.send_interaction_card("iid", "sebas", "approval", "Approve?", {})
    assert r["delivery_ref"]["adapter"] == "web"


class _FakeDelivery:
    def __init__(self, channel: str):
        self.channel = channel
        self.db_pool = None
        self.sent: list[str] = []

    async def send_telegram(self, *, agent_id, message, chat_id):
        self.sent.append(message)
        return {"ok": True}


async def test_safe_send_skips_for_web():
    d = _FakeDelivery("web")
    await safe_send_telegram(d, agent_id="sebas", message="hi", log_event="e")
    assert d.sent == []  # no external push on the web channel


async def test_safe_send_sends_for_slack():
    d = _FakeDelivery("slack")
    await safe_send_telegram(d, agent_id="sebas", message="hi", log_event="e")
    assert d.sent == ["hi"]
