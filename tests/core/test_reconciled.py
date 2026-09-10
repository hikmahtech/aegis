"""finance.reconciled_through — the per-account statement watermark.

Spec: docs/superpowers/specs/2026-09-07-statement-reconciliation-design.md
§9.3, §15.10 item 2.
"""

from __future__ import annotations

from datetime import date

import pytest
import pytest_asyncio
from aegis.services import reconciled


@pytest_asyncio.fixture(loop_scope="function", autouse=True)
async def _clean(db_pool):
    await db_pool.execute("DELETE FROM finance.reconciled_through WHERE instrument LIKE 'rt%'")
    yield
    await db_pool.execute("DELETE FROM finance.reconciled_through WHERE instrument LIKE 'rt%'")


@pytest.mark.asyncio
async def test_never_reconciled_reads_none(db_pool):
    assert await reconciled.reconciled_through(db_pool, "rt-hdfc-1225") is None


@pytest.mark.asyncio
async def test_mark_then_read_round_trips(db_pool):
    await reconciled.mark_reconciled(
        db_pool, "rt-hdfc-1225", date(2026, 7, 31), statement_id="stmt-jul"
    )
    assert await reconciled.reconciled_through(db_pool, "rt-hdfc-1225") == date(2026, 7, 31)


@pytest.mark.asyncio
async def test_watermark_only_moves_forward(db_pool):
    await reconciled.mark_reconciled(
        db_pool, "rt-axis-9640", date(2026, 7, 31), statement_id="stmt-jul"
    )
    # A backfill reconciling June AFTER July must not un-reconcile July —
    # the watermark, and the statement that set it, both stand.
    await reconciled.mark_reconciled(
        db_pool, "rt-axis-9640", date(2026, 6, 30), statement_id="stmt-jun"
    )
    assert await reconciled.reconciled_through(db_pool, "rt-axis-9640") == date(2026, 7, 31)
    row = await db_pool.fetchrow(
        "SELECT statement_id FROM finance.reconciled_through WHERE instrument = 'rt-axis-9640'"
    )
    assert row["statement_id"] == "stmt-jul"


@pytest.mark.asyncio
async def test_watermark_advances_when_the_new_date_is_later(db_pool):
    await reconciled.mark_reconciled(
        db_pool, "rt-axis-9640", date(2026, 6, 30), statement_id="stmt-jun"
    )
    await reconciled.mark_reconciled(
        db_pool, "rt-axis-9640", date(2026, 7, 31), statement_id="stmt-jul"
    )
    assert await reconciled.reconciled_through(db_pool, "rt-axis-9640") == date(2026, 7, 31)
    row = await db_pool.fetchrow(
        "SELECT statement_id FROM finance.reconciled_through WHERE instrument = 'rt-axis-9640'"
    )
    assert row["statement_id"] == "stmt-jul"


@pytest.mark.asyncio
async def test_instrument_is_canonicalised_so_case_does_not_split_the_row(db_pool):
    # books.canonical_instrument lowercases a 2-segment (bank) or 3-segment
    # `-cc-` (card) instrument even with no chart to hand — see the module
    # docstring for what it can and cannot do without one. `rtbank-1225` is a
    # made-up bank code, chosen only because its shape (two segments) is what
    # exercises that path; a 3-segment instrument like `rt-hdfc-1225` does not
    # (only the `<bank>-cc-<tail>` 3-segment shape does), so it would come
    # back unchanged and prove nothing.
    await reconciled.mark_reconciled(
        db_pool, "RTBANK-1225", date(2026, 7, 31), statement_id="stmt-jul"
    )
    assert await reconciled.reconciled_through(db_pool, "rtbank-1225") == date(2026, 7, 31)


@pytest.mark.asyncio
async def test_watermark_is_per_instrument(db_pool):
    await reconciled.mark_reconciled(
        db_pool, "rt-hdfc-1225", date(2026, 7, 31), statement_id="stmt-jul"
    )
    assert await reconciled.reconciled_through(db_pool, "rt-axis-9640") is None
