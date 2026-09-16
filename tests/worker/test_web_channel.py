"""Web channel (OSS Phase C): interaction cards land in the admin inbox and
proactive FYIs no-op when channel != slack — no external chat service needed."""

from __future__ import annotations

from aegis_worker.activities.delivery import DeliveryActivities, safe_send_message

from tests.delivery_stub import FakeDelivery


async def test_interaction_card_web_returns_web_ref():
    d = DeliveryActivities(channel="web", comms_url="http://comms:8081")
    r = await d.send_interaction_card("iid", "sebas", "approval", "Approve?", {"yes": "Yes"})
    assert r == {"ok": True, "delivery_ref": {"adapter": "web"}}


async def test_interaction_card_web_when_no_comms_url():
    # Even configured for slack, a missing comms URL falls back to the web ref.
    d = DeliveryActivities(channel="slack", comms_url="")
    r = await d.send_interaction_card("iid", "sebas", "approval", "Approve?", {})
    assert r["delivery_ref"]["adapter"] == "web"


async def test_safe_send_skips_for_web():
    d = FakeDelivery("web")
    await safe_send_message(d, agent_id="sebas", message="hi", log_event="e")
    assert d.sent == []  # no external push on the web channel


async def test_safe_send_sends_for_slack():
    d = FakeDelivery("slack")
    await safe_send_message(d, agent_id="sebas", message="hi", log_event="e")
    assert d.sent == ["hi"]
