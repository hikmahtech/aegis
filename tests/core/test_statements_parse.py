"""Axis statement parsing (spec §6.1, §6.2, §8.3).

The fixtures are `pdftotext -layout` output with the REAL column geometry of the
two Axis layouts — including the page-2 header at different offsets from page
one's, which is what the per-page column map exists for — and invented account
numbers, names and amounts. Verified against 8 real statements while this was
written; nothing derived from one is committed here.
"""

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from aegis.services import statements
from aegis.services.statements import (
    PARSED,
    REFUSED,
    UNIDENTIFIED,
    extract_ref,
    normalise_narration,
    parse_axis_statement,
    row_id_for,
)

FIXTURES = Path(__file__).parent / "fixtures" / "statements"
SUBJECT_SAYING_AUGUST = "Axis Bank Statement for August 2026"


@pytest.fixture
def mailed() -> str:
    return (FIXTURES / "axis_mailed.txt").read_text()


@pytest.fixture
def netbanking() -> str:
    return (FIXTURES / "axis_netbanking.txt").read_text()


@pytest.fixture
def multipage() -> str:
    """The netbanking layout over four pages: a printed column header on page
    one only, then three pages of rows at three different shifted offsets —
    page three printing ONE amount column, which position cannot tell debit
    from credit."""
    return (FIXTURES / "axis_netbanking_multipage.txt").read_text()


def parse(text: str, **kw):
    return parse_axis_statement(text, file_sha256="fixture-sha", **kw)


def drop_line(text: str, needle: str) -> str:
    """Remove the one line containing `needle` — a row lost in the parse."""
    lines = text.splitlines(keepends=True)
    hits = [i for i, line in enumerate(lines) if needle in line]
    assert len(hits) == 1, f"{needle!r} matched {len(hits)} lines"
    del lines[hits[0]]
    return "".join(lines)


def insert_after(text: str, needle: str, line: str) -> str:
    """Add `line` straight after the one line containing `needle`."""
    lines = text.splitlines(keepends=True)
    hits = [i for i, existing in enumerate(lines) if needle in existing]
    assert len(hits) == 1, f"{needle!r} matched {len(hits)} lines"
    lines.insert(hits[0] + 1, line.rstrip() + "\n")
    return "".join(lines)


def edit_line(text: str, needle: str, old: str, new: str) -> str:
    """Change `old` to `new` on the one line holding `needle`, and nowhere else."""
    lines = text.splitlines(keepends=True)
    hits = [i for i, line in enumerate(lines) if needle in line]
    assert len(hits) == 1, f"{needle!r} matched {len(hits)} lines"
    assert old in lines[hits[0]]
    lines[hits[0]] = lines[hits[0]].replace(old, new)
    return "".join(lines)


# ------------------------------------------------------------------ the happy path


def test_the_mailed_statement_parses_every_row_with_its_direction(mailed):
    out = parse(mailed)
    assert (out.status, out.reason) == (PARSED, "")
    assert out.instrument == "axis-4321"
    assert (out.period_start, out.period_end) == (date(2026, 7, 1), date(2026, 7, 31))
    assert (out.opening_balance, out.closing_balance) == (
        Decimal("10000.00"),
        Decimal("12400.00"),
    )
    assert [(r.occurred_on, r.direction, str(r.amount), str(r.balance_after)) for r in out.rows] == [
        (date(2026, 7, 1), "out", "300.00", "9700.00"),
        (date(2026, 7, 2), "in", "1500.00", "11200.00"),
        (date(2026, 7, 3), "out", "2500.00", "8700.00"),
        (date(2026, 7, 5), "out", "50.00", "8650.00"),
        (date(2026, 7, 5), "out", "50.00", "8600.00"),
        (date(2026, 7, 10), "in", "5000.00", "13600.00"),
        (date(2026, 7, 12), "out", "1200.00", "12400.00"),
    ]
    assert out.statement_id == "axis-4321/2026-07-01..2026-07-31"
    assert all(r.file_sha256 == "fixture-sha" for r in out.rows)


def test_the_opening_balance_is_the_statements_not_page_twos_restatement(mailed):
    # Every page reprints an OPENING BALANCE; page two's is the running balance
    # at the top of page two (8600.00 in this fixture), not the statement's.
    assert "8600.00" in mailed.split("\f")[1]
    assert parse(mailed).opening_balance == Decimal("10000.00")


def test_a_wrapped_narration_is_gathered_onto_its_own_row(mailed):
    rows = {r.occurred_on: r.narration for r in parse(mailed).rows}
    assert rows[date(2026, 7, 1)] == "POS/EXAMPLESTORE/MUMBAI/010726/09:12/ 412345678901"
    assert rows[date(2026, 7, 3)] == "CREDITCARD PAYMENT XXXX 1313"


# --------------------------------------------------------------- §6.2 the proof


def test_a_dropped_row_fails_the_arithmetic_check_and_refuses_the_whole_statement(mailed):
    out = parse(drop_line(mailed, "12/07/2026"))
    assert out.status == REFUSED
    assert out.reason == "arithmetic"
    assert out.rows == ()  # never a partial import
    assert out.diagnostics["rows_read"] == 6
    assert out.diagnostics["difference"] == "-1200.00"


def test_a_closing_balance_that_disagrees_refuses_the_statement(mailed):
    # The rows and their running balances are untouched, so only the aggregate
    # identity of §6.2 can catch this one.
    out = parse(edit_line(mailed, "CLOSING BALANCE", "12400.00", "12500.00"))
    assert (out.status, out.reason) == (REFUSED, "arithmetic")
    assert out.diagnostics["difference"] == "100.00"
    assert out.rows == ()


def test_a_statement_whose_bank_totals_disagree_is_refused(mailed):
    out = parse(mailed.replace("4100.00", "4000.00"))
    assert (out.status, out.reason) == (REFUSED, "totals_mismatch")
    assert out.rows == ()


def test_a_page_of_rows_with_no_header_is_read_through_its_own_geometry(mailed):
    # Never through the PREVIOUS page's map, which is how a credit becomes a
    # debit. Page two of the mailed fixture sits at its own offsets, so
    # dropping its header leaves a page that has to be inferred from its rows.
    without = drop_line(mailed, "Tran Date    Value Date")  # page two's header
    out = parse(without)
    assert (out.status, out.reason) == (PARSED, "")
    assert out.diagnostics["inferred_column_pages"] == [1]
    # ...and reads exactly what the printed header read.
    expected = [(r.occurred_on, r.direction, r.amount) for r in parse(mailed).rows]
    assert [(r.occurred_on, r.direction, r.amount) for r in out.rows] == expected


def test_a_row_dated_outside_the_period_is_refused(mailed):
    out = parse(mailed.replace(" 01/07/2026    01/07/2026", " 01/06/2026    01/06/2026"))
    assert (out.status, out.reason) == (REFUSED, "row_outside_period")
    assert out.diagnostics["outside_dates"] == ["2026-06-01"]


def test_a_row_reprinted_with_an_unmoved_balance_is_counted_once(mailed):
    # A real Axis statement printed the last row of one page again at the top of
    # the next; its own TRANSACTION TOTAL counts it once. The running balance is
    # the discriminator — a reprint does not move it.
    lines = mailed.splitlines(keepends=True)
    row = next(i for i, line in enumerate(lines) if "5000.00" in line and "13600.00" in line)
    lines.insert(row + 1, lines[row])
    out = parse("".join(lines))
    assert (out.status, out.reason) == (PARSED, "")
    assert out.diagnostics["reprinted_rows"] == 1
    assert len(out.rows) == 7


def test_two_errors_that_cancel_are_caught_by_the_running_balance(mailed):
    # Both amounts are overstated by 100, one a debit and one a credit, and the
    # bank's own totals are edited to agree — so the aggregate identity of §6.2
    # holds and only the per-row running balance can see it.
    tampered = edit_line(mailed, " 01/07/2026    01/07/2026", "300.00", "400.00")
    tampered = edit_line(tampered, " 02/07/2026    02/07/2026", "1500.00", "1600.00")
    tampered = edit_line(tampered, "TRANSACTION TOTAL", "4100.00", "4200.00")
    tampered = edit_line(tampered, "TRANSACTION TOTAL", "6500.00", "6600.00")
    out = parse(tampered)
    assert (out.status, out.reason) == (REFUSED, "balance_chain")
    assert out.diagnostics["chain_breaks"] == [0, 1]
    assert out.rows == ()


def test_two_identical_payments_on_one_day_stay_two_rows(mailed):
    same_day = [r for r in parse(mailed).rows if r.occurred_on == date(2026, 7, 5)]
    assert len(same_day) == 2
    assert [str(r.amount) for r in same_day] == ["50.00", "50.00"]
    assert same_day[0].row_id != same_day[1].row_id


# ------------------------------------------------------------ §6.1 identification


def test_a_creditcard_payment_narration_still_identifies_the_current_account(mailed):
    assert "CreditCard Payment XXXX 1313" in mailed  # the trap is really in the file
    out = parse(mailed)
    assert out.instrument == "axis-4321"
    assert out.diagnostics["layout"] == "axis_mailed"
    assert "CREDITCARD PAYMENT XXXX 1313" in {r.narration for r in out.rows}


def test_the_period_comes_from_the_header_never_from_the_subject(mailed):
    # Axis names each monthly statement for the month it was SENT.
    out = parse(mailed, subject=SUBJECT_SAYING_AUGUST)
    assert (out.period_start, out.period_end) == (date(2026, 7, 1), date(2026, 7, 31))
    assert out.statement_id.endswith("2026-07-01..2026-07-31")
    assert out.subject_hint == SUBJECT_SAYING_AUGUST


def test_both_axis_header_formats_identify_the_same_account_and_period(mailed, netbanking):
    one, two = parse(mailed), parse(netbanking)
    assert one.instrument == two.instrument == "axis-4321"
    assert (one.period_start, one.period_end) == (two.period_start, two.period_end)
    assert one.diagnostics["layout"] == "axis_mailed"
    assert two.diagnostics["layout"] == "axis_netbanking"


def test_the_two_layouts_agree_on_row_ids_for_the_same_rows(mailed, netbanking):
    # §8.3: the id is layout-independent, so the mailed lane and a netbanking
    # backfill overlapping the same days dedupe instead of posting twice — even
    # though `pdftotext -layout` truncates the narrations differently.
    overlap = {r.row_id for r in parse(netbanking).rows}
    assert overlap <= {r.row_id for r in parse(mailed).rows}
    assert len(overlap) == 3


def test_the_same_statement_parsed_twice_yields_the_same_ids(mailed):
    assert [r.row_id for r in parse(mailed).rows] == [r.row_id for r in parse(mailed).rows]


def test_a_file_matching_no_anchor_is_unidentified_never_guessed():
    card_like = (
        "AXIS BANK CREDIT CARD STATEMENT\n"
        "Card Number 4514 XXXX XXXX 1313    Statement Period 20/07/2026 to 18/08/2026\n"
        " 22/07/2026   EXAMPLE STORE MUMBAI            1,250.00\n"
    )
    out = parse(card_like)
    assert (out.status, out.reason) == (UNIDENTIFIED, "no_header_anchor")
    assert out.instrument is None and out.rows == ()


def test_two_different_accounts_in_one_file_are_unidentified(mailed):
    misfiled = mailed + "\nSTATEMENT BETWEEN 01/07/2026 AND 31/07/2026 FOR A/C: XXXXXXXXXXX9999\n"
    out = parse(misfiled)
    assert (out.status, out.reason) == (UNIDENTIFIED, "conflicting_headers")
    assert out.rows == ()


def test_the_declared_chart_spelling_wins_over_the_parsed_tail(mailed):
    out = parse(mailed, declared={"assets:bank:axis:04321"})
    assert out.instrument == "axis-04321"
    assert all(r.instrument == "axis-04321" for r in out.rows)


# --------------------------------------------------------------------- §8.3 ids


def test_row_id_keys_on_the_balance_and_not_on_the_narration():
    common = {
        "instrument": "axis-4321",
        "occurred_on": date(2026, 7, 5),
        "direction": "out",
        "amount": Decimal("50.00"),
        "occurrence_index": 0,
    }
    same_balance_other_words = row_id_for(
        **common, balance_after=Decimal("8650.00"), narration="UPI/P2M/1/SHOP"
    )
    assert row_id_for(
        **common, balance_after=Decimal("8650.00"), narration="TRUNCATED BY COLUMN WIDTH"
    ) == same_balance_other_words
    assert (
        row_id_for(**common, balance_after=Decimal("8600.00"), narration="UPI/P2M/1/SHOP")
        != same_balance_other_words
    )


def test_row_id_separates_a_debit_from_a_credit_that_share_everything_else():
    common = {
        "instrument": "axis-4321",
        "occurred_on": date(2026, 7, 5),
        "amount": Decimal("50.00"),
        "balance_after": Decimal("8650.00"),
        "occurrence_index": 0,
    }
    assert row_id_for(**common, direction="out") != row_id_for(**common, direction="in")


def test_a_card_row_falls_back_to_the_narration_and_the_occurrence_index():
    # Cards carry no running balance (§8.3), so two identical purchases on one
    # day are separated only by the occurrence index.
    common = {
        "instrument": "axis-cc-1313",
        "occurred_on": date(2026, 7, 5),
        "direction": "out",
        "amount": Decimal("50.00"),
        "balance_after": None,
        "narration": "EXAMPLE SHOP MUMBAI",
    }
    assert row_id_for(**common, occurrence_index=0) != row_id_for(**common, occurrence_index=1)
    assert row_id_for(**common, occurrence_index=0) != row_id_for(
        **{**common, "narration": "OTHER SHOP"}, occurrence_index=0
    )


# ----------------------------------------------------------------- small helpers


def test_normalise_narration_uppercases_and_collapses_whitespace():
    assert normalise_narration("  UPI/p2a/12   /shop  name\n") == "UPI/P2A/12 /SHOP NAME"


@pytest.mark.parametrize(
    "narration,expected",
    [
        ("UPI/P2A/612345678901/SPECIMEN/EXAM/UPI/", "612345678901"),
        ("UPI/P2M/812345678901/EXAMPLE SHOP", "812345678901"),
        ("NEFT/AXISP00123456/SPECIMEN SUPPLIES", "AXISP00123456"),
        ("IMPS/P2A/612345678901/SPECIMEN", "612345678901"),
        # A POS terminal id is not a reference: pass 1 of the matcher is an
        # exact join, so reading one here mis-attributes a payment.
        ("POS/EXAMPLESTORE/MUMBAI/010726/09:12/412345678901", None),
        ("BRN-CASH DEP SELF", None),
    ],
)
def test_extract_ref_reads_only_real_reference_namespaces(narration, expected):
    assert extract_ref(narration) == expected


def test_a_statement_with_no_balance_anchors_is_refused(mailed):
    out = parse(drop_line(mailed, "CLOSING BALANCE"))
    assert (out.status, out.reason) == (REFUSED, "missing_balance_anchor")
    assert out.rows == ()


def test_a_narration_number_beside_the_amount_column_is_not_read_as_money(mailed):
    # `_columned_numbers` ignores a number that ends near no column. If it were
    # read, the sums would move and §6.2 would refuse the statement.
    with_noise = mailed.replace(
        "                            412345678901",
        "                            REF 99999.99 CONTINUED",
    )
    out = parse(with_noise)
    assert out.status == PARSED
    assert out.diagnostics["withdrawals"] == "4100.00"


def test_statement_id_is_the_account_and_the_period():
    assert (
        statements.statement_id_for("axis-4321", date(2026, 7, 1), date(2026, 7, 31))
        == "axis-4321/2026-07-01..2026-07-31"
    )


# ------------------------------------- the netbanking layout over many pages
#
# The mailed layout reprints its column header on every page. The netbanking
# layout prints it once and then shifts each later page, so 48 of the 49 pages
# of a real FY statement carry rows under no header of their own. Those pages
# get their offsets from their own rows. Where that leaves ONE amount column
# the direction cannot be read from position — `pdftotext` closes an empty
# column up, so a debit-only page is laid out exactly like a credit-only one —
# and the running balance decides instead. Which is why every test below that
# touches an inferred direction ends at the bank's own printed totals: derive a
# direction from the balances and the balance checks agree by construction.


def test_the_multipage_netbanking_statement_parses_every_page(multipage):
    out = parse(multipage)
    assert (out.status, out.reason) == (PARSED, "")
    assert out.instrument == "axis-4321"
    assert (out.opening_balance, out.closing_balance) == (
        Decimal("10000.00"),
        Decimal("500.35"),
    )
    assert [(r.occurred_on.day, r.direction, str(r.amount)) for r in out.rows] == [
        (1, "out", "300.00"),
        (2, "in", "1500.00"),
        (3, "out", "2500.00"),
        (5, "out", "50.00"),
        (6, "in", "200.00"),
        (7, "out", "150.00"),
        (10, "out", "100.00"),
        (11, "out", "25.00"),
        (12, "out", "8574.65"),
        (20, "in", "1000.00"),
        (21, "out", "500.00"),
    ]
    assert out.diagnostics["withdrawals"] == "12199.65"
    assert out.diagnostics["deposits"] == "2700.00"


def test_the_result_names_the_pages_whose_columns_were_inferred(multipage):
    # A reviewer has to be able to see which pages were read through a map the
    # parser worked out and which through one the bank printed.
    out = parse(multipage)
    assert out.diagnostics["inferred_column_pages"] == [1, 2, 3]
    assert out.diagnostics["ambiguous_column_pages"] == [2]


def test_a_printed_header_is_never_second_guessed(mailed):
    out = parse(mailed)
    assert out.diagnostics["inferred_column_pages"] == []
    assert out.diagnostics["ambiguous_column_pages"] == []
    assert out.diagnostics["inferred_direction_rows"] == 0


def test_a_page_with_two_amount_columns_takes_its_direction_from_position(multipage):
    # Pages two and four print a debit column AND a credit column, so position
    # says which side each row is and the balance is left free to check it.
    # Only page three's three rows are settled by the balance.
    out = parse(multipage)
    assert out.diagnostics["inferred_direction_rows"] == 3
    directional = [r for r in out.rows if r.occurred_on.day in (5, 6, 7, 20, 21)]
    assert [r.direction for r in directional] == ["out", "in", "out", "in", "out"]


def test_an_inferred_page_reads_its_narration_and_its_reference(multipage):
    # The narration is display only, but `ref` comes out of it and the matcher
    # joins on `ref` — so a narration column inferred a few characters wrong
    # loses the UPI or NEFT reference on every row of the page. The wrapped
    # lines print above and below their own date line and each lands on the
    # nearer row.
    rows = {r.occurred_on.day: (r.narration, r.ref) for r in parse(multipage).rows}
    assert rows[5] == ("UPI/P2M/712345678901/SHOP /BANK/ABCDEF//1X2/", "712345678901")
    assert rows[6] == ("SPECIMEN SUPPLIES LTD NEFT/AXISP00123456/SUPPLIES", "AXISP00123456")
    assert rows[12] == ("SWEEP TO DEPOSIT IMPS/612345678902/SWEEP", "612345678902")
    assert rows[20] == ("NEFT/AXISP00123457/REFUND", "AXISP00123457")


def test_an_amount_printed_short_of_its_column_still_belongs_to_it(multipage):
    # `pdftotext` lays a proportional font onto a character grid, so a
    # right-aligned column's end offsets move by a character or two from row to
    # row. The 12th prints two characters left of the other two amounts on its
    # page; read as a column of its own it would give that page two amount
    # columns and turn the other two rows into credits.
    out = parse(multipage)
    assert out.diagnostics["ambiguous_column_pages"] == [2]
    twelfth = next(r for r in out.rows if r.occurred_on == date(2026, 7, 12))
    assert (twelfth.direction, str(twelfth.amount)) == ("out", "8574.65")


def test_a_money_shaped_token_in_a_narration_does_not_become_an_amount_column(multipage):
    # The 11th carries `01.02.2024` in its narration, far left of the columns.
    # The geometry reads a row's LAST TWO numbers, so that token cannot make a
    # one-column page look like a two-column one and flip every row's side.
    assert "BILL 01.02.2024 ELEC" in multipage
    out = parse(multipage)
    assert out.diagnostics["ambiguous_column_pages"] == [2]
    eleventh = next(r for r in out.rows if r.occurred_on == date(2026, 7, 11))
    assert (eleventh.direction, str(eleventh.amount)) == ("out", "25.00")


def test_a_balance_below_one_rupee_is_read_and_not_dropped(multipage):
    # Axis prints a sub-rupee running balance without its leading zero (`.35`),
    # which two rows of the real FY2024-25 statement do. Read as no balance,
    # the row is unreadable and the whole statement is refused.
    assert "         .35 " in multipage
    twelfth = next(r for r in parse(multipage).rows if r.occurred_on == date(2026, 7, 12))
    assert twelfth.balance_after == Decimal("0.35")


# ----------------------------------------- the check that has to be able to fail


def flip_one_ambiguous_row(text: str) -> str:
    """Make the 10th read as a CREDIT of 100.00 instead of a debit, by moving
    its balance up instead of down and carrying the shift through every later
    balance. The page prints one amount column, so nothing about the row's
    printed position changes — only the running balance, which is the only
    thing that decides its side."""
    text = edit_line(text, "10-07-2026", "8,600.00", "8,800.00")
    text = edit_line(text, "11-07-2026", "8,575.00", "8,775.00")
    text = edit_line(text, "12-07-2026", "      .35", "   200.35")
    text = edit_line(text, "20-07-2026", "1,000.35", "1,200.35")
    text = edit_line(text, "21-07-2026", "500.35", "700.35")
    return edit_line(text, "CLOSING BALANCE", "500.35", "700.35")


def test_a_wrong_inferred_direction_is_caught_by_the_banks_own_totals(multipage):
    out = parse(flip_one_ambiguous_row(multipage))
    assert (out.status, out.reason) == (REFUSED, "totals_mismatch")
    assert out.rows == ()
    # The direction moved by 100.00, so the printed debit total is 100.00 high
    # and the printed credit total 100.00 low against what was read.
    assert out.diagnostics["withdrawals"] == "12099.65"
    assert out.diagnostics["deposits"] == "2800.00"
    assert out.diagnostics["printed_debit"] == "12199.65"
    assert out.diagnostics["printed_credit"] == "2700.00"


def test_the_balance_checks_cannot_catch_a_wrong_inferred_direction(multipage):
    # The reason the test above has to exist. The direction came OUT of the
    # running balance, so putting it back into the running balance proves
    # nothing: on the same tampered statement §6.2's identity still holds
    # exactly and every row's balance still moves by its own amount. A check
    # that cannot fail is worse than no check, because it reads as safety.
    out = parse(flip_one_ambiguous_row(multipage))
    opening, closing = out.opening_balance, out.closing_balance
    deposits = Decimal(out.diagnostics["deposits"])
    withdrawals = Decimal(out.diagnostics["withdrawals"])
    assert closing - opening == deposits - withdrawals
    assert out.reason != "arithmetic"
    assert out.reason != "balance_chain"


def test_an_ambiguous_page_with_no_printed_totals_is_refused(multipage):
    # No independent evidence, so nothing to check the inference against. The
    # statement is refused rather than read on the strength of a tautology.
    out = parse(drop_line(multipage, "TRANSACTION TOTAL"))
    assert (out.status, out.reason) == (REFUSED, "no_independent_totals")
    assert out.rows == ()
    assert out.diagnostics["inferred_direction_rows"] == 3


def test_a_statement_with_no_inferred_direction_still_needs_no_printed_totals(mailed):
    # The gate is on the inference, not on every statement: where the printed
    # columns say which side a row is, the balance checks are real checks.
    out = parse(drop_line(mailed, "TRANSACTION TOTAL"))
    assert (out.status, out.reason) == (PARSED, "")
    assert len(out.rows) == 7


def test_an_ambiguous_row_whose_balance_fits_neither_side_is_refused(multipage):
    out = parse(edit_line(multipage, "11-07-2026", "8,575.00", "8,570.00"))
    assert (out.status, out.reason) == (REFUSED, "undetermined_direction")
    assert out.rows == ()


# ------------------------------------------- when the geometry will not resolve


def test_a_headerless_page_whose_balances_split_in_two_is_refused(multipage):
    # One row's running balance printed six characters left of the others. The
    # page no longer has one balance column, so it has no readable geometry —
    # and a page with no readable geometry is refused, exactly as a page with
    # no header was before.
    broken = edit_line(
        multipage, "07-07-2026", "150.00             8,700.00", "150.00       8,700.00      "
    )
    out = parse(broken)
    assert (out.status, out.reason) == (REFUSED, "no_column_header")
    assert out.diagnostics["headerless_pages"] == [1]
    assert out.rows == ()


def test_a_headerless_page_with_three_amount_columns_is_refused(multipage):
    # Two amount columns are Debit and Credit. Three are not a layout this
    # parser knows, so it refuses rather than picking two of them.
    # The third row's amount printed eight characters left of the other two.
    broken = edit_line(multipage, "07-07-2026", "        150.00", "150.00        ")
    out = parse(broken)
    assert (out.status, out.reason) == (REFUSED, "no_column_header")
    assert out.diagnostics["headerless_pages"] == [1]


def test_a_zero_amount_row_on_an_ambiguous_page_is_refused_not_guessed(multipage):
    # Nothing moves the balance, so both sides fit and the running balance
    # cannot choose. Adding the row leaves every later balance and both printed
    # totals untouched, so this is the ONE thing the statement now fails on.
    nil = "12-07-2026   REVERSAL NIL VALUE".ljust(61) + "0.00".ljust(13) + ".35 KLYN"
    out = parse(insert_after(multipage, "IMPS/612345678902/SWEEP", nil))
    assert (out.status, out.reason) == (REFUSED, "undetermined_direction")
    assert out.diagnostics["rows_read"] == 12
    assert out.rows == ()


@pytest.mark.parametrize(
    "printed,expected",
    [
        ("8,574.65", ["8574.65"]),
        ("-1,200.00", ["-1200.00"]),
        # Axis drops the leading zero on a balance below one rupee. Read as no
        # number at all, the row loses its running balance and the statement is
        # refused — which is what the real FY2024-25 statement did.
        (".35", ["0.35"]),
        # A date in a narration is one spurious token, never two: without the
        # lookbehind `01.02.2024` also yields `.20`.
        ("BILL 01.02.2024 ELEC", ["1.02"]),
        ("CONSUMER 90000123", []),
    ],
)
def test_what_the_parser_reads_as_a_money_token(printed, expected):
    found = statements._NUM.findall(printed)
    assert [str(statements._amount(token)) for token in found] == expected
