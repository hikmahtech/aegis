"""Bundle E — Maou money correctness regression tests.

Covers:
  1. evaluate_renewal_alerts past-due 14d guard
  2. notify_renewal_alert send-level 7d dedup
  3. detect_cancellations cadence IN (...) filter
  4. notify_cancellation chat send
  5. upsert_charges cadence preservation (real → unknown keeps real)

Schema dependency: requires migration 019 (renewal_alert.last_notified_at).
DB-bound tests skip on no Postgres via the shared `db_pool` fixture.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest
from aegis_worker.activities.money import MoneyActivities
from temporalio.testing import ActivityEnvironment

_FX_RATES = {"USD": 84.5}


def _make_act(db_pool, delivery=None):
    return MoneyActivities(
        db_pool=db_pool,
        llm=None,
        delivery=delivery,
        fx_rates=_FX_RATES,
        home_currency="INR",
    )


async def _clean(conn, sender_label_like: str) -> None:
    """Drop dependent rows then the recurring_charge rows. FK constraints
    require this order: renewal_alert + receipt_email both reference
    recurring_charge.id."""
    await conn.execute(
        "DELETE FROM maou.renewal_alert WHERE charge_id IN "
        "(SELECT id FROM maou.recurring_charge WHERE sender_label LIKE $1)",
        sender_label_like,
    )
    await conn.execute(
        "UPDATE maou.receipt_email SET charge_id = NULL WHERE charge_id IN "
        "(SELECT id FROM maou.recurring_charge WHERE sender_label LIKE $1)",
        sender_label_like,
    )
    await conn.execute(
        "DELETE FROM maou.recurring_charge WHERE sender_label LIKE $1",
        sender_label_like,
    )


async def _insert_active_charge(
    conn,
    *,
    account: str,
    sender_label: str,
    amount_cents: int,
    currency: str = "USD",
    cadence: str = "monthly",
    monthly_home: float = 100.0,
) -> str:
    """Minimal helper. Caller bumps next_due_at / last_seen_at as needed
    after the row is created (interval arithmetic is awkward to pass through
    asyncpg's parameter substitution)."""
    row = await conn.fetchrow(
        """
        INSERT INTO maou.recurring_charge
          (account, sender_label, vendor_name, category, amount_cents,
           currency, monthly_home_equivalent, cadence, status)
        VALUES ($1, $2, $3, 'saas', $4, $5, $6, $7, 'active')
        RETURNING id
        """,
        account,
        sender_label,
        sender_label,
        amount_cents,
        currency,
        monthly_home,
        cadence,
    )
    return str(row["id"])


# ----------------- 1. past-due 14d guard ----------------------


@pytest.mark.asyncio
async def test_evaluate_renewal_alerts_includes_recent_past_due(db_pool):
    """A charge that went past-due 3 days ago should still produce one final
    0-day alert (so the user can act on it)."""
    act = _make_act(db_pool)
    async with db_pool.acquire() as conn:
        await _clean(conn, "bundle-e-%")
        charge_id = await _insert_active_charge(
            conn,
            account="acct-pd-recent",
            sender_label="bundle-e-recent",
            amount_cents=999,
        )
        await conn.execute(
            "UPDATE maou.recurring_charge SET next_due_at = NOW() - INTERVAL '3 days' "
            "WHERE id = $1::uuid",
            charge_id,
        )

    env = ActivityEnvironment()
    alerts = await env.run(act.evaluate_renewal_alerts, [0])
    ids = {a["charge_id"] for a in alerts}
    assert charge_id in ids


@pytest.mark.asyncio
async def test_evaluate_renewal_alerts_excludes_long_past_due(db_pool):
    """A charge whose next_due_at is more than 14 days in the past should NOT
    appear in the eligible set, even if days_left <= threshold."""
    act = _make_act(db_pool)
    async with db_pool.acquire() as conn:
        await _clean(conn, "bundle-e-%")
        charge_id = await _insert_active_charge(
            conn,
            account="acct-pd-old",
            sender_label="bundle-e-old",
            amount_cents=1999,
        )
        await conn.execute(
            "UPDATE maou.recurring_charge SET next_due_at = NOW() - INTERVAL '20 days' "
            "WHERE id = $1::uuid",
            charge_id,
        )

    env = ActivityEnvironment()
    alerts = await env.run(act.evaluate_renewal_alerts, [0])
    ids = {a["charge_id"] for a in alerts}
    assert charge_id not in ids


# ----------------- 2. notify_renewal_alert 7d dedup -----------


@pytest.mark.asyncio
async def test_notify_renewal_alert_skips_within_7d(db_pool):
    """Second notify for same (charge_id, threshold_days) within 7d → no send."""
    delivery = AsyncMock()
    delivery.channel = "slack"
    delivery.send_message = AsyncMock(return_value={"ok": True})
    act = _make_act(db_pool, delivery=delivery)

    async with db_pool.acquire() as conn:
        await _clean(conn, "bundle-e-dedup%")
        charge_id = await _insert_active_charge(
            conn,
            account="acct-dedup",
            sender_label="bundle-e-dedup",
            amount_cents=1299,
        )
        alert_row = await conn.fetchrow(
            "INSERT INTO maou.renewal_alert (charge_id, threshold_days) "
            "VALUES ($1::uuid, 7) RETURNING id",
            charge_id,
        )
        alert_id = str(alert_row["id"])

    base_alert = {
        "alert_id": alert_id,
        "charge_id": charge_id,
        "threshold_days": 7,
        "vendor_name": "Netflix",
        "category": "media",
        "currency": "USD",
        "account": "acct-dedup",
        "amount_cents": 1299,
        "monthly_home_equivalent": 109.7,
        "days_left": 5,
        "next_due_at": "2026-06-15T00:00:00",
    }

    env = ActivityEnvironment()
    await env.run(act.notify_renewal_alert, base_alert)
    assert delivery.send_message.await_count == 1

    # Second invocation within 7d → should be silent.
    await env.run(act.notify_renewal_alert, base_alert)
    assert delivery.send_message.await_count == 1


@pytest.mark.asyncio
async def test_notify_renewal_alert_sends_when_no_prior(db_pool):
    """Fresh alert with no prior last_notified_at → send fires once and stamps row."""
    delivery = AsyncMock()
    delivery.channel = "slack"
    delivery.send_message = AsyncMock(return_value={"ok": True})
    act = _make_act(db_pool, delivery=delivery)

    async with db_pool.acquire() as conn:
        await _clean(conn, "bundle-e-fresh%")
        charge_id = await _insert_active_charge(
            conn,
            account="acct-fresh",
            sender_label="bundle-e-fresh",
            amount_cents=499,
        )
        alert_row = await conn.fetchrow(
            "INSERT INTO maou.renewal_alert (charge_id, threshold_days) "
            "VALUES ($1::uuid, 30) RETURNING id",
            charge_id,
        )
        alert_id = str(alert_row["id"])

    env = ActivityEnvironment()
    await env.run(
        act.notify_renewal_alert,
        {
            "alert_id": alert_id,
            "charge_id": charge_id,
            "threshold_days": 30,
            "vendor_name": "DigitalOcean",
            "category": "infra",
            "currency": "USD",
            "account": "acct-fresh",
            "amount_cents": 499,
            "monthly_home_equivalent": 42.0,
            "days_left": 25,
            "next_due_at": "2026-06-22T00:00:00",
        },
    )
    assert delivery.send_message.await_count == 1
    async with db_pool.acquire() as conn:
        stamped = await conn.fetchval(
            "SELECT last_notified_at FROM maou.renewal_alert WHERE id=$1::uuid",
            alert_id,
        )
    assert stamped is not None


# ----------------- 3. detect_cancellations cadence filter -----


@pytest.mark.asyncio
async def test_detect_cancellations_skips_non_standard_cadence(db_pool):
    """A charge with cadence='unknown' (or anything outside monthly/quarterly/yearly)
    should NOT be auto-cancelled regardless of last_seen_at age."""
    act = _make_act(db_pool)
    async with db_pool.acquire() as conn:
        await _clean(conn, "bundle-e-cad%")
        unknown_id = await _insert_active_charge(
            conn,
            account="acct-cad",
            sender_label="bundle-e-cad-unknown",
            amount_cents=799,
            cadence="unknown",
        )
        # Force last_seen_at far in the past.
        await conn.execute(
            "UPDATE maou.recurring_charge SET last_seen_at = NOW() - INTERVAL '24 months' "
            "WHERE id = $1::uuid",
            unknown_id,
        )

    env = ActivityEnvironment()
    cancelled = await env.run(act.detect_cancellations, 2.0)
    ids = {str(c["id"]) for c in cancelled}
    assert unknown_id not in ids
    async with db_pool.acquire() as conn:
        status = await conn.fetchval(
            "SELECT status FROM maou.recurring_charge WHERE id=$1::uuid",
            unknown_id,
        )
    assert status == "active"


@pytest.mark.asyncio
async def test_detect_cancellations_flags_stale_monthly(db_pool):
    """A monthly charge unseen for >2 months IS flagged as cancelled."""
    act = _make_act(db_pool)
    async with db_pool.acquire() as conn:
        await _clean(conn, "bundle-e-cad%")
        monthly_id = await _insert_active_charge(
            conn,
            account="acct-cad",
            sender_label="bundle-e-cad-monthly",
            amount_cents=1299,
            cadence="monthly",
        )
        await conn.execute(
            "UPDATE maou.recurring_charge SET last_seen_at = NOW() - INTERVAL '4 months' "
            "WHERE id = $1::uuid",
            monthly_id,
        )

    env = ActivityEnvironment()
    cancelled = await env.run(act.detect_cancellations, 2.0)
    ids = {str(c["id"]) for c in cancelled}
    assert monthly_id in ids


# ----------------- 4. notify_cancellation ----------------------


@pytest.mark.asyncio
async def test_notify_cancellation_calls_safe_send_message():
    captured: list[dict] = []

    async def fake_send(*, agent_id, message, chat_id=0, keyboard=None):
        captured.append({"agent_id": agent_id, "message": message})
        return {"ok": True, "message_id": 1}

    delivery = AsyncMock()
    delivery.channel = "slack"
    delivery.send_message = fake_send
    act = MoneyActivities(db_pool=None, llm=None, delivery=delivery, fx_rates=_FX_RATES)
    env = ActivityEnvironment()
    await env.run(
        act.notify_cancellation,
        {
            "id": "abc-123",
            "vendor_name": "Netflix",
            "amount_cents": 1599,
            "currency": "USD",
            "cadence": "monthly",
            "last_seen_at": "2026-03-01 00:00:00",
            "account": "personal",
        },
    )
    assert len(captured) == 1
    assert captured[0]["agent_id"] == "maou"
    msg = captured[0]["message"]
    assert "[CANCEL]" in msg
    assert "Netflix" in msg


@pytest.mark.asyncio
async def test_notify_cancellation_html_escapes_vendor():
    captured: list[str] = []

    async def fake_send(*, agent_id, message, chat_id=0, keyboard=None):
        captured.append(message)
        return {"ok": True}

    delivery = AsyncMock()
    delivery.channel = "slack"
    delivery.send_message = fake_send
    act = MoneyActivities(db_pool=None, llm=None, delivery=delivery, fx_rates=_FX_RATES)
    env = ActivityEnvironment()
    await env.run(
        act.notify_cancellation,
        {
            "id": "xx",
            "vendor_name": "<script>alert</script>",
            "amount_cents": 0,
            "currency": "USD",
            "cadence": "monthly",
            "last_seen_at": None,
            "account": "",
        },
    )
    assert "<script>" not in captured[0]
    assert "&lt;script&gt;" in captured[0]


# ----------------- 5. upsert_charges cadence preservation -----


@pytest.mark.asyncio
async def test_upsert_charges_preserves_real_cadence_on_unknown(db_pool):
    """Sequence: insert with cadence='yearly' → reapply with cadence='unknown'.
    Stored cadence must remain 'yearly'."""
    fake_llm = AsyncMock()
    act = MoneyActivities(
        db_pool=db_pool, llm=fake_llm, delivery=None, fx_rates=_FX_RATES
    )

    receipt_id_1 = str(uuid.uuid4())
    receipt_id_2 = str(uuid.uuid4())
    async with db_pool.acquire() as conn:
        await _clean(conn, "bundle-e-cad-merge")
        for rid in (receipt_id_1, receipt_id_2):
            await conn.execute(
                "INSERT INTO maou.receipt_email "
                "(id, message_id, account, sender, subject, received_at, parsed) "
                "VALUES ($1::uuid, $2, 'acct-merge', 'a@b.com', 's', NOW(), '{}'::jsonb) "
                "ON CONFLICT (message_id) DO NOTHING",
                rid,
                f"mm-{rid}",
            )

    env = ActivityEnvironment()
    await env.run(
        act.upsert_charges,
        "acct-merge",
        [
            {
                "receipt_id": receipt_id_1,
                "is_receipt": True,
                "vendor_name": "Acme Corp",
                "sender_label": "bundle-e-cad-merge",
                "category": "saas",
                "amount": 1.99,
                "currency": "USD",
                "cadence": "yearly",
            }
        ],
    )
    await env.run(
        act.upsert_charges,
        "acct-merge",
        [
            {
                "receipt_id": receipt_id_2,
                "is_receipt": True,
                "vendor_name": "Acme Corp",
                "sender_label": "bundle-e-cad-merge",
                "category": "saas",
                "amount": 1.99,
                "currency": "USD",
                "cadence": "unknown",
            }
        ],
    )
    async with db_pool.acquire() as conn:
        cadence = await conn.fetchval(
            "SELECT cadence FROM maou.recurring_charge WHERE sender_label='bundle-e-cad-merge'"
        )
    assert cadence == "yearly"


@pytest.mark.asyncio
async def test_upsert_charges_upgrades_unknown_to_real(db_pool):
    """Sequence: insert with cadence='unknown' → reapply with cadence='monthly'.
    Stored cadence must upgrade to 'monthly'."""
    fake_llm = AsyncMock()
    act = MoneyActivities(
        db_pool=db_pool, llm=fake_llm, delivery=None, fx_rates=_FX_RATES
    )
    receipt_id_1 = str(uuid.uuid4())
    receipt_id_2 = str(uuid.uuid4())
    async with db_pool.acquire() as conn:
        await _clean(conn, "bundle-e-cad-upgrade")
        for rid in (receipt_id_1, receipt_id_2):
            await conn.execute(
                "INSERT INTO maou.receipt_email "
                "(id, message_id, account, sender, subject, received_at, parsed) "
                "VALUES ($1::uuid, $2, 'acct-upg', 'a@b.com', 's', NOW(), '{}'::jsonb) "
                "ON CONFLICT (message_id) DO NOTHING",
                rid,
                f"upg-{rid}",
            )

    env = ActivityEnvironment()
    await env.run(
        act.upsert_charges,
        "acct-upg",
        [
            {
                "receipt_id": receipt_id_1,
                "is_receipt": True,
                "vendor_name": "Beta Inc",
                "sender_label": "bundle-e-cad-upgrade",
                "category": "saas",
                "amount": 5.00,
                "currency": "USD",
                "cadence": "unknown",
            }
        ],
    )
    await env.run(
        act.upsert_charges,
        "acct-upg",
        [
            {
                "receipt_id": receipt_id_2,
                "is_receipt": True,
                "vendor_name": "Beta Inc",
                "sender_label": "bundle-e-cad-upgrade",
                "category": "saas",
                "amount": 5.00,
                "currency": "USD",
                "cadence": "monthly",
            }
        ],
    )
    async with db_pool.acquire() as conn:
        cadence = await conn.fetchval(
            "SELECT cadence FROM maou.recurring_charge WHERE sender_label='bundle-e-cad-upgrade'"
        )
    assert cadence == "monthly"
