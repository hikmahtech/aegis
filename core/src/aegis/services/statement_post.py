"""Step 5 — put a reconciled statement into the books (spec §9.2, §9.3).

One statement, one write. Everything a statement implies happens inside a
single `books._write` envelope, so `hledger check --strict` and the
closing-balance check below both guard the whole thing and a failure reverts
every row of it rather than leaving a half-reconciled account.

Three things happen to a statement's rows, decided entirely by the matcher:

* a **matched** row promotes its journal block — `!` becomes `*`, the block
  takes the bank's date, and a `stmt:` tag records which statement proved it;
* an **unmatched** row is money the books never heard about (a cash
  withdrawal, a bank charge, interest) and is posted as a new block, `*`,
  because here the bank IS the source;
* an **ambiguous** row is posted by nobody. §9.4: one of its candidates
  already carries this money, so posting would add a third copy that the
  balance already counts through the candidate. It is recorded and left.

The closing-balance check is the point of the whole lane, and it runs INSIDE
`mutate` — before `_write_sync` reaches `_check_sync` — so raising reverts the
statement. Everything else in this module is arithmetic in service of being
able to run it honestly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import structlog

from aegis.api.models.money import MoneyEvent, payee_key
from aegis.services import books
from aegis.services.statement_match import AMBIGUOUS, RowOutcome
from aegis.services.statements import ParsedStatement, StatementRow

logger = structlog.get_logger()

#: The msgid namespace for a block this module writes. `row_id` is a content
#: hash over (instrument, date, direction, amount, balance, occurrence), so the
#: same row from a re-sent statement or an overlapping period is the same
#: msgid, and `post_event`'s repo-wide idempotency skips it.
MSGID_PREFIX = "stmt"

#: A card statement prints no running balance, so §9.3's balance check has
#: nothing to stand on and §6.2's arithmetic is the proof instead.
_NO_BALANCE = "no_closing_balance"


def msgid_for(row_id: str) -> str:
    return f"{MSGID_PREFIX}/{row_id}"


@dataclass(frozen=True)
class PostPlan:
    """What a statement will do to the books, computed before anything writes.

    Separated from the write so it can be shown to a person, counted, and
    tested without a repo. `posts` and `promotions` are the two write kinds;
    `skipped` is every row that will be left alone, with the reason.
    """

    statement_id: str
    instrument: str
    entity: str
    posts: tuple[StatementRow, ...] = ()
    promotions: tuple[tuple[str, StatementRow], ...] = ()   # (msgid, the row that proved it)
    skipped: tuple[tuple[str, str], ...] = ()               # (row_id, reason)

    @property
    def writes(self) -> int:
        return len(self.posts) + len(self.promotions)


@dataclass
class PostResult:
    statement_id: str
    posted: list[str] = field(default_factory=list)
    promoted: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    journal_files: list[str] = field(default_factory=list)
    balance_checked: bool = False
    balance_reason: str = ""


def plan(
    statement: ParsedStatement,
    outcomes: dict[str, RowOutcome],
    *,
    entity: str,
) -> PostPlan:
    """What this statement will do, without touching anything.

    `outcomes` is the matcher's verdict per `row_id`. A row with no outcome at
    all is treated as unmatched rather than skipped: the matcher not having
    reached a row is not evidence that the books already hold it, and silently
    dropping it would leave the closing balance short with no line saying why.
    """
    posts: list[StatementRow] = []
    promotions: list[tuple[str, StatementRow]] = []
    skipped: list[tuple[str, str]] = []
    for row in statement.rows:
        outcome = outcomes.get(row.row_id)
        if outcome is not None and outcome.skip_reason == AMBIGUOUS:
            # §9.4. The money is already in the books through one of the
            # candidates; a third copy would be counted by the balance.
            skipped.append((row.row_id, AMBIGUOUS))
            continue
        if outcome is not None and outcome.matched and outcome.msgid:
            promotions.append((outcome.msgid, row))
            continue
        posts.append(row)
    return PostPlan(
        statement_id=statement.statement_id,
        instrument=statement.instrument or "",
        entity=entity,
        posts=tuple(posts),
        promotions=tuple(promotions),
        skipped=tuple(skipped),
    )


def event_for(row: StatementRow, entity: str, rules: list[dict[str, Any]]) -> MoneyEvent:
    """One unmatched statement row as a `MoneyEvent` (spec §9.2).

    `source_class='bank'` and `channel='statement'`: this came from the bank's
    own record, which is what lets `post_statement` write it `*`.

    The account comes from `books.apply_rules` in Python, over the narration.
    Not an hledger `.rules` file — §9.2 has the table of why: the yaml rules
    already in use spell things hledger's `if` conditions cannot express
    (`(?i)`, `\\d`, lookarounds), several would be a hard error and several
    more would silently never match, and hledger cannot choose a file per row,
    which the entity map requires.
    """
    event = MoneyEvent(
        kind="transaction",
        entity=entity,  # type: ignore[arg-type]
        direction=row.direction,  # type: ignore[arg-type]
        amount=row.amount,
        currency="INR",
        payee=row.narration,
        channel="statement",
        instrument=row.instrument,
        ref=row.ref,
        occurred_on=row.occurred_on,
        parser="statement",
        source_class="bank",
        confidence=1.0,
    )
    event.payee_key = payee_key(event.payee)
    rule = books.apply_rules(rules, "", event.payee, direction=event.direction)
    if rule:
        if rule.get("payee"):
            event.payee = str(rule["payee"])
            event.payee_key = payee_key(event.payee)
        if rule.get("account"):
            event.account = str(rule["account"])
        if rule.get("entity") in ("personal", "hikmah"):
            event.entity = rule["entity"]
    return event


def signed(row: StatementRow) -> Decimal:
    """One row's effect on the account, in the direction a balance moves.

    Money in raises an asset, money out lowers it — the same convention the
    statement's own running balance uses, which is what lets the two be
    compared without a translation table.
    """
    return row.amount if row.direction == "in" else -row.amount


def expected_movement(statement: ParsedStatement) -> Decimal | None:
    """What the bank says this account did over the period, or None when the
    statement printed no balances to say it with."""
    if statement.closing_balance is None or statement.opening_balance is None:
        return None
    return statement.closing_balance - statement.opening_balance


def movement_disagreement(
    cleared: Decimal, statement: ParsedStatement, unwritten: Decimal
) -> tuple[bool, str]:
    """Whether the books now disagree with the bank over this period.

    Movement over the period, not a balance at its close. A cumulative balance
    would require every earlier period of the account to have been reconciled
    first — so the account's very first statement could never pass, and one gap
    anywhere would break every statement after it. Movement stands alone, which
    means a backfill can be run in any order and a missing month costs only
    that month.

    `unwritten` is the signed total of rows this statement deliberately did NOT
    write — today only the ambiguous ones (§9.4). They are real money the bank
    moved, so leaving them out of the comparison would make every statement
    containing one fail by exactly their amount, and reverting on that would be
    punishing the lane for its own correct caution. They are added back, which
    keeps the check about "did the rows that should have landed, land?" rather
    than "is the account complete?" — a different and unanswerable question
    while any row is ambiguous.

    A statement with no printed balances (a card) has nothing to check against;
    §6.2's arithmetic over its own rows is the proof there.
    """
    expected = expected_movement(statement)
    if expected is None:
        return False, _NO_BALANCE
    delta = cleared + unwritten - expected
    if delta == 0:
        return False, ""
    return True, (
        f"the books disagree with {statement.statement_id}: the bank moved "
        f"{expected}, the books moved {cleared}"
        + (f" with {unwritten} left unwritten" if unwritten else "")
        + f" — difference {delta}"
    )


async def post_statement(
    statement: ParsedStatement,
    outcomes: dict[str, RowOutcome],
    cfg: books.BooksConfig,
    *,
    entity: str,
    rules: list[dict[str, Any]] | None = None,
    liability: bool = False,
    dry_run: bool = False,
) -> PostResult:
    """Post and promote one statement, then prove the result against the bank.

    ONE write for the whole statement (spec §9.3). The alternative — a write per
    row — would take the flock and run `check --strict` hundreds of times, leave
    hundreds of commits for one bank statement, and, worst of all, make a failed
    balance check unable to undo the rows that had already landed. Here a
    disagreement reverts everything.

    `liability` says this instrument is a card. hledger reports a card negative
    when you owe; the statement prints what you owe as a positive number, so one
    side has to be negated and it is done here, once, rather than in
    `balance_disagreement` where every reader would have to remember it.

    `dry_run` computes and returns the plan without opening the repo, which is
    what a person should look at before the first statement of a backfill.
    """
    plan_ = plan(statement, outcomes, entity=entity)
    result = PostResult(
        statement_id=plan_.statement_id, skipped=list(plan_.skipped)
    )
    if dry_run:
        result.posted = [msgid_for(r.row_id) for r in plan_.posts]
        result.promoted = [m for m, _ in plan_.promotions]
        return result
    if not plan_.writes:
        logger.info("statement_post_nothing_to_do", statement=plan_.statement_id)
        return result

    rules = rules or []
    # Seeded with main.journal and passed BY REFERENCE: `_write_sync` reads this
    # list AFTER `mutate` returns, so building a new list here (`touched + [...]`)
    # would hand it the empty snapshot taken before any path was recorded, and
    # a failed check would revert nothing.
    touched: list[str] = [cfg.main]

    # Signed money this statement moved that is NOT in the cleared total, so
    # the check can tell "a row did not land" from "a row was deliberately left
    # alone". A one-element list because `mutate` is a closure and this has to
    # survive out of it. Filled at each skip site rather than reconstructed
    # afterwards from `result.skipped`, because the two skip reasons differ in
    # exactly this respect and a reconstruction has to re-derive which is which:
    # an ambiguous or missing-block row never reached the journal, while an
    # `already_posted` row is in the period as a `*` block from an earlier run
    # and IS in the cleared total.
    unwritten = [Decimal("0")]

    def mutate() -> None:
        declared = books._declared_accounts_sync(cfg)
        acct = books.instrument_account(statement.instrument, declared)
        unwritten[0] = sum(
            (signed(r) for r in statement.rows
             if any(rid == r.row_id for rid, _ in plan_.skipped)),
            Decimal("0"),
        )

        for msgid, row in plan_.promotions:
            for path in books.journal_files(cfg):
                text = path.read_text()
                if books.find_block(text, msgid) is None:
                    continue
                rel = str(path.relative_to(cfg.path))
                if rel not in touched:
                    touched.append(rel)
                # The bank's date, not the email's: see `rewrite_block`'s `on`.
                # Without it a block dated three days off the bank's lands on
                # the wrong side of the closing date and the check below fails
                # for every row of the statement.
                path.write_text(
                    books.rewrite_block(
                        text,
                        msgid,
                        status="*",
                        on=row.occurred_on,
                        add_tags={"stmt": plan_.statement_id},
                    )
                )
                result.promoted.append(msgid)
                break
            else:
                # The index said there was a block and there is not. Recorded,
                # not raised: one stale index row must not revert a whole
                # statement, and the row stays visible as a skip.
                result.skipped.append((row.row_id, "promotion_block_missing"))
                unwritten[0] += signed(row)

        for row in plan_.posts:
            event = event_for(row, entity, rules)
            msgid = msgid_for(row.row_id)
            rel = books.journal_rel(event.entity, event.occurred_on)
            already = False
            for existing in books.journal_files(cfg):
                if books.find_block(existing.read_text(), msgid):
                    already = True
                    break
            if already:
                result.skipped.append((row.row_id, "already_posted"))
                continue
            counter = event.account or books.account_for(
                event.category, event.direction, event.entity
            )
            if declared and counter not in declared:
                counter = books.UNKNOWN[
                    "hikmah" if event.entity == "hikmah" else "personal"
                ]["in" if event.direction == "in" else "out"]
            if rel not in touched:
                touched.append(rel)
            path = books._ensure_journal_file(cfg, rel)
            path.write_text(
                books.append_block(
                    path.read_text(),
                    # `*`: this row IS the bank's record, so there is nothing
                    # left to prove and nothing will ever promote it.
                    books.render_transaction(
                        event,
                        counter,
                        books.instrument_account(event.instrument, declared),
                        msgid,
                        "*",
                    ),
                )
            )
            result.posted.append(msgid)

        # §9.3, inside the envelope so a disagreement reverts the statement.
        if not statement.period_start or not statement.period_end:
            result.balance_reason = _NO_BALANCE
            return
        if expected_movement(statement) is None:
            result.balance_reason = _NO_BALANCE
            return
        raw = books.cleared_movement_sync(
            cfg, acct, statement.period_start, statement.period_end
        )
        # A card is a liability: hledger reports it negative when you owe,
        # while the statement prints what you owe as a positive number. One
        # side has to be negated and it happens here, once, rather than in
        # every function that reads a figure.
        cleared = -raw if liability else raw
        disagrees, reason = movement_disagreement(cleared, statement, unwritten[0])
        result.balance_checked = True
        if disagrees:
            raise books.BooksCheckError(reason)
        result.balance_reason = reason

    summary = (
        f"reconcile {plan_.statement_id}: "
        f"{len(plan_.posts)} posted, {len(plan_.promotions)} promoted"
    )
    await books._write(cfg, summary, mutate, touched)
    result.journal_files = list(touched)
    return result
