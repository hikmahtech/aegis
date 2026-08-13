"""dispatch_agent_run — the chat-side door to the heavy lane (AgentRunFlow).

Core never imports worker code, so the only contract to lock is the one that
crosses the boundary: the workflow NAME, the task queue, and the arg dict the
flow's dataclass is built from.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from aegis.services.chat import CHAT_TOOLS, TOOL_EXECUTORS, ToolContext, _execute_tool


def _client() -> AsyncMock:
    client = AsyncMock()
    handle = MagicMock()
    handle.id = "agent-run-deadbeef"
    client.start_workflow.return_value = handle
    return client


async def test_dispatch_starts_agent_run_flow_with_agent_and_prompt():
    pool = AsyncMock()
    client = _client()
    ctx = ToolContext(agent_id="pandoras-actor", temporal_client=client)

    result = await _execute_tool(
        pool,
        "dispatch_agent_run",
        {
            "prompt": "Trace why gmail-ingest drops attachments.",
            "repo": "aegis",
            "engine": "claude",
            "purpose": "gmail attachment trace",
        },
        ctx,
    )

    client.start_workflow.assert_called_once()
    args, kwargs = client.start_workflow.call_args
    assert args[0] == "AgentRunFlow"
    payload = args[1]
    # agent_id comes from the calling agent's context, never from the model.
    assert payload["agent_id"] == "pandoras-actor"
    assert payload["prompt"] == "Trace why gmail-ingest drops attachments."
    assert payload["repo"] == "aegis"
    assert payload["engine"] == "claude"
    assert payload["purpose"] == "gmail attachment trace"
    assert kwargs["task_queue"] == "aegis-main"
    assert kwargs["id"].startswith("agent-run-")

    assert kwargs["id"] in result
    assert "results will land in this channel" in result


async def test_dispatch_omits_repo_as_none_so_the_flow_picks_scratch():
    pool = AsyncMock()
    client = _client()
    ctx = ToolContext(agent_id="sebas", temporal_client=client)

    await _execute_tool(pool, "dispatch_agent_run", {"prompt": "research X"}, ctx)

    payload = client.start_workflow.call_args[0][1]
    assert payload["repo"] is None
    assert payload["engine"] == ""


async def test_dispatch_without_temporal_client_returns_error_text():
    pool = AsyncMock()
    ctx = ToolContext(agent_id="sebas", temporal_client=None)
    result = await _execute_tool(pool, "dispatch_agent_run", {"prompt": "x"}, ctx)
    assert "Temporal client not available" in result


async def test_dispatch_rejects_unknown_engine_without_starting_anything():
    pool = AsyncMock()
    client = _client()
    ctx = ToolContext(agent_id="sebas", temporal_client=client)
    result = await _execute_tool(
        pool, "dispatch_agent_run", {"prompt": "x", "engine": "gpt"}, ctx
    )
    assert "unknown engine" in result
    client.start_workflow.assert_not_called()


async def test_dispatch_requires_a_prompt():
    pool = AsyncMock()
    client = _client()
    ctx = ToolContext(agent_id="sebas", temporal_client=client)
    result = await _execute_tool(pool, "dispatch_agent_run", {"prompt": "   "}, ctx)
    assert "prompt is required" in result
    client.start_workflow.assert_not_called()


async def test_dispatch_failure_is_reported_not_raised():
    """Temporal being down must come back as a chat answer, not blow up the
    tool loop mid-reply."""
    pool = AsyncMock()
    client = AsyncMock()
    client.start_workflow.side_effect = RuntimeError("temporal frontend unreachable")
    ctx = ToolContext(agent_id="sebas", temporal_client=client)

    result = await _execute_tool(pool, "dispatch_agent_run", {"prompt": "x"}, ctx)
    assert "temporal frontend unreachable" in result


async def test_dispatch_forwards_gated_to_the_flow():
    """`gated` is the human-in-the-loop switch. Core never imports worker code,
    so this dict key is the entire contract — drop it and the model's request
    for approval cards silently produces a full-auto run."""
    pool = AsyncMock()
    client = _client()
    ctx = ToolContext(agent_id="sebas", temporal_client=client)

    await _execute_tool(
        pool,
        "dispatch_agent_run",
        {"prompt": "Fix the flaky test", "engine": "claude", "gated": True},
        ctx,
    )
    assert client.start_workflow.call_args[0][1]["gated"] is True


async def test_dispatch_defaults_to_ungated():
    """Falsifiability control for the test above: the gate is opt-in, and an
    omitted `gated` must cross as a real False rather than a missing key."""
    pool = AsyncMock()
    client = _client()
    ctx = ToolContext(agent_id="sebas", temporal_client=client)

    await _execute_tool(pool, "dispatch_agent_run", {"prompt": "research X"}, ctx)
    payload = client.start_workflow.call_args[0][1]
    assert "gated" in payload
    assert payload["gated"] is False


async def test_dispatch_forwards_an_explicit_timeout_minutes():
    """The caller's watch window reaches the flow verbatim, gated or not."""
    pool = AsyncMock()
    client = _client()
    ctx = ToolContext(agent_id="sebas", temporal_client=client)

    await _execute_tool(
        pool, "dispatch_agent_run", {"prompt": "long audit", "timeout_minutes": 90}, ctx
    )
    assert client.start_workflow.call_args[0][1]["timeout_minutes"] == 90


async def test_gated_run_without_an_explicit_timeout_gets_the_longer_window():
    """A gated run spends most of its window BLOCKED on a human: each approval
    card holds the CLI for up to 9 minutes, so 3-4 questions exhaust the flow's
    30-minute default while the run is still working and still raising cards —
    the flow stops watching and the transcript is never delivered."""
    pool = AsyncMock()
    client = _client()
    ctx = ToolContext(agent_id="sebas", temporal_client=client)

    await _execute_tool(
        pool,
        "dispatch_agent_run",
        {"prompt": "Refactor the retry policy", "engine": "claude", "gated": True},
        ctx,
    )
    assert client.start_workflow.call_args[0][1]["timeout_minutes"] == 120


async def test_ungated_run_keeps_the_short_default():
    """Falsifiability control: the longer window is tied to `gated`, not to
    everything — a blanket 120 would fail here."""
    pool = AsyncMock()
    client = _client()
    ctx = ToolContext(agent_id="sebas", temporal_client=client)

    await _execute_tool(pool, "dispatch_agent_run", {"prompt": "research X"}, ctx)
    assert client.start_workflow.call_args[0][1]["timeout_minutes"] == 30


async def test_an_explicit_timeout_wins_over_the_gated_default():
    pool = AsyncMock()
    client = _client()
    ctx = ToolContext(agent_id="sebas", temporal_client=client)

    await _execute_tool(
        pool,
        "dispatch_agent_run",
        {"prompt": "x", "engine": "claude", "gated": True, "timeout_minutes": 240},
        ctx,
    )
    assert client.start_workflow.call_args[0][1]["timeout_minutes"] == 240


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(1, 5), (9999, 240), ("nonsense", 30), (None, 30)],
)
async def test_out_of_range_timeouts_are_clamped_never_passed_through(raw, expected):
    """The schema bounds this on the validated paths; the executor clamps so an
    unvalidated caller cannot start a 3-second or a 7-day watch."""
    pool = AsyncMock()
    client = _client()
    ctx = ToolContext(agent_id="sebas", temporal_client=client)

    await _execute_tool(pool, "dispatch_agent_run", {"prompt": "x", "timeout_minutes": raw}, ctx)
    assert client.start_workflow.call_args[0][1]["timeout_minutes"] == expected


def test_timeout_minutes_is_advertised_with_its_bounds():
    """The model can only choose a window it can see, and only a bounded one is
    safe to expose (an unbounded value is a workflow that watches forever)."""
    schema = next(t for t in CHAT_TOOLS if t["function"]["name"] == "dispatch_agent_run")
    spec = schema["function"]["parameters"]["properties"]["timeout_minutes"]
    assert spec["type"] == "integer"
    assert (spec["minimum"], spec["maximum"]) == (5, 240)
    assert "timeout_minutes" not in schema["function"]["parameters"]["required"]


def test_gated_is_advertised_as_a_boolean():
    """A knob the model cannot see is a feature that never gets used."""
    schema = next(t for t in CHAT_TOOLS if t["function"]["name"] == "dispatch_agent_run")
    gated = schema["function"]["parameters"]["properties"]["gated"]
    assert gated["type"] == "boolean"
    assert "approval" in gated["description"].lower()
    # Opt-in: it must never become a required argument.
    assert "gated" not in schema["function"]["parameters"]["required"]


def test_tool_is_advertised_and_dispatchable():
    """A schema with no executor is never dispatched; an executor with no
    schema is never advertised. Both halves have to exist."""
    schemas = [t for t in CHAT_TOOLS if t["function"]["name"] == "dispatch_agent_run"]
    assert len(schemas) == 1
    assert "dispatch_agent_run" in TOOL_EXECUTORS
    params = schemas[0]["function"]["parameters"]
    assert params["required"] == ["prompt"]
    assert params["properties"]["engine"]["enum"] == ["claude", "kimi"]
