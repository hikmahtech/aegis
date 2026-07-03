"""GmailIngestFlow end-to-end integration smoke.

Real DB, real activities. Only the Gmail fetch is mocked via monkeypatch
on _build_gmail_service. The point is to catch integration bugs —
activity wiring, dataclass codec mismatch, cursor advancement — that
pure unit tests miss.
"""
# NOTE: no `from __future__ import annotations` here — that makes all
# annotations lazy strings, which breaks get_type_hints() resolution when
# @activity.defn functions are defined inside closures.

import json
from pathlib import Path

import pytest
import pytest_asyncio
from temporalio import activity, workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

with workflow.unsafe.imports_passed_through():
    from aegis_worker.activities.channels import ChannelActivities
    from aegis_worker.activities.gmail import GmailActivities
    from aegis_worker.activities.interactions import (
        ApplyTimeoutInput,
        InsertInteractionInput,
        InsertInteractionResult,
        ResolveInteractionInput,
        ResolveInteractionResult,
    )
    from aegis_worker.flows.gmail_ingest import GmailIngestFlow, GmailIngestInput
    from aegis_worker.flows.interaction import InteractionFlow


class _FakeGmailRequest:
    def __init__(self, payload):
        self._payload = payload

    def execute(self):
        return self._payload


class _FakeGmailService:
    """Minimal stub for googleapiclient Gmail service."""

    def __init__(self, messages_by_label):
        self._msgs = messages_by_label  # label -> list of message dicts
        self._current_label = None

    def configure(self, label: str):
        self._current_label = label

    def users(self):
        return self

    def messages(self):
        return self

    def threads(self):
        return self

    def list(self, userId="me", q="", maxResults=50, **kwargs):  # noqa: N803
        msgs = self._msgs.get(self._current_label, [])
        return _FakeGmailRequest({"messages": [{"id": m["id"]} for m in msgs]})

    def get(self, userId="me", id="", format="full", **kwargs):  # noqa: N803
        msgs = self._msgs.get(self._current_label, [])
        match = next((m for m in msgs if m["id"] == id), None)
        if match is None:
            return _FakeGmailRequest({"messages": []})
        # Dual-purpose: single message fetch returns the msg, thread fetch
        # wraps it in a {"messages": [...]} shape.
        return _FakeGmailRequest({**match, "messages": [match]})

    def modify(self, userId="me", id="", body=None, **kwargs):  # noqa: N803
        return _FakeGmailRequest({"id": id})


class _FakeLLM:
    """Stub LLM that mimics the old heuristic classifier so e2e can assert categories."""

    async def think(
        self,
        prompt: str,
        model: str = "",
        system_prompt: str = "",
        max_tokens: int = 0,
        **kwargs,
    ) -> dict:
        import json as _json

        low = prompt.lower()
        if "noreply" in low or "no-reply" in low or "donotreply" in low:
            cat, conf = "useless", 0.9
        elif "urgent" in low or "action required" in low:
            cat, conf = "important_action", 0.85
        elif "receipt" in low or "invoice" in low or "payment" in low:
            cat, conf = "important_read", 0.8
        else:
            cat, conf = "informational", 0.6
        return {
            "response": _json.dumps({"category": cat, "confidence": conf, "reason": "test"}),
            "model": model,
            "prompt_tokens": 10,
            "completion_tokens": 10,
        }


@pytest_asyncio.fixture(loop_scope="function")
async def e2e_channels(db_pool):
    """Ensure two email channels + the required agent rows exist."""
    async with db_pool.acquire() as conn:
        # Agent row — interactions.agent_id FK requires an agents row.
        await conn.execute(
            "INSERT INTO agents (id, name, role, system_prompt_path) "
            "VALUES ('sebas', 'Sebas', 'executive-assistant', 'personalities/sebas') "
            "ON CONFLICT (id) DO NOTHING"
        )
        # Clean any prior test rows
        await conn.execute(
            "DELETE FROM channels WHERE kind='email' AND identifier IN ('t1@x.com', 't2@x.com')"
        )
        await conn.execute(
            "DELETE FROM ingest_idempotency WHERE source_type='gmail' AND external_id LIKE 'e2e-%'"
        )
        await conn.execute("DELETE FROM workflow_runs WHERE workflow_id LIKE 'gmail-e2e-%'")
        await conn.execute(
            "INSERT INTO channels (kind, identifier, config, active) "
            "VALUES ('email', 't1@x.com', $1::text::jsonb, true)",
            json.dumps({"label": "t1", "last_cursor_ts": None, "agent_id": "sebas"}),
        )
        await conn.execute(
            "INSERT INTO channels (kind, identifier, config, active) "
            "VALUES ('email', 't2@x.com', $1::text::jsonb, true)",
            json.dumps({"label": "t2", "last_cursor_ts": None, "agent_id": "sebas"}),
        )
    yield
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM channels WHERE kind='email' AND identifier IN ('t1@x.com', 't2@x.com')"
        )
        await conn.execute(
            "DELETE FROM ingest_idempotency WHERE source_type='gmail' AND external_id LIKE 'e2e-%'"
        )


@pytest.fixture
def token_dir(tmp_path):
    tokens = tmp_path / "tokens"
    tokens.mkdir()
    creds_file = tmp_path / "creds.json"
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
    # Token files so _build_gmail_service finds them (monkeypatched anyway,
    # but GmailActivities.fetch_emails computes the token_path and passes it).
    for label in ("t1", "t2"):
        (tokens / f"{label}.json").write_text(
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
    return tokens, creds_file


# ---------------------------------------------------------------------------
# Module-level stub activity definitions (avoids closure type-hint issues).
# The `_stubs` dict is injected via test-local reassignment after the Worker
# block starts — instead we collect per-test via a simple list captured by
# the closure at *module* scope. Because tests run serially, we reset before
# each test.
# ---------------------------------------------------------------------------

_E2E_INSERT_IA: list = []  # (kind, origin, prompt_prefix)
_E2E_CARD: list = []
_E2E_CAPTURES: list = []  # (source_tag, external_id, title)


@activity.defn(name="send_message")
async def _stub_delivery(agent_id: str, message: str, chat_id: int, keyboard) -> dict:
    return {"ok": True, "message_id": 1}


@activity.defn(name="send_interaction_card")
async def _stub_card(
    iid: str, aid: str, kind: str, prompt: str, options, allow_hint: bool = False
) -> dict:
    _E2E_CARD.append((iid, kind))
    return {"ok": True, "message_id": 99}


@activity.defn(name="update_interaction_message_id")
async def _stub_update_msg(iid: str, mid: int) -> None:
    pass


@activity.defn(name="insert_interaction")
async def _stub_insert(inp: InsertInteractionInput) -> InsertInteractionResult:
    _E2E_INSERT_IA.append((inp.kind, inp.origin, inp.prompt[:60]))
    return InsertInteractionResult(interaction_id=f"ia-{len(_E2E_INSERT_IA)}")


@activity.defn(name="resolve_interaction")
async def _stub_resolve(inp: ResolveInteractionInput) -> ResolveInteractionResult:
    return ResolveInteractionResult(already_resolved=False)


@activity.defn(name="apply_interaction_timeout")
async def _stub_timeout(inp: ApplyTimeoutInput) -> None:
    return None


@activity.defn(name="capture_to_inbox")
async def _stub_capture(
    source_tag: str, external_id: str, title: str, description: str | None = None
) -> str | None:
    _E2E_CAPTURES.append((source_tag, external_id, title))
    return f"task-{external_id}"


@activity.defn(name="send_system_event")
async def _stub_system_event(message: str, chat_id: int = 0) -> dict:
    return {"ok": True}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gmail_ingest_e2e(e2e_channels, token_dir, db_pool, monkeypatch):
    """Feed 3 emails across 2 accounts, verify end-to-end behavior."""
    _E2E_INSERT_IA.clear()
    _E2E_CARD.clear()
    _E2E_CAPTURES.clear()

    tokens, creds = token_dir

    # 3 emails: 1 noreply (useless), 1 with 'urgent' subject (important_action),
    # 1 plain (informational). Distributed across 2 accounts.
    emails_by_label = {
        "t1": [
            {
                "id": "e2e-msg-1",
                "threadId": "th-1",
                "payload": {
                    "headers": [
                        {"name": "From", "value": "noreply@spam.com"},
                        {"name": "Subject", "value": "Daily deals"},
                        {"name": "To", "value": "me@x.com"},
                        {"name": "Date", "value": "Wed, 01 Jan 2025"},
                    ]
                },
                "snippet": "some spam",
                "internalDate": "1700000001000",
            },
            {
                "id": "e2e-msg-2",
                "threadId": "th-2",
                "payload": {
                    "headers": [
                        {"name": "From", "value": "boss@work.com"},
                        {"name": "Subject", "value": "URGENT action required"},
                        {"name": "To", "value": "me@x.com"},
                        {"name": "Date", "value": "Wed, 01 Jan 2025"},
                    ]
                },
                "snippet": "please approve",
                "internalDate": "1700000002000",
            },
        ],
        "t2": [
            {
                "id": "e2e-msg-3",
                "threadId": "th-3",
                "payload": {
                    "headers": [
                        {"name": "From", "value": "colleague@work.com"},
                        {"name": "Subject", "value": "FYI project update"},
                        {"name": "To", "value": "me@x.com"},
                        {"name": "Date", "value": "Wed, 01 Jan 2025"},
                    ]
                },
                "snippet": "project news",
                "internalDate": "1700000003000",
            },
        ],
    }
    fake_svc = _FakeGmailService(emails_by_label)

    def _build(creds_file, token_path):
        label = Path(token_path).stem
        fake_svc.configure(label)
        return fake_svc

    monkeypatch.setattr("aegis_worker.activities.gmail._build_gmail_service", _build)

    # Real activity instances
    gmail_act = GmailActivities(
        gmail_credentials_file=str(creds),
        gmail_token_dir=str(tokens),
        aegis_ui_url="https://aegis.example.com",
        llm_client=_FakeLLM(),
    )
    channel_act = ChannelActivities(db_pool=db_pool)

    all_activities = [
        gmail_act.fetch_emails,
        gmail_act.fetch_thread,
        gmail_act.apply_label,
        gmail_act.classify_email,
        channel_act.list_active_channels,
        channel_act.update_channel_config_key,
        channel_act.ingest_idempotency_claim,
        _stub_delivery,
        _stub_card,
        _stub_update_msg,
        _stub_insert,
        _stub_resolve,
        _stub_timeout,
        _stub_capture,
        _stub_system_event,
    ]

    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue="aegis-e2e",
            workflows=[GmailIngestFlow, InteractionFlow],
            activities=all_activities,
        ),
    ):
        result = await env.client.execute_workflow(
            GmailIngestFlow.run,
            GmailIngestInput(
                agent_id="sebas",
                aegis_ui_url="https://aegis.example.com",
                max_per_account=10,
            ),
            id="gmail-e2e-1",
            task_queue="aegis-e2e",
        )

    # ==============================
    # Asserts — integration behavior
    # ==============================

    # 1. Total processed across 2 accounts = 3 emails
    assert result["processed"] == 3

    # 2. by_category counts — 1 useless + 1 important_action + 1 informational
    assert result["by_category"].get("useless", 0) >= 1
    assert result["by_category"].get("important_action", 0) >= 1

    # 3. ingest_idempotency rows were written for all 3 message IDs
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT external_id FROM ingest_idempotency "
            "WHERE source_type='gmail' AND external_id LIKE 'e2e-msg-%' "
            "ORDER BY external_id"
        )
    assert {r["external_id"] for r in rows} == {"e2e-msg-1", "e2e-msg-2", "e2e-msg-3"}

    # 4. channels.config.last_cursor_ts advanced for both accounts
    async with db_pool.acquire() as conn:
        cursor_rows = await conn.fetch(
            "SELECT identifier, config->>'last_cursor_ts' as cursor "
            "FROM channels WHERE kind='email' AND identifier IN ('t1@x.com', 't2@x.com')"
        )
    by_id = {r["identifier"]: r["cursor"] for r in cursor_rows}
    assert by_id["t1@x.com"] is not None
    assert by_id["t2@x.com"] is not None

    # 5. capture_to_inbox was called for the important_action email (Phase 2)
    assert any(c[0] == "#email" for c in _E2E_CAPTURES), (
        f"expected capture_to_inbox call, got: {_E2E_CAPTURES}"
    )

    # 7. Only 1 capture for important_action (useless + informational don't capture)
    assert len(_E2E_CAPTURES) == 1
    # No approval interaction spawned
    assert not any(ia[0] == "approval" for ia in _E2E_INSERT_IA)


@pytest.mark.asyncio
async def test_gmail_ingest_e2e_idempotent_rerun(e2e_channels, token_dir, db_pool, monkeypatch):
    """Running the flow twice with identical inputs processes emails once."""
    _E2E_INSERT_IA.clear()
    _E2E_CARD.clear()

    tokens, creds = token_dir

    emails_by_label = {
        "t1": [
            {
                "id": "e2e-msg-idem-1",
                "threadId": "th-1",
                "payload": {
                    "headers": [
                        {"name": "From", "value": "a@b.com"},
                        {"name": "Subject", "value": "hi"},
                        {"name": "To", "value": "me@x.com"},
                        {"name": "Date", "value": "Wed, 01 Jan 2025"},
                    ]
                },
                "snippet": "",
                "internalDate": "1700000001000",
            }
        ],
        "t2": [],
    }
    fake_svc = _FakeGmailService(emails_by_label)

    def _build(creds_file, token_path):
        label = Path(token_path).stem
        fake_svc.configure(label)
        return fake_svc

    monkeypatch.setattr("aegis_worker.activities.gmail._build_gmail_service", _build)

    gmail_act = GmailActivities(
        gmail_credentials_file=str(creds),
        gmail_token_dir=str(tokens),
        aegis_ui_url="https://aegis.example.com",
        llm_client=_FakeLLM(),
    )
    channel_act = ChannelActivities(db_pool=db_pool)

    all_activities = [
        gmail_act.fetch_emails,
        gmail_act.fetch_thread,
        gmail_act.apply_label,
        gmail_act.classify_email,
        channel_act.list_active_channels,
        channel_act.update_channel_config_key,
        channel_act.ingest_idempotency_claim,
        _stub_delivery,
        _stub_card,
        _stub_update_msg,
        _stub_insert,
        _stub_resolve,
        _stub_timeout,
        _stub_capture,
        _stub_system_event,
    ]

    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue="tq-idem",
            workflows=[GmailIngestFlow, InteractionFlow],
            activities=all_activities,
        ),
    ):
        # First run: processes 1 email
        r1 = await env.client.execute_workflow(
            GmailIngestFlow.run,
            GmailIngestInput(
                agent_id="sebas",
                aegis_ui_url="https://aegis.example.com",
                max_per_account=10,
            ),
            id="gmail-e2e-idem-1",
            task_queue="tq-idem",
        )

        # Second run: same email -> idempotency_claim returns False
        r2 = await env.client.execute_workflow(
            GmailIngestFlow.run,
            GmailIngestInput(
                agent_id="sebas",
                aegis_ui_url="https://aegis.example.com",
                max_per_account=10,
            ),
            id="gmail-e2e-idem-2",
            task_queue="tq-idem",
        )

    assert r1["processed"] == 1
    assert r2["processed"] == 0  # dup skipped
