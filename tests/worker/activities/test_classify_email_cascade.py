"""GmailActivities.classify_email — data-driven cascade (2026-05-30).

Cheapest signal first, LLM last:
  1. confident per-sender cache  -> no LLM (source=cache)
  2. unknown sender + Gmail promo -> useless, no LLM (source=gmail_promo)
  3. else LLM tie-breaker, which teaches the cache (source=llm)
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest
from aegis_worker.activities import gmail as gmail_mod
from aegis_worker.activities.gmail import GmailActivities
from temporalio.testing import ActivityEnvironment


class _CountingLlm:
    def __init__(self, response: str = "{}"):
        self.response = response
        self.calls = 0

    async def think(self, **kwargs):
        self.calls += 1
        return {"response": self.response, "model": "qwen3:14b"}


def _make(llm=None, lookup=None) -> GmailActivities:
    g = GmailActivities(
        gmail_credentials_file="/tmp/x.json",
        gmail_token_dir="/tmp",
        llm_client=llm,
        db_pool=object(),  # truthy sentinel — cache helpers are mocked below
    )
    g._triage_lookup = AsyncMock(return_value=lookup)
    g._triage_upsert = AsyncMock(return_value=None)
    return g


@pytest.mark.asyncio
async def test_confident_cache_hit_skips_llm():
    llm = _CountingLlm()
    g = _make(llm=llm, lookup={"category": "important_read", "n": 5, "confidence": 0.9})
    msg = {"id": "m", "sender": "Acme <a@acme.com>", "subject": "s", "snippet": "b", "labels": []}
    res = await ActivityEnvironment().run(g.classify_email, msg, "")
    assert res["category"] == "important_read"
    assert res["source"] == "cache"
    assert llm.calls == 0
    g._triage_lookup.assert_awaited_once()


@pytest.mark.asyncio
async def test_gmail_promo_unknown_sender_skips_llm():
    llm = _CountingLlm()
    g = _make(llm=llm, lookup=None)
    msg = {
        "id": "m",
        "sender": "promo@shop.com",
        "subject": "Big Sale",
        "snippet": "b",
        "labels": ["CATEGORY_PROMOTIONS"],
    }
    res = await ActivityEnvironment().run(g.classify_email, msg, "")
    assert res["category"] == "useless"
    assert res["source"] == "gmail_promo"
    assert llm.calls == 0
    g._triage_upsert.assert_awaited()  # cached as useless for next time


@pytest.mark.asyncio
async def test_unknown_sender_falls_back_to_llm_and_caches(monkeypatch):
    monkeypatch.setattr(gmail_mod, "record_llm_call", AsyncMock())
    llm = _CountingLlm(json.dumps({"category": "important_action", "confidence": 0.9, "tags": []}))
    g = _make(llm=llm, lookup=None)
    msg = {"id": "m", "sender": "boss@co.com", "subject": "Hi", "snippet": "b", "labels": []}
    res = await ActivityEnvironment().run(g.classify_email, msg, "")
    assert res["category"] == "important_action"
    assert res["source"] == "llm"
    assert llm.calls == 1
    g._triage_upsert.assert_awaited_once()


@pytest.mark.asyncio
async def test_low_confidence_cache_still_uses_llm(monkeypatch):
    monkeypatch.setattr(gmail_mod, "record_llm_call", AsyncMock())
    llm = _CountingLlm(json.dumps({"category": "informational", "confidence": 0.6, "tags": []}))
    # n below _CACHE_MIN_N (3) → not trusted yet
    g = _make(llm=llm, lookup={"category": "useless", "n": 2, "confidence": 0.5})
    msg = {"id": "m", "sender": "x@y.com", "subject": "s", "snippet": "b", "labels": []}
    res = await ActivityEnvironment().run(g.classify_email, msg, "")
    assert res["source"] == "llm"
    assert llm.calls == 1


@pytest.mark.asyncio
async def test_gmail_important_marker_not_leaked_to_llm(monkeypatch):
    """Gmail's liberal auto-IMPORTANT marker must NOT be fed to the LLM as a
    prior — it inflates 'fake important'. The LLM decides from content only."""
    monkeypatch.setattr(gmail_mod, "record_llm_call", AsyncMock())

    captured = {}

    class _Spy(_CountingLlm):
        async def think(self, **kwargs):
            captured["prompt"] = kwargs.get("prompt", "")
            return await super().think(**kwargs)

    llm = _Spy(json.dumps({"category": "informational", "confidence": 0.6, "tags": []}))
    g = _make(llm=llm, lookup=None)
    msg = {"id": "m", "sender": "x@y.com", "subject": "s", "snippet": "b", "labels": ["IMPORTANT"]}
    res = await ActivityEnvironment().run(g.classify_email, msg, "")
    assert res["source"] == "llm"
    assert "Gmail flagged" not in captured["prompt"]
    assert "IMPORTANT" not in captured["prompt"]


@pytest.mark.asyncio
async def test_classify_email_uses_enough_max_tokens(monkeypatch):
    """Regression: max_tokens=120 truncated the JSON (category+confidence+
    reason+2-3 sentence summary+tags) ~3/7 calls -> Unterminated string ->
    silent fallback to informational. The LLM call must request >=256 tokens
    so the full JSON object fits."""
    monkeypatch.setattr(gmail_mod, "record_llm_call", AsyncMock())
    captured = {}

    class _Spy(_CountingLlm):
        async def think(self, **kwargs):
            captured.update(kwargs)
            return await super().think(**kwargs)

    llm = _Spy(json.dumps({"category": "informational", "confidence": 0.6, "tags": []}))
    g = _make(llm=llm, lookup=None)
    msg = {"id": "m", "sender": "x@y.com", "subject": "s", "snippet": "b", "labels": []}
    await ActivityEnvironment().run(g.classify_email, msg, "")
    assert captured.get("max_tokens", 0) >= 256, f"max_tokens too low: {captured.get('max_tokens')}"
