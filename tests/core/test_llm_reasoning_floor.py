"""The kimi floor exists because reasoning models bill hidden reasoning_content
against max_tokens — tight caller budgets truncate to empty visible content."""

from aegis.llm import _REASONING_MIN_TOKENS, _reasoning_floor


def test_kimi_small_budget_floored():
    assert _reasoning_floor("kimi-k2.5", 512) == _REASONING_MIN_TOKENS


def test_kimi_large_budget_untouched():
    assert _reasoning_floor("kimi-k2.5", 4000) == 4000


def test_non_reasoning_model_untouched():
    assert _reasoning_floor("gemma4:e2b", 512) == 512
