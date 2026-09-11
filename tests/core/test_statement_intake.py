"""Step 7 — the Drive walk that fills `finance.statement_rows` (spec §4.1, §6.1).

The fixtures are structurally faithful and numerically invented, which is the
rule for this lane: a real statement carries an account number, a customer id
and a PAN in the clear.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from aegis.services import statement_intake as si
from aegis.services.statement_intake import FileOutcome, IntakeReport
from aegis.services.statements import (
    PARSED,
    REFUSED,
    ParsedStatement,
    StatementRow,
    parse_axis_statement,
    row_id_for,
)
from aegis.services.statements_axis_card import LAYOUT as CARD_LAYOUT
from aegis.services.statements_axis_card import parse_axis_card_statement

FIXTURES = Path(__file__).parent / "fixtures" / "statements"


def _row(day: int, amount: str, *, instrument="hdfc-1225", direction="out",
         narration="TEST", balance=None, occurrence=0) -> StatementRow:
    occurred = date(2026, 7, day)
    money = Decimal(amount)
    after = Decimal(balance) if balance is not None else None
    return StatementRow(
        row_id=row_id_for(
            instrument=instrument, occurred_on=occurred, direction=direction,
            amount=money, balance_after=after, occurrence_index=occurrence,
            narration=narration,
        ),
        instrument=instrument, occurred_on=occurred, narration=narration, ref=None,
        direction=direction, amount=money, balance_after=after,
        statement_id=f"{instrument}/2026-07-01..2026-07-31", file_sha256="fixture",
    )


def _statement(rows, instrument="hdfc-1225") -> ParsedStatement:
    return ParsedStatement(
        status=PARSED, instrument=instrument,
        period_start=date(2026, 7, 1), period_end=date(2026, 7, 31),
        opening_balance=Decimal("0"), closing_balance=Decimal("0"),
        rows=tuple(rows), statement_id=f"{instrument}/2026-07-01..2026-07-31",
        file_sha256="fixture",
    )


@pytest.mark.asyncio
async def test_rows_are_stored_once_however_often_the_file_arrives(db_pool):
    """`row_id` is a content hash, so the same statement downloaded twice, a
    re-sent copy and an overlapping period all collapse to one set of rows."""
    await db_pool.execute("DELETE FROM finance.statement_rows WHERE instrument = 'hdfc-1225'")
    stmt = _statement([_row(2, "100.00"), _row(3, "200.00")])

    stored, existing, refreshed = await si.store_rows(db_pool, stmt)
    assert (stored, existing, refreshed) == (2, 0, 0)

    stored, existing, refreshed = await si.store_rows(db_pool, stmt)
    assert (stored, existing, refreshed) == (0, 2, 0), "the second import changes nothing"

    n = await db_pool.fetchval(
        "SELECT count(*) FROM finance.statement_rows WHERE instrument = 'hdfc-1225'"
    )
    assert n == 2


@pytest.mark.asyncio
async def test_two_identical_payments_on_one_day_stay_two_rows(db_pool):
    """The other half of the hash: dedupe must not swallow a real repeat. Two
    ₹50 coffees on the same day differ only by occurrence index, and a statement
    that collapsed them would understate the account by ₹50 forever."""
    await db_pool.execute("DELETE FROM finance.statement_rows WHERE instrument = 'hdfc-1225'")
    stmt = _statement([
        _row(4, "50.00", narration="COFFEE", occurrence=0),
        _row(4, "50.00", narration="COFFEE", occurrence=1),
    ])
    stored, _, _ = await si.store_rows(db_pool, stmt)
    assert stored == 2


@pytest.mark.asyncio
async def test_a_re_import_does_not_erase_matching_work(db_pool):
    """A row that is already here may have been matched or posted since, and
    re-importing the file must not wipe `matched_msgid` or `posted_at`. The
    conflict clause refreshes `narration` and `ref` and names no other column,
    so the work done to a row since it was stored survives."""
    await db_pool.execute("DELETE FROM finance.statement_rows WHERE instrument = 'hdfc-1225'")
    stmt = _statement([_row(2, "100.00")])
    await si.store_rows(db_pool, stmt)
    row_id = stmt.rows[0].row_id
    await db_pool.execute(
        "UPDATE finance.statement_rows SET matched_msgid = $2, posted_at = now() "
        "WHERE row_id = $1",
        row_id, "mail/1",
    )

    await si.store_rows(db_pool, stmt)

    kept = await db_pool.fetchrow(
        "SELECT matched_msgid, posted_at FROM finance.statement_rows WHERE row_id = $1", row_id
    )
    assert kept["matched_msgid"] == "mail/1" and kept["posted_at"] is not None


@pytest.mark.asyncio
async def test_a_card_rows_foreign_original_survives_the_store(db_pool):
    """§8.5. The card prints `( USD 9.99 )` beside the rupee charge, and that is
    the exact figure the journal block holds. Parsing it and then dropping it at
    the INSERT would leave the matcher converting rupees back through a rate
    production does not have — so this asserts it comes back out of Postgres,
    not merely off the parser.
    """
    await db_pool.execute("DELETE FROM finance.statement_rows WHERE instrument = 'axis-cc-9876'")
    text = (FIXTURES / "axis_credit_card.txt").read_text()
    parsed = parse_axis_card_statement(text, file_sha256="fixture")
    assert parsed.status == PARSED, parsed.reason

    await si.store_rows(db_pool, parsed)

    stored = await db_pool.fetch(
        "SELECT narration, amount, fx_currency, fx_amount FROM finance.statement_rows "
        "WHERE instrument = 'axis-cc-9876' AND fx_currency IS NOT NULL ORDER BY occurred_on"
    )
    assert [(r["fx_currency"], str(r["fx_amount"])) for r in stored] == [
        ("USD", "9.99"),
        ("USD", "20.00"),
        ("USD", "12.34"),
    ]
    assert all(r["amount"] > r["fx_amount"] for r in stored), "amount stays the rupee charge"

    rupee_rows = await db_pool.fetchval(
        "SELECT count(*) FROM finance.statement_rows "
        "WHERE instrument = 'axis-cc-9876' AND fx_currency IS NULL"
    )
    assert rupee_rows == 15, "and a domestic row carries no original"


def test_an_axis_pdf_reaches_the_parser_its_own_header_anchor_names(monkeypatch):
    """§6.1 decides which Axis parser a PDF goes to — the whole-line card
    anchor, never the title and never a substring. Both directions, because a
    dispatch that always picks one is right half the time by accident.
    """
    card = (FIXTURES / "axis_credit_card.txt").read_text()
    account = (FIXTURES / "axis_mailed.txt").read_text()

    monkeypatch.setattr(si, "pdf_text", lambda data, *a, **kw: card)
    out = si.parse_bytes(b"%PDF-1.7 pretend", title="axis current account july.pdf")
    assert (out.status, out.instrument) == (PARSED, "axis-cc-9876")
    assert out.diagnostics["layout"] == CARD_LAYOUT

    monkeypatch.setattr(si, "pdf_text", lambda data, *a, **kw: account)
    out = si.parse_bytes(b"%PDF-1.7 pretend", title="credit card statement.pdf")
    assert (out.status, out.instrument) == (PARSED, "axis-4321")
    assert out.diagnostics["layout"] == "axis_mailed"


def test_a_card_statement_that_fails_its_check_keeps_its_own_reason(monkeypatch):
    """A card statement that cannot be trusted must say why. Trying the card
    parser and falling back to the account parser on any failure would answer
    `no_header_anchor`, which points a human at the wrong problem.
    """
    card = (FIXTURES / "axis_credit_card.txt").read_text()
    broken = card.replace("2,000.00 Dr", "2,000.00 Cr")
    monkeypatch.setattr(si, "pdf_text", lambda data, *a, **kw: broken)

    out = si.parse_bytes(b"%PDF-1.7 pretend", title="statement.pdf")
    assert out.status == REFUSED
    assert out.reason == "totals_mismatch"
    assert out.instrument == "axis-cc-9876", "it still knows whose statement it refused"


def test_the_bytes_decide_the_parser_not_the_filename():
    """A statement in a Drive folder has usually been renamed by hand, so the
    name says nothing about which bank wrote it. A PDF goes to the Axis parser
    and anything else is tried as HDFC HTML — decided on the magic bytes.

    The two paths fail differently, which is what makes this a dispatch test
    rather than a "both are broken" test: the PDF path RAISES `StatementError`
    from `pdf_text` (and `intake_folder` catches it as `unreadable`), while the
    HTML path returns a statement with a non-ok status. Names that point the
    wrong way are used on purpose.
    """
    from aegis.services.statements import StatementError

    with pytest.raises(StatementError):
        si.parse_bytes(b"%PDF-1.4\nnot really a pdf", title="hdfc_statement.html")

    hdfc = si.parse_bytes(b"<html><body>nothing here</body></html>", title="axis.pdf")
    assert hdfc.status != PARSED and hdfc.instrument is None


@pytest.mark.asyncio
async def test_a_misfiled_statement_is_reported_and_not_imported(db_pool, monkeypatch):
    """§4.1: the folder is a cross-check, never the identifier.

    Importing a file whose contents name another account would put one
    account's money on another's balance, and step 5's closing-balance check
    would then revert a statement that was never wrong. The account is read
    from inside the file; the folder only gets to disagree.
    """
    await db_pool.execute("DELETE FROM finance.statement_rows WHERE instrument = 'axis-9640'")
    monkeypatch.setattr(si.drive, "_build_drive_service", lambda _p: object())
    monkeypatch.setattr(
        si.drive, "_list_folder", lambda _s, _f: [{"id": "f1", "name": "july.pdf"}]
    )
    monkeypatch.setattr(si.drive, "_download", lambda _s, _f: b"bytes")
    monkeypatch.setattr(
        si, "parse_bytes",
        lambda data, title="", declared=(): _statement([_row(2, "100.00", instrument="axis-9640")],
                                                       instrument="axis-9640"),
    )

    report = await si.intake_folder(db_pool, __import__("pathlib").Path("/tmp/t.json"),
                                    {"hdfc-1225": "folder-id"})

    assert [o.status for o in report.outcomes] == [si.MISFILED]
    assert "axis-9640" in report.outcomes[0].reason and "hdfc-1225" in report.outcomes[0].reason
    assert report.stored == 0
    n = await db_pool.fetchval(
        "SELECT count(*) FROM finance.statement_rows WHERE instrument = 'axis-9640'"
    )
    assert n == 0, "a misfile is reported, never imported"


@pytest.mark.asyncio
async def test_one_unreadable_file_does_not_abandon_the_rest(db_pool, monkeypatch):
    """A locked PDF or a bank that changed its layout is a thing to report, not
    a reason to skip the eleven statements that parse."""
    await db_pool.execute("DELETE FROM finance.statement_rows WHERE instrument = 'hdfc-1225'")
    files = [{"id": "bad", "name": "locked.pdf"}, {"id": "good", "name": "july.pdf"}]
    monkeypatch.setattr(si.drive, "_build_drive_service", lambda _p: object())
    monkeypatch.setattr(si.drive, "_list_folder", lambda _s, _f: files)

    def _download(_svc, f):
        if f["id"] == "bad":
            raise RuntimeError("no derived password opened the file")
        return b"bytes"

    monkeypatch.setattr(si.drive, "_download", _download)
    monkeypatch.setattr(
        si, "parse_bytes",
        lambda data, title="", declared=(): _statement([_row(9, "700.00")]),
    )

    report = await si.intake_folder(db_pool, __import__("pathlib").Path("/tmp/t.json"),
                                    {"hdfc-1225": "folder-id"})

    assert len(report.outcomes) == 2
    assert report.outcomes[0].status == si.UNREADABLE
    assert "password" in report.outcomes[0].reason
    assert report.outcomes[1].status == PARSED and report.outcomes[1].rows == 1
    assert report.stored == 1, "the readable one still landed"


@pytest.mark.asyncio
async def test_a_dry_run_stores_nothing_but_still_reports(db_pool, monkeypatch):
    await db_pool.execute("DELETE FROM finance.statement_rows WHERE instrument = 'hdfc-1225'")
    monkeypatch.setattr(si.drive, "_build_drive_service", lambda _p: object())
    monkeypatch.setattr(
        si.drive, "_list_folder", lambda _s, _f: [{"id": "f1", "name": "july.pdf"}]
    )
    monkeypatch.setattr(si.drive, "_download", lambda _s, _f: b"bytes")
    monkeypatch.setattr(
        si, "parse_bytes",
        lambda data, title="", declared=(): _statement([_row(2, "100.00")]),
    )

    report = await si.intake_folder(db_pool, __import__("pathlib").Path("/tmp/t.json"),
                                    {"hdfc-1225": "folder-id"}, dry_run=True)

    assert report.outcomes[0].status == PARSED and report.outcomes[0].rows == 1
    assert report.stored == 0
    n = await db_pool.fetchval(
        "SELECT count(*) FROM finance.statement_rows WHERE instrument = 'hdfc-1225'"
    )
    assert n == 0


def test_intake_agrees_with_the_parsers_about_what_success_looks_like():
    """The bug the rest of this file could not catch.

    `intake_folder` originally compared `statement.status` against a literal
    "ok" of its own invention, so every one of the fifteen real statements in
    the Drive folder was recorded as a failure and none was imported. The tests
    passed throughout, because their `_statement()` helper invented the same
    wrong constant — the fixtures agreed with the bug.

    So this test refuses to build a statement. It runs a REAL fixture through
    the real parser and asserts the status intake treats as success is the
    status the parser actually emits. A future rename of `PARSED` breaks this
    test; a divergence between the two modules cannot survive it.
    """
    text = (FIXTURES / "axis_mailed.txt").read_text()
    parsed = parse_axis_statement(text, file_sha256="fixture")
    assert parsed.status == PARSED, "the parser's own verdict on a good statement"
    assert parsed.rows, "and it found rows, so this is not a vacuous pass"

    # The value intake gates on, taken from the same place rather than retyped.
    assert si.PARSED is PARSED
    assert IntakeReport(outcomes=[FileOutcome(
        file_id="f", title="t", folder="axis-9640", status=parsed.status
    )]).failures == [], "a really-parsed statement is not a failure"


def _bank_row(narration: str, ref: str | None) -> StatementRow:
    """A row with a running balance, so §8.3 keys it on the BALANCE and the
    narration is free to change — which is the whole case below."""
    row = _row(9, "300.00", narration=narration, balance="9700.00")
    return replace(row, narration=narration, ref=ref)


@pytest.mark.asyncio
async def test_a_better_parser_reaches_rows_that_are_already_stored(db_pool):
    """#428. The FY2024-25 statement's first 23 rows were stored with 16
    characters cut off the front and a NULL `ref` — `1526/CEX WEBUY YES BANK
    LTD` where the bank printed `UPI/P2M/…1526/CEX WEBUY…`. With a flat
    `DO NOTHING` the parser fix could never reach them however many times the
    file was re-read, because `row_id` keys a bank row on its balance and the
    id therefore did not change either.
    """
    await db_pool.execute("DELETE FROM finance.statement_rows WHERE instrument = 'hdfc-1225'")
    clipped = _statement([_bank_row("1526/CEX WEBUY YES BANK LTD", None)])
    stored, _, _ = await si.store_rows(db_pool, clipped)
    assert stored == 1

    whole = _statement([_bank_row("UPI/P2M/412345678901526/CEX WEBUY", "412345678901526")])
    assert whole.rows[0].row_id == clipped.rows[0].row_id, "the id must not depend on narration"

    stored, existing, refreshed = await si.store_rows(db_pool, whole)
    assert (stored, existing, refreshed) == (0, 0, 1)

    row = await db_pool.fetchrow(
        "SELECT narration, ref FROM finance.statement_rows WHERE row_id = $1",
        whole.rows[0].row_id,
    )
    assert row["narration"] == "UPI/P2M/412345678901526/CEX WEBUY"
    assert row["ref"] == "412345678901526"


@pytest.mark.asyncio
async def test_a_refresh_leaves_the_work_done_to_the_row_alone(db_pool):
    """The refresh names two columns, so a row that has been matched and posted
    keeps both. Re-parsing a file must never un-reconcile an account."""
    await db_pool.execute("DELETE FROM finance.statement_rows WHERE instrument = 'hdfc-1225'")
    await si.store_rows(db_pool, _statement([_bank_row("CLIPPED", None)]))
    row_id = _bank_row("CLIPPED", None).row_id
    await db_pool.execute(
        "UPDATE finance.statement_rows SET matched_msgid = $2, posted_at = now(), "
        "skip_reason = $3 WHERE row_id = $1",
        row_id, "mail/1", "ambiguous",
    )

    _, _, refreshed = await si.store_rows(
        db_pool, _statement([_bank_row("UPI/P2M/999/WHOLE", "999")])
    )
    assert refreshed == 1

    kept = await db_pool.fetchrow(
        "SELECT narration, matched_msgid, posted_at, skip_reason "
        "FROM finance.statement_rows WHERE row_id = $1",
        row_id,
    )
    assert kept["narration"] == "UPI/P2M/999/WHOLE", "the narration did not refresh"
    assert kept["matched_msgid"] == "mail/1"
    assert kept["posted_at"] is not None
    assert kept["skip_reason"] == "ambiguous"


@pytest.mark.asyncio
async def test_a_re_read_that_finds_no_reference_does_not_erase_one(db_pool):
    """The same rule `journal_index.upsert` follows: a reference is a fact about
    the payment, not a verdict about it. The mailed and netbanking layouts
    truncate a narration differently, so the file that can read the UTR and the
    file that cannot are both legitimate re-reads of the same transaction."""
    await db_pool.execute("DELETE FROM finance.statement_rows WHERE instrument = 'hdfc-1225'")
    row_id = _bank_row("x", None).row_id
    await si.store_rows(db_pool, _statement([_bank_row("NEFT/AXISP00123456/RENT", "AXISP00123456")]))

    await si.store_rows(db_pool, _statement([_bank_row("NEFT/AXISP0012345", None)]))

    row = await db_pool.fetchrow(
        "SELECT narration, ref FROM finance.statement_rows WHERE row_id = $1", row_id
    )
    assert row["narration"] == "NEFT/AXISP0012345", "narration takes the newest read"
    assert row["ref"] == "AXISP00123456", "a reference already found was erased"


@pytest.mark.asyncio
async def test_an_unchanged_row_is_not_rewritten(db_pool):
    """Intake re-downloads and re-parses every file on every run. Without the
    `WHERE` on the conflict clause, each run would rewrite all ~2,800 stored
    rows to the values they already hold — twice a day, for table bloat."""
    await db_pool.execute("DELETE FROM finance.statement_rows WHERE instrument = 'hdfc-1225'")
    stmt = _statement([_bank_row("UPI/P2M/999/SHOP", "999")])
    await si.store_rows(db_pool, stmt)
    before = await db_pool.fetchval(
        "SELECT xmin::text FROM finance.statement_rows WHERE row_id = $1", stmt.rows[0].row_id
    )

    stored, existing, refreshed = await si.store_rows(db_pool, stmt)

    assert (stored, existing, refreshed) == (0, 1, 0)
    after = await db_pool.fetchval(
        "SELECT xmin::text FROM finance.statement_rows WHERE row_id = $1", stmt.rows[0].row_id
    )
    assert after == before, "the row was rewritten with the values it already held"
