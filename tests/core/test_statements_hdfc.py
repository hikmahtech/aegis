"""HDFC SmartStatement parsing (spec §6.1, §6.2, §8.3).

The fixture is structurally faithful and numerically false: the real header
block, the real `<select><option>` account number, the real nine-column table
with its two unlabelled trailing columns, `Date` in slashes and `Value date` in
dashes — and invented numbers, names and account digits throughout. Validated
against the three real statements while this was written; nothing derived from
one is committed.

Every test here was proved falsifiable by breaking the code it covers and
watching it fail.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from aegis.services.statements import PARSED, REFUSED, UNIDENTIFIED, row_id_for
from aegis.services.statements_hdfc import (
    REQUIRED_COLUMNS,
    extract_ref,
    parse_hdfc_statement,
)

FIXTURES = Path(__file__).parent / "fixtures" / "statements"
#: HDFC mails the statement well after the period it covers, so the subject is
#: always a month ahead of the truth. It is recorded and never read (§6.1).
SUBJECT_SAYING_AUGUST = "HDFC Bank - Your Account Statement for August 2026"


@pytest.fixture
def html() -> str:
    return (FIXTURES / "hdfc_smartstatement.html").read_text()


def parse(html: str, **kw):
    return parse_hdfc_statement(html, file_sha256="fixture-sha", **kw)


# ------------------------------------------------------------------- mutations


def swap(html: str, old: str, new: str, *, count: int = 1) -> str:
    """Replace `old` exactly `count` times — a mutation that silently matched
    nothing would make the test it powers prove nothing."""
    assert html.count(old) == count, f"{old!r} appears {html.count(old)}x, expected {count}"
    return html.replace(old, new)


def drop_row(html: str, needle: str) -> str:
    """Remove the one `<tr>` holding `needle` — a transaction lost in the parse."""
    rows = re.findall(r"<tr>.*?</tr>", html, re.S)
    hits = [r for r in rows if needle in r]
    assert len(hits) == 1, f"{needle!r} matched {len(hits)} rows"
    return html.replace(hits[0], "")


def edit_cell(html: str, needle: str, old: str, new: str) -> str:
    """Change `old` to `new` inside the one row holding `needle`."""
    rows = re.findall(r"<tr>.*?</tr>", html, re.S)
    hits = [r for r in rows if needle in r]
    assert len(hits) == 1, f"{needle!r} matched {len(hits)} rows"
    assert old in hits[0], f"{old!r} not in the {needle!r} row"
    return html.replace(hits[0], hits[0].replace(old, new, 1))


# --------------------------------------------------------------- the happy path


def test_parses_every_row(html):
    parsed = parse(html)
    assert parsed.status == PARSED
    assert parsed.reason == ""
    assert len(parsed.rows) == 6


def test_identifies_the_account_and_the_period_from_the_header(html):
    parsed = parse(html, subject=SUBJECT_SAYING_AUGUST)
    assert parsed.instrument == "hdfc-9999"
    assert parsed.period_start == date(2026, 7, 12)
    assert parsed.period_end == date(2026, 8, 11)
    # Recorded for the digest, never read: the subject says August, the
    # statement covers 12 July to 11 August.
    assert parsed.subject_hint == SUBJECT_SAYING_AUGUST
    assert parsed.statement_id == "hdfc-9999/2026-07-12..2026-08-11"


def test_reads_opening_and_closing_from_the_summary_block(html):
    parsed = parse(html)
    assert parsed.opening_balance == Decimal("10000.00")
    assert parsed.closing_balance == Decimal("102650.00")


def test_directions_and_amounts(html):
    parsed = parse(html)
    assert [(r.direction, str(r.amount)) for r in parsed.rows] == [
        ("out", "250.00"),
        ("in", "100000.00"),
        ("out", "5000.00"),
        ("out", "2000.00"),
        ("out", "50.00"),
        ("out", "50.00"),
    ]


def test_running_balances(html):
    parsed = parse(html)
    assert [str(r.balance_after) for r in parsed.rows] == [
        "9750.00",
        "109750.00",
        "104750.00",
        "102750.00",
        "102700.00",
        "102650.00",
    ]


def test_narration_is_normalised(html):
    parsed = parse(html)
    # `<br>` becomes a space, `&nbsp;&nbsp;` collapses to one, everything upper.
    assert parsed.rows[1].narration.endswith("ACCOUNTHOLDER N123456789012345")
    assert parsed.rows[4].narration == "SPECIMEN LOCKER CHARGE INCL GST"
    assert all(r.narration == r.narration.upper() for r in parsed.rows)


def test_every_row_carries_the_statement_and_file_identity(html):
    parsed = parse(html)
    assert {r.statement_id for r in parsed.rows} == {parsed.statement_id}
    assert {r.file_sha256 for r in parsed.rows} == {"fixture-sha"}
    assert {r.instrument for r in parsed.rows} == {"hdfc-9999"}


def test_diagnostics_report_what_was_read(html):
    parsed = parse(html)
    assert parsed.diagnostics["rows_read"] == 6
    assert parsed.diagnostics["deposits"] == "100000.00"
    assert parsed.diagnostics["withdrawals"] == "7350.00"
    assert parsed.diagnostics["value_date_differs"] == 5
    assert parsed.diagnostics["refs_found"] == 4


# ------------------------------------------------------- §8.3 the row identity


def test_row_ids_are_distinct(html):
    parsed = parse(html)
    assert len({r.row_id for r in parsed.rows}) == 6


def test_row_id_is_built_from_the_transaction_date(html):
    """§8.3 verbatim, and the value date is 13-07-2026 on this row — so an id
    built from the value date would not be this one."""
    parsed = parse(html)
    first = parsed.rows[0]
    assert first.row_id == row_id_for(
        instrument="hdfc-9999",
        occurred_on=date(2026, 7, 12),
        direction="out",
        amount=Decimal("250.00"),
        balance_after=Decimal("9750.00"),
        occurrence_index=0,
        narration=first.narration,
    )
    assert first.row_id != row_id_for(
        instrument="hdfc-9999",
        occurred_on=date(2026, 7, 13),  # the value date
        direction="out",
        amount=Decimal("250.00"),
        balance_after=Decimal("9750.00"),
        occurrence_index=0,
        narration=first.narration,
    )


def test_the_same_statement_twice_gives_the_same_ids(html):
    assert [r.row_id for r in parse(html).rows] == [r.row_id for r in parse(html).rows]


def test_two_identical_payments_stay_two_rows(html):
    """The two ₹50 debits on different days are distinct rows, and so are their
    ids — money must never collapse out of the books."""
    parsed = parse(html)
    fifties = [r for r in parsed.rows if r.amount == Decimal("50.00")]
    assert len(fifties) == 2
    assert fifties[0].row_id != fifties[1].row_id


# -------------------------------------------------- the value date is not the date


def test_occurred_on_is_the_date_column(html):
    parsed = parse(html)
    assert [r.occurred_on.isoformat() for r in parsed.rows] == [
        "2026-07-12",
        "2026-07-15",
        "2026-07-18",
        "2026-07-20",
        "2026-07-25",
        "2026-07-28",
    ]


def test_a_far_away_value_date_changes_nothing(html):
    """The strongest form of "never the value date": move every value date to
    2020, outside the statement's own period. A parser reading that column
    refuses the statement with `row_outside_period`; this one does not notice."""
    moved = re.sub(r'(<td class="c3">\d{2}-\d{2})-\d{4}(</td>)', r"\g<1>-2020\g<2>", html)
    assert moved != html
    parsed = parse(moved)
    assert parsed.status == PARSED
    assert parsed.rows[0].occurred_on == date(2026, 7, 12)


# --------------------------------------------- the Narration trap (the column name)


def test_the_pdf_column_name_is_refused_by_name(html):
    """`Narration` is the PDF statement's wording. Matching it here finds zero
    rows, and a zero-row statement whose arithmetic is trivially true is the
    silent wrong number this lane exists to prevent — so it must be REFUSED,
    with the missing label named."""
    renamed = swap(html, ">Transaction details<", ">Narration<")
    parsed = parse(renamed)
    assert parsed.status == REFUSED
    assert parsed.reason == "unknown_columns"
    assert parsed.diagnostics["missing_columns"] == ["transaction details"]
    assert parsed.rows == ()


def test_a_refusal_is_not_an_empty_success(html):
    """A statement that genuinely holds no transactions parses, with no rows.
    The `Narration` one must not be mistaken for it: identical row count, and
    the two outcomes still have to be told apart by `status`."""
    empty = swap(html, '"PlainText1550013_1">7,350.00', '"PlainText1550013_1">0.00')
    empty = swap(empty, '"PlainText1550013_3">1,00,000.00', '"PlainText1550013_3">0.00')
    empty = swap(empty, '"PlainText1553165">1,02,650.00', '"PlainText1553165">10,000.00')
    empty = swap(empty, '"PlainText1553166">5', '"PlainText1553166">0')
    empty = swap(empty, '"PlainText1553167">1', '"PlainText1553167">0')
    empty = re.sub(r"<tbody>.*?</tbody>", "<tbody></tbody>", empty, flags=re.S)
    parsed = parse(empty)
    assert parsed.status == PARSED
    assert parsed.rows == ()

    renamed = parse(swap(html, ">Transaction details<", ">Narration<"))
    assert renamed.rows == ()
    assert renamed.status != parsed.status


#: (the `<th>` class in the fixture, the label the parser requires).
_HEADER_CELLS = (
    ("th0", "date"),
    ("th1", "transaction details"),
    ("th2", "cheque/ref no"),
    ("th3", "value date"),
    ("th4", "withdrawal"),
    ("th5", "deposit"),
    ("th6", "closing balance"),
)


def test_the_header_cells_cover_every_required_column():
    """The parametrised test below is only as good as this list."""
    assert {label for _, label in _HEADER_CELLS} == set(REQUIRED_COLUMNS)


@pytest.mark.parametrize(("th_class", "column"), _HEADER_CELLS)
def test_every_required_column_is_required(html, th_class, column):
    renamed = re.sub(rf'<th class="{th_class}">[^<]*</th>', f'<th class="{th_class}">COLX</th>', html)
    assert renamed != html
    parsed = parse(renamed)
    assert parsed.status == REFUSED
    assert parsed.reason == "unknown_columns"
    assert parsed.diagnostics["missing_columns"] == [column]


# ---------------------------------------------------- §6.2 the arithmetic check


def test_a_dropped_row_is_refused(html):
    parsed = parse(drop_row(html, "CreditCard Payment"))
    assert parsed.status == REFUSED
    # Named, so the balance-chain check below cannot quietly stand in for the
    # §6.2 identity and let a deleted arithmetic check keep this test green.
    assert parsed.reason == "arithmetic"
    assert parsed.rows == ()
    assert parsed.diagnostics["difference"] == "-5000.00"


def test_a_dropped_row_is_refused_without_the_printed_totals(html):
    """The identity does the work on its own: strip the bank's printed totals
    and counts, drop a row, and it is still refused."""
    stripped = swap(html, "Debit Amount", "Debit Total Not Printed")
    stripped = swap(stripped, "Credit Amount", "Credit Total Not Printed")
    stripped = swap(stripped, "Debit Count", "Debit Rows Not Printed")
    stripped = swap(stripped, "Credit Count", "Credit Rows Not Printed")
    assert parse(stripped).status == PARSED  # the totals are optional
    parsed = parse(drop_row(stripped, "CreditCard Payment"))
    assert parsed.status == REFUSED
    assert parsed.reason == "arithmetic"


def test_a_changed_closing_balance_is_refused(html):
    parsed = parse(swap(html, '"PlainText1553165">1,02,650.00', '"PlainText1553165">1,02,660.00'))
    assert parsed.status == REFUSED
    assert parsed.reason == "arithmetic"


def test_a_changed_opening_balance_is_refused(html):
    parsed = parse(swap(html, ">10,000.00<", ">11,000.00<"))
    assert parsed.status == REFUSED
    assert parsed.reason == "arithmetic"


def test_the_printed_totals_must_agree(html):
    """Changing a printed total alone leaves the identity intact, so only the
    bank's own totals can catch it."""
    parsed = parse(swap(html, ">7,350.00<", ">7,300.00<"))
    assert parsed.status == REFUSED
    assert parsed.reason == "totals_mismatch"


def test_the_printed_counts_must_agree(html):
    parsed = parse(swap(html, '"PlainText1553166">5<', '"PlainText1553166">4<'))
    assert parsed.status == REFUSED
    assert parsed.reason == "count_mismatch"


def test_two_cancelling_errors_are_caught_by_the_balance_chain(html):
    """The one failure the aggregate cannot see. Swap two INTERIOR running
    balances: the opening, the closing, the totals and the counts all still
    agree, and only the row-by-row chain notices."""
    broken = edit_cell(html, "CreditCard Payment", ">1,04,750.00<", ">1,02,750.00<")
    broken = edit_cell(broken, "SPECIMEN ATM", ">1,02,750.00<", ">1,04,750.00<")
    parsed = parse(broken)
    assert parsed.status == REFUSED
    assert parsed.reason == "balance_chain"


def test_a_row_dated_outside_the_period_is_refused(html):
    parsed = parse(edit_cell(html, "LOCKER CHARGE", ">25/07/2026<", ">25/09/2026<"))
    assert parsed.status == REFUSED
    assert parsed.reason == "row_outside_period"
    assert parsed.diagnostics["outside_dates"] == ["2026-09-25"]


def test_a_missing_opening_balance_is_refused(html):
    parsed = parse(swap(html, "Opening Balance", "Balance Brought Forward"))
    assert parsed.status == REFUSED
    assert parsed.reason == "missing_balance_anchor"


def test_two_different_closing_balances_are_refused(html):
    """Printed twice with different values is an ambiguity, and §8.1's rule is
    that an ambiguity is never resolved by guessing."""
    doubled = swap(
        html,
        '<div class="col-md-3"><p id="PlainText553165">Closing Balance</p></div>',
        '<div class="col-md-3"><p id="PlainText553165">Closing Balance</p></div>'
        '<div class="col-md-3"><p id="X">99,999.00</p></div>'
        '<div class="col-md-3"><p id="Y">Closing Balance</p></div>',
    )
    parsed = parse(doubled)
    assert parsed.status == REFUSED
    assert parsed.reason == "missing_balance_anchor"


def test_every_refusal_carries_no_rows(html):
    for mutated in (
        drop_row(html, "CreditCard Payment"),
        swap(html, ">Transaction details<", ">Narration<"),
        swap(html, ">7,350.00<", ">7,300.00<"),
        swap(html, "Opening Balance", "Balance Brought Forward"),
    ):
        parsed = parse(mutated)
        assert parsed.status == REFUSED
        assert parsed.rows == ()


# ------------------------------------------------------- §6.1 the header anchors


def test_a_narration_that_looks_like_a_period_is_ignored(html):
    """Anchors are read from outside the table only. A narration is the one
    string an attacker — or a merchant with a sense of humour — controls."""
    trapped = edit_cell(
        html,
        "LOCKER CHARGE",
        "SPECIMEN LOCKER CHARGE",
        "Statement Period:From 01/01/2020 To 31/01/2020 LOCKER CHARGE",
    )
    parsed = parse(trapped)
    assert parsed.status == PARSED
    assert parsed.period_start == date(2026, 7, 12)


def test_a_narration_that_looks_like_an_account_label_is_ignored(html):
    trapped = edit_cell(
        html,
        "LOCKER CHARGE",
        '<td class="c2">000123</td>',
        '<td class="c2">Account number</td>',
    )
    trapped = edit_cell(trapped, "LOCKER CHARGE", ">50.00<", ">50.00<")
    parsed = parse(trapped)
    assert parsed.status == PARSED
    assert parsed.instrument == "hdfc-9999"


def test_conflicting_account_numbers_are_unidentified(html):
    conflicted = swap(
        html,
        '<option value="0">50100999999999</option>',
        '<option value="0">50100888888888</option>',
        count=2,
    )
    # both replaced -> still agreeing; put one back so they disagree
    conflicted = conflicted.replace(
        '<option value="0">50100888888888</option>',
        '<option value="0">50100999999999</option>',
        1,
    )
    parsed = parse(conflicted)
    assert parsed.status == UNIDENTIFIED
    assert parsed.reason == "conflicting_accounts"
    assert parsed.rows == ()


def test_a_select_listing_two_accounts_is_unidentified(html):
    """A dropdown that offers a choice is not an identification."""
    two = swap(
        html,
        '<option value="0">50100999999999</option>\n        </select>\n      </div>\n    </div>\n    <div id="DIV_4003_START">',
        '<option value="0">50100999999999</option>\n'
        '          <option value="1">50100777777777</option>\n'
        "        </select>\n      </div>\n    </div>\n    <div id=\"DIV_4003_START\">",
    )
    parsed = parse(two)
    assert parsed.status == UNIDENTIFIED
    assert parsed.reason == "conflicting_accounts"


def test_conflicting_periods_are_unidentified(html):
    conflicted = swap(
        html,
        '<p id="PlainText553699">Statement Period:From 12/07/2026 To 11/08/2026</p>',
        '<p id="PlainText553699">Statement Period:From 12/07/2026 To 11/08/2026</p>'
        '<p id="PlainText553698">Statement Period:From 12/06/2026 To 11/07/2026</p>',
    )
    parsed = parse(conflicted)
    assert parsed.status == UNIDENTIFIED
    assert parsed.reason == "conflicting_periods"


def test_a_missing_period_is_unidentified(html):
    parsed = parse(swap(html, "Statement Period:From 12/07/2026 To 11/08/2026", "&nbsp;"))
    assert parsed.status == UNIDENTIFIED
    assert parsed.reason == "no_period_anchor"


def test_a_missing_account_is_unidentified(html):
    parsed = parse(swap(html, "Account number", "Account label", count=2))
    assert parsed.status == UNIDENTIFIED
    assert parsed.reason == "no_account_anchor"


def test_nothing_at_all_is_unidentified():
    parsed = parse("<html><body><p>Something else entirely</p></body></html>")
    assert parsed.status == UNIDENTIFIED
    assert parsed.reason == "no_header_anchor"
    assert parsed.rows == ()


def test_a_reversed_period_is_unidentified(html):
    parsed = parse(
        swap(html, "Period:From 12/07/2026 To 11/08/2026", "Period:From 11/08/2026 To 12/07/2026")
    )
    assert parsed.status == UNIDENTIFIED
    assert parsed.reason == "reversed_period"


# ------------------------------------------------------------ finding the table


def test_the_table_id_is_not_the_selector(html):
    """`id="Table76"` is generated by HDFC's templating, not a contract. Keying
    on it would turn a re-render into an empty statement that reads as a quiet
    success."""
    renamed = swap(html, 'id="Table76"', 'id="Table91"')
    renamed = swap(renamed, 'class="table table-striped table-bordered"', 'class="tbl"')
    parsed = parse(renamed)
    assert parsed.status == PARSED
    assert len(parsed.rows) == 6


def test_a_column_added_in_front_shifts_nothing(html):
    """Columns are read by label, so a new column is ignored rather than moving
    every value one place."""
    shifted = swap(html, '<th class="th0">Date</th>', '<th class="thx">COL0</th><th class="th0">Date</th>')
    shifted = shifted.replace('<td class="c0">', '<td class="cx">x</td><td class="c0">')
    parsed = parse(shifted)
    assert parsed.status == PARSED
    assert [str(r.amount) for r in parsed.rows][:2] == ["250.00", "100000.00"]


def test_a_wrapped_column_header_is_still_matched(html):
    """`Cheque/Ref No` is printed across two lines in the fixture, as generated
    markup wraps a long cell. The label has to be read whitespace-collapsed or
    the table is not found at all — and nothing downstream repairs a header."""
    assert "Cheque/Ref\n            No" in html
    assert parse(html).status == PARSED

    unwrapped = swap(html, "Cheque/Ref\n            No", "Cheque/Ref No")
    assert parse(unwrapped).status == PARSED


def test_a_nested_table_does_not_merge_its_rows(html):
    nested = edit_cell(
        html,
        "CreditCard Payment",
        "CreditCard Payment XXXX 1313",
        "CreditCard Payment XXXX 1313<table><tr><td>12/07/2026</td><td>x</td></tr></table>",
    )
    parsed = parse(nested)
    assert parsed.status == PARSED
    assert len(parsed.rows) == 6


def test_no_table_at_all(html):
    parsed = parse(re.sub(r'<table class="table.*?</table>', "", html, flags=re.S))
    assert parsed.status == REFUSED
    assert parsed.reason == "no_transaction_table"


# --------------------------------------------------------------------- the ref


def test_the_ref_comes_from_the_bank_s_own_column(html):
    parsed = parse(html)
    assert parsed.rows[0].ref == "987654321012"
    assert parsed.rows[1].ref == "123456789012345"


def test_the_ref_is_read_when_only_the_column_has_it(html):
    """The card payment's narration carries no reference at all, so this value
    can only have come from the `Cheque/Ref No` column."""
    parsed = parse(html)
    assert parsed.rows[2].ref == "556677889900"


def test_the_ref_falls_back_to_the_narration(html):
    """The column is `-` on an ATM withdrawal; the narration still carries the
    reference."""
    parsed = parse(html)
    assert parsed.rows[3].ref == "412345678901"


def test_the_column_wins_over_the_narration(html):
    """The bank's own labelled field beats a regex over prose, and the two are
    made to disagree here so only one of them can be the source."""
    both = edit_cell(
        html,
        "CreditCard Payment",
        "CreditCard Payment XXXX 1313",
        "CreditCard Payment 111222333444 XXXX 1313",
    )
    assert parse(both).rows[2].ref == "556677889900"


def test_a_cheque_number_is_not_a_ref(html):
    """Six digits is a cheque number. Pass 1 of the matcher is an exact join, so
    reading one as a reference mis-attributes a payment."""
    parsed = parse(html)
    assert parsed.rows[4].ref is None
    assert extract_ref("000123", "SPECIMEN LOCKER CHARGE INCL GST") is None


def test_a_row_with_no_reference_anywhere_has_none(html):
    parsed = parse(html)
    assert parsed.rows[5].ref is None


def test_two_candidate_refs_in_one_cell_are_an_ambiguity():
    assert extract_ref("987654321012 123456789012", "") is None


# ------------------------------------------------------------- unreadable rows


def test_a_row_with_both_a_withdrawal_and_a_deposit_is_refused(html):
    parsed = parse(edit_cell(html, "CreditCard Payment", '<td class="c5">-</td>', '<td class="c5">5,000.00</td>'))
    assert parsed.status == REFUSED
    assert parsed.reason == "unreadable_row"


def test_a_row_with_neither_is_refused(html):
    parsed = parse(edit_cell(html, "CreditCard Payment", '<td class="c4">5,000.00</td>', '<td class="c4">-</td>'))
    assert parsed.status == REFUSED
    assert parsed.reason == "unreadable_row"


def test_a_negative_amount_is_refused(html):
    parsed = parse(edit_cell(html, "CreditCard Payment", ">5,000.00<", ">-5,000.00<"))
    assert parsed.status == REFUSED
    assert parsed.reason == "unreadable_row"


def test_an_unreadable_amount_is_refused(html):
    parsed = parse(edit_cell(html, "CreditCard Payment", ">5,000.00<", ">5.000,00 Dr<"))
    assert parsed.status == REFUSED
    assert parsed.reason == "unreadable_row"


def test_an_unreadable_date_is_refused(html):
    parsed = parse(edit_cell(html, "CreditCard Payment", ">18/07/2026<", ">18 July 2026<"))
    assert parsed.status == REFUSED
    assert parsed.reason == "unreadable_row"


def test_an_empty_row_is_skipped_but_a_dateless_row_with_money_is_refused(html):
    """An entirely empty row is a spacer and carries no money, so skipping it is
    safe. A row with no date but an amount in it is not a spacer, and skipping
    THAT would drop money the §6.2 check can no longer see — it only ever sums
    the rows that were read. So it is refused instead."""
    spacer = (
        '<tr><td class="c0"></td><td class="c1"></td><td class="c2"></td>'
        '<td class="c3"></td><td class="c4"></td><td class="c5"></td>'
        '<td class="c6"></td><td class="c7"></td><td class="c8"></td></tr>'
    )
    assert len(parse(swap(html, "<tbody>", "<tbody>" + spacer)).rows) == 6

    with_money = spacer.replace('<td class="c4"></td>', '<td class="c4">99.00</td>')
    parsed = parse(swap(html, "<tbody>", "<tbody>" + with_money))
    assert parsed.status == REFUSED
    assert parsed.reason == "unreadable_row"
