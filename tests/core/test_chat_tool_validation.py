"""jsonschema-based validation of tool-call args with one-shot retry."""

from __future__ import annotations

import json

import pytest
from aegis.services.chat import CHAT_TOOLS, _validate_tool_args


def _schema_for(name: str) -> dict:
    for t in CHAT_TOOLS:
        if t["function"]["name"] == name:
            return t["function"]["parameters"]
    raise KeyError(name)


def test_validate_tool_args_accepts_valid():
    schema = _schema_for("search_knowledge")
    _validate_tool_args("search_knowledge", {"query": "hello"}, schema=schema)  # must not raise


def test_validate_tool_args_rejects_missing_required():
    schema = _schema_for("search_knowledge")
    with pytest.raises(Exception) as exc_info:
        _validate_tool_args("search_knowledge", {}, schema=schema)
    assert "query" in str(exc_info.value).lower()


def test_validate_tool_args_rejects_wrong_type():
    schema = _schema_for("search_knowledge")
    with pytest.raises(Exception) as exc_info:
        _validate_tool_args("search_knowledge", {"query": 123}, schema=schema)
    # jsonschema message includes "is not of type 'string'"
    assert "string" in str(exc_info.value).lower() or "type" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_dispatch_retries_once_on_validation_error():
    """On validation error, dispatch appends a tool message and retries once."""
    from aegis.services.chat import _dispatch_tool_call_with_retry

    calls: list[tuple[str, dict]] = []

    async def _fake_executor(pool, args, ctx):
        calls.append(("exec", args))
        return {"ok": True, "data": args}

    messages: list[dict] = []
    # First args are invalid (missing "query"), retry args are valid.
    result = await _dispatch_tool_call_with_retry(
        pool=None,
        name="search_knowledge",
        tool_call_id="call_1",
        initial_args={},  # invalid
        messages=messages,
        retry_args_provider=lambda err: {"query": "hello"},  # simulates LLM retry
        executor=_fake_executor,
        ctx=None,
    )
    # One retry happened — executor was called once (with the retry args).
    assert [c for c in calls if c[0] == "exec"] == [("exec", {"query": "hello"})]
    assert result == {"ok": True, "data": {"query": "hello"}}
    # A validation-error tool message was appended so the LLM sees it.
    tool_msgs = [m for m in messages if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    assert "validation" in tool_msgs[0]["content"].lower()


@pytest.mark.asyncio
async def test_dispatch_surfaces_error_after_second_failure():
    """Second failure raises ChatToolValidationError."""
    from aegis.services.chat import ChatToolValidationError, _dispatch_tool_call_with_retry

    async def _fake_executor(pool, args, ctx):
        return {"ok": True}

    messages: list[dict] = []
    with pytest.raises(ChatToolValidationError):
        await _dispatch_tool_call_with_retry(
            pool=None,
            name="search_knowledge",
            tool_call_id="call_2",
            initial_args={},  # invalid
            messages=messages,
            retry_args_provider=lambda err: {},  # still invalid on retry
            executor=_fake_executor,
            ctx=None,
        )


@pytest.mark.asyncio
async def test_retry_message_includes_schema_hint():
    """The tool message fed back on a validation failure spells out the
    expected arguments (required fields + enum values) so the model can
    self-correct — the fix for gpt-oss fumbling run_infra_script's required
    `context` enum and then giving up to prose. Uses the real CHAT_TOOLS
    schema via name lookup (the production path)."""
    from aegis.services.chat import _dispatch_tool_call_with_retry

    captured: dict[str, str] = {}

    async def _fake_executor(pool, args, ctx):
        return {"ok": True, "data": args}

    def _retry(err: str) -> dict:
        captured["err"] = err
        return {"context": "swarm", "script_name": "infra-list-nodes"}

    messages: list[dict] = []
    result = await _dispatch_tool_call_with_retry(
        pool=None,
        name="run_infra_script",
        tool_call_id="call_hint",
        initial_args={"script_name": "infra-list-nodes"},  # missing required `context`
        messages=messages,
        retry_args_provider=_retry,
        executor=_fake_executor,
        ctx=None,
    )
    assert result["ok"] is True
    # The retry was told what `context` must be: required + its enum values.
    err = captured["err"]
    assert "context" in err
    assert "required" in err
    assert "one of" in err
    assert "swarm" in err
    # The same enriched message is what the LLM sees in the transcript.
    tool_msgs = [m for m in messages if m.get("role") == "tool"]
    assert tool_msgs and "Expected arguments" in tool_msgs[0]["content"]


@pytest.mark.asyncio
async def test_dispatch_awaits_async_retry_args_provider():
    """Dispatch awaits retry_args_provider when it returns a coroutine (production path)."""
    from aegis.services.chat import _dispatch_tool_call_with_retry

    async def _fake_executor(pool, args, ctx):
        return {"ok": True, "data": args}

    async def _async_retry_provider(err):
        # Simulates _retry_via_llm — returns a coroutine that produces the new args.
        return {"query": "from-async-provider"}

    messages: list[dict] = []
    result = await _dispatch_tool_call_with_retry(
        pool=None,
        name="search_knowledge",
        tool_call_id="call_async",
        initial_args={},  # invalid
        messages=messages,
        retry_args_provider=_async_retry_provider,
        executor=_fake_executor,
        ctx=None,
    )
    assert result == {"ok": True, "data": {"query": "from-async-provider"}}
    # Validation-error message was appended before the retry.
    assert any(m.get("role") == "tool" and "validation" in m["content"].lower() for m in messages)


@pytest.mark.asyncio
async def test_retry_via_llm_reads_flat_tool_call_shape():
    """_retry_via_llm must read the flat tool-call shape LLMClient.chat returns
    ({id,name,arguments}), not the nested OpenAI {function:{...}} shape.

    Regression: it read tc['function']['name'] — a key chat() never returns — so
    the match loop never fired and it always returned {}, silently killing the
    arg-correction retry in production.
    """
    from aegis.services.chat import _retry_via_llm

    class _FakeLLM:
        async def chat(self, messages, model, tools):
            return {
                "response": "",
                "tool_calls": [
                    {
                        "id": "call_retry",
                        "name": "search_knowledge",
                        "arguments": json.dumps({"query": "corrected"}),
                    }
                ],
                "model": model,
            }

    args = await _retry_via_llm(
        _FakeLLM(),
        messages=[],
        model="balanced",
        tools=None,
        original_tool_name="search_knowledge",
        error_msg="Validation error on tool `search_knowledge`.",
    )
    assert args == {"query": "corrected"}
