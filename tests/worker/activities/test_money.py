"""Tests for MoneyActivities (Maou — Money Hygiene).

NOTE: Phase 1 of v3 reshaped `finance.recurring_charge` and `finance.receipt_email`
(identity_hash unique index, dropped sender_label/body_plain/parsed_at/etc).
The DB-bound tests that asserted the v2 schema were deleted; they will be
reintroduced in Phase 3/4 alongside the rewritten MoneyActivities. The tests
below are the schema-agnostic ones that still exercise real behaviour
(LLM-batch wiring + HTML escape).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from aegis_worker.activities import money as money_module
from aegis_worker.activities.money import MoneyActivities, _is_bank_alert_sender


def _recording_pool():
    """Mock asyncpg pool that records every SQL string passed to the
    connection's fetchrow/execute. `async with pool.acquire() as conn:` works."""
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"id": "charge-1"})
    conn.execute = AsyncMock(return_value="OK")
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)
    return pool, conn


def test_is_bank_alert_sender_default_empty_is_noop():
    """AEGIS_BANK_ALERT_SENDERS unset ⇒ _BANK_ALERT_SENDERS is empty and the
    guard never matches — a clean no-op until a self-hoster configures it."""
    assert not money_module._BANK_ALERT_SENDERS
    assert not _is_bank_alert_sender("alerts@axis.bank.in")
    assert not _is_bank_alert_sender("", "")


def test_is_bank_alert_sender_matches_substring(monkeypatch):
    monkeypatch.setattr(
        money_module, "_BANK_ALERT_SENDERS", frozenset({"axis.bank.in", "axisbank.com"})
    )
    assert _is_bank_alert_sender("alerts@axis.bank.in")
    assert _is_bank_alert_sender("", "ALERTS@AXISBANK.COM")  # case-insensitive
    assert not _is_bank_alert_sender("billing@namecheap.com", "namecheap.com")
    assert not _is_bank_alert_sender("", "")


@pytest.mark.asyncio
async def test_upsert_charges_skips_bank_alert_sender(monkeypatch):
    """A receipt whose sender is a configured bank/card-alert domain must NOT
    be upserted as a recurring charge (the prod offenders: autopay reminders
    minting fake Google/AWS charges). A normal vendor receipt in the same
    batch IS written."""
    monkeypatch.setattr(
        money_module, "_BANK_ALERT_SENDERS", frozenset({"axis.bank.in"})
    )
    pool, conn = _recording_pool()
    act = MoneyActivities(db_pool=pool, llm=None, delivery=None, fx_rates={"USD": 84.5})

    extractions = [
        {
            "receipt_id": "00000000-0000-0000-0000-000000000001",
            "is_receipt": True,
            "vendor_name": "Google Workspace",
            "sender": "alerts@axis.bank.in",
            "sender_label": "axis.bank.in",
            "category": "saas",
            "amount": 1999.0,
            "currency": "INR",
            "cadence": "monthly",
        },
        {
            "receipt_id": "00000000-0000-0000-0000-000000000002",
            "is_receipt": True,
            "vendor_name": "Namecheap",
            "sender": "billing@namecheap.com",
            "sender_label": "namecheap.com",
            "category": "domain",
            "amount": 12.99,
            "currency": "USD",
            "cadence": "yearly",
        },
    ]

    processed = await act.upsert_charges("_t", extractions)

    # Both receipts counted as processed...
    assert processed == 2
    # ...but only the legit vendor receipt hit the recurring_charge INSERT.
    insert_calls = [
        c for c in conn.fetchrow.await_args_list if "INSERT INTO finance.recurring_charge" in c.args[0]
    ]
    assert len(insert_calls) == 1
    # INSERT args: (sql, account, sender_label, vendor_name, ...).
    # The single charge written is for Namecheap, never the bank-alert sender.
    assert insert_calls[0].args[3] == "Namecheap"
    sender_labels_inserted = {c.args[2] for c in insert_calls}
    assert "axis.bank.in" not in sender_labels_inserted


@pytest.mark.asyncio
async def test_classify_and_extract_batches_call(db_pool):
    fake_llm = AsyncMock()
    fake_llm.extract_receipts_batch = AsyncMock(
        return_value=[
            {
                "is_receipt": True,
                "vendor_name": "Namecheap",
                "sender_label": "namecheap.com",
                "category": "domain",
                "amount": 12.99,
                "currency": "USD",
                "cadence": "yearly",
                "next_due_at": "2027-04-15",
                "confidence": 0.9,
            }
        ]
    )

    act = MoneyActivities(
        db_pool=db_pool,
        llm=fake_llm,
        delivery=None,
        fx_rates={"USD": 84.5},
    )

    receipts = [
        {
            "id": "00000000-0000-0000-0000-000000000001",
            "account": "_t",
            "message_id": "m1",
            "sender": "billing@namecheap.com",
            "subject": "Domain renewed",
            "body_plain": "Renewed for $12.99",
            "received_at": "2026-04-15T10:00:00+00:00",
        }
    ]
    result = await act.classify_and_extract(receipts)
    assert len(result) == 1
    assert result[0]["is_receipt"] is True
    assert result[0]["vendor_name"] == "Namecheap"
    assert result[0]["receipt_id"] == "00000000-0000-0000-0000-000000000001"
    fake_llm.extract_receipts_batch.assert_awaited_once()


@pytest.mark.asyncio
async def test_classify_and_extract_empty_returns_empty(db_pool):
    act = MoneyActivities(
        db_pool=db_pool,
        llm=None,
        delivery=None,
        fx_rates={},
    )
    assert await act.classify_and_extract([]) == []


@pytest.mark.asyncio
async def test_notify_renewal_alert_sends_str_to_maou():
    """Regression: notify_renewal_alert routes through send_message(agent_id='maou',
    message=str). Pre-PR #257 it called send_system_event with a dict and 422'd."""
    from pydantic import BaseModel

    class DeliveryRequest(BaseModel):
        text: str
        chat_id: int = 0
        agent_id: str = "sebas"
        system_event: bool = False
        parse_mode: str = "HTML"
        reply_markup: dict | None = None

    captured: list[dict] = []

    async def fake_send(*, agent_id, message, chat_id=0, keyboard=None):
        body = {"text": message, "chat_id": chat_id, "agent_id": agent_id}
        captured.append(body)
        return {"ok": True, "message_id": 1, "chat_id": -1, "topic_id": 2756, "used_html": True}

    delivery = AsyncMock()
    delivery.channel = "slack"
    delivery.send_message = fake_send
    act = MoneyActivities(
        db_pool=None, llm=None, delivery=delivery, fx_rates={"USD": 84.5}, home_currency="INR"
    )
    await act.notify_renewal_alert(
        {
            "vendor_name": "Namecheap <example>",
            "category": "domain",
            "currency": "USD",
            "account": "personal",
            "threshold_days": 14,
            "amount_cents": 1299,
            "monthly_home_equivalent": 91.45,
            "days_left": 12,
            "next_due_at": "2026-06-06T00:00:00",
        }
    )
    assert len(captured) == 1
    assert captured[0]["agent_id"] == "maou"
    # html escape of `<example>` survives
    assert "&lt;example&gt;" in captured[0]["text"]
    DeliveryRequest.model_validate(captured[0])


@pytest.mark.asyncio
async def test_notify_subscription_digest_sends_message(db_pool):
    delivery = AsyncMock()
    delivery.channel = "slack"
    act = MoneyActivities(
        db_pool=db_pool,
        llm=None,
        delivery=delivery,
        fx_rates={"USD": 84.5},
        home_currency="INR",
    )
    digest = {
        "period_start": "2026-03-01",
        "period_end": "2026-03-31",
        "total_monthly_inr": 344.95,
        "active_count": 3,
        "by_category": {
            "domain": {"total_inr": 91.45, "count": 1},
            "infra": {"total_inr": 169.0, "count": 1},
        },
        "new_this_month": [{"vendor_name": "Notion", "monthly_home_equivalent": 84.5}],
        "cancelled_this_month": [],
        "top_spenders": [
            {"vendor_name": "DigitalOcean", "monthly_home_equivalent": 169.0},
            {"vendor_name": "<script>", "monthly_home_equivalent": 91.45},
        ],
    }
    await act.notify_subscription_digest(digest)
    delivery.send_message.assert_awaited_once()
    kwargs = delivery.send_message.await_args.kwargs
    assert kwargs["agent_id"] == "maou"
    message = kwargs["message"]
    assert isinstance(message, str)
    # HTML escape — vendor name with <script> must be escaped in body
    assert "<script>" not in message
    assert "&lt;script&gt;" in message
    # Default home_currency=INR ⇒ digest renders the ₹ symbol.
    assert "₹" in message


@pytest.mark.asyncio
async def test_notify_subscription_digest_renders_non_inr_home_symbol(db_pool):
    """A non-INR home_currency renders its own symbol, not ₹."""
    delivery = AsyncMock()
    delivery.channel = "slack"
    act = MoneyActivities(
        db_pool=db_pool,
        llm=None,
        delivery=delivery,
        fx_rates={"USD": 84.5},
        home_currency="USD",
    )
    digest = {
        "period_start": "2026-03-01",
        "period_end": "2026-03-31",
        "total_monthly_inr": 344.95,
        "active_count": 3,
        "by_category": {"domain": {"total_inr": 91.45, "count": 1}},
        "new_this_month": [],
        "cancelled_this_month": [],
        "top_spenders": [{"vendor_name": "DigitalOcean", "monthly_home_equivalent": 169.0}],
    }
    await act.notify_subscription_digest(digest)
    kwargs = delivery.send_message.await_args.kwargs
    message = kwargs["message"]
    assert "$" in message
    assert "₹" not in message
