"""MoneyProcessFlow — per-email money hygiene child workflow."""

from __future__ import annotations

import pytest
from temporalio import activity, workflow
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

with workflow.unsafe.imports_passed_through():
    from aegis_worker.flows.money_process import MoneyProcessFlow, MoneyProcessInput


_calls: dict[str, list] = {
    "store": [],
    "body": [],
    "store_body": [],
    "load": [],
    "classify": [],
    "upsert": [],
}


def _reset() -> None:
    for v in _calls.values():
        v.clear()


_MSG = {
    "id": "gmail-msg-1",
    "sender": "billing@stripe.com",
    "subject": "Your receipt",
    "thread_id": "t1",
    "to": "",
    "date": "",
    "snippet": "paid $9.99",
    "internal_date_ms": 1700000000000,
}


@activity.defn(name="store_receipt_email")
async def stub_store(msg: dict, account: str) -> str:
    _calls["store"].append((msg["id"], account))
    return f"uid-{msg['id']}"


@activity.defn(name="fetch_message_body")
async def stub_body(account_label: str, message_id: str, max_chars: int = 6000) -> str:
    _calls["body"].append((account_label, message_id))
    return "Receipt from Stripe $9.99 Paid"


@activity.defn(name="fetch_message_body")
async def stub_body_empty(account_label: str, message_id: str, max_chars: int = 6000) -> str:
    _calls["body"].append((account_label, message_id))
    return ""


@activity.defn(name="store_receipt_body")
async def stub_store_body(receipt_id: str, body_text: str) -> None:
    _calls["store_body"].append((receipt_id, body_text))


@activity.defn(name="fetch_message_body")
async def stub_body_boom(account_label: str, message_id: str, max_chars: int = 6000) -> str:
    _calls["body"].append((account_label, message_id))
    raise ApplicationError("gmail down", non_retryable=True)


@activity.defn(name="load_receipts")
async def stub_load(ids: list[str]) -> list[dict]:
    _calls["load"].append(list(ids))
    return [
        {
            "id": i,
            "account": "user-personal",
            "message_id": i.replace("uid-", ""),
            "sender": "billing@stripe.com",
            "subject": "Your receipt",
            "body_plain": "paid $9.99",
            "received_at": "",
        }
        for i in ids
    ]


@activity.defn(name="classify_and_extract")
async def stub_classify_receipt(receipts: list[dict], agent_id: str) -> list[dict]:
    _calls["classify"].append((len(receipts), agent_id))
    return [
        {
            "is_receipt": True,
            "vendor_name": "Stripe",
            "sender_label": "stripe.com",
            "category": "saas",
            "amount": 9.99,
            "currency": "USD",
            "cadence": "monthly",
            "confidence": 0.95,
            "receipt_id": r["id"],
        }
        for r in receipts
    ]


@activity.defn(name="upsert_charges")
async def stub_upsert(account: str, exts: list[dict]) -> int:
    _calls["upsert"].append((account, len(exts)))
    return len(exts)


_HAPPY_STUBS = [
    stub_store,
    stub_body,
    stub_store_body,
    stub_load,
    stub_classify_receipt,
    stub_upsert,
]


@pytest.mark.asyncio
async def test_charged_path():
    """Receipt email stored → classified as receipt → charge upserted."""
    _reset()
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue="tq",
            workflows=[MoneyProcessFlow],
            activities=_HAPPY_STUBS,
        ),
    ):
        result = await env.client.execute_workflow(
            MoneyProcessFlow.run,
            MoneyProcessInput(agent_id="maou", msg=_MSG, account_label="user-personal"),
            id="mp-happy-1",
            task_queue="tq",
        )

    assert result["status"] == "charged"
    assert result["processed"] == 1
    assert _calls["store"] == [("gmail-msg-1", "user-personal")]
    assert _calls["classify"] == [(1, "maou")]
    assert _calls["upsert"] == [("user-personal", 1)]
    assert _calls["body"] == [("user-personal", "gmail-msg-1")]
    assert _calls["store_body"] == [("uid-gmail-msg-1", "Receipt from Stripe $9.99 Paid")]


@pytest.mark.asyncio
async def test_empty_body_is_not_stored():
    _reset()
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue="tq",
            workflows=[MoneyProcessFlow],
            activities=[
                stub_store,
                stub_body_empty,
                stub_store_body,
                stub_load,
                stub_classify_receipt,
                stub_upsert,
            ],
        ),
    ):
        result = await env.client.execute_workflow(
            MoneyProcessFlow.run,
            MoneyProcessInput(agent_id="maou", msg=_MSG, account_label="user-personal"),
            id="mp-empty-body",
            task_queue="tq",
        )
    assert result["status"] == "charged"
    assert _calls["store_body"] == []


@pytest.mark.asyncio
async def test_duplicate_short_circuits():
    """store_receipt_email returning '' (ON CONFLICT DO NOTHING) skips downstream."""
    _reset()

    @activity.defn(name="store_receipt_email")
    async def dup_store(msg: dict, account: str) -> str:
        _calls["store"].append((msg["id"], account))
        return ""

    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue="tq",
            workflows=[MoneyProcessFlow],
            activities=[
                dup_store,
                stub_body,
                stub_store_body,
                stub_load,
                stub_classify_receipt,
                stub_upsert,
            ],
        ),
    ):
        result = await env.client.execute_workflow(
            MoneyProcessFlow.run,
            MoneyProcessInput(agent_id="maou", msg=_MSG, account_label="user-personal"),
            id="mp-dup-1",
            task_queue="tq",
        )

    assert result["status"] == "duplicate"
    assert _calls["body"] == []
    assert _calls["store_body"] == []
    assert _calls["load"] == []
    assert _calls["classify"] == []
    assert _calls["upsert"] == []


@pytest.mark.asyncio
async def test_parse_failed_does_not_upsert():
    """Bundle E: extractor returned _parse_failed=True for the item → flow must
    NOT call upsert_charges (we don't want to mark parsed and lose the chance
    to retry next run)."""
    _reset()

    @activity.defn(name="classify_and_extract")
    async def parse_failed_classify(receipts: list[dict], agent_id: str) -> list[dict]:
        _calls["classify"].append((len(receipts), agent_id))
        return [
            {
                "is_receipt": False,
                "confidence": 0.0,
                "_parse_failed": True,
                "receipt_id": r["id"],
            }
            for r in receipts
        ]

    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue="tq",
            workflows=[MoneyProcessFlow],
            activities=[
                stub_store,
                stub_body,
                stub_store_body,
                stub_load,
                parse_failed_classify,
                stub_upsert,
            ],
        ),
    ):
        result = await env.client.execute_workflow(
            MoneyProcessFlow.run,
            MoneyProcessInput(agent_id="maou", msg=_MSG, account_label="user-personal"),
            id="mp-parsefailed-1",
            task_queue="tq",
        )

    assert result["status"] == "parse_failed"
    # upsert_charges must NOT be called — receipt stays unparsed for the next run.
    assert _calls["upsert"] == []


@pytest.mark.asyncio
async def test_classify_raises_returns_extract_failed():
    """Bundle E: when classify_and_extract raises (after retries), flow returns
    extract_failed and skips upsert. The receipt_email row stays unparsed."""
    _reset()

    @activity.defn(name="classify_and_extract")
    async def raising_classify(receipts: list[dict], agent_id: str) -> list[dict]:
        _calls["classify"].append((len(receipts), agent_id))
        raise RuntimeError("simulated LLM batch failure")

    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue="tq",
            workflows=[MoneyProcessFlow],
            activities=[
                stub_store,
                stub_body,
                stub_store_body,
                stub_load,
                raising_classify,
                stub_upsert,
            ],
        ),
    ):
        result = await env.client.execute_workflow(
            MoneyProcessFlow.run,
            MoneyProcessInput(agent_id="maou", msg=_MSG, account_label="user-personal"),
            id="mp-extractfail-1",
            task_queue="tq",
        )

    assert result["status"] == "extract_failed"
    assert _calls["upsert"] == []


@pytest.mark.asyncio
async def test_not_a_receipt_still_marks_parsed():
    """Classifier says is_receipt=False → upsert_charges called to mark parsed, status=not_a_receipt."""
    _reset()

    @activity.defn(name="classify_and_extract")
    async def non_receipt_classify(receipts: list[dict], agent_id: str) -> list[dict]:
        _calls["classify"].append((len(receipts), agent_id))
        return [{"is_receipt": False, "confidence": 0.1, "receipt_id": r["id"]} for r in receipts]

    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue="tq",
            workflows=[MoneyProcessFlow],
            activities=[
                stub_store,
                stub_body,
                stub_store_body,
                stub_load,
                non_receipt_classify,
                stub_upsert,
            ],
        ),
    ):
        result = await env.client.execute_workflow(
            MoneyProcessFlow.run,
            MoneyProcessInput(agent_id="maou", msg=_MSG, account_label="user-personal"),
            id="mp-notreceipt-1",
            task_queue="tq",
        )

    assert result["status"] == "not_a_receipt"
    # upsert_charges still called with the non-receipt so receipt_email.parsed is written.
    assert _calls["upsert"] == [("user-personal", 1)]


@pytest.mark.asyncio
async def test_body_fetch_failure_still_charges():
    """The full body is an ENHANCEMENT. A hard fetch_message_body failure
    (activity error or start_to_close timeout, not the "" soft-fail) must
    not kill the run — the snippet stored by store_receipt_email is still
    a perfectly good extraction input."""
    _reset()
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue="tq",
            workflows=[MoneyProcessFlow],
            activities=[
                stub_store,
                stub_body_boom,
                stub_store_body,
                stub_load,
                stub_classify_receipt,
                stub_upsert,
            ],
        ),
    ):
        result = await env.client.execute_workflow(
            MoneyProcessFlow.run,
            MoneyProcessInput(agent_id="maou", msg=_MSG, account_label="user-personal"),
            id="mp-body-boom",
            task_queue="tq",
        )

    assert result["status"] == "charged"
    assert _calls["store_body"] == []
    # The rest of the pipeline ran on the snippet.
    assert _calls["classify"] == [(1, "maou")]
    assert _calls["upsert"] == [("user-personal", 1)]
