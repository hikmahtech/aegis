"""GmailActivities — fetch, reauth-URL, label."""

from __future__ import annotations

import json

import pytest
from aegis_worker.activities.gmail import (
    FetchEmailsInput,
    FetchEmailsResult,
    GmailActivities,
    GmailAuthExpiredError,
)
from temporalio.testing import ActivityEnvironment


class _FakeGmailRequest:
    def __init__(self, payload, raise_auth):
        self._payload = payload
        self._raise_auth = raise_auth

    def execute(self):
        if self._raise_auth:
            from google.auth.exceptions import RefreshError

            raise RefreshError("invalid_grant")
        return self._payload


class _FakeLabelsEndpoint:
    """Stand-in for svc.users().labels()."""

    def __init__(self, labels: list[dict]):
        self._labels = labels

    def list(self, **kwargs):
        return _FakeGmailRequest({"labels": self._labels}, False)


class _FakeGmailService:
    """Stand-in for the googleapiclient build() result."""

    def __init__(
        self,
        messages: list[dict],
        raise_auth: bool = False,
        labels: list[dict] | None = None,
    ):
        self._messages = messages
        self._raise_auth = raise_auth
        # Default: each test label_id present in messages maps to a name of
        # the same shape, so tests can opt in to lane derivation by setting
        # `labelIds: ["Label_forwarded_acme"]` on a message and
        # supplying a matching label dict.
        self._labels = labels or []

    def users(self):
        return self

    def messages(self):
        return self

    def labels(self):
        return _FakeLabelsEndpoint(self._labels)

    def list(self, **kwargs):
        return _FakeGmailRequest(
            {"messages": [{"id": m["id"]} for m in self._messages]},
            self._raise_auth,
        )

    def get(self, id, **kwargs):
        match = next((m for m in self._messages if m["id"] == id), None)
        return _FakeGmailRequest(match or {}, self._raise_auth)

    def modify(self, **kwargs):
        return _FakeGmailRequest({"id": kwargs.get("id", "")}, self._raise_auth)


@pytest.fixture
def gmail(tmp_path):
    token_dir = tmp_path / "tokens"
    token_dir.mkdir()
    (token_dir / "sebas.json").write_text(
        json.dumps(
            {
                "token": "tok",
                "refresh_token": "rt",
                "client_id": "cid",
                "client_secret": "cs",
                "token_uri": "https://oauth2.googleapis.com/token",
                "scopes": ["https://www.googleapis.com/auth/gmail.modify"],
            }
        )
    )
    creds_file = tmp_path / "google_credentials.json"
    creds_file.write_text(
        json.dumps(
            {
                "installed": {
                    "client_id": "cid",
                    "client_secret": "cs",
                    "redirect_uris": ["http://localhost/cb"],
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                }
            }
        )
    )
    return GmailActivities(
        gmail_credentials_file=str(creds_file),
        gmail_token_dir=str(token_dir),
        aegis_ui_url="https://aegis.example.com",
    )


@pytest.mark.asyncio
async def test_fetch_emails_returns_messages(gmail, monkeypatch):
    fake_service = _FakeGmailService(
        [
            {
                "id": "msg-1",
                "threadId": "thr-1",
                "payload": {
                    "headers": [
                        {"name": "From", "value": "a@b.com"},
                        {"name": "Subject", "value": "hello"},
                        {"name": "To", "value": "me@mine.com"},
                        {"name": "Date", "value": "Wed, 01 Jan 2025 10:00:00 +0000"},
                    ]
                },
                "snippet": "body snippet",
                "internalDate": "1700000000000",
            },
        ]
    )
    monkeypatch.setattr(
        "aegis_worker.activities.gmail._build_gmail_service",
        lambda *a, **k: fake_service,
    )
    env = ActivityEnvironment()
    result = await env.run(
        gmail.fetch_emails,
        FetchEmailsInput(
            account_label="sebas",
            query="is:unread",
            since_cursor_ts=None,
            max_results=10,
        ),
    )
    assert isinstance(result, FetchEmailsResult)
    assert len(result.messages) == 1
    assert result.messages[0]["id"] == "msg-1"
    assert result.messages[0]["sender"] == "a@b.com"
    assert result.messages[0]["subject"] == "hello"
    # Direct-delivery email with no `forwarded/...` label defaults to "own".
    assert result.messages[0]["lane"] == "own"
    assert result.messages[0]["labels"] == []
    assert result.latest_internal_date_ms == 1700000000000


@pytest.mark.asyncio
async def test_fetch_emails_derives_lane_from_forwarded_label(gmail, monkeypatch):
    """An email carrying a `forwarded/<lane>` label resolves to that lane.

    Locks the lane-derivation contract for the user's Gmail filter setup
    (e.g. `forwarded/acme`, `forwarded/ansaar`) so the downstream
    classifier + Todoist description can surface forwarding provenance.
    """
    fake_service = _FakeGmailService(
        [
            {
                "id": "msg-stp-1",
                "threadId": "thr-stp",
                "labelIds": ["INBOX", "Label_42", "Label_99"],
                "payload": {
                    "headers": [
                        {"name": "From", "value": "alerts@example.com"},
                        {"name": "Subject", "value": "Security alert"},
                        {"name": "To", "value": "user@acme.com"},
                        {"name": "Date", "value": "Wed, 01 Jan 2025 10:00:00 +0000"},
                    ]
                },
                "snippet": "Login from new device",
                "internalDate": "1700000005000",
            },
        ],
        labels=[
            {"id": "INBOX", "name": "INBOX"},
            {"id": "Label_42", "name": "forwarded/acme"},
            {"id": "Label_99", "name": "Important"},
        ],
    )
    monkeypatch.setattr(
        "aegis_worker.activities.gmail._build_gmail_service",
        lambda *a, **k: fake_service,
    )
    env = ActivityEnvironment()
    result = await env.run(
        gmail.fetch_emails,
        FetchEmailsInput(
            account_label="user-swarm",
            query="is:unread",
            since_cursor_ts=None,
            max_results=10,
        ),
    )
    msg = result.messages[0]
    assert msg["lane"] == "acme"
    assert "forwarded/acme" in msg["labels"]


@pytest.mark.asyncio
async def test_fetch_emails_first_forwarded_label_wins(gmail, monkeypatch):
    """If more than one `forwarded/<lane>` label is somehow present, the
    first one in labelIds order is the lane. Defensive against future
    Gmail filter changes that might tag a message twice."""
    fake_service = _FakeGmailService(
        [
            {
                "id": "msg-mix",
                "threadId": "thr-mix",
                "labelIds": ["Label_a", "Label_b"],
                "payload": {
                    "headers": [
                        {"name": "From", "value": "x@x.com"},
                        {"name": "Subject", "value": "hi"},
                        {"name": "To", "value": "me@mine.com"},
                        {"name": "Date", "value": "Wed, 01 Jan 2025"},
                    ]
                },
                "snippet": "",
                "internalDate": "1700000006000",
            },
        ],
        labels=[
            {"id": "Label_a", "name": "forwarded/ansaar"},
            {"id": "Label_b", "name": "forwarded/acme"},
        ],
    )
    monkeypatch.setattr(
        "aegis_worker.activities.gmail._build_gmail_service",
        lambda *a, **k: fake_service,
    )
    env = ActivityEnvironment()
    result = await env.run(
        gmail.fetch_emails,
        FetchEmailsInput(
            account_label="user-swarm",
            query="",
            since_cursor_ts=None,
            max_results=10,
        ),
    )
    assert result.messages[0]["lane"] == "ansaar"


@pytest.mark.asyncio
async def test_fetch_emails_raises_auth_expired(gmail, monkeypatch):
    fake_service = _FakeGmailService([{"id": "x"}], raise_auth=True)
    monkeypatch.setattr(
        "aegis_worker.activities.gmail._build_gmail_service",
        lambda *a, **k: fake_service,
    )
    env = ActivityEnvironment()
    with pytest.raises(GmailAuthExpiredError) as exc_info:
        await env.run(
            gmail.fetch_emails,
            FetchEmailsInput(
                account_label="sebas",
                query="is:unread",
                since_cursor_ts=None,
                max_results=10,
            ),
        )
    assert exc_info.value.account_label == "sebas"
    assert "reauth/sebas/initiate" in exc_info.value.reauth_url
    assert "https://aegis.example.com" in exc_info.value.reauth_url


@pytest.mark.asyncio
async def test_apply_label_ok(gmail, monkeypatch):
    fake_service = _FakeGmailService([{"id": "msg-1"}])
    monkeypatch.setattr(
        "aegis_worker.activities.gmail._build_gmail_service",
        lambda *a, **k: fake_service,
    )
    env = ActivityEnvironment()
    result = await env.run(gmail.apply_label, "sebas", "msg-1", "READ")
    assert result["ok"] is True


@pytest.mark.asyncio
async def test_ingest_email_to_kg_writes_content_with_metadata(gmail):
    """An `important_action` email lands in the KG with sender + category
    metadata so Raphael can recall it later via search/ask tools."""
    from unittest.mock import AsyncMock

    kc = AsyncMock()
    kc.ingest_content = AsyncMock(return_value={"job_id": "job-mail-1"})
    gmail.knowledge_connector = kc
    env = ActivityEnvironment()
    msg = {
        "id": "msg-abc",
        "subject": "Action required: domain renewal",
        "sender": "Cloudflare <noreply@cloudflare.com>",
        "permalink": "https://mail.google.com/.../msg-abc",
        "snippet": "Your domain example.com expires in 30 days.",
        "lane": "acme",
    }
    classification = {
        "category": "important_action",
        "confidence": 0.92,
        "tags": ["payments", "deadline"],
        "lane": "acme",
    }
    result = await env.run(
        gmail.ingest_email_to_kg,
        msg,
        "Full thread body about renewal flow",
        classification,
    )
    assert result["ingested"] is True
    kc.ingest_content.assert_awaited_once()
    kwargs = kc.ingest_content.await_args.kwargs
    assert kwargs["source_type"] == "email"
    assert "renewal" in kwargs["raw_text"].lower()
    assert "important_action" in kwargs["tags"]
    assert "lane:acme" in kwargs["tags"]
    assert kwargs["metadata"]["sender"].startswith("Cloudflare")
    assert kwargs["metadata"]["lane"] == "acme"


@pytest.mark.asyncio
async def test_ingest_email_to_kg_no_connector_returns_no_op(gmail):
    """No knowledge_connector configured → activity returns cleanly."""
    gmail.knowledge_connector = None
    env = ActivityEnvironment()
    result = await env.run(
        gmail.ingest_email_to_kg,
        {"id": "m", "subject": "x", "snippet": "y"},
        "",
        {"category": "informational"},
    )
    assert result["ingested"] is False
    assert result["reason"] == "no_connector"


@pytest.mark.asyncio
async def test_ingest_email_to_kg_empty_body_skipped(gmail):
    """Empty snippet AND empty thread_content → skip ingest cleanly."""
    from unittest.mock import AsyncMock

    gmail.knowledge_connector = AsyncMock()
    env = ActivityEnvironment()
    result = await env.run(
        gmail.ingest_email_to_kg,
        {"id": "m", "subject": "x", "snippet": ""},
        "",
        {"category": "informational"},
    )
    assert result["ingested"] is False
    assert result["reason"] == "empty_body"


# ----------- classify_email LLM truncation / empty-response guard -----------


def _make_gmail_with_llm(tmp_path, llm_client):
    """Build a GmailActivities instance wired to a fake LLM client."""
    token_dir = tmp_path / "tokens"
    token_dir.mkdir()
    (token_dir / "sebas.json").write_text(
        json.dumps(
            {
                "token": "tok",
                "refresh_token": "rt",
                "client_id": "cid",
                "client_secret": "cs",
                "token_uri": "https://oauth2.googleapis.com/token",
                "scopes": ["https://www.googleapis.com/auth/gmail.modify"],
            }
        )
    )
    creds_file = tmp_path / "gc.json"
    creds_file.write_text(
        json.dumps(
            {
                "installed": {
                    "client_id": "cid",
                    "client_secret": "cs",
                    "redirect_uris": ["http://localhost/cb"],
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                }
            }
        )
    )
    acts = GmailActivities(
        gmail_credentials_file=str(creds_file),
        gmail_token_dir=str(token_dir),
        aegis_ui_url="https://aegis.example.com",
    )
    acts.llm_client = llm_client
    acts.model_balanced = "gpt-oss:20b"
    acts.db_pool = None
    return acts


@pytest.mark.asyncio
async def test_classify_email_falls_back_on_truncated_response(tmp_path):
    """When the LLM raises LLMTruncationError (empty content + finish_reason=length),
    classify_email must NOT propagate the exception.  It falls back to the
    'informational' default with source='fallback' so the ingest tick continues."""
    from unittest.mock import AsyncMock

    from aegis.llm import LLMTruncationError

    llm = AsyncMock()
    llm.think = AsyncMock(
        side_effect=LLMTruncationError(
            "model=gpt-oss:20b returned empty content with finish_reason=length"
        )
    )
    acts = _make_gmail_with_llm(tmp_path, llm)
    msg = {
        "id": "msg-trunc",
        "sender": "alerts@example.com",
        "subject": "Test",
        "snippet": "Something important",
        "labels": [],
        "lane": "own",
    }
    env = ActivityEnvironment()
    result = await env.run(acts.classify_email, msg)
    assert result["category"] == "informational"
    assert result["source"] == "fallback"


@pytest.mark.asyncio
async def test_classify_email_falls_back_on_empty_response_string(tmp_path):
    """classify_email must also gracefully handle a non-empty-but-blank response
    string (the central guard adds the explicit empty-check before json.loads)."""
    from unittest.mock import AsyncMock

    llm = AsyncMock()
    llm.think = AsyncMock(
        return_value={
            "response": "   ",  # whitespace-only — not valid JSON
            "model": "gpt-oss:20b",
            "prompt_tokens": 50,
            "completion_tokens": 0,
        }
    )
    acts = _make_gmail_with_llm(tmp_path, llm)
    msg = {
        "id": "msg-blank",
        "sender": "noreply@example.com",
        "subject": "Hi",
        "snippet": "body",
        "labels": [],
        "lane": "own",
    }
    env = ActivityEnvironment()
    result = await env.run(acts.classify_email, msg)
    assert result["category"] == "informational"
    assert result["source"] == "fallback"


@pytest.mark.asyncio
async def test_classify_email_returns_correct_category_on_valid_response(tmp_path):
    """Normal LLM response → classification parsed and returned correctly."""
    from unittest.mock import AsyncMock

    llm = AsyncMock()
    llm.think = AsyncMock(
        return_value={
            "response": (
                '{"category": "important_action", "confidence": 0.95, '
                '"reason": "payment required", "summary": "Pay now.", "tags": ["financial"]}'
            ),
            "model": "gpt-oss:20b",
            "prompt_tokens": 40,
            "completion_tokens": 30,
        }
    )
    acts = _make_gmail_with_llm(tmp_path, llm)
    msg = {
        "id": "msg-ok",
        "sender": "billing@stripe.com",
        "subject": "Invoice due",
        "snippet": "Pay now",
        "labels": [],
        "lane": "own",
    }
    env = ActivityEnvironment()
    result = await env.run(acts.classify_email, msg)
    assert result["category"] == "important_action"
    assert result["confidence"] == 0.95
    assert result["source"] == "llm"
    assert "financial" in result["tags"]


@pytest.mark.asyncio
async def test_classify_email_uses_higher_max_tokens(tmp_path):
    """classify_email must call think() with max_tokens >= 512 so reasoning
    models have budget for hidden reasoning_content plus the JSON payload."""
    from unittest.mock import AsyncMock

    llm = AsyncMock()
    llm.think = AsyncMock(
        return_value={
            "response": (
                '{"category": "informational", "confidence": 0.6, '
                '"reason": "digest", "summary": "Low value.", "tags": []}'
            ),
            "model": "gpt-oss:20b",
            "prompt_tokens": 30,
            "completion_tokens": 20,
        }
    )
    acts = _make_gmail_with_llm(tmp_path, llm)
    msg = {
        "id": "msg-tok",
        "sender": "digest@example.com",
        "subject": "Weekly digest",
        "snippet": "...",
        "labels": [],
        "lane": "own",
    }
    env = ActivityEnvironment()
    await env.run(acts.classify_email, msg)

    assert llm.think.called
    _, kwargs = llm.think.call_args
    assert kwargs.get("max_tokens", 0) >= 512, (
        f"max_tokens={kwargs.get('max_tokens')} is too low for a reasoning model; "
        "reasoning_content will crowd out the JSON payload"
    )
