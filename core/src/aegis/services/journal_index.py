"""finance.journal_index — the books' index (spec §5.3).

The hledger journal is the record. This table gives idempotency on the
Gmail message id, receipt<->bank matching (§5.4), dues dedupe and the
admin page. Never treat `amount` here as authoritative; run hledger.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from aegis.api.models.money import MoneyEvent

_MATCH_DAYS = 3
_DUE_DAYS = 45
_DUE_TOLERANCE = Decimal("0.01")


def msgid_for(mailbox: str, message_id: str) -> str:
    return f"{mailbox}/{message_id}"


async def upsert(
    pool: Any,
    msgid: str,
    mailbox: str,
    event: MoneyEvent,
    *,
    journal_file: str | None = None,
    linked: str | None = None,
    todoist_ref: str | None = None,
) -> None:
    await pool.execute(
        """
        INSERT INTO finance.journal_index
          (message_id, mailbox, entity, kind, direction, amount, currency, payee, payee_key,
           account, channel, instrument, occurred_on, due_on, parser, confidence, source_class,
           journal_file, linked_message_id, todoist_ref)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20)
        ON CONFLICT (message_id) DO UPDATE SET
          mailbox = EXCLUDED.mailbox, entity = EXCLUDED.entity, kind = EXCLUDED.kind,
          direction = EXCLUDED.direction, amount = EXCLUDED.amount, currency = EXCLUDED.currency,
          payee = EXCLUDED.payee, payee_key = EXCLUDED.payee_key, account = EXCLUDED.account,
          channel = EXCLUDED.channel, instrument = EXCLUDED.instrument,
          occurred_on = EXCLUDED.occurred_on, due_on = EXCLUDED.due_on, parser = EXCLUDED.parser,
          confidence = EXCLUDED.confidence, source_class = EXCLUDED.source_class,
          journal_file = COALESCE(EXCLUDED.journal_file, journal_index.journal_file),
          linked_message_id = COALESCE(EXCLUDED.linked_message_id, journal_index.linked_message_id),
          todoist_ref = COALESCE(EXCLUDED.todoist_ref, journal_index.todoist_ref),
          updated_at = now()
        """,
        msgid, mailbox, event.entity, event.kind, event.direction, event.amount, event.currency,
        event.payee, event.payee_key, event.account, event.channel, event.instrument,
        event.occurred_on, event.due_on, event.parser, float(event.confidence), event.source_class,
        journal_file, linked, todoist_ref,
    )


async def get(pool: Any, msgid: str) -> dict | None:
    row = await pool.fetchrow("SELECT * FROM finance.journal_index WHERE message_id = $1", msgid)
    return dict(row) if row else None


async def find_match(pool: Any, event: MoneyEvent, exclude_msgid: str) -> dict | None:
    """The bank alert / vendor receipt pair for one payment (spec §5.4), or None.

    Two predicates beyond the obvious class/amount/currency/date matching, both
    of which the caller depends on:

    `entity = $8` — matching is otherwise entity-blind, so two unrelated ₹100
    UPI payments three days apart would link, and a hikmah receipt could link
    to a personal bank alert, after which the enrichment writes an
    `expenses:hikmah:*` account into `personal/2026.journal`. A genuine
    cross-entity payment is rare and is better left as two postings than
    silently merged into the wrong book.

    `journal_file IS NOT NULL` — the caller enriches a match by rewriting its
    journal block, so a row without one is not a candidate. Rows indexed while
    the books were disabled have no block; returning one made
    `books.rewrite_event` raise `BooksError("no journal block carries msgid …")`,
    which the caller does not catch, and the activity then retried forever.
    """
    opposite = {"bank": "receipt", "receipt": "bank"}.get(event.source_class)
    if opposite is None or event.amount is None or event.occurred_on is None or not event.currency:
        return None
    row = await pool.fetchrow(
        """
        SELECT * FROM finance.journal_index
        WHERE kind = 'transaction' AND source_class = $1 AND currency = $2 AND amount = $3
          AND entity = $8 AND occurred_on BETWEEN $4 AND $5
          AND linked_message_id IS NULL AND journal_file IS NOT NULL AND message_id <> $6
        ORDER BY abs(occurred_on - $7::date) ASC, created_at ASC
        LIMIT 1
        """,
        opposite, event.currency, event.amount,
        event.occurred_on - timedelta(days=_MATCH_DAYS),
        event.occurred_on + timedelta(days=_MATCH_DAYS),
        exclude_msgid, event.occurred_on, event.entity,
    )
    return dict(row) if row else None


async def link(pool: Any, a: str, b: str) -> None:
    await pool.execute(
        "UPDATE finance.journal_index "
        "SET linked_message_id = CASE message_id WHEN $1 THEN $2 ELSE $1 END, updated_at = now() "
        "WHERE message_id IN ($1, $2)",
        a, b,
    )


async def mark_due_paid(pool: Any, due_msgid: str, payment_msgid: str) -> None:
    """Close a due by pointing it at the payment — ONE row, unlike `link`.

    `link` writes both sides, which is wrong here: the paying transaction
    usually has a counterpart of its own (a card bill payment arrives as both
    a bank alert and a receipt), and overwriting its `linked_message_id` with
    the due leaves the counterpart pointing back at a row that no longer
    points at it, and the caller's returned `linked` disagreeing with the row.
    The due → payment direction is all `find_open_due` needs to stop
    re-offering a due that has been paid.
    """
    await pool.execute(
        "UPDATE finance.journal_index SET linked_message_id = $2, updated_at = now() "
        "WHERE message_id = $1",
        due_msgid, payment_msgid,
    )


async def find_open_due(
    pool: Any, payee_key: str, amount: Decimal, currency: str, around: date
) -> dict | None:
    row = await pool.fetchrow(
        """
        SELECT * FROM finance.journal_index
        WHERE kind IN ('due', 'failed') AND todoist_ref IS NOT NULL AND linked_message_id IS NULL
          AND payee_key = $1 AND currency = $2
          AND abs(amount - $3::numeric) <= $3::numeric * $4::numeric
          AND due_on BETWEEN $5 AND $6
        ORDER BY abs(due_on - $7::date) ASC LIMIT 1
        """,
        payee_key, currency, amount, _DUE_TOLERANCE,
        around - timedelta(days=_DUE_DAYS), around + timedelta(days=_DUE_DAYS), around,
    )
    return dict(row) if row else None
