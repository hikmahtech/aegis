"""Untyped dict → dataclass seams: every key must be a real field.

Core never imports worker code, so a workflow is started by NAME with a plain
dict and Temporal's data converter fills the dataclass. That converter
**silently ignores an unknown key** (verified against temporalio 1.30.0), so a
typo or a rename on one side of the seam does not error anywhere — the field
just takes its default. In this lane the defaults are exactly the dangerous
values: `gated` defaults to False (a mistyped `"gate": True` is an UNGATED run
reporting success) and `timeout_seconds` to a day.

These tests live in `tests/worker/` because they are the only place both
packages are importable at once — that is the whole point: nothing else in the
repo compares the two sides of these seams.
"""

from __future__ import annotations

import dataclasses
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aegis.api.routes.mcp_server import _handle_approve_tool_use
from aegis.services.chat import _exec_dispatch_agent_run
from aegis.services.tools.base import ToolContext
from aegis_worker.flows.agent_run import AgentRunInput
from aegis_worker.flows.interaction import InteractionFlowInput


def _field_names(cls) -> set[str]:
    return {f.name for f in dataclasses.fields(cls)}


def _capturing_client() -> AsyncMock:
    client = AsyncMock()
    client.start_workflow.return_value = MagicMock()
    return client


def _ctx(client) -> ToolContext:
    return ToolContext(
        agent_id="sebas",
        task_id=None,
        knowledge_connector=None,
        finance_connector=None,
        chat_context=None,
        settings=SimpleNamespace(),
        temporal_client=client,
    )


def _start_payload(client) -> dict:
    client.start_workflow.assert_awaited_once()
    args, _ = client.start_workflow.call_args
    assert isinstance(args[1], dict), "the seam under test is a dict, not a typed arg"
    return args[1]


@pytest.mark.asyncio
async def test_dispatch_agent_run_payload_keys_are_all_agent_run_input_fields():
    """`dispatch_agent_run` → `AgentRunFlow(AgentRunInput)`.

    Falsifiable both ways: rename a field on `AgentRunInput` (or misspell a key
    in the executor) and the set-difference below is non-empty. Without this
    test the same mistake produces a workflow that starts, runs, and reports
    success with that setting silently reverted to its default.
    """
    client = _capturing_client()
    out = await _exec_dispatch_agent_run(
        None,
        {
            "prompt": "audit the retry policy",
            "repo": "bcp",
            "engine": "claude",
            "purpose": "retry audit",
            "gated": True,
            "timeout_minutes": 45,
        },
        _ctx(client),
    )
    assert "Dispatched agent run" in out

    payload = _start_payload(client)
    unknown = set(payload) - _field_names(AgentRunInput)
    assert unknown == set(), f"keys Temporal would silently drop: {sorted(unknown)}"

    # The two settings whose silent loss is invisible in the result, pinned by
    # value as well as by name.
    assert payload["gated"] is True
    assert payload["timeout_minutes"] == 45


@pytest.mark.asyncio
async def test_approval_card_payload_keys_are_all_interaction_flow_input_fields():
    """The gated-run approval card → `InteractionFlow(InteractionFlowInput)`.

    A dropped `timeout_policy` here would leave the card on the flow's default
    policy and a dropped `options` would render a card with no buttons — both
    of which look like a working gate right up to the moment someone has to
    answer one.
    """
    client = _capturing_client()
    client.start_workflow.return_value.result = AsyncMock(
        return_value={"status": "resolved", "response": {"value": "deny"}}
    )
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(temporal_client=client)))

    resp = await _handle_approve_tool_use(
        request, "sebas", 1, {"tool_name": "Bash", "input": {"command": "ls"}}
    )
    decision = json.loads(json.loads(resp.body)["result"]["content"][0]["text"])
    assert decision["behavior"] == "deny"

    payload = _start_payload(client)
    unknown = set(payload) - _field_names(InteractionFlowInput)
    assert unknown == set(), f"keys Temporal would silently drop: {sorted(unknown)}"

    assert payload["kind"] == "choice"
    assert payload["timeout_policy"] == "archive"
    assert set(payload["options"]) == {"approve", "deny"}


def test_the_converter_really_does_ignore_unknown_keys():
    """The premise, asserted rather than assumed: this is why the two tests
    above are worth having. If a future temporalio started REJECTING unknown
    keys, this fails and the seam guards become redundant."""
    from temporalio.converter import DataConverter

    converter = DataConverter.default
    payloads = converter.payload_converter.to_payloads(
        [{"agent_id": "sebas", "prompt": "x", "gate": True}]
    )
    (restored,) = converter.payload_converter.from_payloads(payloads, [AgentRunInput])

    assert isinstance(restored, AgentRunInput)
    assert restored.gated is False, "unknown key silently dropped to the default"
