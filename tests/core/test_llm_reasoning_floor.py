"""The reasoning floor exists because reasoning models bill hidden
reasoning_content against max_tokens — tight caller budgets truncate to empty
visible content."""

from aegis.llm import _REASONING_MIN_TOKENS, _reasoning_floor


def test_kimi_small_budget_floored():
    assert _reasoning_floor("kimi-k2.5", 512) == _REASONING_MIN_TOKENS


def test_large_budget_untouched():
    # The floor only ever RAISES a budget; a caller asking for more than the
    # floor keeps exactly what it asked for. Expressed against the constant so
    # raising the floor doesn't silently invert the property this guards.
    assert _reasoning_floor("kimi-k2.5", _REASONING_MIN_TOKENS * 2) == _REASONING_MIN_TOKENS * 2


def test_floor_never_shrinks_a_budget():
    for budget in (1, 512, 2048, 4096, 100_000):
        assert _reasoning_floor("kimi-k2.5", budget) >= budget


def test_qwen_is_floored_too():
    # qwen3.5:9b ran briefing_frame at a raw max_tokens=2000 and returned empty
    # content on 100% of calls, because the floor matched only "kimi". A
    # reasoning model missing from the list fails silently, so this asserts the
    # membership rather than the mechanism.
    assert _reasoning_floor("qwen3.5:9b", 2000) == _REASONING_MIN_TOKENS


def test_floor_clears_the_largest_observed_output():
    # Largest visible output ever recorded in prod across every purpose was
    # 2944 tokens. A floor at or below that would still truncate the tail.
    assert _REASONING_MIN_TOKENS > 2944


def test_non_reasoning_model_untouched():
    assert _reasoning_floor("gemma4:e2b", 512) == 512
