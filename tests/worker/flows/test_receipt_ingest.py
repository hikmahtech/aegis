"""ReceiptIngestFlow (weekly safety-net) tests.

The batch tail (load_receipts → classify_and_extract → upsert_charges →
detect_cancellations) is gone. Per-message money hygiene is now owned by
MoneyProcessFlow, which the hourly GmailIngestFlow fans out per email.
This flow exists only to catch anything triage missed, by fanning out
stored messages to MoneyProcessFlow with the same ABANDON policy.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from temporalio import activity, workflow
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


@pytest.mark.asyncio
async def test_receipt_flow_sweeps_stuck_receipts():
    """A stuck receipt_email id surfaced by find_stuck_receipts is
    reprocessed directly through load_receipts -> classify_and_extract ->
    upsert_charges. This bypasses MoneyProcessFlow entirely (fix #113):
    MoneyProcessFlow starts from store_receipt_email, which is idempotent
    on message_id and would immediately short-circuit an already-stored
    row as a duplicate — never actually retrying the failed extraction."""
    _reset()
    upserted: list[tuple[str, list[dict]]] = []

    @activity.defn(name="find_stuck_receipts")
    async def find_stuck(limit: int, older_than_days: int) -> list[str]:
        _calls["sweep"].append((limit, older_than_days))
        return ["stuck-1"]

    @activity.defn(name="load_receipts")
    async def load(receipt_ids: list[str]) -> list[dict]:
        assert receipt_ids == ["stuck-1"]
        return [
            {
                "id": "stuck-1",
                "account": "sebas",
                "message_id": "m-stuck-1",
                "sender": "billing@stripe.com",
                "subject": "Receipt",
                "body_plain": "paid $9.99",
                "received_at": "2026-06-01T00:00:00+00:00",
            }
        ]

    @activity.defn(name="classify_and_extract")
    async def classify(receipts: list[dict], agent_id: str) -> list[dict]:
        return [
            {
                "receipt_id": "stuck-1",
                "is_receipt": True,
                "vendor_name": "Stripe",
                "amount": 9.99,
                "currency": "USD",
                "cadence": "monthly",
            }
        ]

    @activity.defn(name="upsert_charges")
    async def upsert(account: str, extractions: list[dict]) -> int:
        upserted.append((account, extractions))
        return len(extractions)

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
                classify,
                upsert,
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
    assert len(upserted) == 1
    assert upserted[0][0] == "sebas"
    assert upserted[0][1][0]["vendor_name"] == "Stripe"


@pytest.mark.asyncio
async def test_receipt_flow_sweep_leaves_still_failing_rows_unparsed():
    """A stuck receipt that fails classification AGAIN (_parse_failed)
    is skipped — not upserted — so it waits for next week's sweep
    instead of writing garbage."""
    _reset()
    upserted: list[tuple[str, list[dict]]] = []

    @activity.defn(name="find_stuck_receipts")
    async def find_stuck(limit: int, older_than_days: int) -> list[str]:
        return ["stuck-2"]

    @activity.defn(name="load_receipts")
    async def load(receipt_ids: list[str]) -> list[dict]:
        return [
            {
                "id": "stuck-2",
                "account": "sebas",
                "message_id": "m-stuck-2",
                "sender": "billing@stripe.com",
                "subject": "Receipt",
                "body_plain": "paid $9.99",
                "received_at": "2026-06-01T00:00:00+00:00",
            }
        ]

    @activity.defn(name="classify_and_extract")
    async def classify(receipts: list[dict], agent_id: str) -> list[dict]:
        return [{"is_receipt": False, "confidence": 0.0, "_parse_failed": True}]

    @activity.defn(name="upsert_charges")
    async def upsert(account: str, extractions: list[dict]) -> int:
        upserted.append((account, extractions))
        return len(extractions)

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
                classify,
                upsert,
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
    assert upserted == []
