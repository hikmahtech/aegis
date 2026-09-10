"""Step 6 — own-account detection, transfer pairing and reversals (§8.4, §8.5).

Row ↔ row matching, with no repo and no journal: these are the decisions the
writer in `statement_post` then acts on, and §8.4 says they are the ones to
test first.

Every case here is falsifiable against a specific line of the module. The three
guards on `own_account` each have a test that a naive implementation passes and
this one does not — a substring scan resolves the invoice number, a
marker-blind scan resolves the merchant handle, a self-blind scan resolves the
payer's own masked account.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from aegis.services import statement_transfers as transfers
from aegis.services.statements import StatementRow, row_id_for

DECLARED = frozenset(
    {
        "assets:bank:hdfc:1225",
        "assets:bank:hdfc:0325",
        "assets:bank:axis:9640",
        "assets:bank:nkgsb:843",
        "liabilities:card:axis:1313",
        "expenses:unknown",
        "income:unknown",
        "equity:transfers",
    }
)

HDFC = "assets:bank:hdfc:1225"


def _row(
    day: int,
    amount: str,
    narration: str,
    *,
    direction: str = "out",
    instrument: str = "hdfc-1225",
    ref: str | None = None,
    balance: str | None = None,
    occurrence: int = 0,
) -> StatementRow:
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
        statement_id=f"{instrument}/2026-07", file_sha256="fixture",
    )


# ------------------------------------------------------- own-account detection


def test_a_creditcard_payment_narration_names_the_card():
    """§8.4's headline case. Left to the rules this matches nothing, lands in
    `expenses:unknown`, and the card liability drifts by the full bill every
    month."""
    assert (
        transfers.own_account("CreditCard Payment XXXX 1313", DECLARED, exclude=HDFC)
        == "liabilities:card:axis:1313"
    )


def test_an_imps_credit_names_the_far_bank_account():
    """The live double-count: ₹1,00,000 from `…9640` posted as `income:unknown`
    because nothing read the tail out of the narration."""
    narration = "IMPS-523018123456-HIKMAHTECHNOLOGIES-UTIB-XXXXXXXXXXX9640-TRANSFER"
    assert (
        transfers.own_account(narration, DECLARED, exclude=HDFC)
        == "assets:bank:axis:9640"
    )


def test_a_padded_tail_finds_the_unpadded_chart():
    """NKGSB mails `X0843` and the chart declares `843`. That mismatch is where
    ₹53,774.56 of real money went once already."""
    assert (
        transfers.own_account("SWEEP TO DEPOSIT X0843", DECLARED, exclude=HDFC)
        == "assets:bank:nkgsb:843"
    )


def test_a_narration_naming_both_accounts_resolves_to_the_far_one():
    """The common IMPS shape names the payer's own masked account as well as
    the payee's. Removing the row's own account BEFORE counting is what keeps
    that from reading as two matches and refusing."""
    narration = "IMPS/612345678903/FROM XXXXXXXXXX1225 TO XXXXXXXXXXX9640"
    assert (
        transfers.own_account(narration, DECLARED, exclude=HDFC)
        == "assets:bank:axis:9640"
    )


def test_a_row_never_resolves_to_its_own_account():
    """`UPI-…-XXXXXXXXXX1225-PAYMENT` on the `…1225` statement echoes the
    payer's own masked number. A row cannot be a transfer to itself, and
    resolving it would post the block against the same account twice."""
    narration = "TRF UPI-987654321012-SPECIMEN STORE.-EXAM-XXXXXXXXXX1225-PAYMENT"
    assert transfers.own_account(narration, DECLARED, exclude=HDFC) is None


def test_a_declared_tail_inside_a_longer_number_does_not_resolve():
    """Guard 2, and the one a substring scan fails.

    `1234196405` is an invoice number that happens to contain `9640`. A scan
    that looked for the tail anywhere in the string would file this merchant
    payment into a bank account the owner holds — and §9.3's closing-balance
    check would still pass, because both accounts are real, so nothing
    downstream would ever ask again.
    """
    narration = "NEFT/AXISP00123456/INVOICE 1234196405 SPECIMEN SUPPLIES"
    assert transfers.own_account(narration, DECLARED, exclude=HDFC) is None


def test_a_third_party_handle_carrying_a_declared_tail_does_not_resolve():
    """Guard 1, and the one a marker-blind scan fails.

    `9640` here IS a standalone group of digits — it survives guard 2 — and it
    is a declared account tail. What it is not is a transfer: it is a merchant's
    UPI handle. The narration never says the money moved between two accounts
    the owner holds, so the tail in it is not read as one.
    """
    assert transfers.own_account("UPI/P2M/712345678901/SHOP-9640@YBL/PAY", DECLARED) is None


def test_two_declared_accounts_with_one_tail_are_ambiguous():
    """Guard 3. Picking one of them is a coin toss, and a wrong pick moves money
    into a real account where nothing catches it."""
    declared = DECLARED | {"assets:bank:hdfc:1313"}
    assert transfers.own_account("CreditCard Payment XXXX 1313", declared, exclude=HDFC) is None


def test_an_undeclared_tail_does_not_resolve():
    """The chart decides. Inventing an account here is what `check --strict`
    would refuse on the way out."""
    assert transfers.own_account("IMPS/612345678904/TO XXXX 7777", DECLARED) is None


# ------------------------------------------------------------ transfer pairing


def _card_payment_pair():
    bank = _row(3, "2500.00", "CREDITCARD PAYMENT XXXX 1313", balance="-2500.00")
    card = _row(4, "2500.00", "PAYMENT RECEIVED THANK YOU",
                direction="in", instrument="axis-cc-1313")
    return bank, card


def test_a_bank_and_card_row_for_one_payment_pair_up():
    bank, card = _card_payment_pair()
    paired = transfers.find_transfers([bank], [card], DECLARED)
    leg = paired[bank.row_id]
    assert leg.peer_row_id == card.row_id
    assert leg.account == "liabilities:card:axis:1313"
    assert leg.kind == transfers.TRANSFER


def test_the_naming_side_posts_and_the_silent_side_does_not():
    """§8.4's "posted from the bank side only", reached without special-casing
    cards: the card statement's credit reads `PAYMENT RECEIVED` and names
    nothing, so the bank side is the only side that can name the far account."""
    bank, card = _card_payment_pair()
    assert transfers.find_transfers([bank], [card], DECLARED)[bank.row_id].posts is True
    assert transfers.find_transfers([card], [bank], DECLARED)[card.row_id].posts is False


def test_when_both_sides_name_each_other_the_out_side_posts():
    """Bank ↔ bank, where §8.4's rule has no answer of its own. The tie is
    broken deterministically, so both statements agree on who writes whichever
    order they run in."""
    hdfc = _row(4, "100000.00", "IMPS/612345678905/FROM XXXXXXXXXXX9640",
                direction="in", balance="100000.00")
    axis = _row(4, "100000.00", "IMPS/612345678905/TO XXXXXXXXXX1225",
                instrument="axis-9640", balance="-100000.00")
    assert transfers.find_transfers([axis], [hdfc], DECLARED)[axis.row_id].posts is True
    assert transfers.find_transfers([hdfc], [axis], DECLARED)[hdfc.row_id].posts is False


def test_two_possible_peers_leave_the_row_unpaired():
    """The matcher's pass-3 rule, applied to row ↔ row: one of them is the
    right one and choosing mis-attributes money silently."""
    bank, card = _card_payment_pair()
    other = _row(5, "2500.00", "PAYMENT RECEIVED THANK YOU",
                 direction="in", instrument="axis-cc-1313", occurrence=1)
    assert transfers.find_transfers([bank], [card, other], DECLARED) == {}


def test_a_row_of_the_same_statement_is_never_a_counterpart():
    bank, _ = _card_payment_pair()
    mirror = _row(3, "2500.00", "CREDITCARD PAYMENT XXXX 1313",
                  direction="in", balance="0.00")
    assert transfers.find_transfers([bank], [mirror], DECLARED) == {}


def test_a_pair_needs_the_narration_to_name_the_other_account():
    """Without it, any two same-amount opposite-direction rows within three
    days on two accounts would read as one movement — which is most of a busy
    month."""
    bank = _row(3, "2500.00", "SOME SHOP", balance="-2500.00")
    card = _row(4, "2500.00", "PAYMENT RECEIVED THANK YOU",
                direction="in", instrument="axis-cc-1313")
    assert transfers.find_transfers([bank], [card], DECLARED) == {}


def test_a_pair_outside_the_window_is_not_a_pair():
    bank, card = _card_payment_pair()
    late = _row(20, "2500.00", "PAYMENT RECEIVED THANK YOU",
                direction="in", instrument="axis-cc-1313")
    assert transfers.find_transfers([bank], [late], DECLARED) == {}
    assert transfers.find_transfers([bank], [card], DECLARED) != {}


def test_amount_and_direction_must_agree():
    bank, card = _card_payment_pair()
    wrong_amount = _row(4, "2400.00", "PAYMENT RECEIVED THANK YOU",
                        direction="in", instrument="axis-cc-1313")
    same_direction = _row(4, "2500.00", "PAYMENT RECEIVED THANK YOU",
                          instrument="axis-cc-1313")
    assert transfers.find_transfers([bank], [wrong_amount], DECLARED) == {}
    assert transfers.find_transfers([bank], [same_direction], DECLARED) == {}
    assert transfers.find_transfers([bank], [card], DECLARED) != {}


def test_no_peers_means_no_pair_and_that_is_not_a_failure():
    """The far statement not being ingested yet is the normal case for the
    first months of a backfill. Own-account detection still gives the row the
    right counter account; only the pair is missing."""
    bank, _ = _card_payment_pair()
    assert transfers.find_transfers([bank], [], DECLARED) == {}
    assert transfers.own_account(bank.narration, DECLARED, exclude=HDFC) is not None


# ------------------------------------------------------------------- reversals


def test_a_failed_payment_and_its_recredit_pair_on_the_reference():
    debit = _row(3, "500.00", "UPI/P2M/612345678906/SHOP",
                 ref="612345678906", balance="-500.00")
    credit = _row(3, "500.00", "UPI/P2M/612345678906/SHOP REVERSAL",
                  direction="in", ref="612345678906", balance="0.00")
    paired = transfers.find_reversals([debit, credit], DECLARED, entity="personal")
    assert paired[debit.row_id].peer_row_id == credit.row_id
    assert paired[credit.row_id].peer_row_id == debit.row_id
    assert {p.kind for p in paired.values()} == {transfers.REVERSAL}
    assert {p.account for p in paired.values()} == {"equity:transfers"}
    assert all(p.posts for p in paired.values()), "both legs post; neither is skipped"


def test_a_reversal_pairs_on_the_narration_when_there_is_no_reference():
    debit = _row(3, "500.00", "ATM WDL SPECIMEN", balance="-500.00")
    credit = _row(3, "500.00", "ATM WDL SPECIMEN", direction="in", balance="0.00")
    assert len(transfers.find_reversals([debit, credit], DECLARED, entity="personal")) == 2


def test_two_unrelated_rows_are_not_a_reversal():
    """Same day, same amount, opposite directions — and nothing saying they are
    one payment. Pairing them would net a real expense against a real credit
    and both would vanish from the books."""
    debit = _row(3, "500.00", "SPECIMEN SHOP", balance="-500.00")
    credit = _row(3, "500.00", "SALARY", direction="in", balance="0.00")
    assert transfers.find_reversals([debit, credit], DECLARED, entity="personal") == {}


def test_a_reversal_on_a_different_day_is_not_paired():
    """§8.5 is same-day. A re-credit days later is a refund, which pass 2 of the
    matcher already handles."""
    debit = _row(3, "500.00", "ATM WDL SPECIMEN", balance="-500.00")
    credit = _row(6, "500.00", "ATM WDL SPECIMEN", direction="in", balance="0.00")
    assert transfers.find_reversals([debit, credit], DECLARED, entity="personal") == {}


def test_both_legs_of_a_reversal_take_the_same_account():
    """The account matters far less than the fact that both legs use one: that
    is what makes the pair net to zero."""
    debit = _row(3, "500.00", "ATM WDL SPECIMEN", balance="-500.00")
    credit = _row(3, "500.00", "ATM WDL SPECIMEN", direction="in", balance="0.00")
    paired = transfers.find_reversals([debit, credit], (), entity="personal")
    assert len({p.account for p in paired.values()}) == 1


def test_an_undeclared_clearing_account_falls_back_and_still_nets_to_zero():
    """A fork whose chart has no `equity:transfers` must not have its statement
    reverted by `check --strict`. Both legs move to the unknown account
    together, so the pair still nets to zero and stays visible in the digest."""
    chart = frozenset({"assets:bank:hdfc:1225", "expenses:unknown", "income:unknown"})
    assert transfers.reversal_account(chart, "personal") == "expenses:unknown"
    assert transfers.reversal_account(chart, "hikmah") == "expenses:hikmah:unknown"
    assert transfers.reversal_account(DECLARED, "personal") == "equity:transfers"
