"""MoneyProcessFlow v2 — store → body → parse → route → index (spec §2)."""

from __future__ import annotations

import pytest
from temporalio import activity, workflow
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

with workflow.unsafe.imports_passed_through():
    from aegis_worker.flows.money_process import MoneyProcessFlow, MoneyProcessInput

_calls: dict[str, list] = {
    k: [] for k in ("store", "body", "store_body", "load", "parse", "due", "post", "result")
}


def _reset() -> None:
    for v in _calls.values():
        v.clear()


_MSG = {
    "id": "gmail-msg-1",
    "sender": "alerts@hdfcbank.bank.in",
    "subject": "UPI txn",
    "thread_id": "t1",
    "to": "",
    "date": "",
    "snippet": "Rs.10.00 is debited",
    "internal_date_ms": 1700000000000,
}

_TXN = {
    "kind": "transaction",
    "direction": "out",
    "amount": "10.00",
    "currency": "INR",
    "payee": "Shop",
    "payee_key": "shop",
    "channel": "upi",
    "instrument": "hdfc-1225",
    "occurred_on": "2026-09-02",
    "entity": "personal",
    "account": "expenses:unknown",
    "parser": "hdfc_upi",
    "confidence": 1.0,
    "source_class": "bank",
}
_DUE = {
    **_TXN,
    "kind": "due",
    "due_on": "2026-09-07",
    "channel": "statement",
    "parser": "axis_cc_statement",
}
_IGN = {
    "kind": "ignore",
    "entity": "none",
    "parser": "mailbox",
    "payee": "",
    "payee_key": "",
    "channel": "other",
    "confidence": 1.0,
    "source_class": "other",
}


@activity.defn(name="store_receipt_email")
async def stub_store(msg: dict, account: str) -> str:
    _calls["store"].append((msg["id"], account))
    return f"uid-{msg['id']}"


@activity.defn(name="store_receipt_email")
async def stub_store_dup(msg: dict, account: str) -> str:
    return ""


@activity.defn(name="fetch_message_body")
async def stub_body(account_label: str, message_id: str, max_chars: int = 6000) -> str:
    _calls["body"].append((account_label, message_id))
    return "full body"


@activity.defn(name="fetch_message_body")
async def stub_body_empty(account_label: str, message_id: str, max_chars: int = 6000) -> str:
    _calls["body"].append((account_label, message_id))
    return ""


@activity.defn(name="fetch_message_body")
async def stub_body_boom(account_label: str, message_id: str, max_chars: int = 6000) -> str:
    _calls["body"].append((account_label, message_id))
    raise ApplicationError("gmail down", non_retryable=True)


@activity.defn(name="store_receipt_body")
async def stub_store_body(receipt_id: str, body_text: str) -> None:
    _calls["store_body"].append((receipt_id, body_text))


@activity.defn(name="load_receipts")
async def stub_load(ids: list[str]) -> list[dict]:
    _calls["load"].append(list(ids))
    return [
        {
            "id": i,
            "account": "user-personal",
            "message_id": i.replace("uid-", ""),
            "sender": _MSG["sender"],
            "subject": _MSG["subject"],
            "body_plain": "full body",
            "received_at": "",
        }
        for i in ids
    ]


@activity.defn(name="load_receipts")
async def stub_load_none(ids: list[str]) -> list[dict]:
    _calls["load"].append(list(ids))
    return []


def _parser(event: dict):
    @activity.defn(name="parse_money_email")
    async def stub_parse(receipt: dict) -> dict:
        _calls["parse"].append(receipt["id"])
        return dict(event)

    return stub_parse


@activity.defn(name="parse_money_email")
async def stub_parse_boom(receipt: dict) -> dict:
    raise ApplicationError("llm down", non_retryable=True)


@activity.defn(name="capture_due")
async def stub_due(event: dict, mailbox: str, message_id: str) -> str | None:
    _calls["due"].append((event["kind"], mailbox, message_id))
    return "task-42"


@activity.defn(name="capture_due")
async def stub_due_boom(event: dict, mailbox: str, message_id: str) -> str | None:
    raise ApplicationError("todoist down", non_retryable=True)


@activity.defn(name="post_money_event")
async def stub_post(
    receipt_id: str, mailbox: str, message_id: str, event: dict, todoist_ref: str | None = None
) -> dict:
    _calls["post"].append((receipt_id, mailbox, message_id, event["kind"], todoist_ref))
    status = "indexed" if event["kind"] != "transaction" else "posted"
    return {
        "msgid": f"{mailbox}/{message_id}",
        "status": status,
        "journal_file": "personal/2026.journal" if status == "posted" else None,
        "linked": None,
        "closed_due": None,
    }


@activity.defn(name="post_money_event")
async def stub_post_books_disabled(
    receipt_id: str, mailbox: str, message_id: str, event: dict, todoist_ref: str | None = None
) -> dict:
    _calls["post"].append((receipt_id, mailbox, message_id, event["kind"], todoist_ref))
    return {
        "msgid": f"{mailbox}/{message_id}",
        "status": "books_disabled",
        "journal_file": None,
        "linked": None,
        "closed_due": None,
    }


@activity.defn(name="store_money_result")
async def stub_result(receipt_id: str, event: dict, journal_file: str | None) -> None:
    _calls["result"].append((receipt_id, event["kind"], journal_file))


def _stubs(
    parse=None, store=stub_store, due=stub_due, body=stub_body, load=stub_load, post=stub_post
):
    return [store, body, stub_store_body, load, parse or _parser(_TXN), due, post, stub_result]


async def _run(stubs, wid: str) -> dict:
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(env.client, task_queue="tq", workflows=[MoneyProcessFlow], activities=stubs),
    ):
        return await env.client.execute_workflow(
            MoneyProcessFlow.run,
            MoneyProcessInput(agent_id="maou", msg=_MSG, account_label="user-personal"),
            id=wid,
            task_queue="tq",
        )


@pytest.mark.asyncio
async def test_transaction_is_posted_and_stamped():
    _reset()
    result = await _run(_stubs(), "mp-txn")
    assert result == {
        "status": "posted",
        "receipt_id": "uid-gmail-msg-1",
        "msgid": "user-personal/gmail-msg-1",
        "kind": "transaction",
    }
    assert _calls["body"] == [("user-personal", "gmail-msg-1")]
    assert _calls["store_body"] == [("uid-gmail-msg-1", "full body")]
    assert _calls["parse"] == ["uid-gmail-msg-1"]
    assert _calls["due"] == []
    assert _calls["post"] == [
        ("uid-gmail-msg-1", "user-personal", "gmail-msg-1", "transaction", None)
    ]
    assert _calls["result"] == [("uid-gmail-msg-1", "transaction", "personal/2026.journal")]


@pytest.mark.asyncio
async def test_due_captures_a_task_then_indexes_with_the_ref():
    _reset()
    result = await _run(_stubs(parse=_parser(_DUE)), "mp-due")
    assert result["status"] == "indexed" and result["kind"] == "due"
    assert _calls["due"] == [("due", "user-personal", "gmail-msg-1")]
    assert _calls["post"][0][4] == "task-42"
    assert _calls["result"] == [("uid-gmail-msg-1", "due", None)]


@pytest.mark.asyncio
async def test_capture_failure_still_indexes():
    _reset()
    result = await _run(_stubs(parse=_parser(_DUE), due=stub_due_boom), "mp-due-fail")
    assert result["status"] == "indexed"
    assert _calls["post"][0][4] is None and len(_calls["result"]) == 1


@pytest.mark.asyncio
async def test_ignore_is_indexed_and_reported_as_ignored():
    _reset()
    result = await _run(_stubs(parse=_parser(_IGN)), "mp-ign")
    assert result["status"] == "ignored" and result["kind"] == "ignore"
    assert _calls["post"] and _calls["result"]


@pytest.mark.asyncio
async def test_duplicate_short_circuits():
    _reset()
    result = await _run(_stubs(store=stub_store_dup), "mp-dup")
    assert result["status"] == "duplicate"
    assert _calls["body"] == [] and _calls["post"] == []


@pytest.mark.asyncio
async def test_parse_failure_leaves_row_unstamped():
    _reset()
    result = await _run(_stubs(parse=_parser({**_IGN, "_parse_failed": True})), "mp-pf")
    assert result["status"] == "parse_failed"
    assert _calls["post"] == [] and _calls["result"] == []


@pytest.mark.asyncio
async def test_parser_exception_is_extract_failed():
    _reset()
    result = await _run(_stubs(parse=stub_parse_boom), "mp-boom")
    assert result["status"] == "extract_failed"
    assert _calls["result"] == []


@pytest.mark.asyncio
async def test_missing_row_is_load_failed():
    _reset()
    result = await _run(_stubs(load=stub_load_none), "mp-load-fail")
    assert result["status"] == "load_failed"
    assert _calls["parse"] == [] and _calls["post"] == [] and _calls["result"] == []


@pytest.mark.asyncio
async def test_empty_body_is_not_stored():
    """A "" body means the fetch soft-failed — keep the stored snippet."""
    _reset()
    result = await _run(_stubs(body=stub_body_empty), "mp-empty-body")
    assert result["status"] == "posted"
    assert _calls["body"] == [("user-personal", "gmail-msg-1")]
    assert _calls["store_body"] == []


@pytest.mark.asyncio
async def test_body_fetch_failure_still_posts():
    """A hard body-fetch failure must degrade to the snippet, not lose the
    receipt: the parse still runs and the row is still stamped."""
    _reset()
    result = await _run(_stubs(body=stub_body_boom), "mp-body-boom")
    assert result["status"] == "posted"
    assert _calls["store_body"] == []
    assert _calls["parse"] == ["uid-gmail-msg-1"]
    assert len(_calls["result"]) == 1


@activity.defn(name="post_money_event")
async def stub_post_failed(
    receipt_id: str, mailbox: str, message_id: str, event: dict, todoist_ref: str | None = None
) -> dict:
    _calls["post"].append((receipt_id, mailbox, message_id, event["kind"], todoist_ref))
    return {
        "msgid": f"{mailbox}/{message_id}",
        "status": "post_failed",
        "journal_file": None,
        "linked": None,
        "closed_due": None,
    }


@pytest.mark.asyncio
async def test_books_disabled_is_not_stamped():
    """No books checkout yet: the event is indexed but never posted, so the
    row must stay BELOW version 2. Stamping it would take it out of
    find_stuck_receipts for good — the payment would never reach the journal,
    and because find_match skips rows with no journal_file, a later
    counterpart would post a second block for the same payment."""
    _reset()
    result = await _run(_stubs(post=stub_post_books_disabled), "mp-books-off")
    assert result["status"] == "books_disabled"
    assert len(_calls["post"]) == 1
    assert _calls["result"] == []


def test_post_money_event_outlives_a_first_write_clone():
    """The first post after the books are enabled clones the repo inside the
    activity, which is allowed `books.CLONE_TIMEOUT_S`. An activity timeout
    below that burns every retry attempt on the same clone. Both call sites —
    MoneyProcessFlow and the ReceiptIngestFlow sweep — must sit above it."""
    from aegis.services.books import CLONE_TIMEOUT_S
    from aegis_worker.flows.money_process import _POST_TIMEOUT
    from aegis_worker.flows.receipt_ingest import _POST_TIMEOUT as _SWEEP_POST_TIMEOUT

    for timeout in (_POST_TIMEOUT, _SWEEP_POST_TIMEOUT):
        assert timeout.total_seconds() > CLONE_TIMEOUT_S


@pytest.mark.asyncio
async def test_a_rejected_block_is_not_stamped_either():
    """`post_failed` means indexed but NOT in the journal, exactly like
    `books_disabled`: the row must stay below version 2 so the weekly sweep
    re-drives it once the chart is fixed. Stamping it would drop the payment
    for good."""
    _reset()
    result = await _run(_stubs(post=stub_post_failed), "mp-post-failed")
    assert result["status"] == "post_failed"
    assert len(_calls["post"]) == 1
    assert _calls["result"] == []
