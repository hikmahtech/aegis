"""finance.reconciled_through — the per-account statement watermark.

Spec: `docs/superpowers/specs/2026-09-07-statement-reconciliation-design.md`
§9.3, §15.10 item 2. Once a bank statement has reconciled an account through
a date, that statement is the record for everything up to and including it,
so `post_money_event` reads this watermark before writing a transaction and
declines to post one dated inside a reconciled period -- see the gate there
for the fail-open contract. The statement poster (spec step 5/6) is the only
writer, through `mark_reconciled`.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from aegis.services import books


def _canonical(instrument: str) -> str:
    """Normalise the way the rest of the lane does (`statement_match._canonical`).

    No chart is available here -- `reconciled_through`/`mark_reconciled` take
    a bare instrument, not a `books.BooksConfig` to read one from -- so this
    calls `books.canonical_instrument` with an empty chart, which is a no-op
    on an already-canonical spelling (`hdfc-1225`, `axis-cc-1313`: the form
    `finance.statement_rows.instrument` uses, per its own comment) but cannot
    resolve a bare `card-1313` to its bank the way a chart-aware caller can.
    A caller holding a chart should canonicalise before calling in, the way
    `post_money_event` does with the chart it already read for indexing.
    """
    return books.canonical_instrument(instrument) or instrument


async def reconciled_through(pool: Any, instrument: str) -> date | None:
    """The date `instrument` is reconciled through, or None if never reconciled."""
    row = await pool.fetchrow(
        "SELECT through_date FROM finance.reconciled_through WHERE instrument = $1",
        _canonical(instrument),
    )
    return row["through_date"] if row else None


async def mark_reconciled(pool: Any, instrument: str, through: date, *, statement_id: str) -> None:
    """Advance the watermark for `instrument` to `through`. Never moves it back.

    A backfill posts statements in whatever order the operator has them in
    hand, so reconciling June after July must not un-reconcile July. The
    `WHERE` clause on the `DO UPDATE` is what enforces that: it is evaluated
    against the row already in the table, so a `through` that is not strictly
    later leaves the existing row -- including its `statement_id` -- alone.
    Read-then-compare in Python would race two concurrent callers; this does
    not, because the check and the write are the same statement.
    """
    await pool.execute(
        """
        INSERT INTO finance.reconciled_through (instrument, through_date, statement_id, updated_at)
        VALUES ($1, $2, $3, now())
        ON CONFLICT (instrument) DO UPDATE
        SET through_date = EXCLUDED.through_date,
            statement_id = EXCLUDED.statement_id,
            updated_at = now()
        WHERE EXCLUDED.through_date > finance.reconciled_through.through_date
        """,
        _canonical(instrument),
        through,
        statement_id,
    )
