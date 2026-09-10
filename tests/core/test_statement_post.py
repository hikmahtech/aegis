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
commodity $ 1000.00
commodity £ 1000.00
account assets:bank:hdfc:1225
account assets:bank:axis:9640
account liabilities:card:axis:1313
account assets:unknown
account expenses:unknown
account expenses:groceries
account expenses:media
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


@pytest.mark.asyncio
async def test_a_transfer_straddling_a_month_boundary_does_not_break_either_check(tmp_path):
    """The block carries the POSTING side's date, and the two banks are a day
    or two apart.

    Axis pays on 31 July; HDFC credits on 2 August. The block is dated 31 July,
    so the money is in the books and NOT in August's cleared movement — and
    August's statement is the one holding the counterpart. Treating "in the
    books" as "in this period" leaves August short by exactly the transfer and
    reverts a statement in which nothing was wrong. Card bills paid at month
    end make this the common shape, not the exotic one.
    """
    cfg = _repo(tmp_path)
    july_row = _row(31, "100000.00", instrument="axis-9640", balance="-100000.00",
                    narration="IMPS/612345678907/TO XXXXXXXXXX1225")
    july = _statement([july_row], "-100000.00", instrument="axis-9640")

    august_credit = StatementRow(
        row_id=row_id_for(
            instrument="hdfc-1225", occurred_on=date(2026, 8, 2), direction="in",
            amount=Decimal("100000.00"), balance_after=Decimal("100000.00"),
            occurrence_index=0, narration="IMPS/612345678907/FROM XXXXXXXXXXX9640",
        ),
        instrument="hdfc-1225", occurred_on=date(2026, 8, 2),
        narration="IMPS/612345678907/FROM XXXXXXXXXXX9640", ref=None, direction="in",
        amount=Decimal("100000.00"), balance_after=Decimal("100000.00"),
        statement_id="hdfc-1225/2026-08-01..2026-08-31", file_sha256="fixture",
    )
    august_salary = StatementRow(
        row_id=row_id_for(
            instrument="hdfc-1225", occurred_on=date(2026, 8, 5), direction="in",
            amount=Decimal("1000.00"), balance_after=Decimal("101000.00"),
            occurrence_index=0, narration="SALARY",
        ),
        instrument="hdfc-1225", occurred_on=date(2026, 8, 5), narration="SALARY", ref=None,
        direction="in", amount=Decimal("1000.00"), balance_after=Decimal("101000.00"),
        statement_id="hdfc-1225/2026-08-01..2026-08-31", file_sha256="fixture",
    )
    august = ParsedStatement(
        status="ok", instrument="hdfc-1225",
        period_start=date(2026, 8, 1), period_end=date(2026, 8, 31),
        opening_balance=Decimal("0"), closing_balance=Decimal("101000.00"),
        rows=(august_credit, august_salary),
        statement_id="hdfc-1225/2026-08-01..2026-08-31", file_sha256="fixture",
    )

    first = await statement_post.post_statement(
        july, {}, cfg, entity="personal", peer_rows=august.rows,
    )
    second = await statement_post.post_statement(
        august, {}, cfg, entity="personal", peer_rows=july.rows,
    )

    assert len(first.posted) == 1 and first.balance_reason == ""
    assert (august_credit.row_id, TRANSFER) in second.skipped
    assert second.balance_checked is True and second.balance_reason == ""

    text = (cfg.path / "personal" / "2026.journal").read_text()
    assert text.count("₹100000.00") == 1
    assert "2026-07-31 * IMPS/612345678907/TO XXXXXXXXXX1225" in text


def _dated_row(instrument: str, occurred: date, amount: str, *,
               direction="out", narration="CARD") -> StatementRow:
    """A row on any date, unlike `_row`, which is fixed to July.

    No `balance_after`: a card prints no running balance, and this is the
    helper the card tests use.
    """
    money = Decimal(amount)
    return StatementRow(
        row_id=row_id_for(
            instrument=instrument, occurred_on=occurred, direction=direction,
            amount=money, balance_after=None, occurrence_index=0, narration=narration,
        ),
        instrument=instrument, occurred_on=occurred, narration=narration, ref=None,
        direction=direction, amount=money, balance_after=None,
        statement_id=f"{instrument}/x", file_sha256="fixture",
    )


def _periodic(instrument, start, end, rows) -> ParsedStatement:
    return ParsedStatement(
        status="ok", instrument=instrument, period_start=start, period_end=end,
        opening_balance=Decimal("0"), closing_balance=Decimal("0"),
        rows=tuple(rows), statement_id=f"{instrument}/{start}..{end}",
        file_sha256="fixture",
    )


def test_the_check_window_is_the_rows_not_the_printed_period():
    """§9.3 asks hledger for the span the ROWS cover.

    An Axis card statement bills 20/07-18/08 and posts its transactions from
    18/07 to 17/08 — the period is billing dates, the rows are posting dates.
    Ask for the printed period and the first two days' blocks sit outside it,
    the cleared movement comes up short by exactly those rows, and a statement
    that was never wrong reverts. Ask for the union and the window runs to
    18/08, which is the day the NEXT statement's rows start.
    """
    stmt = _periodic(
        "axis-cc-1313", date(2026, 7, 20), date(2026, 8, 18),
        [
            _dated_row("axis-cc-1313", date(2026, 7, 18), "100.00"),
            _dated_row("axis-cc-1313", date(2026, 8, 17), "200.00"),
        ],
    )
    assert statement_post.check_window(stmt) == (date(2026, 7, 18), date(2026, 8, 17))


def test_the_check_window_narrows_to_the_rows_inside_a_bank_period():
    """A bank statement's rows sit inside its period, and the window follows
    them there. The opening balance is the balance immediately before the first
    row, so the movement the bank claims is the rows' movement — the empty days
    at either end of the period belong to no row and to no block."""
    stmt = _periodic(
        "hdfc-1225", date(2026, 7, 12), date(2026, 8, 11),
        [_dated_row("hdfc-1225", date(2026, 7, 20), "50.00")],
    )
    assert statement_post.check_window(stmt) == (date(2026, 7, 20), date(2026, 7, 20))


def test_a_statement_with_no_rows_falls_back_to_its_period():
    """Nothing to span. `post_statement` returns before the check in this case
    — there is nothing to write — so this is what a direct caller gets."""
    stmt = _periodic("hdfc-1225", date(2026, 7, 12), date(2026, 8, 11), [])
    assert statement_post.check_window(stmt) == (date(2026, 7, 12), date(2026, 8, 11))


# ------------------------------------------- the check on a liability (§9.3)


def _card_statement(rows, opening, closing, *,
                    start=date(2026, 7, 1), end=date(2026, 7, 31)) -> ParsedStatement:
    """A card statement carrying the balances §15.11 says a card really prints.

    `_card_pair`'s card has `closing=None`, so every card test above stops
    before the check runs. That was the gap: the closing-balance check had
    never once run on a liability, and the sign flip that makes it work on one
    could be deleted with the whole file still green.

    `opening` and `closing` are what the card OWED, positive — the convention
    `Previous Balance` and `Total Payment Due` are printed in.
    """
    return ParsedStatement(
        status="ok", instrument="axis-cc-1313", period_start=start, period_end=end,
        opening_balance=Decimal(opening), closing_balance=Decimal(closing),
        rows=tuple(rows), statement_id=f"axis-cc-1313/{start}..{end}", file_sha256="fixture",
    )


@pytest.mark.asyncio
async def test_a_card_statement_with_printed_balances_passes_the_check(tmp_path):
    """The plainest liability case: two purchases, nothing skipped.

    hledger reports a card negative when you owe and the statement prints what
    you owe as a positive number, so the cleared movement has to be negated
    before it meets the printed figures. Without that the books look like they
    moved -150 against a bank that says +150.
    """
    cfg = _repo(tmp_path)
    rows = [
        _dated_row("axis-cc-1313", date(2026, 7, 5), "100.00"),
        _dated_row("axis-cc-1313", date(2026, 7, 6), "50.00"),
    ]
    result = await statement_post.post_statement(
        _card_statement(rows, "0", "150.00"), {}, cfg, entity="personal", liability=True,
    )

    assert len(result.posted) == 2
    assert result.balance_checked is True and result.balance_reason == ""
    assert _movement(cfg, "liabilities:card:axis:1313") == Decimal("-150.00")


@pytest.mark.asyncio
async def test_a_skipped_row_on_a_card_is_added_back_in_the_card_s_own_sign(tmp_path):
    """`unwritten` is accumulated with `signed()`, which is hledger's
    convention, so it needs the same flip the cleared movement gets.

    A card with one ambiguous ₹100 purchase and one posted ₹50 purchase, going
    from 0 owed to 150 owed. Flip one figure and not the other and the check
    misses by TWICE the skipped row — it reported "difference -200" — so every
    card statement holding a single skip reverted, and the row it reverted on
    was usually the card's own payment.
    """
    cfg = _repo(tmp_path)
    rows = [
        _dated_row("axis-cc-1313", date(2026, 7, 5), "100.00"),
        _dated_row("axis-cc-1313", date(2026, 7, 6), "50.00"),
    ]
    outcomes = {rows[0].row_id: RowOutcome(
        row_id=rows[0].row_id, statement_id="s", instrument="axis-cc-1313",
        occurred_on=date(2026, 7, 5), skip_reason=AMBIGUOUS, candidates=("m/a", "m/b"),
    )}

    result = await statement_post.post_statement(
        _card_statement(rows, "0", "150.00"), outcomes, cfg,
        entity="personal", liability=True,
    )

    assert result.skipped == [(rows[0].row_id, AMBIGUOUS)]
    assert len(result.posted) == 1
    assert result.balance_checked is True and result.balance_reason == ""


@pytest.mark.asyncio
async def test_a_card_running_before_its_bank_statement_still_checks_out(tmp_path):
    """The production shape, and the one that ran every day.

    Statements post in `(instrument, period_end)` order, so `axis-cc-1313` runs
    before every `hdfc-*`: the card's payment credit is skipped as a transfer
    counterpart with the far block not yet written, and its ₹2,500 is added
    back as money that never reached the journal. In the card's own sign that
    is -2,500 — the payment REDUCED what was owed. Left in hledger's sign it
    read +2,500 and the check reported "the bank moved -1600.00, the books
    moved 900.00 with 2500.00 left unwritten — difference 5000.00".
    """
    cfg = _repo(tmp_path)
    bank_row = _row(3, "2500.00", narration="CREDITCARD PAYMENT XXXX 1313", balance="-2500.00")
    payment = _dated_row("axis-cc-1313", date(2026, 7, 4), "2500.00",
                         direction="in", narration="PAYMENT RECEIVED THANK YOU")
    purchase = _dated_row("axis-cc-1313", date(2026, 7, 6), "900.00", narration="AMAZON")

    result = await statement_post.post_statement(
        _card_statement([payment, purchase], "2500.00", "900.00"), {}, cfg,
        entity="personal", liability=True, peer_rows=[bank_row],
    )

    assert (payment.row_id, TRANSFER) in result.skipped
    assert len(result.posted) == 1, "the purchase, and not the payment"
    assert result.balance_checked is True and result.balance_reason == ""


# ------------------------- the window and the previous statement's rows (§9.3)


def _may_and_june_cards():
    """Two consecutive Axis card statements, on the real dates.

    The row spans are contiguous and never overlap — 18/05-17/06, 18/06-17/07 —
    while each printed period runs to the day the NEXT statement's rows start.
    """
    may = _card_statement(
        [
            _dated_row("axis-cc-1313", date(2026, 5, 18), "100.00"),
            _dated_row("axis-cc-1313", date(2026, 6, 17), "200.00"),
        ],
        "0", "300.00", start=date(2026, 5, 20), end=date(2026, 6, 18),
    )
    june = _card_statement(
        [
            _dated_row("axis-cc-1313", date(2026, 6, 18), "400.00"),
            _dated_row("axis-cc-1313", date(2026, 7, 17), "500.00"),
        ],
        "300.00", "1200.00", start=date(2026, 6, 20), end=date(2026, 7, 18),
    )
    return may, june


@pytest.mark.asyncio
async def test_consecutive_card_statements_both_check_out_in_order(tmp_path):
    cfg = _repo(tmp_path)
    may, june = _may_and_june_cards()

    first = await statement_post.post_statement(
        may, {}, cfg, entity="personal", liability=True,
    )
    second = await statement_post.post_statement(
        june, {}, cfg, entity="personal", liability=True,
    )

    assert first.balance_checked is True and first.balance_reason == ""
    assert second.balance_checked is True and second.balance_reason == ""


@pytest.mark.asyncio
async def test_an_older_statement_posted_later_does_not_count_the_newer_one(tmp_path):
    """§15.11's stated property: a backfill runs in any order.

    The operator drops the May statement into Drive after June's is already
    posted. May's rows stop on 17/06 and June's start on 18/06 — but May's
    printed period runs to 18/06, so a window that unions the two counts June's
    first row in May's movement and reverts a statement in which nothing was
    wrong. The row span holds no such day.
    """
    cfg = _repo(tmp_path)
    may, june = _may_and_june_cards()

    first = await statement_post.post_statement(
        june, {}, cfg, entity="personal", liability=True,
    )
    second = await statement_post.post_statement(
        may, {}, cfg, entity="personal", liability=True,
    )

    assert first.balance_checked is True and first.balance_reason == ""
    assert second.balance_checked is True and second.balance_reason == ""


@pytest.mark.asyncio
async def test_a_counterpart_inside_the_rows_but_outside_the_printed_period(tmp_path):
    """The check window and the counterpart test have to be ONE window.

    The bank pays the card bill on 19/07. The card's period starts on 20/07 and
    its rows start on 18/07, so that block is inside the window hledger is
    asked for and outside the printed period. Ask the printed period whether
    the counterpart landed and the answer is "no": its ₹2,500 is added back on
    top of a cleared movement that already counts it, and a correct statement
    reverts by exactly the transfer.
    """
    cfg = _repo(tmp_path)
    bank_row = _row(19, "2500.00", narration="CREDITCARD PAYMENT XXXX 1313", balance="-2500.00")
    bank = _statement([bank_row], "-2500.00")
    payment = _dated_row("axis-cc-1313", date(2026, 7, 21), "2500.00",
                         direction="in", narration="PAYMENT RECEIVED THANK YOU")
    card = _card_statement(
        [
            _dated_row("axis-cc-1313", date(2026, 7, 18), "100.00"),
            payment,
            _dated_row("axis-cc-1313", date(2026, 8, 17), "200.00"),
        ],
        "2500.00", "300.00", start=date(2026, 7, 20), end=date(2026, 8, 18),
    )

    first = await statement_post.post_statement(
        bank, {}, cfg, entity="personal", peer_rows=card.rows,
    )
    second = await statement_post.post_statement(
        card, {}, cfg, entity="personal", liability=True, peer_rows=bank.rows,
    )

    assert first.balance_reason == ""
    assert (payment.row_id, TRANSFER) in second.skipped
    assert second.balance_checked is True and second.balance_reason == ""
    text = (cfg.path / "personal" / "2026.journal").read_text()
    assert text.count("₹2500.00") == 1, "one block for one movement"


async def test_a_foreign_block_promoted_with_its_cost_lets_the_check_run(tmp_path):
    """§8.5 end to end, and the reason two live statements reverted.

    The email lane wrote `$200.00` because that is what the receipt said. The
    card account then held DOLLARS, `hledger -X ₹` had no price dated on or
    before the posting to value them with, and `cleared_movement_sync` refused
    the whole statement rather than parse a mixed cell into a wrong number.

    Promotion now records the rupee figure the bank charged as the posting's
    cost. hledger balances on the cost, the card account becomes rupees, and
    the check can finally run — and it is EXACT, which a market price could
    never be: the card's FX spread means the rate that values $200 is not the
    rate that was charged.
    """
    cfg = _repo(tmp_path)
    (cfg.path / "personal" / "2026.journal").write_text(
        "; p\n\n"
        "2026-07-14 ! Anthropic\n"
        "    ; msgid: mail/anthropic\n"
        "    ; channel: receipt, instrument: axis-cc-1313\n"
        "    expenses:media                          $200.00\n"
        "    liabilities:card:axis:1313\n"
    )
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "seed"],
        cwd=cfg.path, check=True,
    )
    row = _row(14, "19091.99", narration="ANTHROPIC* CLAUDE SUB", instrument="axis-cc-1313")
    stmt = _card_statement([row], "0", "19091.99")
    outcome = RowOutcome(
        row_id=row.row_id, statement_id=stmt.statement_id, instrument="axis-cc-1313",
        occurred_on=row.occurred_on, matched_pass=PASS_WINDOW, msgid="mail/anthropic",
        delta_days=0, foreign=True,
    )

    result = await statement_post.post_statement(
        stmt, {row.row_id: outcome}, cfg, entity="personal", liability=True
    )

    assert result.promoted == ["mail/anthropic"], result
    assert result.balance_checked, result
    assert result.balance_reason == "", result.balance_reason
    text = (cfg.path / "personal" / "2026.journal").read_text()
    assert "$200.00 @@ ₹19091.99" in text, text
    # hledger agrees the card now holds rupees and nothing else.
    out = subprocess.run(
        ["hledger", "-f", str(cfg.path / cfg.main), "balance",
         "liabilities:card:axis:1313", "-X", "₹", "--no-total", "--flat"],
        capture_output=True, text=True, cwd=str(cfg.path),
    ).stdout
    assert "$" not in out, out
    # hledger prints the commodity's own digit grouping — `₹ -19,091.99`.
    assert "19091.99" in out.replace(",", ""), out


async def test_promotion_moves_an_unplaceable_block_onto_the_account_it_proves(tmp_path):
    """A receipt that named no account posts to `assets:unknown` (#407).

    The statement is the bank saying the money moved through THIS account, so
    promotion moves the posting there. Skip it and the block never touches the
    instrument: §9.3's movement comes up short by exactly those rows and the
    whole statement reverts. On the first live card statement two such blocks —
    ₹99.00 and ₹1047.83 — were the entire ₹1146.83 gap.
    """
    cfg = _repo(tmp_path)
    (cfg.path / "personal" / "2026.journal").write_text(
        "; p\n\n"
        "2026-07-23 ! GoDaddy\n"
        "    ; msgid: mail/godaddy\n"
        "    ; channel: receipt\n"
        "    expenses:media                          ₹99.00\n"
        "    assets:unknown\n"
    )
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "seed"],
        cwd=cfg.path, check=True,
    )
    row = _row(23, "99.00", narration="GODADDY,MUMBAI", instrument="axis-cc-1313")
    stmt = _card_statement([row], "0", "99.00")
    outcome = RowOutcome(
        row_id=row.row_id, statement_id=stmt.statement_id, instrument="axis-cc-1313",
        occurred_on=row.occurred_on, matched_pass=PASS_WINDOW, msgid="mail/godaddy",
        delta_days=0,
    )

    result = await statement_post.post_statement(
        stmt, {row.row_id: outcome}, cfg, entity="personal", liability=True
    )

    assert result.promoted == ["mail/godaddy"], result
    assert result.balance_checked and result.balance_reason == "", result.balance_reason
    text = (cfg.path / "personal" / "2026.journal").read_text()
    assert "liabilities:card:axis:1313" in text, text
    assert "assets:unknown" not in text, text


async def test_promotion_leaves_a_real_instrument_account_alone(tmp_path):
    """Only the placeholder moves.

    A block already naming a real account was decided by something with more
    evidence than a matcher's date-and-amount guess, so a promotion must not
    overwrite it — that would let one wrong match silently move money between
    two accounts the owner really holds.
    """
    cfg = _repo(tmp_path)
    (cfg.path / "personal" / "2026.journal").write_text(
        "; p\n\n"
        "2026-07-23 ! GoDaddy\n"
        "    ; msgid: mail/godaddy\n"
        "    ; channel: receipt, instrument: hdfc-1225\n"
        "    expenses:media                          ₹99.00\n"
        "    assets:bank:hdfc:1225\n"
    )
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "seed"],
        cwd=cfg.path, check=True,
    )
    row = _row(23, "99.00", narration="GODADDY,MUMBAI", instrument="axis-cc-1313")
    # No printed closing balance, so §9.3's check does not run: leaving the
    # block on hdfc WOULD fail it, and a revert would hide the very thing this
    # test is about by restoring the file either way.
    stmt = _statement([row], None, instrument="axis-cc-1313")
    outcome = RowOutcome(
        row_id=row.row_id, statement_id=stmt.statement_id, instrument="axis-cc-1313",
        occurred_on=row.occurred_on, matched_pass=PASS_WINDOW, msgid="mail/godaddy",
        delta_days=0,
    )
    result = await statement_post.post_statement(
        stmt, {row.row_id: outcome}, cfg, entity="personal", liability=True
    )
    assert result.promoted == ["mail/godaddy"], result
    text = (cfg.path / "personal" / "2026.journal").read_text()
    assert "assets:bank:hdfc:1225" in text, text
    assert "liabilities:card" not in text, text
