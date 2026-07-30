"""merchant_history + apply_finance_decision."""

from __future__ import annotations

import pytest
import pytest_asyncio
from aegis_worker.activities.agent_task import AgentTaskActivities, extract_merchant


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Anomaly: ? Eleven Labs", "Eleven Labs"),
        ("Anomaly: 8100.00 INR Mahavitaran (MSEDCL)", "Mahavitaran (MSEDCL)"),
        ("Renewal in 19.6 days: Mahavitaran (MSEDCL) (810000 INR)", "Mahavitaran (MSEDCL)"),
        ("Something unrelated", ""),
    ],
)
def test_extract_merchant(title, expected):
    assert extract_merchant(title) == expected


@pytest_asyncio.fixture(loop_scope="function")
async def _charges(db_pool):
    await db_pool.execute("DELETE FROM finance.receipt_email WHERE message_id LIKE 'test-el-%'")
    await db_pool.execute("DELETE FROM finance.recurring_charge WHERE vendor_name = 'Eleven Labs'")
    # ONE charge signature (the table is upsert-keyed, 001_baseline.sql:726),
    # then TWO receipt rows against it — that is where real history lives.
    charge_id = await db_pool.fetchval(
        "INSERT INTO finance.recurring_charge "
        "  (account, sender_label, vendor_name, amount_cents, currency, last_seen_at) "
        "VALUES ('a','s','Eleven Labs', 2200, 'USD', now() - interval '30 days') "
        "RETURNING id"
    )
    await db_pool.execute(
        "INSERT INTO finance.receipt_email "
        "  (message_id, account, sender, subject, received_at, charge_id, parsed) "
        "VALUES ('test-el-1','a','billing@elevenlabs.io','Receipt', "
        "         now() - interval '30 days', $1, "
        "         '{\"is_receipt\": true, \"amount\": 22.0, \"currency\": \"USD\"}'::jsonb), "
        "       ('test-el-2','a','billing@elevenlabs.io','Receipt', "
        "         now() - interval '60 days', $1, "
        "         '{\"is_receipt\": true, \"amount\": 22.0, \"currency\": \"USD\"}'::jsonb)",
        charge_id,
    )
    yield
    await db_pool.execute("DELETE FROM finance.receipt_email WHERE message_id LIKE 'test-el-%'")
    await db_pool.execute("DELETE FROM finance.recurring_charge WHERE vendor_name = 'Eleven Labs'")


async def test_merchant_history_returns_prior_charges(db_pool, _charges):
    act = AgentTaskActivities(db_pool=db_pool)
    result = await act.merchant_history("Anomaly: ? Eleven Labs")
    assert result["merchant"] == "Eleven Labs"
    assert len(result["charges"]) == 2
    assert "22" in result["summary"]


async def test_merchant_history_unknown_merchant_is_empty_not_error(db_pool):
    result = await AgentTaskActivities(db_pool=db_pool).merchant_history("Something unrelated")
    assert result == {"merchant": "", "charges": [], "summary": ""}


def _act_recording(calls: list) -> AgentTaskActivities:
    """AgentTaskActivities with its three terminal-state writers recorded."""
    act = AgentTaskActivities(db_pool=None)

    async def _complete(task_id: str) -> dict:
        calls.append(("complete", task_id))
        return {"completed": True}

    async def _park(task_id: str, reason: str) -> dict:
        calls.append(("park", task_id))
        return {"parked": True}

    async def _comment(task_id: str, agent_id: str, body: str) -> dict:
        return {"ok": True}

    act.complete_task = _complete   # type: ignore[assignment]
    act.park_task = _park           # type: ignore[assignment]
    act.comment = _comment          # type: ignore[assignment]
    return act


async def test_finance_decision_expected_completes_the_task():
    calls: list = []
    meta = {"task_id": "tfin-1", "agent_id": "maou", "merchant": "Eleven Labs"}
    result = await _act_recording(calls).apply_finance_decision(
        "i1", {"value": "expected"}, meta
    )
    assert result == {"applied": "expected"}
    assert ("complete", "tfin-1") in calls


async def test_finance_decision_investigate_parks_the_task():
    calls: list = []
    meta = {"task_id": "tfin-2", "agent_id": "maou", "merchant": "Eleven Labs"}
    result = await _act_recording(calls).apply_finance_decision(
        "i1", {"value": "investigate"}, meta
    )
    assert result == {"applied": "investigate"}
    assert ("park", "tfin-2") in calls
