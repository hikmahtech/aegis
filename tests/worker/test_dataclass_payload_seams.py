"""Untyped dict → dataclass seams: every key must be a real field.

Core never imports worker code, so a workflow is started by NAME with a plain
dict and Temporal's data converter fills the dataclass. That converter
**silently ignores an unknown key** (verified against temporalio 1.30.0), so a
typo or a rename on one side of the seam does not error anywhere — the field
just takes its default. For the books writer the defaults are exactly the
dangerous values: an empty `op` is a write that reports "unknown books write"
for a transaction the user was told to expect.

These tests live in `tests/worker/` because they are the only place both
packages are importable at once — that is the whole point: nothing else in the
repo compares the two sides of these seams.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aegis.services import books
from aegis.services.tools.base import ToolContext
from aegis.services.tools.ledger import LEDGER_WRITE_WAIT_S, _dispatch_books_write
from aegis_worker.flows.books_write import BooksWriteInput


def _field_names(cls) -> set[str]:
    return {f.name for f in dataclasses.fields(cls)}


def _capturing_client() -> AsyncMock:
    client = AsyncMock()
    client.start_workflow.return_value = MagicMock()
    return client


def _ctx(client) -> ToolContext:
    return ToolContext(
        agent_id="sebas",
        task_id=None,
        knowledge_connector=None,
        finance_connector=None,
        chat_context=None,
        settings=SimpleNamespace(),
        temporal_client=client,
    )


def _start_payload(client) -> dict:
    client.start_workflow.assert_awaited_once()
    args, _ = client.start_workflow.call_args
    assert isinstance(args[1], dict), "the seam under test is a dict, not a typed arg"
    return args[1]


@pytest.mark.asyncio
async def test_books_write_payload_keys_are_all_books_write_input_fields():
    """The three ledger writers → `BooksWriteFlow(BooksWriteInput)`.

    Every field here fails silently in a way that reads as success. A dropped
    `op` or `payload` makes the flow report "unknown books write ''" for a
    transaction the user was told to expect; a dropped `reply_after_seconds`
    leaves the flow on its own default, which is what decides whether the user
    ever hears about a slow write.
    """
    client = _capturing_client()
    client.start_workflow.return_value.result = AsyncMock(
        return_value={"ok": True, "message": "posted manual/abc to personal/2026.journal"}
    )
    write = {
        "entity": "personal",
        "date": "2026-09-06",
        "payee": "Corner Store",
        "postings": [
            {"account": "expenses:groceries", "amount": "245.50", "currency": "INR"},
            {"account": "assets:bank:hdfc:1225"},
        ],
        "note": "",
    }
    out = await _dispatch_books_write(
        _ctx(client), "post", write, books.BooksConfig(path=Path("/nonexistent"))
    )
    assert out == "posted manual/abc to personal/2026.journal"

    payload = _start_payload(client)
    unknown = set(payload) - _field_names(BooksWriteInput)
    assert unknown == set(), f"keys Temporal would silently drop: {sorted(unknown)}"
    missing = _field_names(BooksWriteInput) - set(payload)
    assert missing == set(), f"fields left on their default: {sorted(missing)}"

    assert payload["op"] == "post"
    assert payload["payload"] == write
    assert payload["reply_after_seconds"] == LEDGER_WRITE_WAIT_S
    # The id is the write's own content hash, so a retried turn re-attaches.
    _, kwargs = client.start_workflow.call_args
    assert kwargs["id"].startswith("books-write-post-")
    assert kwargs["task_queue"] == "aegis-main"


def test_the_converter_really_does_ignore_unknown_keys():
    """The premise, asserted rather than assumed: this is why the test above
    is worth having. If a future temporalio started REJECTING unknown keys,
    this fails and the seam guard becomes redundant."""
    from temporalio.converter import DataConverter

    converter = DataConverter.default
    payloads = converter.payload_converter.to_payloads(
        [{"agent_id": "maou", "operation": "post"}]
    )
    (restored,) = converter.payload_converter.from_payloads(payloads, [BooksWriteInput])

    assert isinstance(restored, BooksWriteInput)
    assert restored.op == "", "unknown key silently dropped to the default"
