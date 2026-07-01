"""OTel GenAI semantic-convention attrs on LLM spans (Phase 2 eval)."""

from __future__ import annotations

from unittest.mock import MagicMock

from aegis.llm import _set_genai_request, _set_genai_usage


def _attrs(span):
    return {c.args[0]: c.args[1] for c in span.set_attribute.call_args_list}


def test_genai_request_sets_standard_attrs():
    span = MagicMock()
    _set_genai_request(span, "chat", "gpt-oss:20b", 2000)
    a = _attrs(span)
    assert a["gen_ai.system"] == "litellm"
    assert a["gen_ai.operation.name"] == "chat"
    assert a["gen_ai.request.model"] == "gpt-oss:20b"
    assert a["gen_ai.request.max_tokens"] == 2000


def test_genai_request_omits_max_tokens_for_embeddings():
    span = MagicMock()
    _set_genai_request(span, "embeddings", "nomic-embed-text")
    a = _attrs(span)
    assert a["gen_ai.operation.name"] == "embeddings"
    assert "gen_ai.request.max_tokens" not in a


def test_genai_usage_sets_token_counts():
    span = MagicMock()
    _set_genai_usage(span, 120, 48)
    a = _attrs(span)
    assert a["gen_ai.usage.input_tokens"] == 120
    assert a["gen_ai.usage.output_tokens"] == 48
