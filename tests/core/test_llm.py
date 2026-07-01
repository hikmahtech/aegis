"""Tests for LLM client."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from aegis.llm import (
    _BATCH_RECEIPT_PROMPT,
    LLMClient,
    LLMTruncationError,
    _classify_llm_error,
    parse_llm_json,
)


def test_parse_llm_json_handles_fences_prose_arrays_and_garbage():
    # bare object
    assert parse_llm_json('{"a": 1}') == {"a": 1}
    # ```json fenced object
    assert parse_llm_json('```json\n{"a": 1}\n```') == {"a": 1}
    # bare ``` fence
    assert parse_llm_json('```\n{"a": 1}\n```') == {"a": 1}
    # prose around the object
    assert parse_llm_json('Sure!\n{"a": 1}\nDone.') == {"a": 1}
    # array (object/array generality)
    assert parse_llm_json('[{"i": 0}]') == [{"i": 0}]
    assert parse_llm_json('```json\n[1, 2]\n```') == [1, 2]
    # failure modes → None (so callers fall back, not crash)
    assert parse_llm_json("") is None
    assert parse_llm_json("not json at all") is None
    assert parse_llm_json("{broken") is None


def test_batch_receipt_prompt_has_bank_alert_exclusions():
    """Regression guard: the receipt prompt must keep the is_receipt=false rules
    for failed payments, autopay reminders, and card statements so a bank
    notification can't be minted into a recurring charge. If these silently
    disappear the prod offenders (failed razorpay, axis autopay, card bill)
    come back."""
    prompt = _BATCH_RECEIPT_PROMPT.lower()
    for keyword in (
        "payment failed",
        "declined",
        "reversed",
        "refund",
        "upcoming autopay",
        "autopay reminder",
        "mandate",
        "new bill",
        "statement",
        "minimum due",
    ):
        assert keyword in prompt, f"missing exclusion keyword: {keyword!r}"


async def test_think_returns_response():
    """think() calls LLM and returns response text."""
    client = LLMClient(base_url="http://localhost:4000/v1", api_key="test")

    with patch.object(client, "_client") as mock_openai:
        mock_completion = AsyncMock()
        mock_completion.choices = [AsyncMock(message=AsyncMock(content="The root cause is X."))]
        mock_completion.usage = AsyncMock(prompt_tokens=10, completion_tokens=20)
        mock_openai.chat.completions.create = AsyncMock(return_value=mock_completion)

        result = await client.think("Investigate this alert", model="gemma4:e2b")
        assert result["response"] == "The root cause is X."
        assert result["model"] == "gemma4:e2b"


async def test_think_with_system_prompt():
    """think() includes system prompt when provided."""
    client = LLMClient(base_url="http://localhost:4000/v1", api_key="test")

    with patch.object(client, "_client") as mock_openai:
        mock_completion = AsyncMock()
        mock_completion.choices = [AsyncMock(message=AsyncMock(content="Done."))]
        mock_completion.usage = AsyncMock(prompt_tokens=5, completion_tokens=5)
        mock_openai.chat.completions.create = AsyncMock(return_value=mock_completion)

        await client.think("Fix it", model="gemma4:e2b", system_prompt="You are an engineer.")
        call_args = mock_openai.chat.completions.create.call_args
        messages = call_args.kwargs.get("messages", [])
        assert messages[0]["role"] == "system"


def test_classify_llm_error_timeout():
    """Timeout-class exceptions are tagged as 'timeout', everything else as 'error'."""
    class FakeTimeoutError(Exception):
        pass

    assert _classify_llm_error(FakeTimeoutError("connection timed out")) == "timeout"
    assert _classify_llm_error(TimeoutError()) == "timeout"
    assert _classify_llm_error(Exception("ReadTimeout exceeded")) == "timeout"
    assert _classify_llm_error(ValueError("bad json")) == "error"
    assert _classify_llm_error(RuntimeError("connection refused")) == "error"


async def test_think_records_failure_when_pool_and_purpose_set():
    """When db_pool + purpose are provided, exceptions emit a failure row."""
    client = LLMClient(base_url="http://localhost:4000/v1", api_key="test")
    pool = AsyncMock()
    pool.execute = AsyncMock()

    class _UpstreamError(RuntimeError):
        pass

    with patch.object(client, "_client") as mock_openai:
        mock_openai.chat.completions.create = AsyncMock(
            side_effect=_UpstreamError("timed out")
        )
        with pytest.raises(_UpstreamError):
            await client.think(
                "go",
                model="gemma4:e2b",
                db_pool=pool,
                purpose="alert_assessment",
                agent_id="pandoras-actor",
            )

    pool.execute.assert_called_once()
    args = pool.execute.call_args[0]
    assert "INSERT INTO llm_calls" in args[0]
    assert args[1] == "gemma4:e2b"
    assert args[5] == "alert_assessment"
    assert args[6] == "pandoras-actor"
    assert args[7] == "timeout"  # classified from message
    assert "timed out" in args[8]


async def test_think_does_not_record_failure_without_telemetry_hooks():
    """No db_pool/purpose means no failure row — backward compat."""
    client = LLMClient(base_url="http://localhost:4000/v1", api_key="test")

    class _UpstreamError(RuntimeError):
        pass

    with patch.object(client, "_client") as mock_openai:
        mock_openai.chat.completions.create = AsyncMock(
            side_effect=_UpstreamError("boom")
        )
        # Should still raise even when no telemetry hooks are wired.
        with pytest.raises(_UpstreamError):
            await client.think("go", model="gemma4:e2b")


async def test_think_concurrency_limit_serialises_bursts():
    """concurrency_limits caps how many concurrent calls reach the API client."""
    client = LLMClient(
        base_url="http://localhost:4000/v1",
        api_key="test",
        concurrency_limits={"gemma4:e2b": 2},
    )

    inflight = 0
    peak = 0
    lock = asyncio.Lock()
    release_event = asyncio.Event()

    async def slow_create(**_kwargs):
        nonlocal inflight, peak
        async with lock:
            inflight += 1
            peak = max(peak, inflight)
        await release_event.wait()
        async with lock:
            inflight -= 1
        completion = AsyncMock()
        completion.choices = [AsyncMock(message=AsyncMock(content="ok"))]
        completion.usage = AsyncMock(prompt_tokens=1, completion_tokens=1)
        return completion

    with patch.object(client, "_client") as mock_openai:
        mock_openai.chat.completions.create = slow_create

        async def call():
            return await client.think("x", model="gemma4:e2b")

        tasks = [asyncio.create_task(call()) for _ in range(5)]
        # Wait until the semaphore has admitted 2 calls (and held back the rest).
        for _ in range(100):
            await asyncio.sleep(0.005)
            if peak >= 2 and inflight == 2:
                break
        # 5 fired but only 2 should be in-flight under the semaphore.
        assert peak == 2

        release_event.set()
        await asyncio.gather(*tasks)


async def test_think_concurrency_limit_does_not_throttle_other_models():
    """A semaphore on gemma must not affect qwen3 calls."""
    client = LLMClient(
        base_url="http://localhost:4000/v1",
        api_key="test",
        concurrency_limits={"gemma4:e2b": 1},
    )

    assert client._semaphore_for("gemma4:e2b") is not None
    assert client._semaphore_for("qwen3:14b") is None
    # Cached: same semaphore instance returned on subsequent lookups.
    assert client._semaphore_for("gemma4:e2b") is client._semaphore_for("gemma4:e2b")


# ----------------- extract_receipts_batch error surfacing --------


async def test_extract_receipts_batch_raises_on_llm_error():
    """Bundle E: outer exception (LLM call / JSON decode) must propagate up
    so MoneyProcessFlow can decide not to upsert. Used to silently return
    N is_receipt=False items."""
    client = LLMClient(base_url="http://localhost:4000/v1", api_key="test")

    async def boom(*args, **kwargs):
        raise RuntimeError("LiteLLM upstream 502")

    receipts = [
        {
            "id": "r1",
            "sender": "billing@stripe.com",
            "subject": "receipt",
            "body_plain": "...",
        }
    ]
    with (
        patch.object(client, "think", side_effect=boom),
        pytest.raises(RuntimeError, match="LiteLLM upstream 502"),
    ):
        await client.extract_receipts_batch(receipts, model="gemma4:e2b")


async def test_extract_receipts_batch_marks_parse_failed_per_item():
    """Bundle E: when LLM returns a JSON array but individual items don't
    conform to ReceiptExtraction schema, mark _parse_failed=True on those
    rows. Healthy items still get returned populated."""
    client = LLMClient(base_url="http://localhost:4000/v1", api_key="test")

    async def stub_think(*args, **kwargs):
        return {
            "response": (
                '```json\n'
                '[{"is_receipt": true, "vendor_name": "Stripe", "sender_label": "stripe.com", '
                '"category": "saas", "amount": 9.99, "currency": "USD", "cadence": "monthly", '
                '"confidence": 0.9}, '
                '"this is not a dict"]\n'
                '```'
            ),
            "model": "gemma4:e2b",
            "prompt_tokens": 0,
            "completion_tokens": 0,
        }

    receipts = [
        {"id": "r1", "sender": "a", "subject": "b", "body_plain": "c"},
        {"id": "r2", "sender": "a", "subject": "b", "body_plain": "c"},
    ]
    with patch.object(client, "think", side_effect=stub_think):
        out = await client.extract_receipts_batch(receipts, model="gemma4:e2b")
    assert len(out) == 2
    assert out[0]["is_receipt"] is True
    assert "_parse_failed" not in out[0]
    assert out[1]["is_receipt"] is False
    assert out[1].get("_parse_failed") is True


# --------------- LLMTruncationError / reasoning-model guard ---------------


async def _make_truncated_completion(model="gpt-oss:20b", max_tokens=256):
    """Build a mock completion that mimics finish_reason=length + empty content."""
    mock_completion = AsyncMock()
    mock_choice = AsyncMock()
    mock_choice.message = AsyncMock(content="")
    mock_choice.finish_reason = "length"
    mock_completion.choices = [mock_choice]
    mock_completion.usage = AsyncMock(prompt_tokens=50, completion_tokens=max_tokens)
    return mock_completion


async def test_think_raises_truncation_error_on_empty_content_length():
    """think() must raise LLMTruncationError when content is empty and
    finish_reason='length'.  This is the reasoning-model budget exhaustion
    case (gpt-oss:20b hidden reasoning_content consumes all tokens).
    Callers must receive a typed error, never a silent ''."""
    client = LLMClient(base_url="http://localhost:4000/v1", api_key="test")
    completion = await _make_truncated_completion()

    with patch.object(client, "_client") as mock_openai:
        mock_openai.chat.completions.create = AsyncMock(return_value=completion)
        with pytest.raises(LLMTruncationError, match="finish_reason=length"):
            await client.think("classify this email", model="gpt-oss:20b", max_tokens=256)


async def test_think_does_not_raise_truncation_on_normal_response():
    """Normal (non-empty) responses must pass through unchanged — backward compat."""
    client = LLMClient(base_url="http://localhost:4000/v1", api_key="test")

    mock_completion = AsyncMock()
    mock_choice = AsyncMock()
    mock_choice.message = AsyncMock(content='{"category": "informational"}')
    mock_choice.finish_reason = "stop"
    mock_completion.choices = [mock_choice]
    mock_completion.usage = AsyncMock(prompt_tokens=20, completion_tokens=15)

    with patch.object(client, "_client") as mock_openai:
        mock_openai.chat.completions.create = AsyncMock(return_value=mock_completion)
        result = await client.think("classify", model="gpt-oss:20b")
    assert result["response"] == '{"category": "informational"}'


async def test_think_does_not_raise_truncation_when_finish_reason_is_stop_and_empty():
    """An empty string with finish_reason='stop' is not a truncation — the model
    deliberately returned nothing.  Do not raise LLMTruncationError in this case."""
    client = LLMClient(base_url="http://localhost:4000/v1", api_key="test")

    mock_completion = AsyncMock()
    mock_choice = AsyncMock()
    mock_choice.message = AsyncMock(content="")
    mock_choice.finish_reason = "stop"
    mock_completion.choices = [mock_choice]
    mock_completion.usage = AsyncMock(prompt_tokens=10, completion_tokens=0)

    with patch.object(client, "_client") as mock_openai:
        mock_openai.chat.completions.create = AsyncMock(return_value=mock_completion)
        result = await client.think("hello", model="gpt-oss:20b")
    # Returns empty string — NOT a truncation error; caller decides what to do.
    assert result["response"] == ""


async def test_extract_receipts_batch_returns_parse_failed_stubs_on_truncation():
    """When think() raises LLMTruncationError, extract_receipts_batch must NOT
    re-raise.  It returns N _parse_failed stubs so MoneyProcessFlow skips the
    batch without crashing through all 3 Temporal retries."""
    client = LLMClient(base_url="http://localhost:4000/v1", api_key="test")
    receipts = [
        {"id": "r1", "sender": "billing@stripe.com", "subject": "Invoice", "body_plain": "..."},
        {"id": "r2", "sender": "noreply@razorpay.com", "subject": "Payment", "body_plain": "..."},
    ]

    async def truncated_think(*args, **kwargs):
        raise LLMTruncationError("model=gpt-oss:20b returned empty content with finish_reason=length")

    with patch.object(client, "think", side_effect=truncated_think):
        out = await client.extract_receipts_batch(receipts, model="gpt-oss:20b")

    assert len(out) == 2
    for item in out:
        assert item["is_receipt"] is False
        assert item["_parse_failed"] is True


async def test_extract_receipts_batch_still_raises_on_other_llm_errors():
    """Non-truncation LLM failures (e.g. 502 upstream) still propagate so
    MoneyProcessFlow can retry via Temporal's retry policy."""
    client = LLMClient(base_url="http://localhost:4000/v1", api_key="test")
    receipts = [{"id": "r1", "sender": "a", "subject": "b", "body_plain": "c"}]

    async def network_error(*args, **kwargs):
        raise RuntimeError("LiteLLM upstream 502")

    with (
        patch.object(client, "think", side_effect=network_error),
        pytest.raises(RuntimeError, match="502"),
    ):
        await client.extract_receipts_batch(receipts, model="gpt-oss:20b")
