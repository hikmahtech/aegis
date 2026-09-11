"""finance.journal_index — the books' index (spec §5.3).

The hledger journal is the record. This table gives idempotency on the
Gmail message id, receipt<->bank matching (§5.4), dues dedupe and the
admin page. Never treat `amount` here as authoritative; run hledger.
"""

from __future__ import annotations

from collections.abc import Collection
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from aegis.api.models.money import MoneyEvent
from aegis.services import books

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
    declared: Collection[str] = (),
) -> None:
    """Write one index row.

    `declared` is the chart (`books.declared_accounts`), and it is here rather
    than in the parsers because this is the single door EVERY row comes
    through: the deterministic parsers, the LLM extraction that never touches
    `bank_parsers` at all, and the manual `ledger_post`. It buys one canonical
    instrument spelling per account — see `books.canonical_instrument`.
    Omitting it is safe and means "no chart to hand": the instrument is then
    stored exactly as parsed.
    """
    await pool.execute(
        """
        INSERT INTO finance.journal_index
          (message_id, mailbox, entity, kind, direction, amount, currency, payee, payee_key,
           account, channel, instrument, ref, occurred_on, due_on, parser, confidence,
           source_class, journal_file, linked_message_id, todoist_ref)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21)
        ON CONFLICT (message_id) DO UPDATE SET
          mailbox = EXCLUDED.mailbox, entity = EXCLUDED.entity, kind = EXCLUDED.kind,
          direction = EXCLUDED.direction, amount = EXCLUDED.amount, currency = EXCLUDED.currency,
          payee = EXCLUDED.payee, payee_key = EXCLUDED.payee_key, account = EXCLUDED.account,
          channel = EXCLUDED.channel, instrument = EXCLUDED.instrument,
          -- COALESCE, not EXCLUDED, alone among the parsed columns. A
          -- reference is a fact about the payment, not a verdict about it: a
          -- re-index that reads it again reads the same digits, and one that
          -- cannot read them at all (the LLM path never sets `ref`, and
          -- `match_to_event` rebuilds an enriched row from a subset of
          -- columns) must not erase what a better parser already found. A
          -- genuinely different reference still wins — COALESCE only refuses
          -- to overwrite with NULL.
          ref = COALESCE(EXCLUDED.ref, journal_index.ref),
          occurred_on = EXCLUDED.occurred_on, due_on = EXCLUDED.due_on, parser = EXCLUDED.parser,
          confidence = EXCLUDED.confidence, source_class = EXCLUDED.source_class,
          journal_file = COALESCE(EXCLUDED.journal_file, journal_index.journal_file),
          linked_message_id = COALESCE(EXCLUDED.linked_message_id, journal_index.linked_message_id),
          todoist_ref = COALESCE(EXCLUDED.todoist_ref, journal_index.todoist_ref),
          updated_at = now()
        """,
        msgid, mailbox, event.entity, event.kind, event.direction, event.amount, event.currency,
        event.payee, event.payee_key, event.account, event.channel,
        books.canonical_instrument(event.instrument, declared), event.ref,
        event.occurred_on, event.due_on, event.parser, float(event.confidence), event.source_class,
        journal_file, linked, todoist_ref,
    )


async def name_instrument(
    pool: Any, msgids: Collection[str], instrument: str, *, declared: Collection[str] = ()
) -> int:
    """Say which account paid, for rows that did not know (#408).

    A vendor receipt names what you bought, not what paid for it. Measured on
    the live index: of the twelve amount-bearing transaction rows with no
    instrument, NOT ONE of their emails prints a card tail, a masked number, or
    even the words "credit card" — Apple, Amazon Pay, Docker, Groww, GoDaddy
    simply do not say. So there is nothing for a parser to widen onto and
    nothing for the extractor to try harder at; asking either to produce an
    instrument here is asking it to invent one, and a payment posted to the
    wrong account is worse than one posted to `assets:unknown`.

    The bank is the one witness that knows, and by the time this is called it
    has already spoken: `statement_post` promoted these blocks, which rewrites
    an `assets:unknown` posting to the account the statement belongs to. That
    left the journal naming the account and the index still saying NULL — and
    the journal is the record while this table is only its index, so the index
    was simply wrong. This is the index catching up with what the books say.

    `instrument IS NULL` in the predicate is the whole safety argument: a row
    whose mail DID name an account was decided by evidence about that payment,
    and a statement covering the same day and amount must never relabel it.
    """
    if not msgids:
        return 0
    return int(
        await pool.fetchval(
            """
            WITH named AS (
                UPDATE finance.journal_index
                   SET instrument = $2, updated_at = now()
                 WHERE message_id = ANY($1::text[]) AND instrument IS NULL
             RETURNING 1
            )
            SELECT count(*) FROM named
            """,
            list(msgids),
            books.canonical_instrument(instrument, declared),
        )
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


# The extra predicate every "how many dues are still open?" counter carries
# (issue #385). Not `amount > 0`, which is the obvious spelling and the wrong
# one: `>` is NULL for a NULL amount, so it would ALSO drop every due the
# extractor could not size — 20 of the 41 live open dues on 2026-09-05, each a
# real bill of unknown size. `IS DISTINCT FROM 0` says only what is meant.
#
# A ₹0 due is excluded because it can never close: `find_open_due` matches on
# amount and only a ₹0 PAYMENT would settle it, and no such mail arrives. The
# system has already decided such a row is not an obligation — `capture_due`
# refuses to task it, "a zero invoice is not a bill" — so a counter calling it
# an outstanding obligation is the index contradicting that decision, and one
# that can only ever rise.
#
# The row itself stays indexed as `kind='due'`, because that is what the mail
# WAS: the index is a record of what arrived, the events table still shows it,
# and the brief still lists it in its window. Only the count of things you
# still owe leaves it out.
#
# `due_on IS NOT NULL`: a bill with no due date can neither be matched by
# `find_open_due`, which keys on a due-date window, nor be overdue. 17 such rows
# came from the 2026-09-05 backfill, and each could only ever stay "open".
#
# Not ticked off (`TICKED_OFF_SQL`): on 2026-09-11 ten dues counted as open
# although the user had completed their `#bill` tasks, because nothing read a
# completion as "paid". A person's completion is their word that it is handled.
# The clause reads the mirror every time, so un-ticking the task reopens the
# due. `find_open_due` deliberately does not read it: a payment that arrives
# after the tick still links to its due. A task captured through the outbox is
# stored by its `item-…` temp id, which nothing rewrites, so the real id is
# looked up where the drain records it (`todoist_outbox.committed_id`) — the
# same trap the hub projector fell into (#473).
#
# An untasked due stops counting `_UNTASKED_DUE_DAYS` after it falls due. These
# are the autopay notices and twins `capture_due` chose not to task: with no
# task there is nothing for the user to act on, and an autopay almost always
# went through. The row stays indexed.
#
# Every reader selects FROM `finance.journal_index` unaliased, which is what
# the correlated `journal_index.todoist_ref` binds to.
_UNTASKED_DUE_DAYS = 14
# ponytail: the tick lives only in the mirror. A task deleted from Todoist drops
# out of it, and so does every completed task on a Todoist FULL sync: the
# mirror (`aegis_worker/activities/todoist.py`) deletes each id missing from
# the snapshot, which carries live items only. Either way the due counts as
# open again — the fail-open direction — and a full sync would reopen every
# ticked-off bill at once. Production's only full sync so far was the first
# one, on 2026-07-01 (`todoist_sync_state.last_full_sync_at`). The durable
# version is a stamp on the index row, written when the sync mirrors a
# completed bill task.
TICKED_OFF_SQL = (
    "EXISTS (SELECT 1 FROM todoist_tasks tt WHERE tt.is_completed "
    "AND tt.id IN (journal_index.todoist_ref, "
    "(SELECT o.committed_id FROM todoist_outbox o "
    "WHERE o.temp_id = journal_index.todoist_ref AND o.status = 'committed')))"
)
OPEN_DUE_SQL = (
    "(amount IS DISTINCT FROM 0 AND due_on IS NOT NULL "
    f"AND NOT {TICKED_OFF_SQL} "
    f"AND (todoist_ref IS NOT NULL OR due_on >= current_date - {_UNTASKED_DUE_DAYS}))"
)


async def find_open_due(
    pool: Any, payee_key: str, amount: Decimal, currency: str, around: date
) -> dict | None:
    """The open due this payment settles, or None.

    Deliberately NOT conditioned on `todoist_ref IS NOT NULL`. Open means
    unpaid — `linked_message_id IS NULL`. The "dues open" counts
    (`build_month_close`, `/api/admin/money/state`) also apply `OPEN_DUE_SQL`,
    and the brief's dues list `TICKED_OFF_SQL`, so a ₹0, undated, ticked-off or
    stale untasked due drops out of what you still owe. This lookup ignores
    all of that on purpose: a payment that arrives after the user ticked a
    bill off must still link to it. `capture_due`'s three noise guards (a zero invoice, a twin due under
    another payee's name, an autopay notice) all index the due and withhold
    only the Todoist task, so requiring a task ref here made every one of them
    structurally unclosable: nothing else writes `linked_message_id` for a due,
    so the count could only ever rise. Four of seven live bill mails were
    autopay notices, so the first month close would have reported "still open:
    4" for a month in which all of them were paid. The caller skips the task
    completion when there is no ref (`route_money_event`).

    A TASKED row wins the tie, which is why `todoist_ref IS NULL` leads the
    ORDER BY. Admitting untasked dues created a tie that did not exist before:
    one biller mailing the same bill twice gives a tasked row and a
    twin-suppressed one with identical `payee_key`, amount and `due_on`, and
    the payment closes exactly one of them. Picking the untasked row would
    leave the tasked one open with a Todoist task nothing ever completes.
    Postgres returns them in whatever order it likes without this clause — a
    seq scan usually gives insertion order, which happens to be right, but
    that is luck.
    """
    row = await pool.fetchrow(
        """
        SELECT * FROM finance.journal_index
        WHERE kind IN ('due', 'failed') AND linked_message_id IS NULL
          AND payee_key = $1 AND currency = $2
          AND abs(amount - $3::numeric) <= $3::numeric * $4::numeric
          AND due_on BETWEEN $5 AND $6
        ORDER BY (todoist_ref IS NULL), abs(due_on - $7::date) ASC LIMIT 1
        """,
        payee_key, currency, amount, _DUE_TOLERANCE,
        around - timedelta(days=_DUE_DAYS), around + timedelta(days=_DUE_DAYS), around,
    )
    return dict(row) if row else None
