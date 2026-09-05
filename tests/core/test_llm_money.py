"""Task 6 — `LLMClient.extract_money_batch`, the v2 money extractor.

The v1 batch extractor (deleted 2026-09) asked "is this a receipt?" over a
200-char snippet. This one reads the FULL body and returns one `MoneyEvent` per email,
so a declined payment, an autopay reminder and a paid invoice stop looking
alike. The failure semantics are what these tests pin: a truncation degrades to
one stub per input (a bad token budget must not take MoneyProcessFlow down), a
single garbage item degrades to a stub for that item only, and any other
batch-level failure still raises so a real outage is not laundered into
"nothing to book".
"""

from __future__ import annotations

import json
import re

import pytest
from aegis.llm import _LLM_EVENT_FIELDS, _MONEY_EVENT_PROMPT, LLMClient, LLMTruncationError

RECEIPT = {
    "id": "r1", "account": "arshad-personal", "message_id": "m1",
    "sender": "Google Play <googleplay-noreply@google.com>",
    "subject": "Payment declined for Medium subscription",
    "body_plain": "Your subscription will be cancelled. Amount Due ₹199.00 Fix by 15 Sept 2026",
    "received_at": "2026-09-03T10:00:00+00:00",
}


class _Client(LLMClient):
    def __init__(self, response):
        super().__init__(base_url="http://x", api_key="k")
        self._response = response

    async def think(self, **kw):
        if isinstance(self._response, Exception):
            raise self._response
        assert kw["purpose"] == "money_event_extraction" and kw["max_tokens"] == 4000
        assert "Medium subscription" in kw["prompt"] and "Fix by" in kw["prompt"]
        return {"response": self._response}


@pytest.mark.asyncio
async def test_parses_a_failed_payment_into_a_money_event():
    payload = [{
        "kind": "failed", "direction": "out", "amount": 199, "currency": "INR",
        "payee": "Medium", "category": "media", "channel": "other", "instrument": None,
        "occurred_on": None, "due_on": "2026-09-15", "is_recurring": True, "confidence": 0.9,
    }]
    out = await _Client("```json\n" + json.dumps(payload) + "\n```").extract_money_batch(
        [RECEIPT], model="m"
    )
    assert len(out) == 1
    ev = out[0]
    assert ev["kind"] == "failed" and ev["amount"] == "199.00" and ev["due_on"] == "2026-09-15"
    assert ev["payee_key"] == "medium" and ev["parser"] == "llm" and ev["source_class"] == "other"
    assert "_parse_failed" not in ev


@pytest.mark.asyncio
async def test_receipt_channel_gets_receipt_source_class_and_unknown_keys_are_dropped():
    payload = [{"kind": "transaction", "direction": "out", "amount": "1,936.00", "currency": "INR",
                "payee": "Eleven Labs", "channel": "receipt", "occurred_on": "2026-08-25",
                "confidence": 0.95, "bogus": 1}]
    out = await _Client(json.dumps(payload)).extract_money_batch([RECEIPT], model="m")
    assert out[0]["source_class"] == "receipt" and out[0]["amount"] == "1936.00"


@pytest.mark.asyncio
async def test_fields_the_prompt_never_asks_for_are_refused():
    """The email body is spliced straight into the prompt, so every key the
    model emits is reachable by whoever wrote the email. Three of `MoneyEvent`'s
    fields decide where money lands and are NOT in the prompt: `account` wins
    over the category→account map in `post_event` (`event.account or
    account_for(...)`), `entity` picks the ledger, and `ref` is free-text
    provenance. A model-fields allowlist admits all three. Only the twelve keys
    the prompt actually asks for may cross this boundary; the rest keep their
    defaults for the caller to set from the mailbox."""
    payload = [{
        "kind": "transaction", "direction": "out", "amount": 500, "currency": "INR",
        "payee": "Acme", "channel": "upi",
        "entity": "hikmah", "account": "expenses:hikmah:infra", "ref": "x",
    }]
    out = await _Client(json.dumps(payload)).extract_money_batch([RECEIPT], model="m")
    ev = out[0]
    assert ev["entity"] == "personal", "the model must not choose the ledger"
    assert ev["account"] is None, "the model must not bypass the account map"
    assert ev["ref"] is None
    # The legitimate fields still land, so this is a filter and not a wipe.
    assert ev["payee"] == "Acme" and ev["amount"] == "500.00" and ev["channel"] == "upi"


def test_the_allowlist_matches_the_prompt():
    """The allowlist is only safe while it equals what the prompt asks for.
    Add a field to the prompt and forget the frozenset and the model's answer
    is silently dropped; add it to the frozenset alone and an unasked-for key
    becomes reachable again. Derive the prompt's list from the prompt itself so
    neither drift can pass CI."""
    prompted = set(re.findall(r"^- (\w+):", _MONEY_EVENT_PROMPT, re.M))
    assert prompted == set(_LLM_EVENT_FIELDS)


@pytest.mark.asyncio
async def test_bad_item_is_flagged_not_raised():
    payload = [{"kind": "nonsense"}]
    out = await _Client(json.dumps(payload)).extract_money_batch([RECEIPT], model="m")
    assert out[0]["_parse_failed"] is True and out[0]["kind"] == "ignore"


@pytest.mark.asyncio
async def test_truncation_returns_stubs_and_other_errors_raise():
    out = await _Client(LLMTruncationError("cut")).extract_money_batch([RECEIPT], model="m")
    assert out[0]["_parse_failed"] is True
    with pytest.raises(RuntimeError):
        await _Client(RuntimeError("down")).extract_money_batch([RECEIPT], model="m")


@pytest.mark.asyncio
async def test_empty_input_is_empty_output():
    assert await _Client("[]").extract_money_batch([], model="m") == []
