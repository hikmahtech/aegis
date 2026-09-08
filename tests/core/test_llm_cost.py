"""Cost per call, taken from the LiteLLM proxy rather than a price list.

The proxy already prices every model it serves, Bedrock included, and returns
the figure on the response. AEGIS keeps that number; it does not compute one.
These tests pin the two halves of that: reading the headers, and storing the
result — including the case that matters most, which is a call nobody priced
landing as NULL rather than as a free call.
"""

from __future__ import annotations

import uuid

import pytest
from aegis.llm import _cost_from_headers
from aegis.observability import record_llm_call
from aegis.services.llm_governor import llm_spend_last_24h

pytestmark = pytest.mark.asyncio


def test_the_cost_is_the_total_the_proxy_states_after_any_discount():
    """Measured against the live proxy: it sends a combined total AND the
    components AND a pre-discount `-original`. The total is the one to keep —
    summing components would report the pre-discount figure the moment a
    discount is configured.

    Falsifiable: sum the components instead and this returns 1.52e-06 for the
    discounted call below, which is what the call did NOT cost."""
    assert _cost_from_headers(
        {
            "x-litellm-response-cost": "1.52e-06",
            "x-litellm-response-cost-original": "1.52e-06",
            "x-litellm-response-cost-input": "5.6e-07",
            "x-litellm-response-cost-output": "9.6e-07",
            "x-litellm-response-cost-tool-usage": "0.0",
        }
    ) == pytest.approx(1.52e-06)
    # Discounted: the components still add to the ORIGINAL, and the total is
    # what was actually charged.
    assert _cost_from_headers(
        {
            "x-litellm-response-cost": "0.5",
            "x-litellm-response-cost-original": "1.0",
            "x-litellm-response-cost-input": "0.4",
            "x-litellm-response-cost-output": "0.6",
        }
    ) == pytest.approx(0.5)
    # A proxy version with no combined total falls back to the components.
    assert _cost_from_headers(
        {"x-litellm-response-cost-input": "9.6e-07", "x-litellm-response-cost-output": "9.6e-07"}
    ) == pytest.approx(1.92e-06)
    # A local model really is free; that is a 0.0, not an absence.
    assert _cost_from_headers({"x-litellm-response-cost": "0"}) == 0.0


def test_a_backend_that_prices_nothing_reports_none_not_zero():
    """`None` and `0.0` mean different things and a spend total must not
    confuse them: one is "we do not know", the other is "it was free"."""
    assert _cost_from_headers({}) is None
    assert _cost_from_headers(None) is None
    assert _cost_from_headers({"content-type": "application/json"}) is None
    # A header that is present but unreadable is a proxy contract change, not
    # a free call.
    assert _cost_from_headers({"x-litellm-response-cost-input": "free"}) is None


async def test_the_recorded_call_carries_its_cost(db_pool):
    purpose = f"zzcost-{uuid.uuid4().hex[:8]}"
    await record_llm_call(
        db_pool,
        model="bedrock-kimi-k2.5",
        prompt_tokens=12,
        completion_tokens=2,
        latency_ms=177,
        purpose=purpose,
        cost_usd=1.92e-06,
    )
    row = await db_pool.fetchrow(
        "SELECT model, cost_usd, input_tokens FROM llm_calls WHERE purpose = $1", purpose
    )
    assert float(row["cost_usd"]) == pytest.approx(1.92e-06)
    assert row["input_tokens"] == 12


async def test_an_unpriced_call_is_null_and_counted_separately(db_pool):
    """The number that gets believed is the total, so a call nobody priced
    must be visible as unpriced rather than disappear into it as zero."""
    purpose = f"zzcost-{uuid.uuid4().hex[:8]}"
    await record_llm_call(
        db_pool,
        model="some-other-backend",
        prompt_tokens=1,
        completion_tokens=1,
        latency_ms=1,
        purpose=purpose,
    )
    row = await db_pool.fetchrow(
        "SELECT cost_usd FROM llm_calls WHERE purpose = $1", purpose
    )
    assert row["cost_usd"] is None

    spend = await llm_spend_last_24h(db_pool, model_filter="some-other-backend")
    assert spend["usd"] == 0.0
    assert spend["unpriced"] >= 1 and spend["priced"] == 0


async def test_spend_is_filtered_by_model_the_way_the_governor_asks(db_pool):
    tag = uuid.uuid4().hex[:8]
    await record_llm_call(
        db_pool, model=f"bedrock-a-{tag}", prompt_tokens=1, completion_tokens=1,
        latency_ms=1, purpose=f"zzc-{tag}", cost_usd=0.25,
    )
    await record_llm_call(
        db_pool, model=f"local-b-{tag}", prompt_tokens=1, completion_tokens=1,
        latency_ms=1, purpose=f"zzc-{tag}", cost_usd=0.75,
    )
    only_bedrock = await llm_spend_last_24h(db_pool, model_filter=f"bedrock-a-{tag}")
    assert only_bedrock["usd"] == pytest.approx(0.25)
    assert only_bedrock["priced"] == 1
