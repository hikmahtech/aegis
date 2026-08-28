"""`synthesize_agent_reply` must hand `send_message` every dependency it takes.

That function is the entry point for the Slack DM and Todoist-comment surfaces,
and its docstring promises they "behave identically to a web chat". For months
they did not: `settings` and four connectors were silently dropped, so those
surfaces built a half-populated ToolContext. Nothing raised — the tools just
degraded, differently from the admin UI, which is the hardest kind of bug to
notice. `aegis_self_diagnose` returned "settings not threaded into ToolContext"
and the knowledge / money / search / vercel tools ran connector-less.

The signature test is the point: it fails when a NEW dependency is added to
`send_message` and not forwarded, which is exactly how this happened.
"""

from __future__ import annotations

import inspect
from unittest.mock import AsyncMock, patch

from aegis.services.chat import send_message, synthesize_agent_reply

# Arguments `send_message` takes that are NOT dependencies to forward:
# call-shape parameters that this surface sets itself.
_NOT_FORWARDED = {
    "pool",
    "llm_client",
    "agent_id",
    "message",
    "thread_id",
    "user_metadata",  # built here from task_id / surface
    "background_tasks",
    "tier_override",
}


def test_agent_reply_accepts_every_dependency_send_message_takes():
    send_params = set(inspect.signature(send_message).parameters)
    reply_params = set(inspect.signature(synthesize_agent_reply).parameters)
    expected = send_params - _NOT_FORWARDED
    missing = expected - reply_params
    assert not missing, (
        f"synthesize_agent_reply cannot accept {sorted(missing)}, so the Slack and "
        "Todoist-comment surfaces will build a weaker ToolContext than the admin UI"
    )


async def test_agent_reply_forwards_every_dependency():
    """Accepting them is not enough — they must actually reach send_message."""
    sentinels = {
        "settings": object(),
        "temporal_client": object(),
        "knowledge_connector": object(),
        "finance_connector": object(),
        "search_connector": object(),
        "remote_script_connector": object(),
        "vercel_connector": object(),
        "mcp_manager": object(),
    }
    fake = AsyncMock(
        return_value={"response": "ok", "model": "m", "tool_calls": [], "error": None}
    )
    with patch("aegis.services.chat.send_message", fake):
        await synthesize_agent_reply(
            pool=None,
            llm_client=None,
            agent_id="sebas",
            message="hi",
            thread_id="t1",
            **sentinels,
        )

    forwarded = fake.await_args.kwargs
    for name, value in sentinels.items():
        assert name in forwarded, f"{name} was not forwarded to send_message"
        assert forwarded[name] is value, f"{name} was forwarded but not intact"


async def test_a_dropped_dependency_would_be_caught():
    """Control: the forwarding assertion is only meaningful if a miss fails it."""
    fake = AsyncMock(
        return_value={"response": "ok", "model": "m", "tool_calls": [], "error": None}
    )
    with patch("aegis.services.chat.send_message", fake):
        await synthesize_agent_reply(
            pool=None, llm_client=None, agent_id="sebas", message="hi", thread_id="t1"
        )
    # Called with no dependencies, every one arrives as None — which is exactly
    # the broken state this test file exists to prevent shipping again.
    assert fake.await_args.kwargs["settings"] is None
