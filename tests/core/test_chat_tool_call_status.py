"""`chat_tool_calls.status` must not say `success` when the tool failed.

Executors report failure by RETURNING an error envelope, not by raising —
`_exec_infra` turns a non-zero exit into `{"error": ..., "exit_code": ...}` so
the model can read and relay it. Only a raise reached the loop's `except` arms,
so every such failure was stored as `status='success'` with the error sitting in
`result`, and any "which tools are failing?" query answered "none".

That is exactly how the infra tools stayed broken and invisible from 2026-07-16
to 2026-08-28: four calls in production recorded `status=success` while every one
of them returned `bash: /opt/aegis/scripts/infra/*.sh: No such file or directory`
with `exit_code: 127`.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

from aegis.config import Settings
from aegis.services.chat import send_message


def _settings() -> Settings:
    return Settings(
        database_url="postgresql://test:test@localhost/test",
        litellm_url="https://litellm.test/v1",
        temporal_ui_url="https://temporal.test",
        n8n_ui_url="https://n8n.test",
        admin_username="admin",
        admin_password="admin",
        n8n_webhook_secret="test-secret",
    )


def _mock_pool():
    pool = AsyncMock()
    pool.fetchrow.return_value = {
        "id": "pandoras-actor",
        "name": "Pandora's Actor",
        "system_prompt_path": "personalities/pandoras-actor/SOUL.md",
    }
    pool.fetch.return_value = []
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value="balanced")
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool.acquire = MagicMock(return_value=ctx)
    return pool


def _llm_calling(tool_name: str, arguments: str = '{"context": "swarm"}'):
    """First turn asks for the tool; second turn answers, ending the loop.

    `arguments` must satisfy the tool's real JSON schema — the loop validates
    before dispatch, and an invalid call records `validation_failed` and never
    reaches the code under test.
    """
    llm = AsyncMock()
    llm.chat = AsyncMock(
        side_effect=[
            {
                "response": "",
                "tool_calls": [{"id": "c1", "name": tool_name, "arguments": arguments}],
                "model": "kimi-k2.5",
                "prompt_tokens": 0,
                "completion_tokens": 0,
            },
            {
                "response": "done",
                "tool_calls": [],
                "model": "kimi-k2.5",
                "prompt_tokens": 0,
                "completion_tokens": 0,
            },
        ]
    )
    return llm


async def _run_with_tool_result(payload: str) -> list:
    """Drive one tool call whose executor RETURNS `payload`; return recorded calls."""
    recorded: list = []

    async def _fake_record(pool, **kwargs):
        recorded.append(kwargs)

    async def _fake_execute(pool, name, args, ctx=None, **kw):
        return payload

    with (
        patch("aegis.services.chat.record_tool_call", _fake_record),
        patch("aegis.services.chat._execute_tool", _fake_execute),
    ):
        await send_message(
            _mock_pool(),
            _llm_calling("list_services"),
            "pandoras-actor",
            "list the services",
            settings=_settings(),
        )
    return recorded


async def test_returned_error_envelope_is_recorded_as_error():
    """The production case: exit 127 from a missing infra script."""
    payload = json.dumps(
        {
            "error": "bash: /opt/aegis/scripts/infra/infra_list_services.sh: "
            "No such file or directory",
            "exit_code": 127,
        }
    )
    recorded = await _run_with_tool_result(payload)
    assert recorded, "the tool call was never recorded"
    assert recorded[0]["status"] == "error"
    # The payload must still be stored — the status is an index, not a replacement.
    assert recorded[0]["tool_result"]["exit_code"] == 127


async def test_a_genuine_success_is_still_success():
    """Falsifiability control: the downgrade must not fire on healthy output."""
    recorded = await _run_with_tool_result(json.dumps([{"name": "aegis_core"}]))
    assert recorded[0]["status"] == "success"


async def test_a_result_with_an_empty_error_field_is_not_downgraded():
    """`error: null` / `error: ""` is how some tools say 'no error'."""
    recorded = await _run_with_tool_result(json.dumps({"ok": True, "error": None}))
    assert recorded[0]["status"] == "success"


async def test_plain_prose_is_left_as_success():
    """Documented limit: a prose apology is indistinguishable from an answer.

    Pinned deliberately so the narrow detection is a decision on record rather
    than an accident someone later "fixes" by guessing from free text.
    """
    recorded = await _run_with_tool_result("The coding host is not configured.")
    assert recorded[0]["status"] == "success"
