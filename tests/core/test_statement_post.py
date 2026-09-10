"""Step 5 — posting a reconciled statement (spec §9.2, §9.3).

Every test that writes runs against a real git checkout and a real hledger, so
the write envelope, `check --strict` and the revert are the actual ones. That
matters more here than anywhere else in the lane: this is the first code that
writes many blocks at once, and the property being tested — a disagreement
undoes ALL of them — cannot be observed against a fake.
"""

from __future__ import annotations

import shutil
import subprocess
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from aegis.services import books, statement_post
from aegis.services.statement_match import AMBIGUOUS, PASS_WINDOW, RowOutcome
from aegis.services.statement_transfers import REVERSAL, TRANSFER
from aegis.services.statements import ParsedStatement, StatementRow, row_id_for

HAS_HLEDGER = shutil.which("hledger") is not None and shutil.which("git") is not None
pytestmark = pytest.mark.skipif(not HAS_HLEDGER, reason="hledger/git not installed")

ACCOUNTS = """commodity ₹ 1,00,000.00
account assets:bank:hdfc:1225
account assets:bank:axis:9640
account liabilities:card:axis:1313
account assets:unknown
account expenses:unknown
account expenses:groceries
account expenses:fees
account income:unknown
account equity:transfers
"""


def _repo(tmp_path: Path) -> books.BooksConfig:
    root = tmp_path / "books"
    (root / "personal").mkdir(parents=True)
    (root / "rules").mkdir()
    (root / "accounts.journal").write_text(ACCOUNTS)
    (root / "prices.journal").write_text("")
    (root / "personal" / "2026.journal").write_text("; p\n")
    (root / "rules" / "accounts.yaml").write_text("- match: 'ATM'\n  account: expenses:fees\n")
    (root / "main.journal").write_text(
        "include accounts.journal\ninclude prices.journal\ninclude personal/2026.journal\n"
    )
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A"], cwd=root, check=True
    )
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init"],
        cwd=root, check=True,
    )
    return books.BooksConfig(path=root)


def _row(day: int, amount: str, *, direction="out", narration="TEST", balance=None,
         instrument="hdfc-1225", ref=None, occurrence=0) -> StatementRow:
    occurred = date(2026, 7, day)
    money = Decimal(amount)
    after = Decimal(balance) if balance is not None else None
    return StatementRow(
        row_id=row_id_for(
            instrument=instrument, occurred_on=occurred, direction=direction,
            amount=money, balance_after=after, occurrence_index=occurrence,
            narration=narration,
        ),
        instrument=instrument, occurred_on=occurred, narration=narration, ref=ref,
        direction=direction, amount=money, balance_after=after,
        statement_id=f"{instrument}/2026-07-01..2026-07-31", file_sha256="fixture",
    )


def _statement(rows, closing, instrument="hdfc-1225") -> ParsedStatement:
    return ParsedStatement(
        status="ok", instrument=instrument,
        period_start=date(2026, 7, 1), period_end=date(2026, 7, 31),
        opening_balance=Decimal("0"), closing_balance=Decimal(closing) if closing else None,
        rows=tuple(rows), statement_id=f"{instrument}/2026-07-01..2026-07-31",
        file_sha256="fixture",
    )


def _rules(cfg):
    return books.load_rules(cfg) if hasattr(books, "load_rules") else []


# ------------------------------------------------------------------ the plan


def test_the_plan_sorts_every_row_into_post_promote_or_skip():
    rows = [_row(2, "100.00"), _row(3, "200.00"), _row(4, "300.00")]
    outcomes = {
        rows[1].row_id: RowOutcome(
            row_id=rows[1].row_id, statement_id="s", instrument="hdfc-1225",
            occurred_on=date(2026, 7, 3), matched_pass=PASS_WINDOW, msgid="m/1",
        ),
        rows[2].row_id: RowOutcome(
            row_id=rows[2].row_id, statement_id="s", instrument="hdfc-1225",
            occurred_on=date(2026, 7, 4), skip_reason=AMBIGUOUS, candidates=("m/a", "m/b"),
        ),
    }
    p = statement_post.plan(_statement(rows, "0"), outcomes, entity="personal")
    assert [r.row_id for r in p.posts] == [rows[0].row_id]
    assert [m for m, _ in p.promotions] == ["m/1"]
    assert p.skipped == ((rows[2].row_id, AMBIGUOUS),)


def test_a_row_the_matcher_never_reached_is_posted_not_dropped():
    """An absent outcome is not evidence that the books already hold the row.
    Dropping it would leave the closing balance short by exactly that amount,
    with nothing anywhere saying which row went missing."""
    rows = [_row(2, "100.00")]
    p = statement_post.plan(_statement(rows, "0"), {}, entity="personal")
    assert [r.row_id for r in p.posts] == [rows[0].row_id] and p.skipped == ()


def test_an_ambiguous_row_is_never_posted():
    """§9.4. One of its candidates already carries this money, so a post adds a
    third copy that the closing balance counts through the candidate."""
    rows = [_row(2, "100.00")]
    outcomes = {rows[0].row_id: RowOutcome(
        row_id=rows[0].row_id, statement_id="s", instrument="hdfc-1225",
        occurred_on=date(2026, 7, 2), skip_reason=AMBIGUOUS, candidates=("m/a", "m/b"),
    )}
    p = statement_post.plan(_statement(rows, "0"), outcomes, entity="personal")
    assert p.posts == () and p.promotions == () and p.writes == 0


# ------------------------------------------------------------------ the write


@pytest.mark.asyncio
async def test_unmatched_rows_post_cleared_and_the_balance_agrees(tmp_path):
    """The end-to-end shape: rows the books never heard about become blocks,
    written `*` because the bank is the source, and the account then balances
    to the statement's own closing figure."""
    cfg = _repo(tmp_path)
    rows = [
        _row(2, "1000.00", direction="in", narration="SALARY", balance="1000.00"),
        _row(5, "300.00", narration="ATM WDL", balance="700.00"),
    ]
    result = await statement_post.post_statement(
        _statement(rows, "700.00"), {}, cfg, entity="personal",
        rules=books.load_rules_sync(cfg) if hasattr(books, "load_rules_sync") else [],
    )
    assert len(result.posted) == 2 and result.promoted == []
    assert result.balance_checked is True and result.balance_reason == ""

    text = (cfg.path / "personal" / "2026.journal").read_text()
    assert "2026-07-02 * SALARY" in text and "2026-07-05 * ATM WDL" in text
    # One commit for the whole statement, not one per row.
    log = subprocess.run(["git", "log", "--oneline"], cwd=cfg.path,
                         capture_output=True, text=True, check=True).stdout
    assert log.count("\n") == 2 and "reconcile hdfc-1225" in log


@pytest.mark.asyncio
async def test_a_movement_that_disagrees_reverts_the_whole_statement(tmp_path):
    """The property the single write envelope exists for.

    A statement is only worth trusting as a unit: if the books do not end where
    the bank says they end, one of its rows is wrong and there is no way to know
    which. So none of them may stay — including the ones that wrote cleanly.
    """
    cfg = _repo(tmp_path)
    before = (cfg.path / "personal" / "2026.journal").read_text()
    rows = [_row(2, "1000.00", direction="in", narration="SALARY", balance="1000.00")]

    with pytest.raises(books.BooksCheckError, match="the books disagree with"):
        await statement_post.post_statement(
            _statement(rows, "999.00"), {}, cfg, entity="personal",
        )

    assert (cfg.path / "personal" / "2026.journal").read_text() == before
    log = subprocess.run(["git", "log", "--oneline"], cwd=cfg.path,
                         capture_output=True, text=True, check=True).stdout
    assert log.count("\n") == 1, "the failed statement left no commit"


@pytest.mark.asyncio
async def test_one_bad_row_undoes_the_good_ones_in_the_same_statement(tmp_path):
    """The same property, made visible: two rows post fine and the third makes
    the total wrong, so all three are gone. A per-row write would have left the
    first two behind and the account quietly overstated."""
    cfg = _repo(tmp_path)
    rows = [
        _row(2, "1000.00", direction="in", narration="SALARY", balance="1000.00"),
        _row(3, "100.00", narration="SHOP", balance="900.00"),
        _row(4, "200.00", narration="SHOP TWO", balance="700.00"),
    ]
    with pytest.raises(books.BooksCheckError):
        await statement_post.post_statement(
            _statement(rows, "12345.00"), {}, cfg, entity="personal",
        )
    text = (cfg.path / "personal" / "2026.journal").read_text()
    assert "SALARY" not in text and "SHOP" not in text


@pytest.mark.asyncio
async def test_promotion_takes_the_bank_s_date_so_the_check_can_pass(tmp_path):
    """The blocker this step could not ship without.

    The email said 30 June; the bank posted on 1 July. hledger evaluates the
    closing balance over every posting dated on or before 31 July, so keeping
    the email's date leaves the July statement's own transaction outside July —
    the check misses by exactly that amount and reverts a statement in which
    nothing was wrong.
    """
    cfg = _repo(tmp_path)
    from aegis.api.models.money import MoneyEvent
    event = MoneyEvent(
        kind="transaction", direction="out", amount=Decimal("300.00"), currency="INR",
        payee="Corner Store", channel="upi", instrument="hdfc-1225",
        occurred_on=date(2026, 6, 30), entity="personal", account="expenses:groceries",
        source_class="bank",
    )
    await books.post_event(event, "mail/1", cfg)
    assert "2026-06-30 ! Corner Store" in (cfg.path / "personal" / "2026.journal").read_text()

    row = _row(1, "300.00", narration="CORNER STORE", balance="-300.00")
    outcomes = {row.row_id: RowOutcome(
        row_id=row.row_id, statement_id="s", instrument="hdfc-1225",
        occurred_on=date(2026, 7, 1), matched_pass=PASS_WINDOW, msgid="mail/1", delta_days=1,
    )}
    result = await statement_post.post_statement(
        _statement([row], "-300.00"), outcomes, cfg, entity="personal",
    )

    assert result.promoted == ["mail/1"] and result.posted == []
    assert result.balance_checked is True and result.balance_reason == ""
    text = (cfg.path / "personal" / "2026.journal").read_text()
    assert "2026-07-01 * Corner Store" in text, "re-dated to the bank's day and cleared"
    assert "2026-06-30" not in text
    assert "stmt: hdfc-1225/2026-07-01..2026-07-31" in text


@pytest.mark.asyncio
async def test_the_same_statement_twice_writes_nothing_the_second_time(tmp_path):
    """`row_id` is a content hash, so a re-sent statement or an overlapping
    period produces the same msgids and `find_block` refuses them. A statement
    that is imported twice must not double the account."""
    cfg = _repo(tmp_path)
    rows = [_row(2, "1000.00", direction="in", narration="SALARY", balance="1000.00")]
    stmt = _statement(rows, "1000.00")
    first = await statement_post.post_statement(stmt, {}, cfg, entity="personal")
    second = await statement_post.post_statement(stmt, {}, cfg, entity="personal")

    assert len(first.posted) == 1
    assert second.posted == [] and second.skipped == [(rows[0].row_id, "already_posted")]
    text = (cfg.path / "personal" / "2026.journal").read_text()
    assert text.count("SALARY") == 1


@pytest.mark.asyncio
async def test_a_card_statement_has_no_balance_to_check_and_says_so(tmp_path):
    """A card prints no running balance, so §9.3 has nothing to stand on and
    §6.2's arithmetic is the proof instead. The result must SAY that rather than
    reporting a check it never ran — a silent `balance_checked=False` reads
    exactly like a passing check to every counter downstream."""
    cfg = _repo(tmp_path)
    rows = [_row(2, "500.00", narration="AMAZON", instrument="axis-cc-1313")]
    result = await statement_post.post_statement(
        _statement(rows, None, instrument="axis-cc-1313"), {}, cfg, entity="personal",
    )
    assert len(result.posted) == 1
    assert result.balance_checked is False
    assert result.balance_reason == "no_closing_balance"


@pytest.mark.asyncio
async def test_a_dry_run_opens_nothing(tmp_path):
    cfg = _repo(tmp_path)
    before = (cfg.path / "personal" / "2026.journal").read_text()
    rows = [_row(2, "1000.00", direction="in", narration="SALARY")]
    result = await statement_post.post_statement(
        _statement(rows, "1000.00"), {}, cfg, entity="personal", dry_run=True,
    )
    assert len(result.posted) == 1 and result.balance_checked is False
    assert (cfg.path / "personal" / "2026.journal").read_text() == before


@pytest.mark.asyncio
async def test_a_pending_block_dated_into_this_period_cannot_break_it(tmp_path):
    """The failure that made step 5 un-shippable, and the reason the check is
    `--cleared`.

    An email arrives on 31 July for money the bank posts on 2 August. The block
    is written with the EMAIL's date, so it sits inside July — but July's
    statement says nothing about it, because for the bank it happened in August.
    A status-blind check would compare July's books against July's statement,
    find them over by that amount, and revert a statement in which every single
    row was correct.

    `!` means precisely "no statement has vouched for this", so excluding it is
    not a workaround for the dating problem; it is the flag being used for what
    it is. The block stays in the journal, stays pending, and is promoted when
    August is reconciled.
    """
    cfg = _repo(tmp_path)
    from aegis.api.models.money import MoneyEvent
    early = MoneyEvent(
        kind="transaction", direction="out", amount=Decimal("5000.00"), currency="INR",
        payee="August Rent", channel="upi", instrument="hdfc-1225",
        occurred_on=date(2026, 7, 31), entity="personal", account="expenses:unknown",
        source_class="bank",
    )
    await books.post_event(early, "mail/aug", cfg)

    rows = [_row(2, "1000.00", direction="in", narration="SALARY", balance="1000.00")]
    result = await statement_post.post_statement(
        _statement(rows, "1000.00"), {}, cfg, entity="personal",
    )

    assert result.balance_checked is True and result.balance_reason == ""
    text = (cfg.path / "personal" / "2026.journal").read_text()
    assert "2026-07-31 ! August Rent" in text, "still there, still unproven"
    assert "2026-07-02 * SALARY" in text


@pytest.mark.asyncio
async def test_an_ambiguous_row_does_not_fail_the_statement_it_sits_in(tmp_path):
    """§9.4 refuses to post an ambiguous row, and that refusal must not then be
    punished by the check.

    The bank moved that money, so a naive comparison is short by exactly the
    ambiguous amount and reverts the whole statement — the lane failing because
    it was correctly cautious. The rows it deliberately left alone are added
    back, which keeps the check asking "did the rows that should have landed,
    land?" rather than "is this account complete?", which nothing can answer
    while a row is ambiguous.
    """
    cfg = _repo(tmp_path)
    rows = [
        _row(2, "1000.00", direction="in", narration="SALARY", balance="1000.00"),
        _row(3, "250.00", narration="MYSTERY", balance="750.00"),
    ]
    outcomes = {rows[1].row_id: RowOutcome(
        row_id=rows[1].row_id, statement_id="s", instrument="hdfc-1225",
        occurred_on=date(2026, 7, 3), skip_reason=AMBIGUOUS, candidates=("m/a", "m/b"),
    )}
    result = await statement_post.post_statement(
        _statement(rows, "750.00"), outcomes, cfg, entity="personal",
    )

    assert len(result.posted) == 1
    assert (rows[1].row_id, AMBIGUOUS) in result.skipped
    assert result.balance_checked is True and result.balance_reason == ""
    assert "MYSTERY" not in (cfg.path / "personal" / "2026.journal").read_text()


@pytest.mark.asyncio
async def test_the_check_is_movement_so_the_first_statement_of_an_account_passes(tmp_path):
    """A cumulative balance at the closing date needs every earlier period
    reconciled first, so an account's FIRST statement could never pass — its
    opening balance is money the books have never seen. Movement over the
    period needs no history, which is also what lets a backfill run in any
    order and costs only the missing month when one is skipped."""
    cfg = _repo(tmp_path)
    rows = [_row(5, "100.00", narration="SHOP", balance="49900.00")]
    result = await statement_post.post_statement(
        ParsedStatement(
            status="ok", instrument="hdfc-1225",
            period_start=date(2026, 7, 1), period_end=date(2026, 7, 31),
            opening_balance=Decimal("50000.00"),   # never posted to the books
            closing_balance=Decimal("49900.00"),
            rows=tuple(rows), statement_id="hdfc-1225/2026-07-01..2026-07-31",
            file_sha256="fixture",
        ),
        {}, cfg, entity="personal",
    )
    assert result.balance_checked is True and result.balance_reason == ""


@pytest.mark.asyncio
async def test_a_transaction_on_the_closing_day_is_inside_the_period(tmp_path):
    """hledger's `-e` is EXCLUSIVE, so passing the closing date itself drops
    every transaction on it. A statement's last day is rarely empty — a salary
    credit or a month-end fee lands there constantly — and the check would then
    be short by exactly that amount and revert the statement.

    Every other fixture here stops before the period end, so without this the
    off-by-one is invisible: the whole file passes with the bug in place.
    """
    cfg = _repo(tmp_path)
    rows = [
        _row(2, "1000.00", direction="in", narration="SALARY", balance="1000.00"),
        _row(31, "150.00", narration="MONTH END FEE", balance="850.00"),
    ]
    result = await statement_post.post_statement(
        _statement(rows, "850.00"), {}, cfg, entity="personal",
    )
    assert len(result.posted) == 2
    assert result.balance_checked is True and result.balance_reason == ""


# ----------------------------------------------- step 6: transfers, reversals


def _card_pair():
    """One card bill, printed on both statements (§8.4).

    The bank statement names the card — `CREDITCARD PAYMENT XXXX 1313` — and
    the card statement's credit names nothing, which is the whole reason §8.4
    posts a card payment from the bank side. The card also carries a purchase,
    so its run has something to write and the skip is visible beside it.
    """
    bank_row = _row(3, "2500.00", narration="CREDITCARD PAYMENT XXXX 1313", balance="-2500.00")
    card_payment = _row(4, "2500.00", direction="in", instrument="axis-cc-1313",
                        narration="PAYMENT RECEIVED THANK YOU")
    card_purchase = _row(6, "900.00", narration="AMAZON", instrument="axis-cc-1313")
    return (
        _statement([bank_row], "-2500.00"),
        _statement([card_payment, card_purchase], None, instrument="axis-cc-1313"),
    )


def _movement(cfg, account):
    return books.cleared_movement_sync(cfg, account, date(2026, 7, 1), date(2026, 7, 31))


@pytest.mark.asyncio
async def test_a_creditcard_payment_row_credits_the_card_not_expenses_unknown(tmp_path):
    """§8.4, and the half that works with no card statement at all.

    Left to `books.apply_rules`, `CREDITCARD PAYMENT XXXX 1313` matches
    nothing, lands in `expenses:unknown`, and the card liability drifts by the
    full bill every month — the account is never credited, so hledger says the
    owner still owes money that was paid.
    """
    cfg = _repo(tmp_path)
    rows = [_row(3, "2500.00", narration="CREDITCARD PAYMENT XXXX 1313", balance="-2500.00")]
    result = await statement_post.post_statement(
        _statement(rows, "-2500.00"), {}, cfg, entity="personal",
    )
    assert len(result.posted) == 1
    assert result.balance_checked is True and result.balance_reason == ""

    text = (cfg.path / "personal" / "2026.journal").read_text()
    assert "liabilities:card:axis:1313" in text
    assert "expenses:unknown" not in text
    assert _movement(cfg, "liabilities:card:axis:1313") == Decimal("2500.00")


@pytest.mark.asyncio
async def test_the_bank_row_posts_the_transfer_and_the_card_row_skips(tmp_path):
    """Bank statement first, then the card. §8.4's own ordering."""
    cfg = _repo(tmp_path)
    bank, card = _card_pair()

    first = await statement_post.post_statement(
        bank, {}, cfg, entity="personal", peer_rows=card.rows,
    )
    second = await statement_post.post_statement(
        card, {}, cfg, entity="personal", liability=True, peer_rows=bank.rows,
    )

    assert len(first.posted) == 1
    assert (card.rows[0].row_id, TRANSFER) in second.skipped
    assert len(second.posted) == 1, "the purchase, and not the payment"

    text = (cfg.path / "personal" / "2026.journal").read_text()
    assert text.count("₹2500.00") == 1, "one block for one movement"
    assert _movement(cfg, "liabilities:card:axis:1313") == Decimal("1600.00")
    assert _movement(cfg, "assets:bank:hdfc:1225") == Decimal("-2500.00")


@pytest.mark.asyncio
async def test_the_card_statement_first_still_posts_the_transfer_once(tmp_path):
    """The same pair in the other order, and the requirement that matters.

    The operator picks the order, so neither statement may depend on having run
    first. The card row is not the side that posts (§8.4), so running it first
    writes nothing for the payment and leaves the bank statement to do it — and
    the books end in exactly the state the other order produced.
    """
    cfg = _repo(tmp_path)
    bank, card = _card_pair()

    first = await statement_post.post_statement(
        card, {}, cfg, entity="personal", liability=True, peer_rows=bank.rows,
    )
    second = await statement_post.post_statement(
        bank, {}, cfg, entity="personal", peer_rows=card.rows,
    )

    assert (card.rows[0].row_id, TRANSFER) in first.skipped
    assert len(first.posted) == 1, "the purchase, and not the payment"
    assert len(second.posted) == 1

    text = (cfg.path / "personal" / "2026.journal").read_text()
    assert text.count("₹2500.00") == 1, "one block for one movement"
    assert _movement(cfg, "liabilities:card:axis:1313") == Decimal("1600.00")
    assert _movement(cfg, "assets:bank:hdfc:1225") == Decimal("-2500.00")


def _imps_pair():
    """₹1,00,000 out of the Axis current account and into HDFC, printed twice.

    Both narrations name the other account, so §8.4's "the side that names"
    rule ties and the `out` side breaks it — the Axis statement posts. The HDFC
    statement carries a salary row as well, so its closing-balance check has
    something to run against beside the counterpart.
    """
    axis_row = _row(4, "100000.00", instrument="axis-9640", balance="-100000.00",
                    narration="IMPS/612345678905/TO XXXXXXXXXX1225")
    salary = _row(2, "1000.00", direction="in", narration="SALARY", balance="1000.00")
    hdfc_imps = _row(4, "100000.00", direction="in", balance="101000.00",
                     narration="IMPS/612345678905/FROM XXXXXXXXXXX9640")
    return (
        _statement([axis_row], "-100000.00", instrument="axis-9640"),
        _statement([salary, hdfc_imps], "101000.00"),
    )


@pytest.mark.asyncio
async def test_the_balance_check_passes_on_a_statement_holding_a_counterpart(tmp_path):
    """The `unwritten` accounting, which is the subtle half of step 6.

    A `transfer_counterpart` is the opposite of an ambiguous row. The far
    statement put that money in the books, against THIS account, so it is in the
    cleared total already. Adding it back — which is what every other skip
    needs — would make the check over by exactly ₹1,00,000 and revert a
    statement in which nothing was wrong.
    """
    cfg = _repo(tmp_path)
    axis, hdfc = _imps_pair()

    first = await statement_post.post_statement(
        axis, {}, cfg, entity="personal", peer_rows=hdfc.rows,
    )
    second = await statement_post.post_statement(
        hdfc, {}, cfg, entity="personal", peer_rows=axis.rows,
    )

    assert len(first.posted) == 1 and first.balance_reason == ""
    assert (hdfc.rows[1].row_id, TRANSFER) in second.skipped
    assert len(second.posted) == 1, "the salary, and not the transfer"
    assert second.balance_checked is True and second.balance_reason == ""

    text = (cfg.path / "personal" / "2026.journal").read_text()
    assert text.count("₹100000.00") == 1
    assert text.count("income:unknown") == 1, "the salary only; a transfer is not income"
    assert _movement(cfg, "assets:bank:hdfc:1225") == Decimal("101000.00")
    assert _movement(cfg, "assets:bank:axis:9640") == Decimal("-100000.00")


@pytest.mark.asyncio
async def test_adding_a_counterpart_back_would_revert_a_correct_statement(tmp_path, monkeypatch):
    """The falsification of the test above, kept rather than done once by hand.

    Put `transfer_counterpart` in the "money never reached the journal" set —
    the shape every other skip has — and the statement above fails by exactly
    the transfer's amount and reverts. This is what pins `_NEVER_WRITTEN` down:
    without it, treating all skips alike passes every other test in this file.
    """
    cfg = _repo(tmp_path)
    axis, hdfc = _imps_pair()
    await statement_post.post_statement(axis, {}, cfg, entity="personal", peer_rows=hdfc.rows)

    monkeypatch.setattr(
        statement_post, "_NEVER_WRITTEN", statement_post._NEVER_WRITTEN | {TRANSFER}
    )
    with pytest.raises(books.BooksCheckError, match="difference 100000"):
        await statement_post.post_statement(
            hdfc, {}, cfg, entity="personal", peer_rows=axis.rows,
        )


@pytest.mark.asyncio
async def test_a_reversal_pair_nets_to_zero_and_leaves_the_balance_intact(tmp_path):
    """§8.5. A failed UPI is a debit and a same-day re-credit, and the email
    lane records it as `kind='failed'` — not a transaction, so it posts no
    block and both rows arrive here unmatched. Rules alone would file them as
    `expenses:unknown` + `income:unknown`, a pair that sits in the digest
    forever and makes both totals wrong by ₹500."""
    cfg = _repo(tmp_path)
    debit = _row(3, "500.00", ref="612345678906", balance="-500.00",
                 narration="UPI/P2M/612345678906/SHOP")
    credit = _row(3, "500.00", direction="in", ref="612345678906", balance="0.00",
                  narration="UPI/P2M/612345678906/SHOP REVERSAL")
    spend = _row(5, "100.00", narration="SHOP", balance="-100.00")

    result = await statement_post.post_statement(
        _statement([debit, credit, spend], "-100.00"), {}, cfg, entity="personal",
    )

    assert len(result.posted) == 3, "both legs post; a reversal hides nothing"
    assert result.balance_checked is True and result.balance_reason == ""

    text = (cfg.path / "personal" / "2026.journal").read_text()
    assert "income:unknown" not in text
    assert text.count("equity:transfers") == 2
    assert f"{REVERSAL}: {credit.row_id}" in text, "each leg names the other"
    assert _movement(cfg, "equity:transfers") == Decimal("0")
    assert _movement(cfg, "assets:bank:hdfc:1225") == Decimal("-100.00")


@pytest.mark.asyncio
async def test_promotion_moves_an_equity_transfers_block_to_the_card(tmp_path):
    """§8.4's last sentence. The email lane posts a card bill to
    `equity:transfers`, because a debit alert cannot say where the money went.
    The pair can, so promoting such a block moves the posting to the card —
    otherwise the liability is never credited and the clearing account grows by
    the bill every month."""
    cfg = _repo(tmp_path)
    from aegis.api.models.money import MoneyEvent
    await books.post_event(
        MoneyEvent(
            kind="transaction", direction="out", amount=Decimal("2500.00"), currency="INR",
            payee="Card Bill", channel="imps", instrument="hdfc-1225",
            occurred_on=date(2026, 7, 2), entity="personal", account="equity:transfers",
            source_class="bank",
        ),
        "mail/card", cfg,
    )
    bank_row = _row(3, "2500.00", narration="CREDITCARD PAYMENT XXXX 1313", balance="-2500.00")
    card_row = _row(4, "2500.00", direction="in", instrument="axis-cc-1313",
                    narration="PAYMENT RECEIVED THANK YOU")
    outcomes = {bank_row.row_id: RowOutcome(
        row_id=bank_row.row_id, statement_id="s", instrument="hdfc-1225",
        occurred_on=date(2026, 7, 3), matched_pass=PASS_WINDOW, msgid="mail/card", delta_days=1,
    )}

    result = await statement_post.post_statement(
        _statement([bank_row], "-2500.00"), outcomes, cfg,
        entity="personal", peer_rows=[card_row],
    )

    assert result.promoted == ["mail/card"] and result.posted == []
    text = (cfg.path / "personal" / "2026.journal").read_text()
    assert "2026-07-03 * Card Bill" in text
    assert "liabilities:card:axis:1313" in text
    assert "equity:transfers" not in text
    assert _movement(cfg, "liabilities:card:axis:1313") == Decimal("2500.00")


@pytest.mark.asyncio
async def test_a_promoted_block_is_what_the_far_statement_sees(tmp_path):
    """The other way a transfer reaches the books: not a `stmt/` block, but an
    email block the matcher gave to the far row and the far statement promoted.
    A counterpart check that only looked for `stmt/<row_id>` would miss it,
    post the money a second time, and fail this statement's own check."""
    cfg = _repo(tmp_path)
    from aegis.api.models.money import MoneyEvent
    await books.post_event(
        MoneyEvent(
            kind="transaction", direction="out", amount=Decimal("100000.00"), currency="INR",
            payee="IMPS Transfer", channel="imps", instrument="axis-9640",
            occurred_on=date(2026, 7, 3), entity="personal", account="equity:transfers",
            source_class="bank",
        ),
        "mail/imps", cfg,
    )
    axis, hdfc = _imps_pair()
    outcomes = {axis.rows[0].row_id: RowOutcome(
        row_id=axis.rows[0].row_id, statement_id="s", instrument="axis-9640",
        occurred_on=date(2026, 7, 4), matched_pass=PASS_WINDOW, msgid="mail/imps", delta_days=1,
    )}

    first = await statement_post.post_statement(
        axis, outcomes, cfg, entity="personal", peer_rows=hdfc.rows,
    )
    second = await statement_post.post_statement(
        hdfc, outcomes, cfg, entity="personal", peer_rows=axis.rows,
    )

    assert first.promoted == ["mail/imps"] and first.posted == []
    assert (hdfc.rows[1].row_id, TRANSFER) in second.skipped
    assert second.balance_checked is True and second.balance_reason == ""

    text = (cfg.path / "personal" / "2026.journal").read_text()
    assert text.count("₹100000.00") == 1
    assert "equity:transfers" not in text, "the pair proved the far account"
    assert _movement(cfg, "assets:bank:hdfc:1225") == Decimal("101000.00")
    assert _movement(cfg, "assets:bank:axis:9640") == Decimal("-100000.00")


@pytest.mark.asyncio
async def test_a_third_party_row_carrying_a_declared_tail_still_goes_to_the_rules(tmp_path):
    """The guard that keeps own-account detection from being the §6.1 bug.

    `1234196405` is an invoice number that contains `9640`, a declared account
    tail. Resolving it would move a supplier payment into a bank account the
    owner holds — and the closing-balance check would still pass, because both
    accounts are real and the block still balances. Nothing downstream would
    ever ask again.
    """
    cfg = _repo(tmp_path)
    rows = [_row(4, "1200.00", balance="-1200.00",
                 narration="NEFT/AXISP00123456/INVOICE 1234196405 SPECIMEN SUPPLIES")]
    result = await statement_post.post_statement(
        _statement(rows, "-1200.00"), {}, cfg, entity="personal",
    )
    assert len(result.posted) == 1
    text = (cfg.path / "personal" / "2026.journal").read_text()
    assert "expenses:unknown" in text
    assert "assets:bank:axis:9640" not in text


@pytest.mark.asyncio
async def test_a_far_statement_arriving_later_does_not_post_the_money_twice(tmp_path):
    """The strongest form of the ordering requirement.

    The HDFC statement is posted before the Axis one has been parsed at all, so
    there are no peer rows and no pair — own-account detection alone puts the
    transfer in the books, with both accounts right. When the Axis statement
    does arrive it is the side §8.4 nominates to post, and it must still not
    post: the money is already there, under the other row's msgid.
    """
    cfg = _repo(tmp_path)
    axis, hdfc = _imps_pair()

    blind = await statement_post.post_statement(hdfc, {}, cfg, entity="personal")
    assert len(blind.posted) == 2, "no peers, no pair — the credit posts on its own"
    assert blind.balance_checked is True and blind.balance_reason == ""

    later = await statement_post.post_statement(
        axis, {}, cfg, entity="personal", peer_rows=hdfc.rows,
    )
    assert later.posted == []
    assert (axis.rows[0].row_id, TRANSFER) in later.skipped
    assert later.balance_checked is True and later.balance_reason == ""

    text = (cfg.path / "personal" / "2026.journal").read_text()
    assert text.count("₹100000.00") == 1
    assert _movement(cfg, "assets:bank:axis:9640") == Decimal("-100000.00")
    assert _movement(cfg, "assets:bank:hdfc:1225") == Decimal("101000.00")


@pytest.mark.asyncio
async def test_promotion_leaves_an_account_that_is_not_the_clearing_one_alone(tmp_path):
    """Only `equity:transfers` is rewritten.

    A block already naming a real account was decided by something with more
    evidence than a tail in a narration — a rule, or the owner's own
    `ledger_reclassify`. A pair is not licence to overwrite that.
    """
    cfg = _repo(tmp_path)
    from aegis.api.models.money import MoneyEvent
    await books.post_event(
        MoneyEvent(
            kind="transaction", direction="out", amount=Decimal("2500.00"), currency="INR",
            payee="Card Bill", channel="imps", instrument="hdfc-1225",
            occurred_on=date(2026, 7, 2), entity="personal", account="expenses:groceries",
            source_class="bank",
        ),
        "mail/card", cfg,
    )
    bank_row = _row(3, "2500.00", narration="CREDITCARD PAYMENT XXXX 1313", balance="-2500.00")
    card_row = _row(4, "2500.00", direction="in", instrument="axis-cc-1313",
                    narration="PAYMENT RECEIVED THANK YOU")
    outcomes = {bank_row.row_id: RowOutcome(
        row_id=bank_row.row_id, statement_id="s", instrument="hdfc-1225",
        occurred_on=date(2026, 7, 3), matched_pass=PASS_WINDOW, msgid="mail/card", delta_days=1,
    )}

    result = await statement_post.post_statement(
        _statement([bank_row], "-2500.00"), outcomes, cfg,
        entity="personal", peer_rows=[card_row],
    )

    assert result.promoted == ["mail/card"]
    text = (cfg.path / "personal" / "2026.journal").read_text()
    assert "expenses:groceries" in text
    assert "liabilities:card:axis:1313" not in text
