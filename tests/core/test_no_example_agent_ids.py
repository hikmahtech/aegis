"""Core finds no agent by an example id (#579).

A call with no calling agent resolves the capability-tag holder; a start with
no agent runs as the flow's `activities` row owner; and the chat front door
routes a message that names nobody instead of refusing it.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import aegis.api.routes.chat as chat_route
import pytest
from aegis.api.app import create_app
from aegis.api.deps import get_settings
from aegis.api.routes._flow_trigger import start_named_workflow
from aegis.config import Settings
from aegis.services.chat import ToolContext, _exec_create_schedule, _exec_dispatch_agent_run
from aegis.services.workflows import trigger_workflow, with_owner, workflow_owner
from fastapi.testclient import TestClient

# --- the chat front door --------------------------------------------------------


@pytest.fixture
def client():
    settings = Settings(
        database_url="postgresql://test:test@localhost/test",
        litellm_url="https://litellm.test/v1",
        temporal_ui_url="https://temporal.test",
        n8n_ui_url="https://n8n.test",
        admin_username="admin",
        admin_password="admin",
        n8n_webhook_secret="test-secret",
        api_key="test-key",
    )
    app = create_app(run_lifespan=False)
    app.dependency_overrides[get_settings] = lambda: settings
    app.state.db_pool = AsyncMock()
    app.state.settings = settings
    app.state.llm = None
    return TestClient(app, headers={"X-API-Key": "test-key"})


def test_a_message_naming_no_agent_is_routed_and_the_answer_says_who(client, monkeypatch):
    seen: dict = {}

    async def classify(message, llm, settings, pool=None):
        return {"agent_id": "jeeves", "method": "default"}

    async def send(pool, llm, agent_id, message, **kwargs):
        seen["agent_id"] = agent_id
        return {"response": "hello", "assistant_message_id": None}

    monkeypatch.setattr(chat_route, "classify_intent", classify)
    monkeypatch.setattr(chat_route, "send_message", send)
    r = client.post("/api/chat", json={"message": "hi", "agent_id": ""})
    assert r.status_code == 200
    assert r.json()["agent_id"] == "jeeves"
    assert seen["agent_id"] == "jeeves"


def test_nobody_to_route_to_is_a_400_that_says_why(client, monkeypatch):
    async def classify(*args, **kwargs):
        return {"agent_id": "", "method": "default"}

    monkeypatch.setattr(chat_route, "classify_intent", classify)
    r = client.post("/api/chat", json={"message": "hi"})
    assert r.status_code == 400
    assert "gtd" in r.json()["detail"]


def test_a_named_agent_is_not_rerouted(client, monkeypatch):
    classify = AsyncMock()

    async def send(pool, llm, agent_id, message, **kwargs):
        return {"response": "ok"}

    monkeypatch.setattr(chat_route, "classify_intent", classify)
    monkeypatch.setattr(chat_route, "send_message", send)
    r = client.post("/api/chat", json={"message": "hi", "agent_id": "sebas"})
    assert r.json()["agent_id"] == "sebas"
    classify.assert_not_awaited()


def test_a_message_is_still_required(client):
    assert client.post("/api/chat", json={"agent_id": "sebas"}).status_code == 400


# --- tools called with no calling agent ----------------------------------------


def _pool(*fetch_results) -> AsyncMock:
    pool = AsyncMock()
    pool.fetch = AsyncMock(side_effect=list(fetch_results))
    return pool


async def test_dispatch_with_no_calling_agent_runs_as_the_gtd_holder():
    temporal = AsyncMock()
    ctx = ToolContext(agent_id=None, temporal_client=temporal)
    out = await _exec_dispatch_agent_run(_pool([{"id": "jeeves"}]), {"prompt": "look"}, ctx)
    assert "Dispatched" in out
    assert temporal.start_workflow.await_args.args[1]["agent_id"] == "jeeves"


async def test_dispatch_with_nobody_holding_gtd_refuses_rather_than_guessing():
    temporal = AsyncMock()
    ctx = ToolContext(agent_id=None, temporal_client=temporal)
    out = await _exec_dispatch_agent_run(_pool([]), {"prompt": "look"}, ctx)
    assert "gtd" in out
    temporal.start_workflow.assert_not_awaited()


async def test_a_schedule_with_no_calling_agent_is_owned_by_the_gtd_holder():
    pool = _pool([{"workflow_type": "DailyBriefingFlow"}], [{"id": "jeeves"}])
    pool.fetchrow = AsyncMock(
        return_value={
            "slug": "s",
            "workflow_type": "DailyBriefingFlow",
            "agent_id": "jeeves",
            "schedule_cron": "0 9 * * *",
        }
    )
    args = {"workflow_type": "DailyBriefingFlow", "cron": "0 9 * * *", "slug": "s"}
    await _exec_create_schedule(pool, args, ToolContext(agent_id=None))
    assert pool.fetchrow.await_args.args[3] == "jeeves"


async def test_a_schedule_with_nobody_holding_gtd_is_refused():
    pool = _pool([{"workflow_type": "DailyBriefingFlow"}], [])
    pool.fetchrow = AsyncMock()
    args = {"workflow_type": "DailyBriefingFlow", "cron": "0 9 * * *"}
    out = json.loads(await _exec_create_schedule(pool, args, ToolContext(agent_id=None)))
    assert "gtd" in out["error"]
    pool.fetchrow.assert_not_awaited()


# --- a start with no agent runs as the flow's owner ------------------------------


async def test_workflow_owner_reads_the_activities_row():
    pool = AsyncMock()
    pool.fetchval = AsyncMock(return_value="jeeves")
    assert await workflow_owner(pool, "MoneyBriefFlow") == "jeeves"


@pytest.mark.parametrize("value", [None, "", MagicMock()])
async def test_workflow_owner_is_none_without_a_usable_row(value):
    pool = AsyncMock()
    pool.fetchval = AsyncMock(return_value=value)
    assert await workflow_owner(pool, "X") is None


async def test_workflow_owner_never_raises():
    pool = AsyncMock()
    pool.fetchval = AsyncMock(side_effect=RuntimeError("db down"))
    assert await workflow_owner(pool, "X") is None
    assert await workflow_owner(None, "X") is None


def test_with_owner_fills_only_a_missing_agent():
    assert with_owner(None, "jeeves") == {"agent_id": "jeeves"}
    assert with_owner({"agent_id": "maou"}, "jeeves") == {"agent_id": "maou"}
    assert with_owner({"x": 1}, None) == {"x": 1}


async def test_a_chat_trigger_runs_as_the_flow_owner():
    pool = AsyncMock()
    pool.fetch = AsyncMock(return_value=[{"workflow_type": "MoneyBriefFlow"}])
    pool.fetchval = AsyncMock(return_value="jeeves")
    client = AsyncMock()
    client.start_workflow = AsyncMock(return_value=MagicMock(id="w"))
    await trigger_workflow(client, pool, "MoneyBriefFlow", None)
    assert client.start_workflow.await_args.kwargs["arg"] == {"agent_id": "jeeves"}


async def test_a_manual_route_start_runs_as_the_flow_owner():
    pool = AsyncMock()
    pool.fetchval = AsyncMock(return_value="jeeves")
    client = AsyncMock()
    await start_named_workflow("brief", {}, client, {"brief": "MoneyBriefFlow"}, pool=pool)
    assert client.start_workflow.await_args.args[1] == {"agent_id": "jeeves"}
