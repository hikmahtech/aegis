"""Bank statement parsing — Axis PDF (spec `2026-09-07-statement-reconciliation-design.md`
§5.2, §6.1, §6.2, §8.3).

Deterministic, and deliberately model-free: both banks print fixed, labelled
columns, and a language model near a number is how a ledger becomes confidently
wrong (§6.3).

Three rules from the spec do the load-bearing work here, and each exists because
a manual dry run got it wrong first:

* **Identify by header anchor only** (§6.1). The account and the period come from
  a whole-line anchor that cannot appear in a transaction row, never from a
  substring scan and never from the email subject. A first attempt classified
  nine Axis *current account* statements as credit-card because it looked for
  the substring `Credit Card`, which the current account statement carries in a
  `CreditCard Payment` narration; and Axis names each monthly statement for the
  month it was *sent*, so "Statement for August 2026" covers July. Anything
  matching no anchor is `UNIDENTIFIED` and stays visible — it is never guessed.
* **The statement proves itself** (§6.2). Closing minus opening must equal
  deposits minus withdrawals, and where the bank prints its own
  `TRANSACTION TOTAL` that must equal the sums too. A statement that fails is
  **refused whole** — `rows` comes back empty — because a parser that quietly
  returns fewer rows than the statement holds is the worst outcome in this lane.
* **`row_id` is layout-independent** (§8.3): instrument, transaction date,
  direction, amount, running balance and the occurrence index within that group.
  The narration is display only — `pdftotext -layout` wraps and truncates it by
  column width and Axis prints two different layouts for one account, so a
  narration-keyed id would hash the same row twice across the backfill/live
  overlap and post the money twice.

Text extraction is `pdftotext -layout` reading the PDF from **stdin**, with
`pikepdf` decrypting in memory when a password is needed: `-upw <password>` puts
the password in argv where `/proc` can read it, and `pdfminer` (already a
dependency) returns the table column-by-column and rebuilt zero complete rows
from three real statements.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from io import BytesIO
from typing import Any

from aegis.services import books

_CENT = Decimal("0.01")

#: Outcomes. `UNIDENTIFIED` is a bucket that stays visible (§6.1); `REFUSED`
#: carries no rows at all (§6.2).
PARSED = "parsed"
UNIDENTIFIED = "unidentified"
REFUSED = "refused"


class StatementError(Exception):
    """A statement file could not be turned into text."""


class StatementLocked(StatementError):  # noqa: N818 — a state, not an error suffix
    """No derived password opened the file. Names nothing about the attempt."""


# --------------------------------------------------------------- text extraction


def file_digest(data: bytes) -> str:
    """`file_sha256` for the row: a regenerated period is a second file."""
    return hashlib.sha256(data).hexdigest()


def _pdftotext(data: bytes) -> str:
    """`pdftotext -layout` over stdin. No password ever reaches argv."""
    try:
        proc = subprocess.run(  # noqa: S603 — fixed argv, no shell, no user input
            ["pdftotext", "-layout", "-", "-"],
            input=data,
            capture_output=True,
            timeout=120,
        )
    except FileNotFoundError as exc:  # poppler-utils missing from the image
        raise StatementError("pdftotext is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise StatementError("pdftotext timed out") from exc
    if proc.returncode != 0:
        raise StatementError(f"pdftotext failed (rc={proc.returncode})")
    return proc.stdout.decode("utf-8", "replace")


def pdf_text(data: bytes, passwords: Sequence[str] = ()) -> str:
    """PDF bytes -> `pdftotext -layout` text, decrypting in memory if needed.

    `passwords` is the ordered candidate list from `statement_passwords`. The
    decrypted PDF is never written to disk and a candidate is never logged or
    raised: a failure is `StatementLocked`, and the caller reports the account.
    """
    import pikepdf

    try:
        with pikepdf.open(BytesIO(data)):
            return _pdftotext(data)
    except pikepdf.PasswordError:
        pass
    except pikepdf.PdfError as exc:
        raise StatementError("not a readable PDF") from exc

    for password in passwords:
        try:
            with pikepdf.open(BytesIO(data), password=password) as pdf:
                buf = BytesIO()
                pdf.save(buf)
        except pikepdf.PasswordError:
            continue
        return _pdftotext(buf.getvalue())
    raise StatementLocked("the statement could not be opened")


# ------------------------------------------------------------------- §6.1 anchors

# Whole-line, both of them: that is what makes an anchor an anchor. A transaction
# narration starts in the middle of the line and can never be one, so no
# substring of a narration can move the account or the period.
_AXIS_MAILED = re.compile(
    r"^\s*STATEMENT BETWEEN\s+(\d{2}/\d{2}/\d{4})\s+AND\s+(\d{2}/\d{2}/\d{4})"
    r"\s+FOR A/C:\s*[Xx*]*(\d{4,})\s*$"
)
_AXIS_NETBANKING = re.compile(
    r"^\s*Statement of Account No\s*:\s*[Xx*]*(\d{4,})\s+for the period"
    r"\s*\(\s*From\s*:\s*(\d{2}-\d{2}-\d{4})\s+To\s*:\s*(\d{2}-\d{2}-\d{4})\s*\)\s*$",
    re.I,
)


@dataclass(frozen=True)
class StatementHeader:
    account_tail: str
    period_start: date
    period_end: date
    layout: str


def _d(text: str) -> date:
    return datetime.strptime(text.replace("-", "/"), "%d/%m/%Y").date()


def identify_axis(text: str) -> tuple[StatementHeader | None, str]:
    """(header, reason). Every anchor in the file must agree; anything else is
    `UNIDENTIFIED` — a file holding two accounts is a misfile, not a guess."""
    found: list[StatementHeader] = []
    for line in text.splitlines():
        mailed = _AXIS_MAILED.match(line)
        if mailed:
            start, end, account = mailed.groups()
            found.append(StatementHeader(account[-4:], _d(start), _d(end), "axis_mailed"))
            continue
        netbanking = _AXIS_NETBANKING.match(line)
        if netbanking:
            account, start, end = netbanking.groups()
            found.append(StatementHeader(account[-4:], _d(start), _d(end), "axis_netbanking"))
    if not found:
        return None, "no_header_anchor"
    first = found[0]
    if any(
        (h.account_tail, h.period_start, h.period_end)
        != (first.account_tail, first.period_start, first.period_end)
        for h in found
    ):
        return None, "conflicting_headers"
    return first, ""


# --------------------------------------------------------------------- the table

# The column labels of the two Axis layouts. `Value Date`/`Chq. No.` appear in
# one and not the other, and the offsets move page to page, so the map is read
# from each column-header line and applied to the rows that follow it.
_DATE_LABEL = "Tran Date"
_NARRATION_LABELS = ("Transaction Details", "Particulars")
_NUM = re.compile(r"-?\d[\d,]*\.\d{2}")
_ROW_DATE = re.compile(r"(\d{2}[-/]\d{2}[-/]\d{4})")
_OPENING = re.compile(r"^\s*OPENING BALANCE\s*:?\s", re.I)
_CLOSING = re.compile(r"^\s*CLOSING BALANCE\s*:?\s", re.I)
_TOTAL = re.compile(r"^\s*TRANSACTION TOTAL\s*(?:DR/CR)?\s*:?\s", re.I)
#: Row-shaped, for the "this page has rows and no column header" diagnosis only.
#: Never used to read a value out of a line.
_LOOSE_ROW = re.compile(r"^\s{0,6}\d{2}[-/]\d{2}[-/]\d{4}\s")

#: How far a right-aligned number may sit from its column label's end. The
#: observed offsets are +3 to +7 and the columns are 12 to 16 apart, so this
#: separates them with room to spare while ignoring numbers that live in the
#: narration or the branch column — an amount that lands nowhere is dropped and
#: the §6.2 check then refuses the statement, which is the visible failure.
_COL_SLACK = 10
#: How far a row's transaction date may sit from the `Tran Date` label.
_DATE_SLACK = 4


@dataclass(frozen=True)
class _Columns:
    date_start: int
    narration_start: int
    narration_end: int
    debit_end: int
    credit_end: int
    balance_end: int


def _column_map(line: str) -> _Columns | None:
    """A column-header line -> the character offsets of its columns."""
    date_start = line.find(_DATE_LABEL)
    narration_start = max(line.find(label) for label in _NARRATION_LABELS)
    debit = line.find("Debit")
    credit = line.find("Credit")
    balance = line.find("Balance")
    if min(date_start, narration_start, debit, credit, balance) < 0:
        return None
    if not (narration_start < debit < credit < balance):
        return None
    chq = line.find("Chq")
    ends = [c for c in (chq, debit) if c > narration_start]
    return _Columns(
        date_start=date_start,
        narration_start=narration_start,
        narration_end=min(ends),
        debit_end=debit + len("Debit"),
        credit_end=credit + len("Credit"),
        balance_end=balance + len("Balance"),
    )


def _amount(token: str) -> Decimal:
    try:
        return Decimal(token.replace(",", "")).quantize(_CENT)
    except InvalidOperation as exc:  # pragma: no cover — the regex guarantees the shape
        raise StatementError(f"unreadable amount: {token!r}") from exc


def _columned_numbers(line: str, cols: _Columns) -> dict[str, Decimal]:
    """Right-aligned numbers on one line, keyed by the column they end in.

    A number that ends near no column (a terminal id inside a narration, a
    branch code) is ignored; a column that gets two numbers keeps neither, so
    the row reads as unparseable rather than as half of itself.
    """
    targets = {"debit": cols.debit_end, "credit": cols.credit_end, "balance": cols.balance_end}
    out: dict[str, Decimal] = {}
    clashed: set[str] = set()
    for match in _NUM.finditer(line):
        name, distance = min(
            ((n, abs(match.end() - end)) for n, end in targets.items()), key=lambda kv: kv[1]
        )
        if distance > _COL_SLACK:
            continue
        if name in out:
            clashed.add(name)
        out[name] = _amount(match.group())
    for name in clashed:
        out.pop(name, None)
    return out


def _single_number(line: str) -> Decimal | None:
    """The one number on a labelled summary line.

    `OPENING BALANCE` and `CLOSING BALANCE` are NOT printed in the balance
    column — Axis indents them about ten characters further right, and the
    offset varies by statement — so they are read by count, not by position.
    Reading them through the column map worked on July's statement and lost
    June's by one character, which is exactly the kind of near-miss that makes a
    test pass for the wrong reason.
    """
    numbers = _NUM.findall(line)
    return _amount(numbers[0]) if len(numbers) == 1 else None


def _pair_of_numbers(line: str) -> tuple[Decimal, Decimal] | None:
    """The (debit, credit) pair on a `TRANSACTION TOTAL` line, left to right —
    the printed column order in both Axis layouts."""
    numbers = _NUM.findall(line)
    if len(numbers) != 2:
        return None
    return _amount(numbers[0]), _amount(numbers[1])


def normalise_narration(text: str) -> str:
    """Uppercase, whitespace collapsed — the `statement_rows.narration` shape."""
    return " ".join(text.split()).upper()


_REF_PATTERNS = (
    re.compile(r"\bUPI/(?:P2A|P2M|CREDIT|DEBIT|[A-Z0-9]{2,6})/(\d{9,})"),
    re.compile(r"\bIMPS[/-](?:[A-Z0-9]{2,6}[/-])?(\d{9,})"),
    re.compile(r"\bNEFT[/-]([A-Z]{4}[A-Z0-9]{6,})"),
    re.compile(r"\bRTGS[/-]([A-Z]{4}[A-Z0-9]{6,})"),
)


def extract_ref(narration: str) -> str | None:
    """The UTR/RRN out of the narration, or None.

    Only the reference namespaces a bank actually issues (UPI RRN, IMPS
    reference, NEFT/RTGS UTR). A POS terminal id is not a reference and must not
    be read as one: pass 1 of the matcher is an exact join, so a wrong value
    here mis-attributes a payment.
    """
    text = normalise_narration(narration)
    for pattern in _REF_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(1)
    return None


# --------------------------------------------------------------------- row model


@dataclass(frozen=True)
class StatementRow:
    """One `finance.statement_rows` row (§7)."""

    row_id: str
    instrument: str
    occurred_on: date
    narration: str
    ref: str | None
    direction: str
    amount: Decimal
    balance_after: Decimal | None
    statement_id: str
    file_sha256: str


@dataclass(frozen=True)
class ParsedStatement:
    status: str
    reason: str = ""
    instrument: str | None = None
    period_start: date | None = None
    period_end: date | None = None
    opening_balance: Decimal | None = None
    closing_balance: Decimal | None = None
    rows: tuple[StatementRow, ...] = ()
    statement_id: str = ""
    file_sha256: str = ""
    subject_hint: str = ""
    diagnostics: dict[str, Any] = field(default_factory=dict)


def statement_id_for(instrument: str, period_start: date, period_end: date) -> str:
    return f"{instrument}/{period_start.isoformat()}..{period_end.isoformat()}"


def row_id_for(
    *,
    instrument: str,
    occurred_on: date,
    direction: str,
    amount: Decimal,
    balance_after: Decimal | None,
    occurrence_index: int,
    narration: str = "",
) -> str:
    """§8.3. The running balance is in the key because it is bank-authoritative
    and layout-independent; **cards have no running balance** and fall back to
    the normalised narration in that slot. The occurrence index counts within
    the `(instrument, date, direction, amount, balance)` group, so two genuinely
    separate ₹50 payments on one day stay two rows — without it they collapse
    into one and money vanishes from the books. The group is content-defined,
    never file-defined, so two overlapping statements covering the same day
    produce identical ids and dedupe correctly.
    """
    slot = (
        f"{balance_after.quantize(_CENT)}"
        if balance_after is not None
        else normalise_narration(narration)
    )
    payload = "|".join(
        [
            instrument,
            occurred_on.isoformat(),
            direction,
            f"{amount.quantize(_CENT)}",
            slot,
            str(occurrence_index),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def assign_row_ids(
    records: Sequence[dict[str, Any]], *, instrument: str, statement_id: str, file_sha256: str
) -> tuple[StatementRow, ...]:
    """Records in file order -> rows, numbering each occurrence within its group."""
    seen: dict[tuple[Any, ...], int] = {}
    rows: list[StatementRow] = []
    for rec in records:
        narration = normalise_narration(rec["narration"])
        balance = rec["balance_after"]
        slot = balance if balance is not None else narration
        key = (instrument, rec["occurred_on"], rec["direction"], rec["amount"], slot)
        index = seen.get(key, 0)
        seen[key] = index + 1
        rows.append(
            StatementRow(
                row_id=row_id_for(
                    instrument=instrument,
                    occurred_on=rec["occurred_on"],
                    direction=rec["direction"],
                    amount=rec["amount"],
                    balance_after=balance,
                    occurrence_index=index,
                    narration=narration,
                ),
                instrument=instrument,
                occurred_on=rec["occurred_on"],
                narration=narration,
                ref=extract_ref(narration),
                direction=rec["direction"],
                amount=rec["amount"],
                balance_after=balance,
                statement_id=statement_id,
                file_sha256=file_sha256,
            )
        )
    return tuple(rows)


# ------------------------------------------------------------------- the parser


def _row_date(line: str, cols: _Columns) -> date | None:
    """The transaction date — the leftmost date sitting in the `Tran Date`
    column. Never the value date, which the mailed layout prints beside it."""
    for match in _ROW_DATE.finditer(line):
        if abs(match.start() - cols.date_start) <= _DATE_SLACK:
            try:
                return _d(match.group(1))
            except ValueError:
                return None
        if match.start() > cols.date_start + _DATE_SLACK:
            break
    return None


def _narration_cell(line: str, cols: _Columns) -> str:
    return line[cols.narration_start : cols.narration_end].strip()


def parse_axis_statement(
    text: str,
    *,
    file_sha256: str,
    subject: str = "",
    declared: Collection[str] = (),
) -> ParsedStatement:
    """`pdftotext -layout` text -> a parsed, self-validated Axis statement.

    `subject` is recorded for the digest and **never read for the period**
    (§6.1): Axis names each monthly statement for the month it was sent, so a
    subject saying August belongs to a July statement.
    """
    header, reason = identify_axis(text)
    if header is None:
        return ParsedStatement(
            status=UNIDENTIFIED, reason=reason, file_sha256=file_sha256, subject_hint=subject
        )

    instrument = books.canonical_instrument(f"axis-{header.account_tail}", declared) or ""
    statement_id = statement_id_for(instrument, header.period_start, header.period_end)
    base = {
        "instrument": instrument,
        "period_start": header.period_start,
        "period_end": header.period_end,
        "statement_id": statement_id,
        "file_sha256": file_sha256,
        "subject_hint": subject,
    }

    opening: Decimal | None = None
    closing: Decimal | None = None
    printed_totals: dict[str, Decimal] = {}
    records: list[dict[str, Any]] = []
    row_lines: list[int] = []
    continuations: list[tuple[int, str]] = []
    unreadable: list[int] = []
    reprinted: list[int] = []
    headerless_pages: list[int] = []
    index = -1
    stopped = False

    # The column map is PER PAGE. `pdftotext -layout` sizes each column to the
    # widest thing on that page, so the offsets move from page to page — the
    # mailed layout reprints its header on every page and is read page by page,
    # and a page carrying rows with no header of its own is refused rather than
    # read through a stale map, which is how a credit becomes a debit.
    for page_number, page in enumerate(text.split("\f")):
        if stopped:
            break
        cols: _Columns | None = None
        for line in page.splitlines():
            index += 1
            mapped = _column_map(line)
            if mapped is not None:
                cols = mapped
                continue
            if cols is None:
                if _LOOSE_ROW.match(line) and _NUM.search(line):
                    headerless_pages.append(page_number)
                continue
            if _CLOSING.match(line):
                closing = _single_number(line)
                stopped = True  # the table ends here; a charge statement may follow it
                break
            if _OPENING.match(line):
                # The first one: every page restates it, and page two's is the
                # running balance at the top of page two, not the statement's.
                if opening is None:
                    opening = _single_number(line)
                continue
            if _TOTAL.match(line):
                pair = _pair_of_numbers(line)
                if pair is not None:
                    printed_totals = {"debit": pair[0], "credit": pair[1]}
                continue
            numbers = _columned_numbers(line, cols)
            occurred_on = _row_date(line, cols)
            if occurred_on is None:
                cell = _narration_cell(line, cols)
                if cell and not numbers:
                    continuations.append((index, cell))
                continue
            debit, credit = numbers.get("debit"), numbers.get("credit")
            balance = numbers.get("balance")
            if (debit is None) == (credit is None) or balance is None:
                unreadable.append(index)
                continue
            amount = debit if debit is not None else credit
            if amount < 0:
                unreadable.append(index)
                continue
            direction = "out" if debit is not None else "in"
            # A page-boundary reprint: Axis printed the last row of one page
            # again at the top of the next (seen in the real April 2026
            # statement), and its own TRANSACTION TOTAL counts it once. The
            # discriminator is the running balance, which the bank owns: a
            # SECOND genuine ₹50 debit moves the balance again, a reprint does
            # not. Dropping it is self-validating — drop a row that was real and
            # the §6.2 check below fails and the statement is refused.
            previous = records[-1] if records else None
            if (
                previous is not None
                and amount > 0
                and (previous["occurred_on"], previous["direction"], previous["amount"])
                == (occurred_on, direction, amount)
                and previous["balance_after"] == balance
            ):
                reprinted.append(index)
                continue
            row_lines.append(index)
            records.append(
                {
                    "occurred_on": occurred_on,
                    "direction": direction,
                    "amount": amount,
                    "balance_after": balance,
                    "narration": [(index, _narration_cell(line, cols))],
                }
            )

    # Axis centres a wrapped narration on its own date line, so its lines print
    # both above and below the row: each continuation goes to the nearest row
    # line. It is display only — `row_id` and the §6.2 check never read it,
    # which is exactly why §8.3 keys on the running balance instead.
    for line_no, cell in continuations:
        if not row_lines:
            continue
        nearest = min(
            range(len(row_lines)),
            key=lambda i: (
                abs(line_no - row_lines[i]),
                # A tie means the line sits one row above and one row below.
                # Prefer the row that printed NO narration of its own: a row
                # that already carries its text has a complete block, so the
                # line belongs to its neighbour. Failing that, prefer the LATER
                # row, which is the head of the next block.
                bool(records[i]["narration"][0][1]),
                -row_lines[i],
            ),
        )
        records[nearest]["narration"].append((line_no, cell))
    for rec in records:
        rec["narration"] = " ".join(cell for _, cell in sorted(rec["narration"]))

    deposits = sum((r["amount"] for r in records if r["direction"] == "in"), Decimal("0"))
    withdrawals = sum((r["amount"] for r in records if r["direction"] == "out"), Decimal("0"))
    diagnostics: dict[str, Any] = {
        "rows_read": len(records),
        "reprinted_rows": len(reprinted),
        "deposits": str(deposits),
        "withdrawals": str(withdrawals),
        "layout": header.layout,
    }

    def refuse(why: str, **extra: Any) -> ParsedStatement:
        return ParsedStatement(
            status=REFUSED,
            reason=why,
            opening_balance=opening,
            closing_balance=closing,
            diagnostics={**diagnostics, **extra},
            **base,
        )

    if headerless_pages:
        return refuse("no_column_header", headerless_pages=sorted(set(headerless_pages)))
    if unreadable:
        return refuse("unreadable_row", unreadable_lines=unreadable[:10])
    if opening is None or closing is None:
        return refuse("missing_balance_anchor")
    outside = [r["occurred_on"].isoformat() for r in records
               if not header.period_start <= r["occurred_on"] <= header.period_end]
    if outside:
        return refuse("row_outside_period", outside_dates=sorted(set(outside))[:10])
    # §6.2 — every statement carries its own proof, and this is the check that
    # earns it: the amounts and directions above are read from the printed
    # columns, so a dropped, mis-columned or double-read row breaks the
    # identity. Refuse the WHOLE statement; never import part of one.
    if closing - opening != deposits - withdrawals:
        return refuse("arithmetic", difference=str((closing - opening) - (deposits - withdrawals)))
    if printed_totals and (
        printed_totals.get("debit", withdrawals) != withdrawals
        or printed_totals.get("credit", deposits) != deposits
    ):
        return refuse(
            "totals_mismatch",
            printed_debit=str(printed_totals.get("debit")),
            printed_credit=str(printed_totals.get("credit")),
        )
    # The one failure the aggregate cannot see: two errors that cancel. Every
    # row's running balance must move by exactly its own amount, in its own
    # direction. Runs AFTER the §6.2 check so the aggregate stays the primary
    # guard and this stays the belt-and-braces one.
    running = opening
    chain_breaks: list[int] = []
    for position, rec in enumerate(records):
        signed = rec["amount"] if rec["direction"] == "in" else -rec["amount"]
        if rec["balance_after"] - running != signed:
            chain_breaks.append(position)
        running = rec["balance_after"]
    if chain_breaks:
        return refuse("balance_chain", chain_breaks=chain_breaks[:10])

    return ParsedStatement(
        status=PARSED,
        opening_balance=opening,
        closing_balance=closing,
        rows=assign_row_ids(
            records, instrument=instrument, statement_id=statement_id, file_sha256=file_sha256
        ),
        diagnostics=diagnostics,
        **base,
    )
