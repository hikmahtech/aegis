"""The sender cache must carry content tags, not just a category.

`GmailIngestFlow` fans out to `MoneyProcessFlow` on `financial`/`payments`
tags. The cache path returned `tags: []` unconditionally, so the moment a
financial sender crossed the cache threshold (n>=3, conf>=0.75) its mail
stopped triggering receipt extraction — permanently, because a cached sender
never reaches the LLM again. Prod had `alerts@axis.bank.in` sitting at n=20,
confidence 1.0.

Same defect class as #263, which fixed it for `sender_overrides` only.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest
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
        db_pool=object(),  # truthy sentinel — cache helpers are mocked
    )
    g._triage_lookup = AsyncMock(return_value=lookup)
    g._triage_upsert = AsyncMock(return_value=None)
    return g


def _msg(sender: str = "Axis <alerts@axis.bank.in>") -> dict:
    return {
        "id": "m1",
        "sender": sender,
        "subject": "Txn alert",
        "snippet": "debited",
        "labels": [],
    }


@pytest.mark.asyncio
async def test_cache_hit_replays_the_tags_that_drive_the_money_fanout():
    """The regression itself: a cached financial sender must still emit
    financial/payments so `GmailIngestFlow` starts MoneyProcessFlow."""
    llm = _CountingLlm()
    g = _make(
        llm=llm,
        lookup={
            "category": "informational",
            "n": 20,
            "confidence": 1.0,
            "tags": ["financial", "payments"],
        },
    )
    res = await ActivityEnvironment().run(g.classify_email, _msg(), "")

    assert res["source"] == "cache"
    assert llm.calls == 0, "a confident cached sender must not reach the LLM"
    # The assertion that fails on the old code, which hardcoded [].
    assert set(res["tags"]) == {"financial", "payments"}


@pytest.mark.asyncio
async def test_tagless_cached_sender_falls_through_to_the_llm_once():
    """Rows written before tags existed carry none. Short-circuiting on them
    would strand the sender tagless forever, because a cached sender never
    reaches the LLM again — so the cache deliberately declines to answer."""
    llm = _CountingLlm(
        json.dumps(
            {
                "category": "important_read",
                "confidence": 0.8,
                "tags": ["financial", "receipt"],
                "reason": "r",
                "summary": "s",
            }
        )
    )
    g = _make(llm=llm, lookup={"category": "important_read", "n": 9, "confidence": 1.0})

    res = await ActivityEnvironment().run(g.classify_email, _msg(), "")

    assert llm.calls == 1, "a tagless cache row must not short-circuit the LLM"
    assert res["source"] == "llm"
    assert set(res["tags"]) == {"financial", "receipt"}
    # and the LLM's tags are written back, so the NEXT message takes the cache
    g._triage_upsert.assert_awaited_once()
    assert g._triage_upsert.await_args.args[2] == ["financial", "receipt"]


@pytest.mark.asyncio
async def test_empty_tag_list_is_a_real_answer_and_still_short_circuits():
    """`[]` means "the LLM looked and found no tags" — distinct from a row
    that never recorded any. It must keep skipping the LLM, or every untagged
    sender (the majority) would re-enter the LLM on every message."""
    llm = _CountingLlm()
    g = _make(
        llm=llm,
        lookup={"category": "useless", "n": 5, "confidence": 0.9, "tags": []},
    )
    res = await ActivityEnvironment().run(g.classify_email, _msg("spam@x.com"), "")

    assert llm.calls == 0
    assert res["source"] == "cache"
    assert res["tags"] == []
