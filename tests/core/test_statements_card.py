"""Axis credit-card statement parsing (spec §6.1, §6.2, §8.3, §8.5).

The fixture is `pdftotext -layout` output with the REAL column geometry of the
card's two page layouts — page one's amount column ends at character 230, page
two's at 129 — and an invented cardholder, card number, merchants and amounts.
Its summary figures were computed from its own rows, so the §6.2 identity closes
to the paisa; verified against three real statements while this was written, and
nothing derived from one is committed here.

Every test below was run against broken code before it was trusted: the
behaviour it names was removed, the test failed, and the code was put back.
"""

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from aegis.services.statements import (
    PARSED,
    REFUSED,
    UNIDENTIFIED,
    normalise_narration,
    parse_axis_statement,
)
from aegis.services.statements_axis_card import (
    END_MARKER,
    LAYOUT,
    identify_axis_card,
    is_axis_card_statement,
    parse_axis_card_statement,
)

FIXTURES = Path(__file__).parent / "fixtures" / "statements"
SUBJECT_SAYING_SEPTEMBER = "Axis Bank Credit Card Statement for September 2026"


@pytest.fixture
def card() -> str:
    """The card layout: a payment summary, the bank's own arithmetic, rows over
    two page geometries, an `EMI BALANCES` block and pages of boilerplate after
    the end marker."""
    return (FIXTURES / "axis_credit_card.txt").read_text()


@pytest.fixture
def mailed() -> str:
    """A current-account statement — and it carries a `CreditCard Payment`
    narration row, which is the misfiling trap of §6.1."""
    return (FIXTURES / "axis_mailed.txt").read_text()


def parse(text: str, **kw):
    return parse_axis_card_statement(text, file_sha256="fixture-sha", **kw)


def drop_line(text: str, needle: str) -> str:
    """Remove the one line containing `needle` — a row lost in the parse."""
    lines = text.splitlines(keepends=True)
    hits = [i for i, line in enumerate(lines) if needle in line]
    assert len(hits) == 1, f"{needle!r} matched {len(hits)} lines"
    del lines[hits[0]]
    return "".join(lines)


def edit_line(text: str, needle: str, old: str, new: str) -> str:
    """Change `old` to `new` on the one line holding `needle`, and nowhere else."""
    lines = text.splitlines(keepends=True)
    hits = [i for i, line in enumerate(lines) if needle in line]
    assert len(hits) == 1, f"{needle!r} matched {len(hits)} lines"
    assert old in lines[hits[0]]
    lines[hits[0]] = lines[hits[0]].replace(old, new)
    return "".join(lines)


def insert_after(text: str, needle: str, line: str) -> str:
    """Add `line` straight after the one line containing `needle`."""
    lines = text.splitlines(keepends=True)
    hits = [i for i, existing in enumerate(lines) if needle in existing]
    assert len(hits) == 1, f"{needle!r} matched {len(hits)} lines"
    lines.insert(hits[0] + 1, line.rstrip() + "\n")
    return "".join(lines)


# ------------------------------------------------------------------- the happy path


def test_the_card_statement_parses_every_row_with_its_direction(card):
    out = parse(card)
    assert (out.status, out.reason) == (PARSED, "")
    assert out.instrument == "axis-cc-9876"
    assert (out.period_start, out.period_end) == (date(2026, 7, 20), date(2026, 8, 18))
    # `Previous Balance` and `Total Payment Due`, both printed `Dr`. A card DOES
    # carry the two numbers §9.3's closing-balance check needs.
    assert (out.opening_balance, out.closing_balance) == (
        Decimal("30000.00"),
        Decimal("44453.36"),
    )
    assert [(r.occurred_on, r.direction, str(r.amount)) for r in out.rows] == [
        (date(2026, 7, 18), "out", "900.00"),
        (date(2026, 7, 19), "out", "31.50"),
        (date(2026, 7, 19), "out", "5.67"),
        (date(2026, 7, 20), "out", "1250.00"),
        (date(2026, 7, 21), "out", "4.13"),
        (date(2026, 7, 21), "out", "2.36"),
        (date(2026, 7, 21), "out", "2.36"),
        (date(2026, 7, 25), "out", "2000.00"),
        (date(2026, 7, 27), "in", "20.00"),
        (date(2026, 8, 2), "in", "30000.00"),
        (date(2026, 8, 3), "out", "1800.00"),
        (date(2026, 8, 4), "out", "63.00"),
        (date(2026, 8, 4), "out", "11.34"),
        (date(2026, 8, 9), "out", "850.00"),
        (date(2026, 8, 9), "out", "153.00"),
        (date(2026, 8, 9), "out", "12500.00"),
        (date(2026, 8, 12), "in", "1100.00"),
        (date(2026, 8, 15), "out", "26000.00"),
    ]
    assert out.statement_id == "axis-cc-9876/2026-07-20..2026-08-18"
    assert out.diagnostics["layout"] == LAYOUT
    assert all(r.balance_after is None for r in out.rows), "a card prints no running balance"
    assert all(r.file_sha256 == "fixture-sha" for r in out.rows)


def test_the_suffix_is_the_direction_not_a_guess_from_a_balance(card):
    """`Dr` is a charge and `Cr` a payment or refund, and the card prints one on
    every row — so unlike the netbanking layout nothing is inferred."""
    rows = {r.narration: r.direction for r in parse(card).rows}
    assert rows["BBPS PAYMENT RECEIVED - BD000000EXAMPLEAAAA1"] == "in"
    assert rows["FUEL CASHBACK REBATES"] == "in"
    assert rows["EXAMPLE APPLIANCES,MUMBAI ELECTRONICS"] == "out"


def test_rows_before_the_printed_period_start_are_normal(card):
    """The printed `Statement Period` is the billing cycle, not the transaction
    window: all three real statements run from two days before it. Refusing
    those rows would refuse every real statement."""
    out = parse(card)
    assert out.period_start == date(2026, 7, 20)
    assert min(r.occurred_on for r in out.rows) == date(2026, 7, 18)
    assert out.diagnostics["row_date_min"] == "2026-07-18"


# --------------------------------------------------------------- §6.2 the proof


def test_a_dropped_charge_fails_the_check_and_refuses_the_whole_statement(card):
    out = parse(drop_line(card, "SAMPLE FUEL STOP"))
    assert out.status == REFUSED
    assert (out.reason, out.diagnostics["side"]) == ("totals_mismatch", "debit")
    assert out.rows == (), "never a partial import"
    assert out.diagnostics["rows_read"] == 17
    assert (out.diagnostics["printed"], out.diagnostics["parsed"]) == ("45573.36", "43573.36")


def test_a_dropped_payment_fails_the_credit_side_of_the_check(card):
    out = parse(drop_line(card, "BBPS PAYMENT RECEIVED"))
    assert (out.status, out.reason) == (REFUSED, "totals_mismatch")
    assert out.diagnostics["side"] == "credit"
    assert out.rows == ()


def test_a_row_read_twice_fails_the_check(card):
    """The other half of §6.2: a double-read row breaks the same identity a
    dropped one does."""
    doubled = insert_after(
        card,
        "SAMPLE FUEL STOP",
        " 25/07/2026            SAMPLE FUEL STOP,MUMBAI            FUEL"
        + " " * 60
        + "2,000.00 Dr",
    )
    out = parse(doubled)
    assert (out.status, out.reason) == (REFUSED, "totals_mismatch")
    assert out.diagnostics["parsed"] == "47573.36"


def test_a_statement_whose_own_arithmetic_does_not_close_is_refused(card):
    """The bank's identity line is what makes the two side checks safe. If it
    does not close, this parser misread the figures it checks the rows against
    — so it refuses before those checks run."""
    out = parse(edit_line(card, "30,000.00 Dr", "30,000.00 Dr", "31,000.00 Dr"))
    assert (out.status, out.reason) == (REFUSED, "printed_totals_broken")
    assert out.rows == ()


def test_the_two_printed_closing_balances_must_agree(card):
    """The card prints its closing balance twice — the payment summary and the
    identity line — and §9.3 asserts the whole journal against it."""
    out = parse(edit_line(card, "11,114.00 Dr", "44,453.36 Dr", "44,553.36 Dr"))
    assert (out.status, out.reason) == (REFUSED, "conflicting_closing")
    assert out.diagnostics["summary_total_due"] == "44553.36"
    assert out.rows == ()


def test_a_statement_with_no_printed_totals_is_refused_not_read(card):
    without = drop_line(card, "Previous Balance - Payments")
    without = drop_line(without, "30,000.00 Dr")
    out = parse(without)
    assert (out.status, out.reason) == (REFUSED, "missing_balance_anchor")
    assert (out.opening_balance, out.closing_balance) == (None, None)
    assert out.rows == ()


def test_a_date_led_line_that_does_not_parse_is_named_never_skipped(card):
    """Money this parser did not read has to be visible. A dropped row would
    only ever surface as an arithmetic failure with no line to look at."""
    out = parse(insert_after(card, "SAMPLE FUEL STOP", " 26/07/2026            NO AMOUNT HERE"))
    assert (out.status, out.reason) == (REFUSED, "unreadable_row")
    assert out.diagnostics["unreadable_lines"]
    assert out.rows == ()


def test_a_row_dated_after_the_statement_was_generated_is_refused(card):
    """`period_end` is the generation date, so nothing can be charged after it —
    and a wrong date breaks no sum, so this check is the only one that sees it."""
    out = parse(edit_line(card, "EXAMPLE APPLIANCES,MUMBAI", "15/08/2026", "19/08/2026"))
    assert (out.status, out.reason) == (REFUSED, "row_after_period")
    assert out.diagnostics["late_dates"] == ["2026-08-19"]
    assert out.rows == ()


# ------------------------------------------------------------ §6.1 identification


def test_a_current_account_statement_with_a_creditcard_payment_row_is_not_a_card(mailed):
    """The trap that earned §6.1: a substring check on `Credit Card` once filed
    nine Axis current-account statements as credit-card statements."""
    assert "CreditCard Payment XXXX 1313" in mailed, "the trap is really in the file"
    assert is_axis_card_statement(mailed) is False
    assert identify_axis_card(mailed) == (None, "no_header_anchor")
    # And the account parser still reads it, unchanged.
    savings = parse_axis_statement(mailed, file_sha256="fixture-sha")
    assert (savings.status, savings.instrument) == (PARSED, "axis-4321")


def test_a_card_statement_is_not_read_as_a_bank_account(card):
    """The mirror. The card carries none of the account layouts' anchors, so it
    is UNIDENTIFIED there rather than silently parsed as a savings account."""
    assert is_axis_card_statement(card) is True
    out = parse_axis_statement(card, file_sha256="fixture-sha")
    assert (out.status, out.reason) == (UNIDENTIFIED, "no_header_anchor")
    assert out.rows == ()


def test_the_boilerplate_line_naming_the_card_is_not_the_anchor(card):
    """The same page prints `payable to Axis Bank Card No.<number>` in its
    boilerplate. The anchor needs the colon and the `Name` field, so that line
    cannot move the account."""
    assert "Card No.400000******9876" in card
    stripped = drop_line(card, "Card No: 400000")
    assert "Card No.400000******9876" in stripped
    assert is_axis_card_statement(stripped) is False


def test_the_period_comes_from_the_header_never_from_the_subject(card):
    """Axis names each monthly statement for the month it was SENT."""
    out = parse(card, subject=SUBJECT_SAYING_SEPTEMBER)
    assert (out.period_start, out.period_end) == (date(2026, 7, 20), date(2026, 8, 18))
    assert out.statement_id.endswith("2026-07-20..2026-08-18")
    assert out.subject_hint == SUBJECT_SAYING_SEPTEMBER


def test_two_different_cards_in_one_file_are_unidentified(card):
    misfiled = card + "\n                     Card No: 400000******5555      Name MR OTHER HOLDER\n"
    out = parse(misfiled)
    assert (out.status, out.reason) == (UNIDENTIFIED, "conflicting_headers")
    assert out.instrument is None and out.rows == ()


def test_a_card_with_no_period_says_so_rather_than_looking_unidentifiable(card):
    """A card statement that fails to identify must report ITS reason. Falling
    back to the account parser would report `no_header_anchor`, which points at
    the wrong thing."""
    out = parse(edit_line(card, "20/07/2026 - 18/08/2026", "20/07/2026 - 18/08/2026", ""))
    assert (out.status, out.reason) == (UNIDENTIFIED, "no_period_anchor")


def test_the_declared_chart_spelling_wins_over_the_parsed_tail(card):
    out = parse(card, declared={"liabilities:card:axis:09876"})
    assert out.instrument == "axis-cc-09876"
    assert all(r.instrument == "axis-cc-09876" for r in out.rows)


# -------------------------------------------------------- the end of the table


def test_the_boilerplate_after_the_end_marker_contributes_no_rows(card):
    """Pages of examples in rupees follow the marker (`Rs. 25000.00`, a worked
    `Db` table). The row shape excludes them — none leads with a date — and the
    marker means they are never looked at either."""
    after = card.split(END_MARKER)[1]
    assert "Rs. 25000.00" in after and "Db" in after
    out = parse(card)
    assert out.status == PARSED
    assert Decimal("25000.00") not in {r.amount for r in out.rows}
    assert len(out.rows) == 18


def test_a_row_shaped_line_after_the_end_marker_is_never_read(card):
    """The belt, tested on its own: a line the row shape WOULD accept, printed
    after the marker, must not reach the totals."""
    planted = insert_after(
        card, END_MARKER, "01/09/2026   PLANTED AFTER THE MARKER" + " " * 40 + "9,999.00 Dr"
    )
    out = parse(planted)
    assert out.status == PARSED, "the marker stopped the table before this line"
    assert len(out.rows) == 18
    assert Decimal("9999.00") not in {r.amount for r in out.rows}


def test_the_emi_balances_block_is_not_a_transaction(card):
    """It sits between the last row and the marker, and carries an amount with
    no leading date and no `Dr`/`Cr`."""
    assert "EMI BALANCES" in card and "62,500.00" in card
    assert Decimal("62500.00") not in {r.amount for r in parse(card).rows}


# ------------------------------------------------------------------- §8.5 the FX


def test_foreign_currency_rows_keep_their_original_currency_and_amount(card):
    """The bank prints the original beside the rupee charge. §8.5's alternative
    is a 5% band through a rate production does not have."""
    fx = {
        r.narration: (r.fx_currency, r.fx_amount, r.amount)
        for r in parse(card).rows
        if r.fx_currency
    }
    assert fx == {
        "EXAMPLE VAULT,TORONTO ( USD 9.99 ) RETAIL STORES": (
            "USD",
            Decimal("9.99"),
            Decimal("900.00"),
        ),
        "NOWHERE SOFTWARE,NOWHERE.CO ( USD 20.00 ) MISCELLANEOUS": (
            "USD",
            Decimal("20.00"),
            Decimal("1800.00"),
        ),
        # A refund, so the original rides a credit too.
        "SPECIMEN SOFTWARE,COVINA ( USD 12.34 ) MISCELLANEOUS": (
            "USD",
            Decimal("12.34"),
            Decimal("1100.00"),
        ),
    }
    assert parse(card).diagnostics["fx_rows"] == 3


def test_a_rupee_row_carries_no_foreign_original(card):
    assert all(
        (r.fx_currency, r.fx_amount) == (None, None)
        for r in parse(card).rows
        if "USD" not in r.narration
    )


# --------------------------------------------------------------------- §8.3 ids


def test_two_identical_charges_on_one_day_stay_two_rows(card):
    """A card has no running balance to separate them, so only the occurrence
    index does — and without it money vanishes from the books."""
    same = [r for r in parse(card).rows if r.narration == "GST" and r.amount == Decimal("2.36")]
    assert len(same) == 2
    assert same[0].row_id != same[1].row_id


def test_the_row_id_survives_a_change_of_column_geometry(card):
    """§8.3: the id must not move when the layout does. `pdftotext -layout`
    gives the card two geometries already — page one's amount column ends at
    character 230 and page two's at 129 — so the narration is read as content
    between the date and the amount, never sliced at an offset.
    """
    respaced = "\n".join(" ".join(line.split()) if line.strip() else line
                         for line in card.splitlines())
    out, moved = parse(card), parse(respaced)
    assert moved.status == PARSED, moved.reason
    assert [r.row_id for r in moved.rows] == [r.row_id for r in out.rows]
    assert [r.narration for r in moved.rows] == [r.narration for r in out.rows]


def test_the_same_statement_parsed_twice_yields_the_same_ids(card):
    assert [r.row_id for r in parse(card).rows] == [r.row_id for r in parse(card).rows]


def test_the_narration_is_everything_the_bank_printed_between_date_and_amount(card):
    """Including the merchant category. Splitting it off would need a per-page
    column offset, and the narration is in the `row_id` on a card — so a wrong
    split changes the id of a row that did not change.
    """
    narrations = {r.narration for r in parse(card).rows}
    assert "SPECIMEN GROCERS,NAVI MUMBAI DEPT STORES" in narrations
    assert "EMI PRINCIPAL - 2/6, REF# 11223344 UTILITIES" in narrations
    assert all(n == normalise_narration(n) for n in narrations)
