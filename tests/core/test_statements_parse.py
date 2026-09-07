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


def parse(text: str, **kw):
    return parse_axis_statement(text, file_sha256="fixture-sha", **kw)


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


def test_a_page_of_rows_with_no_column_header_is_refused_not_read_through_a_stale_map(mailed):
    without = drop_line(mailed, "Tran Date    Value Date")  # page two's header
    out = parse(without)
    assert (out.status, out.reason) == (REFUSED, "no_column_header")
    assert out.diagnostics["headerless_pages"] == [1]
    assert out.rows == ()


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
