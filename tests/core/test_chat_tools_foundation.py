"""Tests for chat tool-calling foundation: config, truncation, timeout, observability."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from aegis.config import Settings
from aegis.observability import record_tool_call
from aegis.services.chat import _truncate_result, send_message


async def test_record_tool_call():
    """record_tool_call inserts into chat_tool_calls with the real schema.

    Regression: the prior INSERT used non-existent columns (thread_id,
    tool_args, tool_result) and every write blew up silently — production
    chat_tool_calls had zero rows despite tool calls running. Pin the
    column list and arg layout to the actual table.
    """
    pool = AsyncMock()
    await record_tool_call(
        pool,
        agent_id="sebas",
        thread_id="t1",
        tool_name="list_tasks",
        tool_args={"status": "pending"},
        tool_result={"tasks": []},
        status="success",
        latency_ms=42,
    )
    pool.execute.assert_called_once()
    sql, *params = pool.execute.call_args[0]
    assert "chat_tool_calls" in sql
    assert "(agent_id, tool_name, args, result, status, latency_ms)" in sql
    assert params == ["sebas", "list_tasks", {"status": "pending"}, {"tasks": []}, "success", 42]


async def test_record_tool_call_never_raises():
    """record_tool_call swallows exceptions."""
    pool = AsyncMock()
    pool.execute.side_effect = RuntimeError("DB down")
    # Should not raise
    await record_tool_call(
        pool,
        agent_id="sebas",
        thread_id=None,
        tool_name="create_task",
        tool_args={},
        tool_result={"error": "test"},
        status="error",
        latency_ms=0,
    )


def test_truncate_short_result_unchanged():
    """Results under max_bytes pass through unchanged."""
    short = json.dumps({"ok": True})
    assert _truncate_result(short, max_bytes=4096) == short


def test_truncate_large_list():
    """Large list truncated to first 5 items with total count."""
    big_list = [{"id": i, "title": f"Item {i}"} for i in range(100)]
    result = _truncate_result(json.dumps(big_list), max_bytes=2000)
    data = json.loads(result)
    assert data["truncated"] is True
    assert data["total"] == 100
    assert len(data["results"]) == 5


def test_truncate_large_list_extreme():
    """When even smart truncation exceeds budget, return minimal summary."""
    big_list = [{"id": i, "title": f"Item {i}"} for i in range(100)]
    result = _truncate_result(json.dumps(big_list), max_bytes=50)
    data = json.loads(result)
    assert data["truncated"] is True
    assert data["total"] == 100
    assert "note" in data


def test_truncate_large_dict():
    """Large dict truncated to first 5 keys with indicator."""
    big_dict = {f"key_{i}": f"value_{i}" * 10 for i in range(50)}
    result = _truncate_result(json.dumps(big_dict), max_bytes=2000)
    data = json.loads(result)
    assert data["_truncated"] is True
    assert data["_total_keys"] == 50


def test_truncate_non_json_string():
    """Non-JSON strings truncated by raw slicing."""
    raw = "x" * 10000
    result = _truncate_result(raw, max_bytes=500)
    assert len(result) == 500


def test_truncate_list_with_huge_strings_shrinks_content_instead_of_giving_up():
    """A list of 100 items each with a 5KB string body must still surface
    SOME content (with shrink markers) rather than falling through to the
    'Results too large to display' stub."""
    big_list = [
        {"id": i, "title": f"Item {i}", "body": "LOREM " * 1000}  # ~6KB body each
        for i in range(100)
    ]
    result = _truncate_result(json.dumps(big_list), max_bytes=4096)
    data = json.loads(result)

    # Did NOT hit the minimal-summary fallback.
    assert "results" in data
    assert data["truncated"] is True
    assert data["total"] == 100
    # At least one item was preserved (with its content shrunk).
    assert len(data["results"]) >= 1
    # String shrinkage marker is visible in a kept body.
    assert any("[truncated]" in str(r.get("body", "")) for r in data["results"])


def test_truncate_dict_with_huge_values_preserves_keys():
    """A dict with a few keys but huge string values still returns the
    keys (with values shrunk) instead of the minimal summary."""
    big_dict = {f"key_{i}": "x" * 3000 for i in range(3)}
    result = _truncate_result(json.dumps(big_dict), max_bytes=4096)
    data = json.loads(result)

    assert data["_truncated"] is True
    assert data["_total_keys"] == 3
    # At least one original key survived (i.e. we didn't hit the minimal stub).
    surviving_keys = [k for k in data if k.startswith("key_")]
    assert surviving_keys, f"no original keys preserved in {list(data.keys())}"
    # Values were shrunk rather than dropped.
    assert any("[truncated]" in str(data[k]) for k in surviving_keys)


def test_truncate_shrink_marker_fits_in_budget():
    """The ellipsis marker must not push a shrunk string OVER its budget."""
    # 10 items × 1200-char bodies → 12KB. With a 4KB budget the function
    # should settle on a shrink pass that keeps items and shrinks strings.
    rows = [{"id": i, "body": "y" * 1200} for i in range(10)]
    result = _truncate_result(json.dumps(rows), max_bytes=4096)
    assert len(result.encode()) <= 4096
    data = json.loads(result)
    # Structure preserved.
    assert data["truncated"] is True
    assert data["total"] == 10
    for item in data["results"]:
        # Each body either untouched (unlikely given size) or shrunk with marker.
        assert len(item["body"]) <= 1200
        if len(item["body"]) < 1200:
            assert item["body"].endswith("[truncated]")


def test_truncate_final_fallback_when_even_one_item_too_big():
    """If even a single heavily-shrunk item exceeds the budget, fall back
    to the minimal summary."""
    # One item with a 50KB string, budget 100 bytes — even the tightest
    # shrink pass (1 item, 150-char strings) can't fit given overhead.
    rows = [{"body": "z" * 50000}] * 5
    result = _truncate_result(json.dumps(rows), max_bytes=100)
    data = json.loads(result)
    assert data["truncated"] is True
    assert data["total"] == 5
    assert data.get("note")


# --- send_message integration tests ---


@pytest.fixture
def settings():
    return Settings(
        database_url="postgresql://test:test@localhost/test",
        litellm_url="https://litellm.test/v1",
        temporal_ui_url="https://temporal.test",
        n8n_ui_url="https://n8n.test",
        admin_username="admin",
        admin_password="admin",
        n8n_webhook_secret="test-secret",
        model_balanced="test-model",
        tool_calling_enabled=True,
        tool_max_iterations=5,
        tool_result_max_bytes=4096,
        tool_timeout_seconds=30,
    )


@pytest.fixture
def mock_llm():
    llm = AsyncMock()
    llm.chat = AsyncMock(
        return_value={
            "response": "Hello!",
            "tool_calls": [],
            "model": "test-model",
            "prompt_tokens": 10,
            "completion_tokens": 5,
        }
    )
    return llm


@pytest.fixture
def mock_pool():
    pool = AsyncMock()
    pool.fetchrow.return_value = {
        "id": "sebas",
        "name": "Sebas",
        "system_prompt_path": "personalities/sebas/SOUL.md",
    }
    pool.fetch.return_value = []  # no history
    # Support `async with pool.acquire() as conn:` used by resolve_model_for_agent.
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=None)  # → falls back to 'balanced' tier
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool.acquire = MagicMock(return_value=ctx)
    return pool


async def test_send_message_uses_tier_model(mock_pool, mock_llm, settings):
    """send_message resolves model via agents.model_tier, not settings.model_balanced."""
    # mock_pool.acquire conn returns fetchval=None → 'balanced' fallback → "qwen3:14b"
    await send_message(mock_pool, mock_llm, "sebas", "hello", settings=settings)
    call_kwargs = mock_llm.chat.call_args[1]
    assert call_kwargs["model"] == "qwen3:14b"


async def test_send_message_kill_switch(mock_pool, mock_llm, settings):
    """tool_calling_enabled=False sends no tools to LLM."""
    settings.tool_calling_enabled = False
    await send_message(mock_pool, mock_llm, "sebas", "hello", settings=settings)
    call_kwargs = mock_llm.chat.call_args[1]
    assert call_kwargs["tools"] is None


async def test_send_message_malformed_json_no_crash(mock_pool, mock_llm, settings):
    """Malformed tool arguments return error to LLM, don't crash."""
    mock_llm.chat = AsyncMock(
        side_effect=[
            {
                "response": "",
                "tool_calls": [{"id": "tc1", "name": "list_tasks", "arguments": "{bad json"}],
                "model": "test-model",
                "prompt_tokens": 10,
                "completion_tokens": 5,
            },
            {
                "response": "Sorry, I had trouble with that.",
                "tool_calls": [],
                "model": "test-model",
                "prompt_tokens": 20,
                "completion_tokens": 10,
            },
        ]
    )
    result = await send_message(mock_pool, mock_llm, "sebas", "list tasks", settings=settings)
    assert result["response"] == "Sorry, I had trouble with that."


async def test_send_message_timeout(mock_pool, mock_llm, settings):
    """Tool that exceeds timeout returns error result to LLM."""
    settings.tool_timeout_seconds = 0.001  # near-instant timeout

    mock_llm.chat = AsyncMock(
        side_effect=[
            {
                "response": "",
                "tool_calls": [{"id": "tc1", "name": "list_tasks", "arguments": "{}"}],
                "model": "test-model",
                "prompt_tokens": 10,
                "completion_tokens": 5,
            },
            {
                "response": "The tool timed out, sorry.",
                "tool_calls": [],
                "model": "test-model",
                "prompt_tokens": 20,
                "completion_tokens": 10,
            },
        ]
    )
    result = await send_message(mock_pool, mock_llm, "sebas", "list tasks", settings=settings)
    assert "response" in result


async def test_send_message_injection_log_uses_v3_columns(mock_pool, mock_llm, settings):
    """The knowledge_injection_log INSERT must target v3 schema columns.

    Regression check for the v2 schema-drift bug where the INSERT silently
    failed (columns chat_exchange_id/injected_items/referenced_items don't
    exist in v3, so every write threw and was swallowed by the try/except).
    """
    kc = AsyncMock()
    kc.search.return_value = [
        {
            "title": "Runbook A",
            "summary": "Restart sequence for service X",
            "source_type": "runbook",
            "similarity": 0.9,
            "url": "ks://content/abc",
            "content_id": "abc-123",
        },
    ]
    kc.query_kg.return_value = []
    mock_llm.chat = AsyncMock(
        return_value={
            "response": "Restart sequence for service X is documented in the runbook.",
            "tool_calls": [],
            "model": "test-model",
            "prompt_tokens": 10,
            "completion_tokens": 5,
        }
    )

    await send_message(
        mock_pool,
        mock_llm,
        "raphael",
        "what's the restart sequence?",
        thread_id="thread-42",
        knowledge_connector=kc,
        settings=settings,
    )

    inj_calls = [
        c
        for c in mock_pool.execute.call_args_list
        if c.args and "INSERT INTO knowledge_injection_log" in c.args[0]
    ]
    assert inj_calls, "injection log INSERT was never issued"
    sql, agent, thread, content_ids, payload = inj_calls[0].args
    assert "(agent_id, thread_id, workflow_run_id, source, content_ids, triples_used)" in sql
    assert "'chat'" in sql
    assert agent == "raphael"
    assert thread == "thread-42"
    assert "abc-123" in content_ids
    # Pass a Python dict for the jsonb column (asyncpg encodes via the
    # registered codec). Sending a json.dumps()'d string would double-encode.
    assert isinstance(payload, dict)
    assert "injected" in payload
    assert "referenced" in payload


async def test_send_message_returns_assistant_message_id(mock_pool, mock_llm, settings):
    """send_message returns the UUID of the assistant row it inserted so the
    comms bot can patch it with the reply's outgoing message ref
    once `_send_with_html_fallback` returns. Without this id round-trip the
    chat-reply cleanup gap stays open."""
    mock_pool.fetchval = AsyncMock(
        return_value="00000000-0000-0000-0000-000000000abc"
    )
    result = await send_message(mock_pool, mock_llm, "sebas", "hi", settings=settings)
    assert result["assistant_message_id"] == "00000000-0000-0000-0000-000000000abc"


async def test_send_message_stores_user_metadata(mock_pool, mock_llm, settings):
    """Passing user_metadata causes the user chat_history row's INSERT to
    use the 5-column form including metadata — required so the comms
    bot can stash the incoming message ref on the user turn."""
    mock_pool.fetchval = AsyncMock(return_value="00000000-0000-0000-0000-000000000abc")
    meta = {"kind": "user_message", "chat_id": -100, "telegram_message_id": 9876}
    await send_message(
        mock_pool, mock_llm, "sebas", "hi", settings=settings, user_metadata=meta
    )

    user_inserts = [
        c
        for c in mock_pool.execute.call_args_list
        if c.args
        and "INSERT INTO chat_history" in c.args[0]
        and "'user'" not in c.args[0]  # parameterised, not literal
        and "metadata" in c.args[0]
    ]
    # The user-row INSERT now includes metadata. Find the one where the
    # role parameter passed is "user".
    user_with_meta = [c for c in user_inserts if "user" in c.args]
    assert user_with_meta, "user row INSERT didn't carry metadata"
    last_meta = user_with_meta[0].args[-1]
    assert last_meta["telegram_message_id"] == 9876
    assert last_meta["chat_id"] == -100


async def test_send_message_early_stops_on_repeated_tool_calls(mock_pool, mock_llm, settings):
    """Bug B: a model that calls the SAME tool with the SAME args every turn
    must NOT burn the whole iteration budget. After the repeat threshold the
    tool loop breaks and ONE final no-tools call produces a text answer —
    never the bare 'Max tool iterations reached.' placeholder."""
    settings.tool_max_iterations = 10  # high, to prove we don't loop to the cap

    same_tool_response = {
        "response": "",
        "tool_calls": [{"id": "tc1", "name": "list_tasks", "arguments": "{}"}],
        "model": "test-model",
        "prompt_tokens": 10,
        "completion_tokens": 5,
    }
    final_no_tools_response = {
        "response": "Here's what I found with the data I have.",
        "tool_calls": [],
        "model": "test-model",
        "prompt_tokens": 20,
        "completion_tokens": 10,
    }

    async def _chat(*args, **kwargs):
        # The graceful finalizer calls chat with tools=None.
        if kwargs.get("tools") is None:
            return final_no_tools_response
        return same_tool_response

    mock_llm.chat = AsyncMock(side_effect=_chat)

    result = await send_message(mock_pool, mock_llm, "sebas", "list tasks", settings=settings)

    # Did NOT return the old bare placeholder.
    assert result["response"] != "Max tool iterations reached."
    assert result["response"] == "Here's what I found with the data I have."

    # Broke early rather than looping to the 10-iteration cap.
    assert mock_llm.chat.call_count <= 5

    # Exactly one final no-tools call was made to force the text answer.
    no_tools_calls = [c for c in mock_llm.chat.call_args_list if c.kwargs.get("tools") is None]
    assert len(no_tools_calls) == 1
