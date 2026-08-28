"""POST /api/mcp-server/{agent_id}/gated — AEGIS enforces the approval itself.

Live E2E on 2026-08-13 (issue #294) showed a gated run executing
`mcp__aegis__capture_to_inbox` with ZERO approval cards: the claude CLI trusts
tools that arrive via an explicitly-passed `--mcp-config` in `-p` mode and never
routes them to `--permission-prompt-tool`. #287's tests asserted flag
composition, which is why they stayed green through it. These tests assert the
only thing that survives a CLI whose policy cannot be trusted: what THIS process
does when the tool call arrives.

Two invariants carry the feature and each has its own test:

* **execute-once** — one approval authorises exactly ONE execution of exactly
  those arguments (`test_an_approval_is_single_use`);
* **fail-closed** — any internal error anywhere in the gate blocks, never
  executes (`test_an_internal_error_blocks_it_never_executes`).

The pool is real and the interactions rows are real, because the gate's whole
mechanism is a row lookup plus an atomic single-use claim — a stubbed pool
would test the mock.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from aegis.api.app import create_app
from aegis.api.deps import get_settings
from aegis.api.routes import mcp_server as mcp
from aegis.config import Settings
from aegis.services import chat as chat_mod
from httpx import ASGITransport, AsyncClient

AGENT = "zzc1-gated-agent"
GATED = f"/api/mcp-server/{AGENT}/gated"
UNGATED = f"/api/mcp-server/{AGENT}"
AUTH = {"X-API-Key": "test-key"}

# One read-only tool and one mutating tool, both granted: the gate has to
# separate them by classification, not by what the agent may call.
TOOL_SET = ["whats_next", "capture_to_inbox"]
MUTATING = "capture_to_inbox"
READ_ONLY = "whats_next"
ARGS = {"text": "buy milk", "source": "chat"}


def _settings(**over) -> Settings:
    base = {
        "database_url": "postgresql://test:test@localhost/test",
        "litellm_url": "https://litellm.test/v1",
        "temporal_ui_url": "https://temporal.test",
        "n8n_ui_url": "https://n8n.test",
        "admin_username": "admin",
        "admin_password": "admin",
        "n8n_webhook_secret": "test-secret",
        "api_key": "test-key",
        "mcp_server_enabled": True,
        # Short on purpose: these tests never want to wait out a human.
        "mcp_gate_wait_seconds": 1,
    }
    base.update(over)
    return Settings(**base)


def _app(db_pool, temporal=None, settings: Settings | None = None):
    settings = settings or _settings()
    app = create_app(run_lifespan=False)
    app.dependency_overrides[get_settings] = lambda: settings
    app.state.settings = settings
    app.state.db_pool = db_pool
    app.state.temporal_client = temporal
    return app


def _never_answers():
    """A Temporal client whose InteractionFlow never resolves — i.e. the
    operator has not answered yet, which is the common case for the FIRST call
    of a gated mutation."""
    handle = MagicMock()

    async def _result():
        await asyncio.sleep(3600)

    handle.result = _result
    client = AsyncMock()
    client.start_workflow.return_value = handle
    return client


def _answers(payload: dict):
    """A Temporal client whose InteractionFlow resolves immediately."""
    handle = MagicMock()

    async def _result():
        return payload

    handle.result = _result
    client = AsyncMock()
    client.start_workflow.return_value = handle
    return client


@pytest_asyncio.fixture(loop_scope="function")
async def agent_row(db_pool):
    # The MCP surface records tool calls, a FK child of `agents`.
    await db_pool.execute("DELETE FROM chat_tool_calls WHERE agent_id = $1", AGENT)
    await db_pool.execute("DELETE FROM agents WHERE id = $1", AGENT)
    await db_pool.execute(
        "INSERT INTO agents (id, name, role, system_prompt_path, metadata, active) "
        "VALUES ($1, 'Zzc1', 'test', '', $2, true)",
        AGENT,
        {"tool_set": TOOL_SET},
    )
    yield AGENT
    await db_pool.execute("DELETE FROM interactions WHERE agent_id = $1", AGENT)
    # The MCP surface records tool calls, a FK child of `agents`.
    await db_pool.execute("DELETE FROM chat_tool_calls WHERE agent_id = $1", AGENT)
    await db_pool.execute("DELETE FROM agents WHERE id = $1", AGENT)


@pytest_asyncio.fixture(loop_scope="function")
async def executed(monkeypatch):
    """Swap the mutating tool's executor for a recorder. The list IS the
    evidence: `len(executed) == 0` is what "the gate blocked it" means."""
    calls: list[dict] = []

    async def _fake(pool, args, ctx):
        calls.append(args)
        return json.dumps({"ok": True, "task_id": "t-1"})

    monkeypatch.setitem(chat_mod.TOOL_EXECUTORS, MUTATING, _fake)
    return calls


async def _call(db_pool, temporal, *, tool=MUTATING, args=None, path=GATED, settings=None):
    app = _app(db_pool, temporal, settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.post(
            path,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": tool, "arguments": ARGS if args is None else args},
            },
            headers=AUTH,
        )
    assert resp.status_code == 200, resp.text
    return resp.json()["result"]


async def _seed_gate(db_pool, *, status: str, value: str | None, note: str = "", args=None):
    """Insert the interactions row a previous call's card would have left."""
    response = None if value is None else {"value": value, "note": note}
    row = await db_pool.fetchrow(
        "INSERT INTO interactions "
        "(flow_run_id, agent_id, kind, origin, prompt, options, status, response, metadata) "
        "VALUES ($1, $2, 'choice', $3, 'approve?', $4, $5, $6, $7) RETURNING id",
        "agent-run-gate-test",
        AGENT,
        mcp._GATE_ORIGIN,
        mcp._APPROVAL_OPTIONS,
        status,
        response,
        {"gate_key": mcp._gate_key(AGENT, MUTATING, ARGS if args is None else args)},
    )
    return str(row["id"])


def _text(result: dict) -> str:
    return result["content"][0]["text"]


# -- classification --------------------------------------------------------


def test_the_read_only_set_names_real_tools():
    """A typo or a renamed tool would silently make an entry dead — harmless
    for safety (it just gets gated) but it would quietly re-raise the card
    volume #289 exists to cut."""
    names = {t["function"]["name"] for t in chat_mod.CHAT_TOOLS}
    assert names >= mcp._READ_ONLY_TOOLS


def test_every_read_only_executor_really_is_read_only():
    """The classification is hand-curated, so this re-derives it from the
    executors' own source: an executor that GAINS a write (or a channel
    delivery) has to be removed from the set in the same PR, or CI fails.

    Deliberately crude — it is a tripwire on the allow-list, not a static
    analyser. A false positive is a prompt to re-read the executor and, if it
    really is still a read, to say so here.
    """
    writes = re.compile(
        r"(INSERT\s+INTO|UPDATE\s+\w|DELETE\s+FROM|\.execute\(|start_workflow|_deliver_documents)",
        re.I,
    )
    offenders = {}
    for name in sorted(mcp._READ_ONLY_TOOLS):
        src = inspect.getsource(chat_mod.TOOL_EXECUTORS[name])
        hits = [
            line.strip()
            for line in src.splitlines()
            if writes.search(line) and not line.strip().startswith("#")
        ]
        if hits:
            offenders[name] = hits
    assert offenders == {}


# -- the ungated endpoint is untouched --------------------------------------


async def test_the_ungated_endpoint_still_executes_mutations_directly(
    db_pool, agent_row, executed
):
    """Regression guard: gating is a property of the /gated URL, not a new
    global rule. An ungated run must keep working exactly as before."""
    result = await _call(db_pool, None, path=UNGATED)
    assert result["isError"] is False
    assert executed == [ARGS]


async def test_gated_get_is_405_and_delete_is_204(db_pool, agent_row):
    app = _app(db_pool)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        assert (await c.get(GATED, headers=AUTH)).status_code == 405
        assert (await c.delete(GATED, headers=AUTH)).status_code == 204


async def test_the_permission_tool_is_served_on_the_gated_endpoint_too(db_pool, agent_row):
    """`approve_tool_use` still gates the CLI's BUILT-IN tools (Bash/Edit do
    reach it), so it has to be advertised on the URL a gated run mounts."""
    app = _app(db_pool)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.post(
            GATED, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers=AUTH
        )
    names = {t["name"] for t in resp.json()["result"]["tools"]}
    assert "approve_tool_use" in names
    assert {READ_ONLY, MUTATING} <= names


# -- the gate ---------------------------------------------------------------


async def test_a_read_only_tool_needs_no_approval(db_pool, agent_row, monkeypatch):
    """The point of the classification: `whats_next` must not interrupt anyone."""
    ran: list[dict] = []

    async def _fake(pool, args, ctx):
        ran.append(args)
        return "[]"

    monkeypatch.setitem(chat_mod.TOOL_EXECUTORS, READ_ONLY, _fake)
    temporal = _never_answers()

    result = await _call(db_pool, temporal, tool=READ_ONLY, args={"limit": 3})

    assert result["isError"] is False
    assert ran == [{"limit": 3}]
    temporal.start_workflow.assert_not_awaited()
    # And no card was left behind.
    assert await db_pool.fetchval(
        "SELECT count(*) FROM interactions WHERE agent_id = $1", AGENT
    ) == 0


async def test_a_mutating_tool_raises_a_card_and_tells_the_model_to_retry(
    db_pool, agent_row, executed
):
    """The first call of a mutating tool executes NOTHING. It gets a card and
    an instruction, because the CLI abandons an MCP call at ~60s and cannot be
    made to wait for a human."""
    temporal = _never_answers()

    result = await _call(db_pool, temporal)

    assert result["isError"] is True
    assert executed == [], "the gate must not execute a call nobody approved"
    text = _text(result)
    assert "Pending operator approval" in text
    assert "Retry this exact tool call" in text
    # The instruction must demand IMMEDIATE chained retries and never name a
    # wait interval: in one-shot -p mode a model cannot wait (live E2E showed
    # it backgrounding `sleep 60` and ending its turn — run over, approval
    # never consumed). The server holds each attempt open instead.
    assert "IMMEDIATELY" in text
    assert "60 seconds" not in text, "a timed-retry instruction regressed — see gated E2E v2"
    temporal.start_workflow.assert_awaited_once()
    payload = temporal.start_workflow.await_args.args[1]
    assert payload["origin"] == "agent_run_gate"
    assert payload["metadata"]["gate_key"] == mcp._gate_key(AGENT, MUTATING, ARGS)
    # The operator has to be told the contract too — they are approving a
    # RETRY, not releasing a blocked call.
    assert "retry" in payload["prompt"].lower()
    assert "single use" in payload["prompt"].lower()


async def test_an_approved_card_executes_on_the_retry(db_pool, agent_row, executed):
    """The whole protocol end to end: the approval from the previous call is
    found by (agent, tool, args) and the tool finally runs."""
    interaction_id = await _seed_gate(db_pool, status="resolved", value="approve")

    result = await _call(db_pool, _never_answers())

    assert result["isError"] is False
    assert json.loads(_text(result)) == {"ok": True, "task_id": "t-1"}
    assert executed == [ARGS]
    consumed = await db_pool.fetchval(
        "SELECT metadata->>'gate_consumed_at' FROM interactions WHERE id = $1::uuid",
        interaction_id,
    )
    assert consumed is not None, "an executed approval must be marked consumed"


async def test_an_approval_is_single_use(db_pool, agent_row, executed):
    """INVARIANT: one approval authorises exactly ONE execution.

    Falsifiable — drop the `_claim_gate_approval` call (or its
    `gate_consumed_at IS NULL` predicate) and the second call finds the same
    approved row and executes again, so `executed` has two entries.
    """
    await _seed_gate(db_pool, status="resolved", value="approve")

    first = await _call(db_pool, _never_answers())
    second = await _call(db_pool, _never_answers())

    assert first["isError"] is False
    assert second["isError"] is True
    assert executed == [ARGS], "the approval was spent by the first execution"
    # The second call is told to get a fresh approval, not that it succeeded.
    assert "Pending operator approval" in _text(second)


async def test_an_approval_is_bound_to_the_exact_arguments(db_pool, agent_row, executed):
    """An approval covers the arguments the human READ. Change one and it is a
    different call needing a different card."""
    await _seed_gate(db_pool, status="resolved", value="approve")

    result = await _call(db_pool, _never_answers(), args={**ARGS, "text": "wire $5000"})

    assert result["isError"] is True
    assert executed == []


async def test_argument_order_is_not_a_different_call(db_pool, agent_row, executed):
    """...but a re-serialised retry is the SAME call: the hash is canonical, so
    key order can't cost the operator a second card."""
    await _seed_gate(db_pool, status="resolved", value="approve")

    reordered = dict(reversed(list(ARGS.items())))
    assert list(reordered) != list(ARGS)
    result = await _call(db_pool, _never_answers(), args=reordered)

    assert result["isError"] is False
    assert executed == [reordered]


async def test_a_denial_is_permanent_and_carries_the_note(db_pool, agent_row, executed):
    await _seed_gate(db_pool, status="resolved", value="deny", note="not on prod data")

    first = await _call(db_pool, _never_answers())
    second = await _call(db_pool, _never_answers())

    assert first["isError"] is True
    assert "not on prod data" in _text(first)
    assert "do NOT retry" in _text(first)
    # A denial is not consumed, so the retry finds the same verdict rather than
    # raising a fresh card at the operator.
    assert "Denied by operator" in _text(second)
    assert executed == []


async def test_an_archived_card_says_the_approval_expired(db_pool, agent_row, executed):
    """Nobody answered within the 15-minute window: the run is told to start
    over rather than being left to poll a dead card."""
    await _seed_gate(db_pool, status="archived", value=None)

    result = await _call(db_pool, _never_answers())

    assert result["isError"] is True
    assert "expired" in _text(result)
    assert executed == []


async def test_a_pending_card_is_waited_on_then_reported_as_pending(
    db_pool, agent_row, executed, monkeypatch
):
    """A retry that arrives while the operator is still deciding re-reads the
    row for the wait budget before answering."""
    monkeypatch.setattr(mcp, "_GATE_POLL_INTERVAL_S", 0.05)
    await _seed_gate(db_pool, status="pending", value=None)
    temporal = _never_answers()

    result = await _call(db_pool, temporal)

    assert result["isError"] is True
    assert "Pending operator approval" in _text(result)
    assert executed == []
    # It waited on the EXISTING card instead of raising a second one.
    temporal.start_workflow.assert_not_awaited()


async def test_an_approval_answered_within_the_wait_executes_immediately(
    db_pool, agent_row, executed
):
    """The fast path: the operator answers while the first call is still open,
    so no retry is needed at all."""
    interaction_id = await _seed_gate(db_pool, status="resolved", value="approve")
    # The card the flow "just raised" resolves to that row.
    temporal = _answers(
        {
            "interaction_id": interaction_id,
            "status": "resolved",
            "response": {"value": "approve"},
        }
    )
    # Hide the seeded row from the lookup so the create-path is exercised.
    await db_pool.execute(
        "UPDATE interactions SET metadata = metadata - 'gate_key' WHERE id = $1::uuid",
        interaction_id,
    )

    result = await _call(db_pool, temporal)

    assert result["isError"] is False
    assert executed == [ARGS]
    temporal.start_workflow.assert_awaited_once()


# -- fail-closed ------------------------------------------------------------


async def test_an_internal_error_blocks_it_never_executes(db_pool, agent_row, executed, monkeypatch):
    """INVARIANT: any failure inside the gate blocks the call.

    Falsifiable — map the exception to "allow" (or move the `_gate_tool_call`
    call after `_execute_tool`) and `executed` gains an entry.
    """

    async def _boom(pool, gate_key):
        raise RuntimeError("interactions table is on fire")

    monkeypatch.setattr(mcp, "_read_gate_row", _boom)

    result = await _call(db_pool, _never_answers())

    assert result["isError"] is True
    assert executed == []
    assert "nothing was executed" in _text(result)


async def test_no_temporal_client_blocks_rather_than_executing(db_pool, agent_row, executed):
    """Nobody to ask ⇒ nothing runs. (The ungated endpoint would have run it —
    that is the whole difference between the two URLs.)"""
    result = await _call(db_pool, None)

    assert result["isError"] is True
    assert executed == []
    assert "nothing was executed" in _text(result)


async def test_a_resolved_card_with_no_response_is_not_an_approval(db_pool, agent_row, executed):
    """Only an explicit approve value opens the gate; a malformed row denies."""
    await _seed_gate(db_pool, status="resolved", value=None)

    result = await _call(db_pool, _never_answers())

    assert result["isError"] is True
    assert executed == []


@pytest.mark.parametrize("value", ["approve", "approved", "allow"])
async def test_the_approve_vocabulary_matches_the_cards_buttons(
    db_pool, agent_row, executed, value
):
    """Slack and the admin panel each emit their own value for the same tap;
    the gate has to accept every one the card can produce."""
    await _seed_gate(db_pool, status="resolved", value=value)

    result = await _call(db_pool, _never_answers())

    assert result["isError"] is False
    assert executed == [ARGS]
