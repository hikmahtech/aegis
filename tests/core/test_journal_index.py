from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
import pytest_asyncio
from aegis.api.models.money import MoneyEvent
from aegis.services import journal_index as ji


@pytest_asyncio.fixture(loop_scope="function", autouse=True)
async def _clean(db_pool):
    await db_pool.execute("DELETE FROM finance.journal_index WHERE message_id LIKE 'ji-%'")
    yield
    await db_pool.execute("DELETE FROM finance.journal_index WHERE message_id LIKE 'ji-%'")


def _bank(amount="10.00", day=2, **kw) -> MoneyEvent:
    base = {
        "kind": "transaction", "direction": "out", "amount": Decimal(amount), "currency": "INR",
        "payee": "Corner Store", "payee_key": "corner store", "channel": "upi",
        "instrument": "hdfc-1225", "occurred_on": date(2026, 9, day), "entity": "personal",
        "account": "expenses:unknown", "parser": "hdfc_upi", "source_class": "bank",
    }
    base.update(kw)
    return MoneyEvent(**base)


def test_msgid_for():
    assert ji.msgid_for("arshad-personal", "1a06") == "arshad-personal/1a06"


@pytest.mark.asyncio
async def test_upsert_get_and_coalescing_update(db_pool):
    ev = _bank()
    await ji.upsert(db_pool, "ji-mail/1", "arshad-personal", ev, journal_file="personal/2026.journal")
    row = await ji.get(db_pool, "ji-mail/1")
    assert row["amount"] == Decimal("10.00") and row["journal_file"] == "personal/2026.journal"
    assert row["source_class"] == "bank" and row["occurred_on"] == date(2026, 9, 2)
    # re-upsert without journal_file keeps it; payee change lands
    await ji.upsert(db_pool, "ji-mail/1", "arshad-personal", ev.model_copy(update={"payee": "Shop"}))
    row = await ji.get(db_pool, "ji-mail/1")
    assert row["journal_file"] == "personal/2026.journal" and row["payee"] == "Shop"
    assert await ji.get(db_pool, "ji-nope") is None


@pytest.mark.asyncio
async def test_find_match_opposite_class_same_amount_within_3_days(db_pool):
    await ji.upsert(db_pool, "ji-bank/1", "arshad-personal", _bank(day=2), journal_file="personal/2026.journal")
    receipt = _bank(day=4, source_class="receipt", channel="receipt", parser="stripe_receipt", instrument=None)
    m = await ji.find_match(db_pool, receipt, "ji-rcpt/1")
    assert m is not None and m["message_id"] == "ji-bank/1"
    # same class never matches
    assert await ji.find_match(db_pool, _bank(day=2), "ji-bank/2") is None
    # 4 days apart does not match
    assert await ji.find_match(db_pool, receipt.model_copy(update={"occurred_on": date(2026, 9, 6)}), "ji-rcpt/2") is None
    # different amount does not match
    assert await ji.find_match(db_pool, receipt.model_copy(update={"amount": Decimal("11.00")}), "ji-rcpt/3") is None
    # a linked row is no longer a candidate
    await ji.link(db_pool, "ji-bank/1", "ji-rcpt/1")
    assert (await ji.get(db_pool, "ji-bank/1"))["linked_message_id"] == "ji-rcpt/1"
    assert await ji.find_match(db_pool, receipt, "ji-rcpt/9") is None


@pytest.mark.asyncio
async def test_find_match_prefers_nearest_date(db_pool):
    await ji.upsert(db_pool, "ji-bank/far", "arshad-personal", _bank(day=1),
                    journal_file="personal/2026.journal")
    await ji.upsert(db_pool, "ji-bank/near", "arshad-personal", _bank(day=3),
                    journal_file="personal/2026.journal")
    receipt = _bank(day=4, source_class="receipt")
    assert (await ji.find_match(db_pool, receipt, "ji-r"))["message_id"] == "ji-bank/near"


@pytest.mark.asyncio
async def test_find_match_skips_rows_with_no_journal_block(db_pool):
    """A row indexed while the books were disabled has no block to enrich.
    Returning it made `books.rewrite_event` raise an uncaught BooksError and
    the activity retry forever."""
    await ji.upsert(db_pool, "ji-bank/nofile", "arshad-personal", _bank(day=2))
    receipt = _bank(day=2, source_class="receipt")
    assert await ji.find_match(db_pool, receipt, "ji-rcpt/1") is None
    # Same row, now posted: every other filter already admitted it, so only
    # `journal_file IS NOT NULL` can have been rejecting it.
    await ji.upsert(db_pool, "ji-bank/nofile", "arshad-personal", _bank(day=2),
                    journal_file="personal/2026.journal")
    m = await ji.find_match(db_pool, receipt, "ji-rcpt/1")
    assert m is not None and m["message_id"] == "ji-bank/nofile"


@pytest.mark.asyncio
async def test_find_match_never_crosses_entities(db_pool):
    """A hikmah receipt must not link to a personal bank alert — the
    enrichment would write an `expenses:hikmah:*` account into
    `personal/2026.journal`."""
    await ji.upsert(db_pool, "ji-bank/personal", "arshad-personal", _bank(day=2),
                    journal_file="personal/2026.journal")
    same = _bank(day=2, source_class="receipt", entity="personal")
    assert (await ji.find_match(db_pool, same, "ji-r/1"))["message_id"] == "ji-bank/personal"
    cross = _bank(day=2, source_class="receipt", entity="hikmah")
    assert await ji.find_match(db_pool, cross, "ji-r/2") is None


@pytest.mark.asyncio
async def test_mark_due_paid_leaves_the_payment_link_alone(db_pool):
    """`link` writes both sides; closing a due must write only the due, or it
    clobbers the payment's own link to its bank/receipt counterpart."""
    await ji.upsert(db_pool, "ji-pay/1", "arshad-personal", _bank(day=6),
                    journal_file="personal/2026.journal")
    await ji.upsert(db_pool, "ji-cnt/1", "arshad-personal", _bank(day=6, source_class="receipt"),
                    journal_file="personal/2026.journal")
    await ji.link(db_pool, "ji-pay/1", "ji-cnt/1")
    # an unknown due is a no-op, not an error
    await ji.mark_due_paid(db_pool, "ji-due/9", "ji-pay/1")
    due = MoneyEvent(kind="due", direction="out", amount=Decimal("10.00"), currency="INR",
                     payee="Corner Store", payee_key="corner store", channel="statement",
                     due_on=date(2026, 9, 7), entity="personal", parser="axis_cc_statement",
                     source_class="bank")
    await ji.upsert(db_pool, "ji-due/1", "arshad-personal", due, todoist_ref="task-1")
    await ji.mark_due_paid(db_pool, "ji-due/1", "ji-pay/1")
    assert (await ji.get(db_pool, "ji-due/1"))["linked_message_id"] == "ji-pay/1"
    assert (await ji.get(db_pool, "ji-pay/1"))["linked_message_id"] == "ji-cnt/1"
    assert (await ji.get(db_pool, "ji-cnt/1"))["linked_message_id"] == "ji-pay/1"
    # and the due is out of the open-due pool
    assert await ji.find_open_due(
        db_pool, "corner store", Decimal("10.00"), "INR", date(2026, 9, 6)
    ) is None


@pytest.mark.asyncio
async def test_find_open_due_tolerance_and_window(db_pool):
    due = MoneyEvent(kind="due", direction="out", amount=Decimal("100308.53"), currency="INR",
                     payee="Axis credit card XX13", payee_key="axis credit card xx13",
                     channel="statement", due_on=date(2026, 9, 7), entity="personal",
                     parser="axis_cc_statement", source_class="bank")
    await ji.upsert(db_pool, "ji-due/1", "arshad-personal", due, todoist_ref="task-1")
    hit = await ji.find_open_due(db_pool, "axis credit card xx13", Decimal("100300.00"), "INR", date(2026, 9, 6))
    assert hit is not None and hit["todoist_ref"] == "task-1"
    assert await ji.find_open_due(db_pool, "axis credit card xx13", Decimal("90000.00"), "INR", date(2026, 9, 6)) is None
    assert await ji.find_open_due(db_pool, "axis credit card xx13", Decimal("100308.53"), "INR", date(2026, 12, 1)) is None
    # an unrelated payee never matches — asserted while ji-due/1 is still the one open row,
    # so only the payee filter can be rejecting it
    assert await ji.find_open_due(db_pool, "other", Decimal("100308.53"), "INR", date(2026, 9, 6)) is None
    # ji-due/2 is the same due with NO todoist_ref, which is what every due
    # `capture_due` refused to task looks like — a zero invoice, a twin already
    # tasked under another payee's name, an autopay notice. Open means unpaid
    # (`linked_message_id IS NULL`), not "has a task": nothing else writes that
    # column for a due, so requiring a ref here left every guarded due
    # permanently open in the brief, the month close and the admin page.
    await ji.upsert(db_pool, "ji-due/2", "arshad-personal", due)
    await db_pool.execute("DELETE FROM finance.journal_index WHERE message_id = 'ji-due/1'")
    hit = await ji.find_open_due(db_pool, "axis credit card xx13", Decimal("100300.00"), "INR", date(2026, 9, 6))
    assert hit is not None and hit["message_id"] == "ji-due/2"
    assert hit["todoist_ref"] is None
    # ...and paying it still takes it out of the pool, task or no task.
    await ji.mark_due_paid(db_pool, "ji-due/2", "ji-pay/1")
    assert await ji.find_open_due(db_pool, "axis credit card xx13", Decimal("100300.00"), "INR", date(2026, 9, 6)) is None
