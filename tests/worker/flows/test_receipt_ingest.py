"""ReceiptIngestFlow (weekly safety-net) tests.

Per-message money hygiene is owned by MoneyProcessFlow, which the hourly
GmailIngestFlow fans out per email. This flow exists to catch anything
triage missed — fanning out stored messages to MoneyProcessFlow with the
same ABANDON policy — and to sweep every receipt_email row still below
`parsed.version` 2 down the v2 books path (parse_money_email →
capture_due? → post_money_event → store_money_result), which is also how
a backfill drains.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from temporalio import activity, workflow
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

with workflow.unsafe.imports_passed_through():
    from aegis_worker.activities.gmail import FetchEmailsInput, FetchEmailsResult
    from aegis_worker.flows.money_process import MoneyProcessInput
    from aegis_worker.flows.receipt_ingest import ReceiptIngestFlow, ReceiptIngestInput


_calls: dict[str, list] = {
    "list": [],
    "fetch": [],
    "idem": [],
    "cursor": [],
    "money_inputs": [],
    "sweep": [],
}


def _reset() -> None:
    for v in _calls.values():
        v.clear()


@activity.defn(name="list_active_channels")
async def stub_list(kind: str) -> list[dict]:
    _calls["list"].append(kind)
    return [
        {
            "id": "ch-1",
            "kind": "email",
            "identifier": "a@b.com",
            "config": {"label": "sebas"},
            "active": True,
        }
    ]


@activity.defn(name="fetch_emails")
async def stub_fetch(inp: FetchEmailsInput) -> FetchEmailsResult:
    _calls["fetch"].append(inp.account_label)
    return FetchEmailsResult(
        messages=[
            {
                "id": "rc-1",
                "sender": "billing@stripe.com",
                "subject": "Receipt",
                "thread_id": "",
                "to": "",
                "date": "",
                "snippet": "paid $9.99",
                "internal_date_ms": 1700000000000,
            },
            {
                "id": "rc-2",
                "sender": "receipts@razorpay.com",
                "subject": "Your subscription",
                "thread_id": "",
                "to": "",
                "date": "",
                "snippet": "paid ₹499",
                "internal_date_ms": 1700000005000,
            },
        ],
        latest_internal_date_ms=1700000005000,
    )


@activity.defn(name="ingest_idempotency_claim")
async def stub_idem(source_type: str, external_id: str) -> bool:
    _calls["idem"].append((source_type, external_id))
    return True


@activity.defn(name="update_channel_config_key")
async def stub_cursor(kind: str, identifier: str, key: str, value: str) -> None:
    _calls["cursor"].append((kind, identifier, key, value))


@activity.defn(name="find_stuck_receipts")
async def stub_find_stuck(limit: int, older_than_days: int) -> list[str]:
    """No stuck receipts by default — the sweep-behavior test registers
    its own stub with a non-empty return."""
    _calls["sweep"].append((limit, older_than_days))
    return []


# Stub MoneyProcessFlow — registered on the test Worker under the same
# name="MoneyProcessFlow" so the parent's start_child_workflow finds it.
# We capture its inputs via a helper activity since workflow bodies can't
# mutate module-level Python state directly (non-deterministic).


@activity.defn(name="capture_money_input")
async def stub_capture(payload: dict) -> None:
    _calls["money_inputs"].append(payload)


@workflow.defn(name="MoneyProcessFlow")
class StubMoneyProcessFlow:
    @workflow.run
    async def run(self, input: MoneyProcessInput) -> dict:
        await workflow.execute_activity(
            "capture_money_input",
            {
                "agent_id": input.agent_id,
                "msg_id": input.msg.get("id", ""),
                "account_label": input.account_label,
            },
            start_to_close_timeout=timedelta(seconds=10),
        )
        return {"status": "stub"}


ALL_STUBS = [stub_list, stub_fetch, stub_idem, stub_cursor, stub_capture, stub_find_stuck]
ALL_WORKFLOWS = [ReceiptIngestFlow, StubMoneyProcessFlow]


@pytest.mark.asyncio
async def test_receipt_flow_fans_out_per_message():
    """2 stored messages → MoneyProcessFlow started once per message with correct input."""
    import asyncio

    _reset()

    async with (
        await WorkflowEnvironment.start_local() as env,
        Worker(
            env.client,
            task_queue="tq",
            workflows=ALL_WORKFLOWS,
            activities=ALL_STUBS,
        ),
    ):
        result = await env.client.execute_workflow(
            ReceiptIngestFlow.run,
            ReceiptIngestInput(agent_id="maou", aegis_ui_url="https://x"),
            id="rec-fanout-1",
            task_queue="tq",
        )

        # Parent uses ABANDON → returns immediately. Poll briefly for children.
        for _ in range(100):
            await asyncio.sleep(0.05)
            if len(_calls["money_inputs"]) >= 2:
                break

    assert result == {"stored": 2, "accounts": 1, "errors": 0, "swept": 0}
    assert _calls["idem"] == [("receipt", "rc-1"), ("receipt", "rc-2")]

    captured = sorted(_calls["money_inputs"], key=lambda p: p["msg_id"])
    assert captured == [
        {"agent_id": "maou", "msg_id": "rc-1", "account_label": "sebas"},
        {"agent_id": "maou", "msg_id": "rc-2", "account_label": "sebas"},
    ]


@pytest.mark.asyncio
async def test_receipt_flow_cursor_advances():
    """Cursor key receipt_last_cursor_ts is written after successful fetch."""
    _reset()

    async with (
        await WorkflowEnvironment.start_local() as env,
        Worker(
            env.client,
            task_queue="tq",
            workflows=ALL_WORKFLOWS,
            activities=ALL_STUBS,
        ),
    ):
        await env.client.execute_workflow(
            ReceiptIngestFlow.run,
            ReceiptIngestInput(),
            id="rec-cursor-1",
            task_queue="tq",
        )

    cursor_updates = _calls["cursor"]
    assert any(c[2] == "receipt_last_cursor_ts" for c in cursor_updates), (
        f"receipt_last_cursor_ts not written; cursor calls: {cursor_updates}"
    )


@pytest.mark.asyncio
async def test_receipt_flow_all_dedup():
    """All idempotency claims return False → no fan-out, stored=0."""
    _reset()

    @activity.defn(name="ingest_idempotency_claim")
    async def all_dup(source_type: str, external_id: str) -> bool:
        return False

    async with (
        await WorkflowEnvironment.start_local() as env,
        Worker(
            env.client,
            task_queue="tq",
            workflows=ALL_WORKFLOWS,
            activities=[stub_list, stub_fetch, all_dup, stub_cursor, stub_capture, stub_find_stuck],
        ),
    ):
        result = await env.client.execute_workflow(
            ReceiptIngestFlow.run,
            ReceiptIngestInput(),
            id="rec-dedup-1",
            task_queue="tq",
        )

    assert result["stored"] == 0
    assert result["accounts"] == 1
    assert _calls["money_inputs"] == []


_SWEPT_TXN = {
    "kind": "transaction",
    "direction": "out",
    "amount": "9.99",
    "currency": "USD",
    "payee": "Stripe",
    "payee_key": "stripe",
    "channel": "card",
    "instrument": "hdfc-1225",
    "occurred_on": "2026-06-01",
    "entity": "personal",
    "account": "expenses:software",
    "parser": "llm",
    "confidence": 1.0,
    "source_class": "receipt",
}


def _stuck_row(receipt_id: str) -> dict:
    return {
        "id": receipt_id,
        "account": "sebas",
        "message_id": f"m-{receipt_id}",
        "sender": "billing@stripe.com",
        "subject": "Receipt",
        "body_plain": "paid $9.99",
        "received_at": "2026-06-01T00:00:00+00:00",
    }


def test_default_sender_filter_includes_the_bank_senders():
    from aegis_worker.flows.receipt_ingest import DEFAULT_SENDER_FILTER, ReceiptIngestInput

    assert "alerts@hdfcbank.bank.in" in DEFAULT_SENDER_FILTER
    assert "alerts@axis.bank.in" in DEFAULT_SENDER_FILTER
    inp = ReceiptIngestInput(query_window="after:2026/06/30", sender_filter="(from:x@y.z)")
    assert inp.query == "(from:x@y.z) after:2026/06/30"
    assert ReceiptIngestInput().query.startswith(DEFAULT_SENDER_FILTER)


@pytest.mark.asyncio
async def test_receipt_flow_sweeps_stuck_receipts():
    """A stuck receipt_email id surfaced by find_stuck_receipts is re-driven
    down the same v2 path MoneyProcessFlow uses — load_receipts →
    fetch_message_body → parse_money_email → post_money_event →
    store_money_result. This bypasses MoneyProcessFlow entirely (fix #113):
    that flow starts from store_receipt_email, which short-circuits an
    already-v2 row as a duplicate and would never re-drive it."""
    _reset()
    posted: list[tuple] = []
    stamped: list[tuple] = []
    captured: list[tuple] = []

    @activity.defn(name="find_stuck_receipts")
    async def find_stuck(limit: int, older_than_days: int) -> list[str]:
        _calls["sweep"].append((limit, older_than_days))
        return ["stuck-1"]

    @activity.defn(name="load_receipts")
    async def load(receipt_ids: list[str]) -> list[dict]:
        assert receipt_ids == ["stuck-1"]
        return [_stuck_row("stuck-1")]

    parsed_bodies: list[str] = []

    @activity.defn(name="parse_money_email")
    async def parse(receipt: dict) -> dict:
        parsed_bodies.append(receipt["body_plain"])
        return dict(_SWEPT_TXN)

    # Recorded, not raised: the sweep swallows capture_due failures by design,
    # so an AssertionError in here would be logged and lost.
    @activity.defn(name="capture_due")
    async def capture(event: dict, mailbox: str, message_id: str) -> str | None:
        captured.append((event["kind"], mailbox, message_id))
        return "task-should-not-happen"

    @activity.defn(name="post_money_event")
    async def post(
        receipt_id: str,
        mailbox: str,
        message_id: str,
        event: dict,
        todoist_ref: str | None = None,
    ) -> dict:
        posted.append((receipt_id, mailbox, message_id, event["kind"], todoist_ref))
        return {
            "msgid": f"{mailbox}/{message_id}",
            "status": "posted",
            "journal_file": "personal/2026.journal",
            "linked": None,
            "closed_due": None,
        }

    @activity.defn(name="store_money_result")
    async def store_result(receipt_id: str, event: dict, journal_file: str | None) -> None:
        stamped.append((receipt_id, event["kind"], journal_file))

    body_calls: list[tuple] = []

    @activity.defn(name="fetch_message_body")
    async def stub_body(account_label: str, message_id: str, max_chars: int = 6000) -> str:
        body_calls.append((account_label, message_id))
        return "full body"

    @activity.defn(name="store_receipt_body")
    async def stub_store_body(receipt_id: str, body_text: str) -> None:
        return None

    async with (
        await WorkflowEnvironment.start_local() as env,
        Worker(
            env.client,
            task_queue="tq",
            workflows=ALL_WORKFLOWS,
            activities=[
                stub_list,
                stub_fetch,
                stub_idem,
                stub_cursor,
                stub_capture,
                find_stuck,
                load,
                parse,
                capture,
                post,
                store_result,
                stub_body,
                stub_store_body,
            ],
        ),
    ):
        result = await env.client.execute_workflow(
            ReceiptIngestFlow.run,
            ReceiptIngestInput(agent_id="maou", aegis_ui_url="https://x"),
            id="rec-sweep-1",
            task_queue="tq",
        )

    assert result["swept"] == 1
    assert _calls["sweep"] == [(20, 1)]
    assert captured == []
    assert posted == [("stuck-1", "sebas", "m-stuck-1", "transaction", None)]
    assert stamped == [("stuck-1", "transaction", "personal/2026.journal")]
    assert body_calls == [("sebas", "m-stuck-1")]
    # The fetched body, not the stored snippet, is what the parser sees.
    assert parsed_bodies == ["full body"]


@pytest.mark.asyncio
async def test_receipt_flow_sweep_captures_a_due_before_posting():
    """A swept row that parses as a due mints the Todoist task first and
    hands the ref to post_money_event — the same order the flow uses."""
    _reset()
    posted: list[tuple] = []
    captured: list[tuple] = []

    @activity.defn(name="find_stuck_receipts")
    async def find_stuck(limit: int, older_than_days: int) -> list[str]:
        return ["stuck-due"]

    @activity.defn(name="load_receipts")
    async def load(receipt_ids: list[str]) -> list[dict]:
        return [_stuck_row("stuck-due")]

    @activity.defn(name="parse_money_email")
    async def parse(receipt: dict) -> dict:
        return {**_SWEPT_TXN, "kind": "due", "due_on": "2026-06-15"}

    @activity.defn(name="capture_due")
    async def capture(event: dict, mailbox: str, message_id: str) -> str | None:
        captured.append((event["kind"], mailbox, message_id))
        return "task-77"

    @activity.defn(name="post_money_event")
    async def post(
        receipt_id: str,
        mailbox: str,
        message_id: str,
        event: dict,
        todoist_ref: str | None = None,
    ) -> dict:
        posted.append((receipt_id, event["kind"], todoist_ref))
        return {
            "msgid": f"{mailbox}/{message_id}",
            "status": "indexed",
            "journal_file": None,
            "linked": None,
            "closed_due": None,
        }

    stamped: list[tuple] = []

    @activity.defn(name="store_money_result")
    async def store_result(receipt_id: str, event: dict, journal_file: str | None) -> None:
        stamped.append((receipt_id, event["kind"], journal_file))

    @activity.defn(name="fetch_message_body")
    async def stub_body(account_label: str, message_id: str, max_chars: int = 6000) -> str:
        return "full body"

    @activity.defn(name="store_receipt_body")
    async def stub_store_body(receipt_id: str, body_text: str) -> None:
        return None

    async with (
        await WorkflowEnvironment.start_local() as env,
        Worker(
            env.client,
            task_queue="tq",
            workflows=ALL_WORKFLOWS,
            activities=[
                stub_list,
                stub_fetch,
                stub_idem,
                stub_cursor,
                stub_capture,
                find_stuck,
                load,
                parse,
                capture,
                post,
                store_result,
                stub_body,
                stub_store_body,
            ],
        ),
    ):
        result = await env.client.execute_workflow(
            ReceiptIngestFlow.run,
            ReceiptIngestInput(agent_id="maou", aegis_ui_url="https://x"),
            id="rec-sweep-due",
            task_queue="tq",
        )

    assert result["swept"] == 1
    assert captured == [("due", "sebas", "m-stuck-due")]
    assert posted == [("stuck-due", "due", "task-77")]
    assert stamped == [("stuck-due", "due", None)]


@pytest.mark.asyncio
async def test_receipt_flow_sweep_leaves_still_failing_rows_unparsed():
    """A stuck receipt that fails to parse AGAIN (_parse_failed) is skipped —
    not posted, not stamped — so it stays below version 2 and waits for next
    week's sweep instead of writing garbage into the journal."""
    _reset()
    posted: list[tuple] = []
    stamped: list[tuple] = []
    captured: list[tuple] = []

    @activity.defn(name="find_stuck_receipts")
    async def find_stuck(limit: int, older_than_days: int) -> list[str]:
        return ["stuck-2"]

    @activity.defn(name="load_receipts")
    async def load(receipt_ids: list[str]) -> list[dict]:
        return [_stuck_row("stuck-2")]

    @activity.defn(name="parse_money_email")
    async def parse(receipt: dict) -> dict:
        return {"kind": "ignore", "parser": "llm", "_parse_failed": True}

    @activity.defn(name="capture_due")
    async def capture(event: dict, mailbox: str, message_id: str) -> str | None:
        captured.append((event["kind"], mailbox, message_id))
        return "task-should-not-happen"

    @activity.defn(name="post_money_event")
    async def post(
        receipt_id: str,
        mailbox: str,
        message_id: str,
        event: dict,
        todoist_ref: str | None = None,
    ) -> dict:
        posted.append((receipt_id, event["kind"], todoist_ref))
        return {"msgid": "", "status": "indexed", "journal_file": None}

    @activity.defn(name="store_money_result")
    async def store_result(receipt_id: str, event: dict, journal_file: str | None) -> None:
        stamped.append((receipt_id, event["kind"], journal_file))

    @activity.defn(name="fetch_message_body")
    async def stub_body(account_label: str, message_id: str, max_chars: int = 6000) -> str:
        return "full body"

    @activity.defn(name="store_receipt_body")
    async def stub_store_body(receipt_id: str, body_text: str) -> None:
        return None

    async with (
        await WorkflowEnvironment.start_local() as env,
        Worker(
            env.client,
            task_queue="tq",
            workflows=ALL_WORKFLOWS,
            activities=[
                stub_list,
                stub_fetch,
                stub_idem,
                stub_cursor,
                stub_capture,
                find_stuck,
                load,
                parse,
                capture,
                post,
                store_result,
                stub_body,
                stub_store_body,
            ],
        ),
    ):
        result = await env.client.execute_workflow(
            ReceiptIngestFlow.run,
            ReceiptIngestInput(agent_id="maou", aegis_ui_url="https://x"),
            id="rec-sweep-2",
            task_queue="tq",
        )

    assert result["swept"] == 0
    assert captured == []
    assert posted == [] and stamped == []


@pytest.mark.asyncio
async def test_receipt_flow_sweep_survives_body_fetch_failure():
    """Same stuck receipt, but fetch_message_body fails hard instead of
    returning "". The body is an enhancement — the sweep must fall back to
    the stored snippet and still parse + post, not abandon the row for
    another week."""
    _reset()
    posted: list[tuple] = []
    captured: list[tuple] = []

    @activity.defn(name="find_stuck_receipts")
    async def find_stuck(limit: int, older_than_days: int) -> list[str]:
        _calls["sweep"].append((limit, older_than_days))
        return ["stuck-3"]

    @activity.defn(name="load_receipts")
    async def load(receipt_ids: list[str]) -> list[dict]:
        assert receipt_ids == ["stuck-3"]
        return [_stuck_row("stuck-3")]

    parsed_bodies: list[str] = []

    @activity.defn(name="parse_money_email")
    async def parse(receipt: dict) -> dict:
        parsed_bodies.append(receipt["body_plain"])
        return dict(_SWEPT_TXN)

    # Recorded, not raised: the sweep swallows capture_due failures by design,
    # so an AssertionError in here would be logged and lost.
    @activity.defn(name="capture_due")
    async def capture(event: dict, mailbox: str, message_id: str) -> str | None:
        captured.append((event["kind"], mailbox, message_id))
        return "task-should-not-happen"

    @activity.defn(name="post_money_event")
    async def post(
        receipt_id: str,
        mailbox: str,
        message_id: str,
        event: dict,
        todoist_ref: str | None = None,
    ) -> dict:
        posted.append((receipt_id, mailbox, event["kind"], todoist_ref))
        return {
            "msgid": f"{mailbox}/{message_id}",
            "status": "posted",
            "journal_file": "personal/2026.journal",
            "linked": None,
            "closed_due": None,
        }

    stamped: list[tuple] = []

    @activity.defn(name="store_money_result")
    async def store_result(receipt_id: str, event: dict, journal_file: str | None) -> None:
        stamped.append((receipt_id, event["kind"], journal_file))

    body_calls: list[tuple] = []

    @activity.defn(name="fetch_message_body")
    async def boom_body(account_label: str, message_id: str, max_chars: int = 6000) -> str:
        body_calls.append((account_label, message_id))
        raise ApplicationError("gmail down", non_retryable=True)

    @activity.defn(name="store_receipt_body")
    async def stub_store_body(receipt_id: str, body_text: str) -> None:
        raise AssertionError("store_receipt_body must not run when the fetch failed")

    async with (
        await WorkflowEnvironment.start_local() as env,
        Worker(
            env.client,
            task_queue="tq",
            workflows=ALL_WORKFLOWS,
            activities=[
                stub_list,
                stub_fetch,
                stub_idem,
                stub_cursor,
                stub_capture,
                find_stuck,
                load,
                parse,
                capture,
                post,
                store_result,
                boom_body,
                stub_store_body,
            ],
        ),
    ):
        result = await env.client.execute_workflow(
            ReceiptIngestFlow.run,
            ReceiptIngestInput(agent_id="maou", aegis_ui_url="https://x"),
            id="rec-sweep-body-boom",
            task_queue="tq",
        )

    assert result["swept"] == 1
    assert body_calls == [("sebas", "m-stuck-3")]
    # The stored snippet, not an abandoned row, is what the parser saw.
    assert parsed_bodies == ["paid $9.99"]
    assert captured == []
    assert posted == [("stuck-3", "sebas", "transaction", None)]
    assert stamped == [("stuck-3", "transaction", "personal/2026.journal")]
