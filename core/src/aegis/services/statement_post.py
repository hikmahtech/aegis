"""Step 5 — put a reconciled statement into the books (spec §9.2, §9.3).

One statement, one write. Everything a statement implies happens inside a
single `books._write` envelope, so `hledger check --strict` and the
closing-balance check below both guard the whole thing and a failure reverts
every row of it rather than leaving a half-reconciled account.

Four things happen to a statement's rows:

* a **matched** row promotes its journal block — `!` becomes `*`, the block
  takes the bank's date, and a `stmt:` tag records which statement proved it;
* an **unmatched** row is money the books never heard about (a cash
  withdrawal, a bank charge, interest) and is posted as a new block, `*`,
  because here the bank IS the source;
* an **ambiguous** row is posted by nobody. §9.4: one of its candidates
  already carries this money, so posting would add a third copy that the
  balance already counts through the candidate. It is recorded and left.
* a **transfer counterpart** is a row the far statement's row already put in
  the books (§8.4). Both statements print the same movement, so posting both
  counts the money twice.

The first three come from the matcher, which compares a row with the journal.
The fourth comes from `statement_transfers`, which compares a row with another
statement's row — a different question, in its own module.

The closing-balance check is the point of the whole lane, and it runs INSIDE
`mutate` — before `_write_sync` reaches `_check_sync` — so raising reverts the
statement. Everything else in this module is arithmetic in service of being
able to run it honestly.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

import structlog

from aegis.api.models.money import MoneyEvent, payee_key
from aegis.services import books, statement_transfers
from aegis.services.statement_match import AMBIGUOUS, RowOutcome
from aegis.services.statement_transfers import REVERSAL, TRANSFER, PairedRow
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

#: The index said a block existed and it does not. Recorded, not raised.
PROMOTION_BLOCK_MISSING = "promotion_block_missing"

#: An earlier run of this statement already wrote this row's block.
ALREADY_POSTED = "already_posted"

#: Skip reasons whose money NEVER REACHED THE JOURNAL. `movement_disagreement`
#: adds these back, because the bank really moved them and a check that left
#: them out would fail by exactly their amount and revert a statement in which
#: nothing was wrong.
#:
#: The set is explicit because the skips are not alike, and two of them are the
#: opposite case:
#:
#: * `ALREADY_POSTED` — an earlier run wrote this row as a `*` block. It is in
#:   the period and in the cleared total.
#: * `TRANSFER` — the FAR statement's row put this money in the books, under
#:   its own msgid, touching this account. Adding it back would make the check
#:   over by exactly that amount and revert a statement that was never wrong.
#:   That is only true once the far block exists and is dated inside the
#:   window the check runs over, though, and the far statement may not have run
#:   yet — so `post_statement` looks in the journal (`_far_block`,
#:   `_in_window`) and adds the row back when the money is genuinely not in
#:   that window's cleared total. That is the one conditional case, and it is
#:   decided at its own site rather than by this set.
_NEVER_WRITTEN = frozenset({AMBIGUOUS, PROMOTION_BLOCK_MISSING})


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
    #: row_id -> the row ↔ row pair that decided this row's counter account
    #: (§8.4, §8.5). Covers rows in `posts` AND rows skipped as `TRANSFER`.
    paired: Mapping[str, PairedRow] = field(default_factory=dict)

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
    #: What each posted block actually holds: `(msgid, event, journal file)`.
    #: The caller needs it to write the `finance.journal_index` row, and only
    #: this loop knows which counter account won — own-account detection, a
    #: proven pair, or the rules. Recomputing it outside would be a second
    #: implementation of that decision, free to drift from this one.
    indexed: list[tuple[str, MoneyEvent, str]] = field(default_factory=list)


def plan(
    statement: ParsedStatement,
    outcomes: dict[str, RowOutcome],
    *,
    entity: str,
    declared: Collection[str] = (),
    peer_rows: Sequence[StatementRow] = (),
) -> PostPlan:
    """What this statement will do, without touching anything.

    `outcomes` is the matcher's verdict per `row_id`. A row with no outcome at
    all is treated as unmatched rather than skipped: the matcher not having
    reached a row is not evidence that the books already hold it, and silently
    dropping it would leave the closing balance short with no line saying why.

    `peer_rows` is every statement row known for OTHER accounts, which is what
    lets §8.4's transfer pairing run. None supplied means no pairs, not a
    degraded answer — see `statement_transfers.find_transfers`.

    A matched row promotes even when it is half of a pair: it HAS a block, and
    that block is where the money already is. The pair still has a say, because
    the far account it proves is what the promotion rewrites `equity:transfers`
    to (§8.4's last sentence).
    """
    paired = dict(statement_transfers.find_reversals(statement.rows, declared, entity=entity))
    for row_id, leg in statement_transfers.find_transfers(
        statement.rows, peer_rows, declared
    ).items():
        # A reversal wins on a row that is somehow in both. It is the tighter
        # key — same instrument, same day, same reference — and both of its
        # legs post, so it can never hide money the way a wrong skip would.
        paired.setdefault(row_id, leg)

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
        leg = paired.get(row.row_id)
        if leg is not None and not leg.posts:
            skipped.append((row.row_id, TRANSFER))
            continue
        posts.append(row)
    return PostPlan(
        statement_id=statement.statement_id,
        instrument=statement.instrument or "",
        entity=entity,
        posts=tuple(posts),
        promotions=tuple(promotions),
        skipped=tuple(skipped),
        paired=paired,
    )


def event_for(
    row: StatementRow,
    entity: str,
    rules: list[dict[str, Any]],
    *,
    declared: Collection[str] = (),
    account: str | None = None,
) -> MoneyEvent:
    """One unmatched statement row as a `MoneyEvent` (spec §9.2).

    `source_class='bank'` and `channel='statement'`: this came from the bank's
    own record, which is what lets `post_statement` write it `*`.

    The counter account is decided in three steps, strongest evidence first.

    **`account`**, when a row ↔ row pair proved the far side. A pair is two
    statements agreeing, which outranks anything read out of one narration.

    **Own-account detection**, next — BEFORE the rules, which §8.4 is explicit
    about and gives the reason for: left to the rules,
    `CREDITCARD PAYMENT XXXX 1313` matches nothing, lands in `expenses:unknown`
    and the card liability drifts by the full bill every month. A hit ends the
    decision; the rules are not consulted at all, because a rule cannot know
    that a counter account is one the owner holds and a merchant pattern that
    happened to fire would take the transfer somewhere plausible and wrong.

    **`books.apply_rules`**, in Python, over the narration. Not an hledger
    `.rules` file — §9.2 has the table of why: the yaml rules already in use
    spell things hledger's `if` conditions cannot express (`(?i)`, `\\d`,
    lookarounds), several would be a hard error and several more would silently
    never match, and hledger cannot choose a file per row, which the entity map
    requires.
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
    if account:
        event.account = account
        return event
    own = statement_transfers.own_account(
        row.narration, declared, exclude=books.instrument_account(row.instrument, declared)
    )
    if own:
        event.account = own
        return event
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
    cleared: Decimal, statement: ParsedStatement, unwritten: Decimal, *, liability: bool
) -> tuple[bool, str]:
    """Whether the books now disagree with the bank over this period.

    **The one place a card's sign is flipped, and it flips BOTH figures.**
    hledger reports a liability negative when you owe; a card statement prints
    what you owe as a positive number, so the two figures coming from the books
    — the cleared movement and the unwritten total, which `signed()` also
    builds in hledger's convention — have to be turned round before they meet
    `expected_movement`. `liability` is keyword-only and has no default so a
    caller cannot forget it, and both flips happen here so nobody can do one
    and not the other: that miss made the check wrong by TWICE any skipped row,
    and a card's own payment row is skipped on every statement.

    Movement over the period, not a balance at its close. A cumulative balance
    would require every earlier period of the account to have been reconciled
    first — so the account's very first statement could never pass, and one gap
    anywhere would break every statement after it. Movement stands alone, which
    means a backfill can be run in any order and a missing month costs only
    that month.

    `unwritten` is the signed total of rows whose money never reached the
    journal — see `_NEVER_WRITTEN` for which skips those are, and which are the
    opposite case. They are real money the bank moved, so leaving them out of
    the comparison would make every statement containing one fail by exactly
    their amount, and reverting on that would be punishing the lane for its own
    correct caution. They are added back, which keeps the check about "did the
    rows that should have landed, land?" rather than "is the account complete?"
    — a different and unanswerable question while any row is ambiguous.

    A statement with no printed balances (a card) has nothing to check against;
    §6.2's arithmetic over its own rows is the proof there.
    """
    expected = expected_movement(statement)
    if expected is None:
        return False, _NO_BALANCE
    if liability:
        cleared, unwritten = -cleared, -unwritten
    delta = cleared + unwritten - expected
    if delta == 0:
        return False, ""
    return True, (
        f"the books disagree with {statement.statement_id}: the bank moved "
        f"{expected}, the books moved {cleared}"
        + (f" with {unwritten} left unwritten" if unwritten else "")
        + f" — difference {delta}"
    )


def _declared_before_write(cfg: books.BooksConfig) -> set[str]:
    """The chart, read before the write envelope opens.

    `plan` needs it to resolve own-account tails, and the plan is what decides
    whether there is anything to write at all. A checkout that does not exist
    yet — the clone happens INSIDE the envelope — has no chart to read, and an
    empty set is the honest answer for it: own-account detection resolves
    nothing and every row falls through to the rules, which is where it would
    have gone anyway. `mutate` re-reads the chart after the pull, and that copy
    is the one the writes are checked against.
    """
    if not (cfg.path / cfg.main).exists():
        return set()
    return books._declared_accounts_sync(cfg)


def _far_block(
    cfg: books.BooksConfig, leg: PairedRow, outcomes: Mapping[str, RowOutcome]
) -> tuple[bool, date | None]:
    """Is the other half of this transfer already a block, and when is it dated?

    Decidable without the database, which is what makes the two statements
    order-independent. Two ways the far row can be in the books, and the second
    is not optional: the far statement may have posted the row under its own
    `stmt/<row_id>` msgid, OR the matcher may have matched it to a block the
    email lane wrote — in which case that block, not a new one, is where the
    money is.

    The date comes back because a transfer's block carries the POSTING side's
    date, and the two banks are a day or two apart — see `_in_window`. `None`
    means the header could not be read, which only a hand-edited block can
    manage.
    """
    wanted = [msgid_for(leg.peer_row_id)]
    peer = outcomes.get(leg.peer_row_id)
    if peer is not None and peer.matched and peer.msgid:
        wanted.append(peer.msgid)
    for path in books.journal_files(cfg):
        text = path.read_text()
        for msgid in wanted:
            span = books.find_block(text, msgid)
            if span is not None:
                header = books._HEADER_RE.match(text[span[0]:span[1]].splitlines()[0])
                return True, date.fromisoformat(header.group(1)) if header else None
    return False, None


def _in_window(when: date | None, window: tuple[date | None, date | None]) -> bool:
    """Does a block dated `when` fall inside the window the check runs over?

    A transfer's block carries the date of the side that POSTED it, and the two
    banks are a day or two apart. Straddle a month boundary — a card bill paid
    on the 31st and credited on the 2nd, which is most card bills — and the
    money is genuinely in the books while being outside THIS period's cleared
    movement. The counterpart then has to be added back like any unwritten row,
    or the check misses by exactly the transfer and reverts a statement in
    which nothing was wrong.

    **It takes the window, not the statement, and that is the point.** The
    question here is only ever "does hledger count this block in the figure the
    check compares?", so it has to be asked of the same dates hledger was
    asked. Asking the printed period instead makes a block in a day the window
    covers and the period does not — which is most of a card's first days —
    counted in the cleared movement AND added back on top of it. The caller
    computes the window once and passes it to both, so the two cannot drift.

    An unreadable header, or a window with no dates, counts as inside. That is
    the direction that fails loudly: if the block really was outside, the check
    disagrees and says so, rather than quietly excusing a missing row.
    """
    start, end = window
    if when is None or start is None or end is None:
        return True
    return start <= when <= end


def _counter_account(block: str) -> str:
    """The account on a block's FIRST posting line — the counter account."""
    for line in block.splitlines():
        if line.startswith(books._INDENT) and not line.startswith(f"{books._INDENT};"):
            match = books._POSTING_RE.match(line)
            return match.group(1) if match else ""
    return ""


def check_window(statement: ParsedStatement) -> tuple[date | None, date | None]:
    """The dates §9.3's cleared-movement check must span for this statement.

    **The rows, not the printed period.** The two are not the same window, and
    on a card they are reliably different: all three real Axis card statements
    run their transactions from `period_start - 2` to `period_end - 1`, because
    the period is billing dates while the rows are posting dates.

    The row span is the arithmetically correct window, and the reason is what
    `expected_movement` measures. It is `closing_balance - opening_balance`,
    and in both layouts the opening figure is the balance IMMEDIATELY BEFORE
    THE FIRST ROW — a bank statement's is re-derived from the first row's
    `balance_after` minus that row, and a card's `Previous Balance` is the
    previous statement's closing. So the movement the bank claims is the rows'
    movement. The empty days at either end of a printed period belong to no row
    of this statement and to no block it writes.

    Both other candidates count somebody else's rows:

    * the printed period alone drops a card's first two days, the movement
      comes up short by exactly those rows, and a correct statement reverts;
    * the union of the two runs to `period_end`, and the NEXT statement's rows
      start the day the period ends — measured on the real Axis card
      statements, whose row spans are contiguous and never overlap (18/05-17/06,
      18/06-17/07, 18/07-17/08) while every union window ends on the next one's
      first row. Post those statements in any order but oldest-first and the
      older one counts a newer row and reverts.

    A statement with no rows falls back to the printed period. `post_statement`
    returns before the check in that case — no rows means no writes — so this
    is what a direct caller gets, and the period is the only window it could
    mean.
    """
    days = [r.occurred_on for r in statement.rows]
    if not days:
        return statement.period_start, statement.period_end
    return min(days), max(days)


async def post_statement(
    statement: ParsedStatement,
    outcomes: dict[str, RowOutcome],
    cfg: books.BooksConfig,
    *,
    entity: str,
    rules: list[dict[str, Any]] | None = None,
    liability: bool = False,
    dry_run: bool = False,
    peer_rows: Sequence[StatementRow] = (),
) -> PostResult:
    """Post and promote one statement, then prove the result against the bank.

    ONE write for the whole statement (spec §9.3). The alternative — a write per
    row — would take the flock and run `check --strict` hundreds of times, leave
    hundreds of commits for one bank statement, and, worst of all, make a failed
    balance check unable to undo the rows that had already landed. Here a
    disagreement reverts everything.

    `liability` says this instrument is a card. It is passed straight to
    `movement_disagreement`, which is the single place the sign is turned
    round — every figure the check compares goes through that one function, so
    there is no way to flip one and forget another.

    `peer_rows` is every statement row known for OTHER accounts — one read of
    `finance.statement_rows` in production. It is what lets §8.4's transfer
    pairing see both halves of one movement; without it a pair is simply not
    found, and the row posts with whatever account its own narration proves.

    `dry_run` computes and returns the plan without writing, which is what a
    person should look at before the first statement of a backfill.
    """
    declared = _declared_before_write(cfg)
    plan_ = plan(
        statement, outcomes, entity=entity, declared=declared, peer_rows=peer_rows
    )
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
    # survive out of it. Decided per skip REASON, from `_NEVER_WRITTEN`, rather
    # than by treating every skip alike: an ambiguous or missing-block row never
    # reached the journal, while an `already_posted` row is in the period as a
    # `*` block from an earlier run and IS in the cleared total — and a transfer
    # counterpart is the same case, once its far side exists.
    unwritten = [Decimal("0")]

    def mutate() -> None:
        declared = books._declared_accounts_sync(cfg)
        acct = books.instrument_account(statement.instrument, declared)
        # ONE window, computed once. hledger is asked for it, and so is every
        # "did the far side of this transfer land in it?" question below — a
        # block counted by one and not the other is counted twice.
        window = check_window(statement)
        by_id = {row.row_id: row for row in statement.rows}
        unwritten[0] = Decimal("0")
        for row_id, reason in plan_.skipped:
            row = by_id.get(row_id)
            if row is None:
                continue
            if reason in _NEVER_WRITTEN:
                unwritten[0] += signed(row)
            elif reason == TRANSFER:
                found, when = _far_block(cfg, plan_.paired[row_id], outcomes)
                if not found or not _in_window(when, window):
                    # Either the far statement has not run yet, or its block
                    # is dated outside this window. Both mean the money is not
                    # in the cleared movement the check compares, and it has to
                    # be told. The row stays skipped either way: posting it is
                    # exactly the double count the pair exists to prevent, and
                    # the far statement will write it.
                    unwritten[0] += signed(row)

        for msgid, row in plan_.promotions:
            for path in books.journal_files(cfg):
                text = path.read_text()
                span = books.find_block(text, msgid)
                if span is None:
                    continue
                rel = str(path.relative_to(cfg.path))
                if rel not in touched:
                    touched.append(rel)
                # §8.4's last sentence. The email lane posts a card bill or an
                # IMPS transfer to `equity:transfers`, because an alert cannot
                # tell which account the money went to. The pair can, so a
                # promotion that has one moves the posting to it — otherwise
                # the card liability is never credited and the clearing account
                # grows by the bill every month. Only `equity:transfers` is
                # rewritten: a block already naming a real account was decided
                # by something with more evidence than this.
                leg = plan_.paired.get(row.row_id)
                far = (
                    leg.account
                    if leg is not None
                    and leg.kind == TRANSFER
                    and _counter_account(text[span[0]:span[1]])
                    == statement_transfers.CLEARING_ACCOUNT
                    else None
                )
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
                        account=far,
                        add_tags={"stmt": plan_.statement_id},
                    )
                )
                result.promoted.append(msgid)
                break
            else:
                # The index said there was a block and there is not. Recorded,
                # not raised: one stale index row must not revert a whole
                # statement, and the row stays visible as a skip.
                result.skipped.append((row.row_id, PROMOTION_BLOCK_MISSING))
                unwritten[0] += signed(row)

        for row in plan_.posts:
            leg = plan_.paired.get(row.row_id)
            if leg is not None and leg.kind == TRANSFER:
                found, when = _far_block(cfg, leg, outcomes)
                if found:
                    # This side is the one §8.4 nominates to post, but the far
                    # side got there first — its own row, or the email block
                    # the matcher gave it. The money is in the books once and
                    # must stay that way, whichever statement the operator ran
                    # first. It is in the cleared total unless the far block
                    # is dated outside this window.
                    result.skipped.append((row.row_id, TRANSFER))
                    if not _in_window(when, window):
                        unwritten[0] += signed(row)
                    continue
            event = event_for(
                row,
                entity,
                rules,
                declared=declared,
                account=leg.account if leg is not None else None,
            )
            msgid = msgid_for(row.row_id)
            rel = books.journal_rel(event.entity, event.occurred_on)
            already = False
            for existing in books.journal_files(cfg):
                if books.find_block(existing.read_text(), msgid):
                    already = True
                    break
            if already:
                result.skipped.append((row.row_id, ALREADY_POSTED))
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
            if leg is not None and leg.kind == REVERSAL:
                # §8.5. `render_transaction` builds its tag line from the
                # event's own fields and has no room for one more, so the tag
                # goes on through the sanctioned rewriter. The peer's row_id is
                # the only thing linking the two legs in the journal.
                path.write_text(
                    books.rewrite_block(
                        path.read_text(), msgid, add_tags={REVERSAL: leg.peer_row_id}
                    )
                )
            result.posted.append(msgid)
            result.indexed.append((msgid, event, rel))

        # §9.3, inside the envelope so a disagreement reverts the statement.
        if not statement.period_start or not statement.period_end:
            result.balance_reason = _NO_BALANCE
            return
        if expected_movement(statement) is None:
            result.balance_reason = _NO_BALANCE
            return
        # Both figures go in as hledger reports them. `movement_disagreement`
        # is the one place a card's sign is turned round, and it turns BOTH.
        raw = books.cleared_movement_sync(cfg, acct, *window)
        disagrees, reason = movement_disagreement(
            raw, statement, unwritten[0], liability=liability
        )
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
