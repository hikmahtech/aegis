"""Regression test for LLMClient.embed.

The OpenAI SDK always sends `encoding_format` (base64), which LiteLLM's Ollama
embeddings provider rejects with a 400. embed() must POST /embeddings directly
WITHOUT that param.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx
from aegis.llm import LLMClient

pytestmark = pytest.mark.asyncio


async def test_embed_omits_encoding_format_and_parses():
    client = LLMClient(base_url="http://litellm.test/v1", api_key="k")
    with respx.mock:
        route = respx.post("http://litellm.test/v1/embeddings").mock(
            return_value=httpx.Response(200, json={"data": [{"embedding": [0.1, 0.2, 0.3]}]})
        )
        out = await client.embed(["hello"], model="nomic-embed-text")

    assert out == [[0.1, 0.2, 0.3]]
    sent = json.loads(route.calls[0].request.content)
    assert "encoding_format" not in sent  # the whole point
    assert sent == {"model": "nomic-embed-text", "input": ["hello"]}
    await client.close()


async def test_embed_empty_input_short_circuits():
    client = LLMClient(base_url="http://litellm.test/v1")
    assert await client.embed([]) == []
    await client.close()
