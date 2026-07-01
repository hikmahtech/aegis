"""DeliveryActivities.send_voice — global AEGIS_TTS_ENABLED gate + POST shape."""

from __future__ import annotations

import json

import httpx
import respx
from aegis_worker.activities.delivery import DeliveryActivities
from temporalio.testing import ActivityEnvironment


async def test_send_voice_disabled_skips_network():
    """tts_enabled=False → no HTTP call, returns a skipped marker."""
    act = DeliveryActivities(telegram_url="http://comms", api_key="k", tts_enabled=False)
    env = ActivityEnvironment()
    result = await env.run(act.send_voice, "sebas", "hello")
    assert result == {"ok": False, "skipped": "tts_disabled"}


@respx.mock
async def test_send_voice_enabled_posts_to_comms():
    """tts_enabled=True → POSTs {text, agent_id} to /api/deliver/voice."""
    act = DeliveryActivities(telegram_url="http://comms", api_key="k", tts_enabled=True)
    env = ActivityEnvironment()
    route = respx.post("http://comms/api/deliver/voice").mock(
        return_value=httpx.Response(200, json={"ok": True, "agent_id": "maou"})
    )
    result = await env.run(act.send_voice, "maou", "your digest")
    assert route.called
    body = json.loads(route.calls.last.request.content)
    assert body == {"text": "your digest", "agent_id": "maou"}
    assert result["ok"] is True


async def test_send_voice_enabled_no_comms_url():
    act = DeliveryActivities(telegram_url="", api_key="k", tts_enabled=True)
    env = ActivityEnvironment()
    result = await env.run(act.send_voice, "sebas", "hi")
    assert result["ok"] is False
