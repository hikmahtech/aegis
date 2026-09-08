"""The statement matcher (spec §8) — passes, determinism, the FX class, the report.

Every fixture is invented. The instrument spellings are the real ones the live
index carries (`card-1313`, `nkgsb-0843`, `axis-1`), because they are what makes
`canonical_instrument` on both sides load-bearing.
"""

from __future__ import annotations

import ast
import re
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio
from aegis.api.models.money import MoneyEvent
from aegis.services import journal_index as ji
from aegis.services import statement_match as sm
from aegis.services import statements
from aegis.services.statement_match import (
    AMBIGUOUS,
    DUPLICATE,
    PASS_REF,
    PASS_WINDOW,
    PASS_WINDOW_NO_INSTRUMENT,
    Candidate,
    match_statements,
)

#: The chart the live books declare, in the spellings hledger accepts.
DECLARED = frozenset(
    {
        "assets:bank:axis:9640",
        "assets:bank:hdfc:1225",
        "assets:bank:nkgsb:843",
        "liabilities:card:axis:1313",
    }
)
JULY = (date(2026, 7, 1), date(2026, 7, 31))


def make_row(
    day: int,
    amount: str,
    *,
    direction: str = "out",
    instrument: str = "axis-9640",
    ref: str | None = None,
    narration: str = "TEST ROW",
    balance: str | None = None,
    statement: str | None = None,
    occurrence: int = 0,
) -> statements.StatementRow:
    occurred = date(2026, 7, day)
    money = Decimal(amount)
    after = Decimal(balance) if balance is not None else None
    return statements.StatementRow(
        row_id=statements.row_id_for(
            instrument=instrument,
            occurred_on=occurred,
            direction=direction,
            amount=money,
            balance_after=after,
            occurrence_index=occurrence,
            narration=narration,
        ),
        instrument=instrument,
        occurred_on=occurred,
        narration=narration,
        ref=ref,
        direction=direction,
        amount=money,
        balance_after=after,
        statement_id=statement or statements.statement_id_for(instrument, *JULY),
        file_sha256="fixture-sha",
    )


def make_candidate(
    msgid: str,
    day: int,
    amount: str,
    *,
    direction: str = "out",
    instrument: str | None = "axis-9640",
    currency: str = "INR",
    entity: str = "personal",
    ref: str | None = None,
    parser: str | None = None,
) -> Candidate:
    return Candidate(
        msgid=msgid,
        entity=entity,
        direction=direction,
        amount=Decimal(amount),
        currency=currency,
        occurred_on=date(2026, 7, day),
        instrument=instrument,
        ref=ref,
        parser=parser,
    )


def run(rows, candidates, **kw):
    kw.setdefault("declared", DECLARED)
    return match_statements(rows, candidates, **kw)


def only(run_result) -> sm.RowOutcome:
    assert len(run_result.outcomes) == 1
    return run_result.outcomes[0]


# ------------------------------------------------------------------ pass 1, ref


def test_pass_1_matches_on_the_reference_alone():
    """The reference is the strongest key: neither the instrument nor the date
    window is required, which is the whole reason it runs first."""
    row = make_row(10, "500.00", ref="123456789012")
    candidate = make_candidate("m/1", 25, "999.00", instrument="hdfc-1225", ref="123456789012")
    outcome = only(run([row], [candidate]))
    assert outcome.matched_pass == PASS_REF
    assert outcome.msgid == "m/1"
    assert outcome.delta_days == -15


def test_pass_1_never_matches_across_direction():
    row = make_row(10, "500.00", direction="out", ref="123456789012")
    candidate = make_candidate("m/1", 10, "500.00", direction="in", ref="123456789012")
    outcome = only(run([row], [candidate]))
    assert outcome.matched_pass is None and outcome.msgid is None


def test_a_row_with_no_reference_does_not_match_a_candidate_with_no_reference():
    """Two absent references are not a match. Comparing them directly makes
    every referenceless row match the first referenceless transaction."""
    row = make_row(10, "500.00", ref=None)
    candidate = make_candidate("m/1", 25, "999.00", instrument="hdfc-1225", ref=None)
    assert only(run([row], [candidate])).matched_pass is None


def test_the_reference_comparison_ignores_case_and_padding():
    row = make_row(10, "500.00", ref="neft0001abcd")
    candidate = make_candidate("m/1", 10, "500.00", ref=" NEFT0001ABCD ")
    assert only(run([row], [candidate])).matched_pass == PASS_REF


def test_two_candidates_sharing_a_reference_are_ambiguous_not_a_coin_toss():
    row = make_row(10, "500.00", ref="123456789012")
    pool = [
        make_candidate("m/1", 10, "500.00", ref="123456789012"),
        make_candidate("m/2", 10, "500.00", ref="123456789012"),
    ]
    outcome = only(run([row], pool))
    assert outcome.skip_reason == AMBIGUOUS and outcome.candidates == ("m/1", "m/2")


# ----------------------------------------------------------------- pass 2, window


def test_pass_2_matches_instrument_direction_amount_and_window():
    row = make_row(10, "500.00")
    outcome = only(run([row], [make_candidate("m/1", 8, "500.00")]))
    assert outcome.matched_pass == PASS_WINDOW
    assert outcome.msgid == "m/1" and outcome.delta_days == 2


def test_pass_2_never_matches_across_direction():
    row = make_row(10, "500.00", direction="out")
    assert only(run([row], [make_candidate("m/1", 10, "500.00", direction="in")])).msgid is None


def test_pass_2_never_matches_a_different_amount():
    row = make_row(10, "500.00")
    assert only(run([row], [make_candidate("m/1", 10, "500.01")])).msgid is None


def test_pass_2_never_matches_another_instrument():
    row = make_row(10, "500.00", instrument="axis-9640")
    assert only(run([row], [make_candidate("m/1", 10, "500.00", instrument="hdfc-1225")])).msgid is None


@pytest.mark.parametrize(("day", "expected"), [(7, PASS_WINDOW), (6, None)])
def test_the_window_is_match_days_and_no_wider(day, expected):
    """±3 days inclusive — `journal_index._MATCH_DAYS`, the one window."""
    assert ji._MATCH_DAYS == 3
    row = make_row(10, "500.00")
    assert only(run([row], [make_candidate("m/1", day, "500.00")])).matched_pass == expected


def test_the_window_can_be_widened_for_the_measurement():
    """§8.2's report needs deltas the lane's own window would never match, or
    the histogram is capped by construction and can never move the constant."""
    row = make_row(20, "500.00")
    candidate = make_candidate("m/1", 10, "500.00")
    assert only(run([row], [candidate])).matched_pass is None
    outcome = only(run([row], [candidate], window_days=30))
    assert outcome.matched_pass == PASS_WINDOW and outcome.delta_days == 10


def test_instruments_are_compared_through_canonical_instrument_on_both_sides():
    """`card-1313` in the index and `axis-cc-1313` on the statement are one card."""
    row = make_row(10, "500.00", instrument="axis-cc-1313")
    candidate = make_candidate("m/1", 10, "500.00", instrument="card-1313")
    assert only(run([row], [candidate])).matched_pass == PASS_WINDOW
    # …and it is the chart that says so. With no chart to hand, the two
    # spellings are two different accounts and must not be merged.
    assert only(match_statements([row], [candidate], declared=())).matched_pass is None


@pytest.mark.parametrize(
    ("row_spelling", "index_spelling"),
    [("nkgsb-843", "nkgsb-0843"), ("nkgsb-0843", "nkgsb-843")],
)
def test_a_padded_instrument_tail_is_the_same_account(row_spelling, index_spelling):
    """Both sides go through `canonical_instrument`, so it does not matter which
    of them carries the pad."""
    row = make_row(10, "500.00", instrument=row_spelling)
    candidate = make_candidate("m/1", 10, "500.00", instrument=index_spelling)
    assert only(run([row], [candidate])).matched_pass == PASS_WINDOW


def test_an_instrument_the_chart_does_not_declare_matches_only_itself():
    """`axis-1` is a real live spelling for nothing the chart declares. It must
    stay itself rather than resolve onto the nearest account."""
    row = make_row(10, "500.00", instrument="axis-9640")
    assert only(run([row], [make_candidate("m/1", 10, "500.00", instrument="axis-1")])).msgid is None


# ----------------------------------------------------------- pass 2b, no instrument


def test_pass_2b_rescues_a_candidate_with_no_instrument():
    """The 12 live blocks posted against `assets:unknown` are what this lane is for."""
    row = make_row(10, "500.00", instrument="hdfc-1225")
    candidate = make_candidate("m/1", 9, "500.00", instrument=None, entity="personal")
    outcome = only(
        run([row], [candidate], entity_for_instrument={"hdfc-1225": "personal"})
    )
    assert outcome.matched_pass == PASS_WINDOW_NO_INSTRUMENT
    assert outcome.msgid == "m/1" and outcome.delta_days == 1


def test_a_null_instrument_candidate_in_the_wrong_entity_does_not_match():
    row = make_row(10, "500.00", instrument="hdfc-1225")
    candidate = make_candidate("m/1", 10, "500.00", instrument=None, entity="hikmah")
    result = run([row], [candidate], entity_for_instrument={"hdfc-1225": "personal"})
    assert only(result).matched_pass is None


def test_pass_2b_is_skipped_and_reported_when_the_account_has_no_entity():
    """No entity means no scope, and an entity-blind match writes a hikmah
    payment into the personal journal. Skipped visibly, never guessed."""
    row = make_row(10, "500.00", instrument="hdfc-1225")
    result = run([row], [make_candidate("m/1", 10, "500.00", instrument=None)])
    assert only(result).matched_pass is None
    assert result.unscoped_instruments == ("hdfc-1225",)


def test_pass_2b_never_takes_a_candidate_that_names_another_instrument():
    """2b is for blocks with NO instrument. A block that names a different
    account is a different account, whatever entity it belongs to."""
    row = make_row(10, "500.00", instrument="hdfc-1225")
    candidate = make_candidate("m/1", 10, "500.00", instrument="axis-9640", entity="personal")
    result = run([row], [candidate], entity_for_instrument={"hdfc-1225": "personal"})
    assert only(result).matched_pass is None


def test_pass_2_is_preferred_over_pass_2b():
    """A candidate that names the instrument wins over one that does not, even
    when the nameless one is closer in time."""
    row = make_row(10, "500.00", instrument="hdfc-1225")
    pool = [
        make_candidate("m/named", 8, "500.00", instrument="hdfc-1225"),
        make_candidate("m/null", 10, "500.00", instrument=None, entity="personal"),
    ]
    result = run([row], pool, entity_for_instrument={"hdfc-1225": "personal"})
    outcome = only(result)
    assert outcome.matched_pass == PASS_WINDOW and outcome.msgid == "m/named"


# --------------------------------------------------------------- pass 3, ambiguity


def test_an_ambiguous_row_matches_nothing_and_promotes_nothing():
    """§9.4 — one of the two candidates IS this row, so choosing adds a third
    copy of money the balance already counts through the candidate."""
    row = make_row(10, "500.00")
    pool = [make_candidate("m/2", 9, "500.00"), make_candidate("m/1", 11, "500.00")]
    result = run([row], pool)
    outcome = only(result)
    assert outcome.skip_reason == AMBIGUOUS
    assert outcome.matched_pass is None and outcome.msgid is None
    assert outcome.candidates == ("m/1", "m/2")  # sorted, so the list is stable
    assert result.claimed == {}
    assert result.summaries[0].ambiguous == ((row.row_id, ("m/1", "m/2")),)
    assert result.summaries[0].unmatched == 0


def test_an_ambiguous_row_does_not_fall_through_to_pass_2b():
    """A weaker pass can only widen a field the row already cannot choose in."""
    row = make_row(10, "500.00", instrument="hdfc-1225")
    pool = [
        make_candidate("m/1", 10, "500.00", instrument="hdfc-1225"),
        make_candidate("m/2", 10, "500.00", instrument="hdfc-1225"),
        make_candidate("m/3", 10, "500.00", instrument=None, entity="personal"),
    ]
    result = run([row], pool, entity_for_instrument={"hdfc-1225": "personal"})
    assert only(result).candidates == ("m/1", "m/2")
    assert result.claimed == {}


# ------------------------------------------------------------------- claiming


def test_a_claimed_transaction_is_not_claimed_twice():
    rows = [make_row(10, "500.00"), make_row(11, "500.00")]
    result = run(rows, [make_candidate("m/1", 10, "500.00")])
    matched = [o for o in result.outcomes if o.matched]
    assert len(matched) == 1 and matched[0].msgid == "m/1"
    assert sum(1 for o in result.outcomes if not o.matched and o.skip_reason is None) == 1
    assert result.claimed == {"m/1": matched[0].row_id}


def test_pass_1_claims_before_pass_2_whatever_the_row_order():
    """Every pass runs over every unresolved row before the next pass starts. Run
    the passes per row instead and the referenceless row, which sorts first,
    takes the transaction the referenced row proves is its own."""
    referenced = make_row(30, "555.00", ref="123456789012")
    windowed = make_row(10, "500.00")
    candidate = make_candidate("m/1", 10, "500.00", ref="123456789012")
    for rows in ([referenced, windowed], [windowed, referenced]):
        result = run(rows, [candidate])
        by_id = {o.row_id: o for o in result.outcomes}
        assert by_id[referenced.row_id].matched_pass == PASS_REF
        assert by_id[windowed.row_id].matched_pass is None


def test_reversing_the_input_row_order_gives_the_same_matches():
    """The order rows arrive in must not decide who claims what. Here the two
    rows compete: fed the other way round without a canonical sort, the day-12
    row reads as ambiguous and the day-10 row takes `m/x`, instead of the
    day-10 row claiming `m/x` and the day-12 row settling on `m/y`."""
    early = make_row(10, "500.00")
    late = make_row(12, "500.00")
    pool = [make_candidate("m/x", 10, "500.00"), make_candidate("m/y", 14, "500.00")]
    forward = run([early, late], pool)
    reverse = run([late, early], pool)
    assert forward.outcomes == reverse.outcomes
    assert forward.claimed == reverse.claimed
    by_id = {o.row_id: o for o in forward.outcomes}
    assert by_id[early.row_id].msgid == "m/x"
    assert by_id[late.row_id].msgid == "m/y"


def test_the_same_row_in_two_statements_is_matched_once():
    """Overlapping statements produce identical `row_id`s for one printed row,
    by design (§8.3). The twin must not claim a second transaction, and must not
    read as an unmatched row in the report either."""
    july = make_row(10, "500.00", balance="1000.00")
    august = make_row(
        10,
        "500.00",
        balance="1000.00",
        statement=statements.statement_id_for("axis-9640", date(2026, 8, 1), date(2026, 8, 31)),
    )
    assert july.row_id == august.row_id
    result = run([july, august], [make_candidate("m/1", 10, "500.00")])
    assert sorted(o.skip_reason or o.matched_pass for o in result.outcomes) == [
        DUPLICATE,
        PASS_WINDOW,
    ]
    assert sum(s.duplicates for s in result.summaries) == 1
    assert sum(s.unmatched for s in result.summaries) == 0


# ------------------------------------------------------- §8.5 foreign currency


def test_a_dollar_journal_candidate_matches_a_rupee_statement_row():
    """`$4.00 @ ₹84.53` is `₹338.12`; the card printed `₹338`. Without this the
    row posts again in rupees and hledger's per-commodity assertion never sees it."""
    row = make_row(10, "338.00", instrument="axis-cc-1313")
    candidate = make_candidate("m/1", 9, "4.00", instrument="axis-cc-1313", currency="USD")
    result = run([row], [candidate], rates={"$": Decimal("84.53")})
    outcome = only(result)
    assert outcome.matched_pass == PASS_WINDOW and outcome.foreign is True
    assert result.summaries[0].foreign == 1
    assert result.missing_rates == ()


def test_a_rupee_match_is_not_reported_as_foreign():
    row = make_row(10, "500.00")
    result = run([row], [make_candidate("m/1", 10, "500.00")])
    assert only(result).foreign is False and result.summaries[0].foreign == 0


@pytest.mark.parametrize(
    ("printed", "matches"),
    [("10500.00", True), ("9500.00", True), ("10500.01", False), ("9499.99", False)],
)
def test_the_foreign_currency_band_is_five_percent(printed, matches):
    row = make_row(10, printed, instrument="axis-cc-1313")
    candidate = make_candidate("m/1", 10, "100.00", instrument="axis-cc-1313", currency="USD")
    outcome = only(run([row], [candidate], rates={"$": Decimal("100")}))
    assert (outcome.matched_pass == PASS_WINDOW) is matches


def test_a_foreign_candidate_with_no_rate_does_not_match_and_is_reported():
    """A missing rate must not read as "there were no foreign transactions"."""
    row = make_row(10, "338.00", instrument="axis-cc-1313")
    candidate = make_candidate("m/1", 10, "4.00", instrument="axis-cc-1313", currency="USD")
    result = run([row], [candidate], rates={})
    assert only(result).matched_pass is None
    assert result.missing_rates == ("USD",)


def test_rates_may_be_keyed_by_the_journal_symbol_or_by_the_iso_code():
    """`books.latest_prices` returns symbols (`£`), the index stores ISO codes."""
    row = make_row(10, "12776.00", instrument="axis-9640", direction="in")
    candidate = make_candidate(
        "m/1", 10, "100.00", instrument="axis-9640", direction="in", currency="GBP"
    )
    for rates in ({"£": Decimal("127.76")}, {"GBP": Decimal("127.76")}):
        assert only(run([row], [candidate], rates=rates)).matched_pass == PASS_WINDOW


def test_a_candidate_with_no_currency_never_matches_a_rupee_row():
    row = make_row(10, "500.00")
    candidate = make_candidate("m/1", 10, "500.00", currency="")
    assert only(run([row], [candidate])).matched_pass is None


# ------------------------------------------------------------- the date-delta report


def test_the_date_delta_report_counts_pass_2_and_2b_never_pass_1():
    """§8.2's deliverable. `journal_index.ref` is filled only by the
    deterministic parsers, so a report that counted pass 1 would describe a
    handful of rows while reading as evidence about all of them."""
    rows = [
        make_row(10, "500.00", ref="123456789012"),
        make_row(11, "600.00"),
        make_row(12, "700.00", instrument="hdfc-1225"),
    ]
    pool = [
        make_candidate("m/ref", 4, "500.00", ref="123456789012"),
        make_candidate("m/win", 9, "600.00"),
        make_candidate("m/null", 12, "700.00", instrument=None, entity="personal"),
    ]
    result = run(rows, pool, entity_for_instrument={"hdfc-1225": "personal"})
    assert sorted(o.matched_pass for o in result.outcomes) == sorted(sm.PASSES)
    report = sm.date_delta_report(result.outcomes)
    assert report == {"axis": {2: 1}, "hdfc": {0: 1}}  # the pass-1 delta of 6 is absent


def test_the_date_delta_report_splits_by_bank():
    rows = [make_row(10, "500.00"), make_row(10, "600.00", instrument="hdfc-1225")]
    pool = [
        make_candidate("m/1", 9, "500.00"),
        make_candidate("m/2", 8, "600.00", instrument="hdfc-1225"),
    ]
    assert sm.date_delta_report(run(rows, pool).outcomes) == {"axis": {1: 1}, "hdfc": {2: 1}}


def test_the_delta_is_signed_statement_date_minus_journal_date():
    """A positive delta means the bank posted after the email announced it."""
    late_bank = only(run([make_row(12, "500.00")], [make_candidate("m/1", 10, "500.00")]))
    early_bank = only(run([make_row(10, "500.00")], [make_candidate("m/1", 12, "500.00")]))
    assert late_bank.delta_days == 2 and early_bank.delta_days == -2


def test_window_coverage_says_what_a_window_would_buy():
    report = {"axis": {0: 5, 2: 2, -1: 1, 9: 3}}
    assert sm.window_coverage(report, 3) == {"axis": (8, 11)}
    assert sm.window_coverage(report, 0) == {"axis": (5, 11)}


def test_bank_of_reads_the_first_segment():
    assert sm.bank_of("axis-cc-1313") == "axis"
    assert sm.bank_of("hdfc-1225") == "hdfc"
    assert sm.bank_of("") == "unknown"


# ------------------------------------------------------------------ the run summary


def test_the_summary_counts_every_row_by_outcome_per_statement():
    hdfc = statements.statement_id_for("hdfc-1225", *JULY)
    rows = [
        make_row(10, "500.00"),  # matched, pass 2
        make_row(11, "900.00"),  # unmatched
        make_row(12, "700.00"),  # ambiguous
        make_row(10, "800.00", instrument="hdfc-1225", statement=hdfc),  # other statement
    ]
    pool = [
        make_candidate("m/1", 10, "500.00"),
        make_candidate("m/a", 12, "700.00"),
        make_candidate("m/b", 12, "700.00"),
        make_candidate("m/2", 10, "800.00", instrument="hdfc-1225"),
    ]
    result = run(rows, pool)
    axis, hdfc_summary = result.summaries
    assert axis.instrument == "axis-9640" and axis.rows == 3
    assert axis.matched == {PASS_REF: 0, PASS_WINDOW: 1, PASS_WINDOW_NO_INSTRUMENT: 0}
    assert axis.matched_total == 1 and axis.unmatched == 1
    assert [msgids for _, msgids in axis.ambiguous] == [("m/a", "m/b")]
    assert hdfc_summary.statement_id == hdfc and hdfc_summary.matched_total == 1


def test_the_journal_pool_is_shared_across_statements():
    """A transaction claimed by the July statement is not offered to August's."""
    august = statements.statement_id_for("axis-9640", date(2026, 8, 1), date(2026, 8, 31))
    rows = [make_row(10, "500.00"), make_row(11, "500.00", statement=august)]
    result = run(rows, [make_candidate("m/1", 10, "500.00")])
    assert sum(s.matched_total for s in result.summaries) == 1
    assert sum(s.unmatched for s in result.summaries) == 1


# ------------------------------------------------------------------- report only


def test_the_matcher_never_writes_anything():
    """Step 3 is report-only: no journal write, no INSERT, no UPDATE. Read from
    the module's own AST rather than trusting the docstring that says so."""
    tree = ast.parse(Path(sm.__file__).read_text(encoding="utf-8"))
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert not called & {
        "post_event",
        "rewrite_event",
        "remove_event",
        "append_rule",
        "execute",
        "executemany",
        "upsert",
        "link",
    }
    literals = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    assert not [s for s in literals if re.search(r"\b(INSERT|UPDATE|DELETE)\b", s)]


# ------------------------------------------------------------------ load_candidates


@pytest_asyncio.fixture(loop_scope="function")
async def _clean(db_pool):
    await db_pool.execute("DELETE FROM finance.journal_index WHERE message_id LIKE 'sm-%'")
    yield
    await db_pool.execute("DELETE FROM finance.journal_index WHERE message_id LIKE 'sm-%'")


def _event(**kw) -> MoneyEvent:
    base = {
        "kind": "transaction",
        "direction": "out",
        "amount": Decimal("500.00"),
        "currency": "INR",
        "payee": "Corner Store",
        "payee_key": "corner store",
        "channel": "upi",
        "instrument": "card-1313",
        "occurred_on": date(2026, 7, 10),
        "entity": "personal",
        "account": "expenses:unknown",
        "parser": "axis_upi",
        "source_class": "bank",
    }
    base.update(kw)
    return MoneyEvent(**base)


@pytest.mark.asyncio
async def test_load_candidates_offers_only_transactions_that_have_a_block(db_pool, _clean):
    """The bank alert and the receipt for one payment are two index rows and
    only the first posts a block. Without `journal_file IS NOT NULL` one payment
    offers two candidates and every such row reads as ambiguous."""
    await ji.upsert(db_pool, "sm-posted/1", "mb", _event(), journal_file="personal/2026.journal")
    await ji.upsert(db_pool, "sm-blockless/1", "mb", _event())
    await ji.upsert(
        db_pool, "sm-due/1", "mb", _event(kind="due"), journal_file="personal/2026.journal"
    )
    loaded = await sm.load_candidates(db_pool, start=JULY[0], end=JULY[1], declared=DECLARED)
    assert [c.msgid for c in loaded] == ["sm-posted/1"]


@pytest.mark.asyncio
async def test_load_candidates_canonicalises_the_instrument_and_widens_the_window(
    db_pool, _clean
):
    await ji.upsert(
        db_pool, "sm-posted/1", "mb", _event(), journal_file="personal/2026.journal"
    )
    await ji.upsert(
        db_pool,
        "sm-posted/2",
        "mb",
        _event(occurred_on=date(2026, 6, 29)),
        journal_file="personal/2026.journal",
    )
    await ji.upsert(
        db_pool,
        "sm-posted/3",
        "mb",
        _event(occurred_on=date(2026, 6, 20)),
        journal_file="personal/2026.journal",
    )
    loaded = await sm.load_candidates(db_pool, start=JULY[0], end=JULY[1], declared=DECLARED)
    assert [c.msgid for c in loaded] == ["sm-posted/1", "sm-posted/2"]
    assert {c.instrument for c in loaded} == {"axis-cc-1313"}
    assert loaded[0].entity == "personal" and loaded[0].currency == "INR"


@pytest.mark.asyncio
async def test_a_loaded_candidate_matches_a_statement_row_end_to_end(db_pool, _clean):
    """The one path that proves the SELECT's column names line up with the
    matcher's — a typo in either is invisible to every test above."""
    await ji.upsert(
        db_pool,
        "sm-posted/1",
        "mb",
        _event(instrument="nkgsb-0843", occurred_on=date(2026, 7, 9)),
        journal_file="personal/2026.journal",
    )
    loaded = await sm.load_candidates(db_pool, start=JULY[0], end=JULY[1], declared=DECLARED)
    row = make_row(10, "500.00", instrument="nkgsb-843")
    outcome = only(run([row], loaded))
    assert outcome.matched_pass == PASS_WINDOW and outcome.delta_days == 1


def test_a_card_used_for_both_entities_matches_candidates_from_both():
    """An account does not have one entity.

    Measured on production 2026-09-07: `axis-cc-1313` carries 6 hikmah and 5
    personal transactions. With a single declared entity, pass 2b saw only the
    half that matched it — and pass 2b exists precisely to reach blockless
    rows no instrument-aware pass can see, so declaring either value silently
    threw away half the card's reach.
    """
    rows = [
        make_row(10, "500.00", instrument="axis-cc-1313"),
        make_row(11, "700.00", instrument="axis-cc-1313"),
    ]
    pool = [
        make_candidate("m/personal", 10, "500.00", instrument=None, entity="personal"),
        make_candidate("m/hikmah", 11, "700.00", instrument=None, entity="hikmah"),
    ]
    result = run(rows, pool, entity_for_instrument={"axis-cc-1313": ["personal", "hikmah"]})
    got = {o.msgid for o in result.outcomes if o.matched}
    assert got == {"m/personal", "m/hikmah"}
    assert all(o.matched_pass == PASS_WINDOW_NO_INSTRUMENT for o in result.outcomes)
    assert result.unscoped_instruments == ()


def test_a_declared_set_still_excludes_an_entity_not_in_it():
    """Widening is per account, not a general loosening: a set of one is still
    a scope, and a three-entity world does not become entity-blind because one
    card is shared."""
    row = make_row(10, "500.00", instrument="axis-cc-1313")
    candidate = make_candidate("m/1", 10, "500.00", instrument=None, entity="hikmah")
    result = run([row], [candidate], entity_for_instrument={"axis-cc-1313": ["personal"]})
    assert only(result).matched_pass is None


def test_an_empty_entity_set_is_no_declaration_at_all():
    """`{"axis-cc-1313": []}` reads as "I have not decided yet", and the wrong
    reading of it is "match anything" — which is how a hikmah payment lands in
    `personal/2026.journal`. It must behave exactly like the missing key:
    pass 2b skipped, and the instrument reported."""
    row = make_row(10, "500.00", instrument="axis-cc-1313")
    candidate = make_candidate("m/1", 10, "500.00", instrument=None, entity="personal")
    result = run([row], [candidate], entity_for_instrument={"axis-cc-1313": []})
    assert only(result).matched_pass is None
    assert result.unscoped_instruments == ("axis-cc-1313",)


def test_pass_1_will_not_match_a_real_reference_belonging_to_another_payment():
    """A reference is issued by someone else's system and arrives in mail this
    code does not control, so "these digits are in the email" is never "these
    digits name THIS payment". A refund quoting the original UTR, a card
    summary listing several RRNs, a merchant echoing a previous order's bank
    ref — each puts a real reference for a different payment on a block, and
    `llm._ref_from_body` can only check the characters are present.

    Without the amount check this block wins pass 1 outright: only 4 of 13
    deterministic parsers set `ref`, so the genuine block usually has none to
    compete with. Pass 1 outranks every later pass, so step 5 would promote the
    wrong block to `*` and post the real row again — the money counted twice, by
    the pass that is meant to be the exact one.
    """
    row = make_row(10, "500.00", instrument="hdfc-1225", ref="526112345678")
    wrong = make_candidate(
        "m/other", 10, "9500.00", instrument="hdfc-1225", ref="526112345678", parser="llm"
    )
    assert only(run([row], [wrong])).matched_pass is None

    # And the scope of that rule: a reference a deterministic parser lifted from
    # a bank's own structured alert IS this payment's reference, so pass 1 still
    # matches on it alone — a card auth and its settlement can differ by a tip.
    parsed = make_candidate(
        "m/parsed", 10, "9500.00", instrument="hdfc-1225", ref="526112345678",
        parser="hdfc_upi",
    )
    assert only(run([row], [parsed])).matched_pass == PASS_REF


def test_pass_1_still_matches_when_the_amount_agrees():
    """The pin that keeps the test above honest: the amount check must not have
    simply disabled pass 1."""
    row = make_row(10, "500.00", instrument="hdfc-1225", ref="526112345678")
    right = make_candidate(
        "m/real", 10, "500.00", instrument="hdfc-1225", ref="526112345678", parser="llm"
    )
    outcome = only(run([row], [right]))
    assert outcome.matched_pass == PASS_REF and outcome.msgid == "m/real"


def test_a_reference_spelt_with_the_bank_s_own_spacing_still_joins():
    """`_norm_ref` runs on both sides. The extractor stores what the mail
    printed (`llm._ref_from_body` verifies on alphanumerics but keeps the
    string verbatim), while a statement narration carries bare digits — so
    without a shared normalisation pass 1 silently misses every LLM-sourced
    reference, which is indistinguishable from having no counterpart."""
    row = make_row(10, "500.00", instrument="hdfc-1225", ref="526112345678")
    spaced = make_candidate(
        "m/real", 10, "500.00", instrument="hdfc-1225", ref="5261-1234 5678", parser="llm"
    )
    assert only(run([row], [spaced])).matched_pass == PASS_REF

    # Only punctuation is noise. A leading label is NOT stripped, and must not
    # be: real references begin with letters — a NEFT UTR like `SBIN0000123456`,
    # the Axis remittance SWIFT ref `GBC…` — so an alpha-prefix rule would
    # quietly corrupt them into a different number.
    labelled = make_candidate(
        "m/labelled", 10, "500.00", instrument="hdfc-1225", ref="UTR 5261-1234 5678",
        parser="llm",
    )
    # It does not join in pass 1 — and pass 2 still catches it on instrument,
    # amount and date, which is the fallback working as designed rather than a
    # lost row.
    assert only(run([row], [labelled])).matched_pass == PASS_WINDOW
