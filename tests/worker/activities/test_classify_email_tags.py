"""GmailActivities.classify_email — tag extraction from LLM JSON."""

from __future__ import annotations

import json

import pytest
from aegis_worker.activities.gmail import GmailActivities
from temporalio.testing import ActivityEnvironment


class _FakeLlm:
    def __init__(self, response: str):
        self._response = response
        self.last_prompt: str | None = None

    async def think(self, **kwargs):
        self.last_prompt = kwargs.get("prompt", "")
        return {"response": self._response, "model": "qwen3:14b"}


@pytest.fixture
def gmail_with_llm():
    def _factory(llm_response: str) -> GmailActivities:
        return GmailActivities(
            gmail_credentials_file="/tmp/unused-creds.json",
            gmail_token_dir="/tmp/unused-tokens",
            aegis_ui_url="https://aegis.example.com",
            llm_client=_FakeLlm(llm_response),
        )

    return _factory


_MSG = {
    "id": "m1",
    "sender": "billing@stripe.com",
    "subject": "Your receipt",
    "snippet": "Thanks for your payment of $19 for subscription",
}


@pytest.mark.asyncio
async def test_tags_parsed_from_llm_json(gmail_with_llm):
    gmail = gmail_with_llm(
        json.dumps(
            {
                "category": "important_read",
                "confidence": 0.9,
                "reason": "Stripe receipt",
                "tags": ["financial", "payments", "receipt"],
            }
        )
    )
    result = await ActivityEnvironment().run(gmail.classify_email, _MSG, "")
    assert result["category"] == "important_read"
    assert result["tags"] == ["financial", "payments", "receipt"]
    assert result["source"] == "llm"


@pytest.mark.asyncio
async def test_unknown_tags_filtered(gmail_with_llm):
    gmail = gmail_with_llm(
        json.dumps(
            {
                "category": "informational",
                "confidence": 0.6,
                "tags": ["financial", "not-a-real-tag", "spam", "payments"],
            }
        )
    )
    result = await ActivityEnvironment().run(gmail.classify_email, _MSG, "")
    assert result["tags"] == ["financial", "payments"]


@pytest.mark.asyncio
async def test_missing_tags_defaults_to_empty(gmail_with_llm):
    gmail = gmail_with_llm(
        json.dumps({"category": "useless", "confidence": 0.8, "reason": "marketing"})
    )
    result = await ActivityEnvironment().run(gmail.classify_email, _MSG, "")
    assert result["tags"] == []


@pytest.mark.asyncio
async def test_tags_deduped_and_lowercased(gmail_with_llm):
    gmail = gmail_with_llm(
        json.dumps(
            {
                "category": "important_read",
                "confidence": 0.9,
                "tags": ["Financial", "financial", "PAYMENTS", "payments"],
            }
        )
    )
    result = await ActivityEnvironment().run(gmail.classify_email, _MSG, "")
    assert result["tags"] == ["financial", "payments"]


@pytest.mark.asyncio
async def test_non_list_tags_defaults_to_empty(gmail_with_llm):
    gmail = gmail_with_llm(
        json.dumps({"category": "informational", "confidence": 0.6, "tags": "financial"})
    )
    result = await ActivityEnvironment().run(gmail.classify_email, _MSG, "")
    assert result["tags"] == []


@pytest.mark.asyncio
async def test_fallback_path_returns_empty_tags(gmail_with_llm):
    gmail = gmail_with_llm("not valid json at all")
    result = await ActivityEnvironment().run(gmail.classify_email, _MSG, "")
    assert result["source"] == "fallback"
    assert result["category"] == "informational"
    assert result["tags"] == []


@pytest.mark.asyncio
async def test_no_llm_client_returns_empty_tags():
    gmail = GmailActivities(
        gmail_credentials_file="/tmp/unused",
        gmail_token_dir="/tmp/unused",
        llm_client=None,
    )
    result = await ActivityEnvironment().run(gmail.classify_email, _MSG, "")
    assert result == {
        "category": "informational",
        "confidence": 0.5,
        "tags": [],
        "reason": "",
        "summary": "",
        "lane": "own",
        "source": "fallback",
    }


@pytest.mark.asyncio
async def test_lane_surfaced_in_result_and_prompt_when_forwarded(gmail_with_llm):
    """A forwarded message carries `lane` in the msg dict; classify_email
    must both inject "Forwarded from: <lane>" into the prompt AND surface
    `lane` in the result so downstream routing can tag the Todoist task."""
    gmail = gmail_with_llm(
        json.dumps(
            {
                "category": "important_action",
                "confidence": 0.9,
                "reason": "Acme alert",
                "summary": "x",
                "tags": ["security"],
            }
        )
    )
    forwarded = {**_MSG, "lane": "acme"}
    result = await ActivityEnvironment().run(gmail.classify_email, forwarded, "")
    assert result["lane"] == "acme"
    assert "Forwarded from: acme" in gmail.llm_client.last_prompt


@pytest.mark.asyncio
async def test_own_lane_omits_forwarded_header_from_prompt(gmail_with_llm):
    """Direct-delivery emails (lane='own') must NOT include a `Forwarded from`
    line — the LLM should treat them as native to the mailbox."""
    gmail = gmail_with_llm(json.dumps({"category": "informational", "confidence": 0.6, "tags": []}))
    result = await ActivityEnvironment().run(gmail.classify_email, _MSG, "")
    assert result["lane"] == "own"
    assert "Forwarded from" not in gmail.llm_client.last_prompt


@pytest.mark.asyncio
async def test_reason_and_summary_returned_from_llm(gmail_with_llm):
    """classify_email must surface ``reason`` and ``summary`` so the
    Todoist description always has substantive content even when the
    Gmail body fetch fails — the captured task otherwise lands with
    just sender + link and no clue what the email is about."""
    gmail = gmail_with_llm(
        json.dumps(
            {
                "category": "important_action",
                "confidence": 0.95,
                "reason": "Card decline blocks autopay",
                "summary": (
                    "Stripe says the Visa ending 4242 was declined on the May "
                    "subscription. Update payment method by Jun 5 or service "
                    "will pause."
                ),
                "tags": ["financial", "payments"],
            }
        )
    )
    result = await ActivityEnvironment().run(gmail.classify_email, _MSG, "")
    assert result["reason"] == "Card decline blocks autopay"
    assert "Visa ending 4242" in result["summary"]
    assert result["category"] == "important_action"


@pytest.mark.asyncio
async def test_missing_summary_defaults_to_empty(gmail_with_llm):
    """Pre-prompt-rev LLM responses (no summary field) must not break the
    contract — return empty string so downstream callers can fall back."""
    gmail = gmail_with_llm(
        json.dumps({"category": "important_read", "confidence": 0.8, "reason": "newsletter"})
    )
    result = await ActivityEnvironment().run(gmail.classify_email, _MSG, "")
    assert result["summary"] == ""
    assert result["reason"] == "newsletter"
