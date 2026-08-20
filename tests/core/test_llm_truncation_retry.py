"""Issue #321 — `think()` re-rolls an empty truncated response once.

The floor (`test_llm_reasoning_floor.py`) sets the steady-state budget. This is
the other half: 30 days of prod kimi-k2.5 ran 984 successes averaging 705
visible output tokens against 22 calls that burned the whole 4096 on hidden
reasoning and returned nothing. A 2% stochastic overthink spiral is not a model
that needs a bigger budget forever — it is a bad roll, and the cure is one more
roll with enough headroom to swallow a spiral.

Two properties carry the fix and both are asserted against the number of
UPSTREAM calls, not against a mock's return value:

* a truncation is rescued — the caller gets content instead of an exception,
  and the second attempt really does go out at the bigger budget;
* a truncation storm is bounded — exactly two calls, never three. An unbounded
  retry on a model in a permanent spiral would multiply spend without limit.

Everything drives the real `LLMClient` with only `chat.completions.create`
stubbed (see `tests/llm_stub.py`), because a fake `think()` would replace the
code under test.
"""

from __future__ import annotations

import pytest
import structlog
from aegis.llm import _REASONING_MIN_TOKENS, _TRUNCATION_RETRY_TOKENS, LLMTruncationError

from tests.llm_stub import StubbedLLMClient

_PREFIX = "zz321-"

#: content + finish_reason for "burned the budget, wrote nothing".
_TRUNCATED = ("", "length")
_BODY = '{"verdict": "ok"}'


def _budgets(client: StubbedLLMClient) -> list[int]:
    return [c["max_tokens"] for c in client.calls]


# --------------------------------------------------------------- the rescue


async def test_think_retries_truncation_at_larger_budget():
    """THE fix. Truncate once, succeed on the re-roll: the caller sees content,
    and attempt 2 went out at `_TRUNCATION_RETRY_TOKENS` — strictly more room
    than attempt 1, which is the only reason a re-roll is worth issuing."""
    client = StubbedLLMClient(
        content=["", _BODY],
        finish_reason=["length", "stop"],
        prompt_tokens=50,
        completion_tokens=4096,
    )

    result = await client.think("classify this", model="kimi-k2.5", max_tokens=512)

    assert result["response"] == _BODY, "a rescuable truncation must not reach the caller"
    assert client.call_count == 2
    first, second = _budgets(client)
    assert second == _TRUNCATION_RETRY_TOKENS
    assert second > first, f"re-rolled at {second}, no more room than the {first} that failed"


async def test_the_retry_is_keyed_on_the_symptom_not_on_a_model_list():
    """The #255 recurrence trap. `gpt-oss:20b` is NOT in `_REASONING_MODELS`,
    so it gets no floor at all — 2000 raw, straight into empty content, exactly
    how qwen3.5:9b failed briefing_frame 3/3 for six days. The retry has no
    model list of its own on purpose: it fires on the truncation, so a
    reasoning model nobody remembered to register is rescued anyway."""
    client = StubbedLLMClient(content=["", _BODY], finish_reason=["length", "stop"])

    result = await client.think("frame the briefing", model="gpt-oss:20b", max_tokens=2000)

    assert result["response"] == _BODY
    assert _budgets(client) == [2000, _TRUNCATION_RETRY_TOKENS]


async def test_the_retry_is_the_identical_request():
    """A re-roll that changed the prompt would be a different question, and a
    passing test would prove nothing about the failing call."""
    client = StubbedLLMClient(content=["", _BODY], finish_reason=["length", "stop"])

    await client.think(
        "why did the deploy wipe the env vars?",
        model="kimi-k2.5",
        system_prompt="You are an SRE.",
        max_tokens=1000,
    )

    first, second = client.calls
    assert first["messages"] == second["messages"]
    assert first["model"] == second["model"]


async def test_the_retry_is_logged():
    """The rescue must be visible in the logs, or a model spiralling on every
    call looks like a model that simply works."""
    client = StubbedLLMClient(content=["", _BODY], finish_reason=["length", "stop"])

    with structlog.testing.capture_logs() as logs:
        await client.think("go", model="kimi-k2.5", max_tokens=512, purpose=f"{_PREFIX}log")

    events = [entry for entry in logs if entry["event"] == "llm_truncated_retrying"]
    assert events, f"no llm_truncated_retrying warning; captured: {[e['event'] for e in logs]}"
    assert events[0]["log_level"] == "warning"
    assert events[0]["retry_max_tokens"] == _TRUNCATION_RETRY_TOKENS
    assert events[0]["first_max_tokens"] == _REASONING_MIN_TOKENS  # 512 floored


# ------------------------------------------------------------- the storm guard


async def test_a_truncation_storm_stops_after_exactly_two_calls():
    """The bound. A model in a permanent overthink spiral truncates the re-roll
    too; the error the caller already handles must arrive after TWO upstream
    calls. Three would mean the re-roll re-rolls, and an unbounded retry on a
    dead model multiplies spend with nothing to show for it."""
    client = StubbedLLMClient(content="", finish_reason="length", completion_tokens=16384)

    with pytest.raises(LLMTruncationError, match="finish_reason=length"):
        await client.think("classify this", model="kimi-k2.5", max_tokens=512)

    assert client.call_count == 2, f"{client.call_count} upstream calls, expected exactly 2"
    assert _budgets(client) == [_REASONING_MIN_TOKENS, _TRUNCATION_RETRY_TOKENS]


async def test_a_caller_already_at_the_retry_budget_is_not_retried():
    """Nothing to re-roll at: a caller asking for `_TRUNCATION_RETRY_TOKENS` or
    more that still truncates gets the raise on the first call, not a pointless
    second one at the same budget."""
    client = StubbedLLMClient(content="", finish_reason="length")

    with pytest.raises(LLMTruncationError):
        await client.think("go", model="kimi-k2.5", max_tokens=_TRUNCATION_RETRY_TOKENS)

    assert client.call_count == 1
    assert _budgets(client) == [_TRUNCATION_RETRY_TOKENS]


# --------------------------------------------- what must NOT trigger a re-roll


async def test_a_clipped_response_is_not_retried():
    """`finish_reason=length` with visible content is the OTHER truncation: the
    response was cut mid-write, and the contract (#255) is that it is recorded
    and returned, never raised. Retrying it would double spend on every clipped
    call in the fleet."""
    client = StubbedLLMClient(content='[{"topic": "half an ans', finish_reason="length")

    result = await client.think("score these", model="kimi-k2.5", max_tokens=512)

    assert result["response"] == '[{"topic": "half an ans'
    assert client.call_count == 1


async def test_an_empty_stop_is_not_retried():
    """An empty body with `finish_reason=stop` is a model that deliberately
    said nothing. It is not truncation and buying a second one changes
    nothing."""
    client = StubbedLLMClient(content="", finish_reason="stop")

    result = await client.think("go", model="kimi-k2.5", max_tokens=512)

    assert result["response"] == ""
    assert client.call_count == 1


async def test_a_successful_call_is_not_retried():
    client = StubbedLLMClient(content=_BODY, finish_reason="stop")

    await client.think("go", model="kimi-k2.5", max_tokens=512)

    assert client.call_count == 1


async def test_an_upstream_error_is_not_retried():
    """The retry is for truncation only. A 502 or a read timeout is Temporal's
    job — retrying it here would silently multiply the activity's own retry
    budget."""

    class _UpstreamError(RuntimeError):
        pass

    client = StubbedLLMClient(raises=_UpstreamError("bad gateway"))

    with pytest.raises(_UpstreamError):
        await client.think("go", model="kimi-k2.5", max_tokens=512)

    assert client.call_count == 1


# ------------------------------------------------------------------ the meter


async def test_both_attempts_are_metered(db_pool):
    """Attempt 1 was a real, billed upstream call, so it keeps its own
    `llm_calls` row — and the row says it was retried. That count is the meter
    for a stale floor: when `(retrying at N)` rows climb, the steady-state
    budget has fallen behind whatever the tier map now resolves to. Losing the
    row would trade a loud failure for a silent cost.
    """
    purpose = f"{_PREFIX}rescued"
    await db_pool.execute("DELETE FROM llm_calls WHERE purpose LIKE $1", f"{_PREFIX}%")
    client = StubbedLLMClient(
        db_pool=db_pool,
        content=["", _BODY],
        finish_reason=["length", "stop"],
        prompt_tokens=50,
        completion_tokens=4096,
    )

    try:
        await client.think("go", model="kimi-k2.5", max_tokens=512, purpose=purpose)

        rows = await db_pool.fetch(
            "SELECT status, error, output_tokens FROM llm_calls "
            "WHERE purpose = $1 ORDER BY created_at",
            purpose,
        )
        assert len(rows) == 2, f"expected one row per billed call, got {len(rows)}"
        assert rows[0]["status"] == "error"
        assert (rows[0]["error"] or "").startswith("truncated: ")
        assert f"(retrying at {_TRUNCATION_RETRY_TOKENS})" in (rows[0]["error"] or "")
        assert rows[0]["output_tokens"] == 4096, "the truncated attempt still burned tokens"
        assert rows[1]["status"] == "success"
        assert rows[1]["error"] is None
    finally:
        await db_pool.execute("DELETE FROM llm_calls WHERE purpose LIKE $1", f"{_PREFIX}%")


async def test_a_storm_meters_both_calls_and_marks_only_the_first(db_pool):
    """The terminal attempt must NOT wear the retry marker — an operator
    counting `(retrying at N)` rows is counting re-rolls issued, and a marker
    on the final failure would double every one of them."""
    purpose = f"{_PREFIX}storm"
    await db_pool.execute("DELETE FROM llm_calls WHERE purpose LIKE $1", f"{_PREFIX}%")
    client = StubbedLLMClient(db_pool=db_pool, content="", finish_reason="length")

    try:
        with pytest.raises(LLMTruncationError):
            await client.think("go", model="kimi-k2.5", max_tokens=512, purpose=purpose)

        rows = await db_pool.fetch(
            "SELECT status, error FROM llm_calls WHERE purpose = $1 ORDER BY created_at",
            purpose,
        )
        assert len(rows) == 2
        assert all(r["status"] == "error" for r in rows)
        marked = [r for r in rows if "retrying at" in (r["error"] or "")]
        assert len(marked) == 1, "exactly one row is the retried attempt"
        assert "retrying at" not in (rows[1]["error"] or "")
    finally:
        await db_pool.execute("DELETE FROM llm_calls WHERE purpose LIKE $1", f"{_PREFIX}%")
