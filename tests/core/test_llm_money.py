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


@pytest.mark.asyncio
async def test_a_null_field_ignore_answer_is_a_correct_answer_not_a_parse_failure():
    """#411. For marketing and notice mail the prompt asks for `kind: "ignore"`
    and the model answers exactly that, with every other field null.

    `MoneyEvent` gives `payee` and `channel` non-null defaults (`""`,
    `"other"`), so an explicit `null` was REJECTED and the item fell through to
    the `_parse_failed` stub. The booked outcome (`ignore`) was right by
    accident while the record said the model had failed — and `_parse_failed`
    is the number used to judge a model on this lane, so 13 of 24 correct
    answers in a live replay read as failures.
    """
    payload = [{
        "kind": "ignore", "direction": None, "amount": None, "currency": None,
        "payee": None, "category": None, "channel": None, "instrument": None,
        "occurred_on": None, "due_on": None, "is_recurring": None, "confidence": None,
    }]
    out = await _Client(json.dumps(payload)).extract_money_batch([RECEIPT], model="m")
    ev = out[0]
    assert "_parse_failed" not in ev, "a correct ignore is not a parse failure"
    assert ev["kind"] == "ignore" and ev["parser"] == "llm"
    # The nulls fall back to the model's own defaults rather than being stored.
    assert ev["payee"] == "" and ev["payee_key"] == "" and ev["channel"] == "other"
    assert ev["amount"] is None and ev["confidence"] == 1.0


@pytest.mark.asyncio
async def test_a_real_transaction_is_untouched_by_the_null_drop():
    """The other half: dropping nulls must not drop VALUES. Every field the
    model actually filled still lands, including the ones a `None` sibling
    sits next to."""
    payload = [{
        "kind": "transaction", "direction": "out", "amount": "1,936.00", "currency": "inr",
        "payee": "Eleven Labs", "category": "software", "channel": "card",
        "instrument": None, "occurred_on": "2026-08-25", "due_on": None,
        "is_recurring": True, "confidence": 0.95,
    }]
    out = await _Client(json.dumps(payload)).extract_money_batch([RECEIPT], model="m")
    ev = out[0]
    assert "_parse_failed" not in ev
    assert ev["kind"] == "transaction" and ev["direction"] == "out"
    assert ev["amount"] == "1936.00" and ev["currency"] == "INR"
    assert ev["payee"] == "Eleven Labs" and ev["payee_key"] == "eleven labs"
    assert ev["category"] == "software" and ev["channel"] == "card"
    assert ev["occurred_on"] == "2026-08-25" and ev["is_recurring"] is True
    assert ev["confidence"] == 0.95


@pytest.mark.asyncio
async def test_a_genuinely_malformed_item_is_still_a_parse_failure():
    """The null-drop must not launder real breakage into a bookable event.

    Three shapes that are still failures: an unparseable amount, an item that
    is not an object at all, and a null `kind` — the one field with no default,
    so dropping it leaves pydantic to reject a missing required field rather
    than inventing one.
    """
    for payload in (
        [{"kind": "transaction", "amount": "abc", "payee": "Acme"}],
        ["not an object"],
        [{"kind": None, "payee": "Acme", "amount": 10}],
    ):
        out = await _Client(json.dumps(payload)).extract_money_batch([RECEIPT], model="m")
        assert out[0]["_parse_failed"] is True, payload
        assert out[0]["kind"] == "ignore"
