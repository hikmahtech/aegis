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
* **A page prints its own columns, or they are inferred from its own rows.**
  The mailed layout reprints the column header on every page; the netbanking
  layout prints it once and then shifts every later page, so 48 of the 49
  pages of a real FY statement hold rows under no header. Those pages get
  their offsets from their own numbers (`_infer_columns`). Where that leaves
  ONE amount column, position cannot tell a debit from a credit — `pdftotext`
  closes an empty column up — so the direction comes from the running balance,
  and the statement is then refused unless the bank printed its own debit and
  credit totals to check it against. Deriving a direction from the balances
  makes the balance checks agree by construction; only a figure the bank
  printed itself can falsify the result.
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
from dataclasses import dataclass, field, replace
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
# A money token. The integer part is OPTIONAL because Axis prints a balance
# below one rupee with no leading zero — `.35`, not `0.35` — which two rows of
# the real FY2024-25 netbanking statement do. Without it those rows carry no
# running balance, read as unreadable, and the whole 1,619-row statement is
# refused. The lookbehind stops that widening from also matching the tail of
# something already read: `.07` out of `01.07.2026` is a date, not money.
_NUM = re.compile(r"(?<![\d.])-?(?:\d[\d,]*)?\.\d{2}")
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
#: `dd-mm-yyyy` — the width of a printed transaction date.
_DATE_WIDTH = 10
#: Two right-aligned numbers share a column when their end offsets are this
#: close. Measured on the real 48-page netbanking statement: one column's end
#: offsets wobble by at most 2 characters (`pdftotext` lays a proportional font
#: onto a character grid), and the nearest two DIFFERENT amount columns ever
#: came was 7. 4 sits between the two with room on both sides.
_CLUSTER_GAP = 4


@dataclass(frozen=True)
class _Columns:
    date_start: int
    narration_start: int
    narration_end: int
    debit_end: int
    credit_end: int
    balance_end: int
    #: False when the page prints ONE amount column. `debit_end` and
    #: `credit_end` then both hold that column, position cannot say which side
    #: of the ledger it is, and the direction is resolved from the running
    #: balance instead — which `parse_axis_statement` only allows when the bank
    #: printed its own debit and credit totals to check the result against.
    directional: bool = True


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


def _cluster(offsets: Sequence[int]) -> list[list[int]]:
    """Sorted offsets, split wherever the step between them exceeds the gap."""
    groups: list[list[int]] = []
    for offset in sorted(offsets):
        if groups and offset - groups[-1][-1] <= _CLUSTER_GAP:
            groups[-1].append(offset)
        else:
            groups.append([offset])
    return groups


def _first_text(line: str, start: int) -> int | None:
    """The offset of the first non-space character at or after `start`."""
    rest = line[start:]
    stripped = rest.lstrip()
    return start + len(rest) - len(stripped) if stripped else None


def _row_text_after_date(line: str) -> int | None:
    """The offset of the first text after a transaction row's own date."""
    if not _LOOSE_ROW.match(line):
        return None
    date_match = _ROW_DATE.search(line)
    return None if date_match is None else _first_text(line, date_match.end())


def _widen_narration(cols: _Columns, header: str, page: str) -> _Columns:
    """Move `narration_start` out to where the page's own rows print (#428).

    A column header names a column; it does not promise the rows below it are
    flush with the label. On the Axis netbanking layout they are not: measured
    on the real FY2024-25 statement, `Particulars` prints at offset 38 and the
    rows print at 22, so a map built from the label alone cut the first 16
    characters off every narration on that page — and with them the UPI/NEFT
    reference `extract_ref` reads, which is what pass 1 of the matcher joins on.
    It cost 23 of that statement's 1,619 rows their reference. Only the one page
    per netbanking statement that prints a header was affected: `_infer_columns`
    takes this same measurement for the 48 that print none, which is how page
    one came to disagree with the rest of its own statement.

    Two bounds keep the widening honest, and the second is why the header line
    is needed here rather than just the rows:

    * The label is the CEILING, so this only ever widens the cell leftwards. A
      narration cell may safely begin in the whitespace before its text — it is
      stripped — so a row that prints flush with or right of its label needs no
      adjustment and gets none.
    * The end of the PREVIOUS label on the header line is the floor, because
      whatever prints left of that belongs to another column. The mailed layout
      is the case that needs it: `Transaction Details` sits at 28 with a
      `Value Date` column ending at 25, and its rows print their value date at
      15 — measure from the date alone and every mailed narration would swallow
      a second date. The same bound keeps a cheque number in the `Chq No`
      column the netbanking layout prints at 13, whose rows here happen to
      leave it blank.
    """
    floor = len(header[: cols.narration_start].rstrip())
    starts = [
        start
        for start in (_row_text_after_date(line) for line in page.splitlines())
        if start is not None and floor <= start < cols.narration_start
    ]
    return cols if not starts else replace(cols, narration_start=min(starts))


def _infer_columns(page: str) -> _Columns | None:
    """The column offsets of a page that carries rows but reprints no header.

    The mailed Axis layout reprints its column header on every page. The
    netbanking layout prints it once, on page one, and then shifts each page's
    offsets — so 48 of the 49 pages of a real FY statement hold rows under no
    header of their own, and the file used to be refused whole.

    The geometry comes from the page's own rows. Every row of a savings or
    current account statement ends in its running balance, so the LAST number
    on a row is the balance and the one before it is the amount. That per-row
    ordering, rather than a distance threshold, is what keeps a stray number
    inside a narration out of the column map — the real statement has 12 of
    them. The balance offsets must then form exactly ONE column, and the amount
    offsets form either:

    * **two** columns — Axis prints `Debit` to the left of `Credit`, so the
      left one is the debit and the direction is read from position, exactly as
      it is under a printed header; or
    * **one**, and position cannot say which side it is. `pdftotext` closes an
      empty column up, so a debit-only page is laid out identically to a
      credit-only one: on the real statement the single amount column sits
      11-13 characters from the balance whether it is a debit column or a
      credit column. Such a page comes back `directional=False` and its rows
      get their direction from the running balance instead.

    `None` means the page could not be inferred — too many amount columns, a
    split balance column, no readable row — and the caller then refuses it, the
    same outcome a headerless page had before.

    There is deliberately no "are these two columns too close together?" guard.
    Right-aligned money columns are separated by the width of the right one, so
    the closest two can print is three or four characters, which is exactly the
    span a legitimate sub-rupee balance occupies — such a guard would refuse a
    real page and could never be shown to catch a wrong one.
    """
    dates: list[int] = []
    amount_ends: list[int] = []
    amount_starts: list[int] = []
    balance_ends: list[int] = []
    text_starts: list[int] = []
    for line in page.splitlines():
        if _LOOSE_ROW.match(line):
            date = _ROW_DATE.search(line)
            if date is None:
                continue
            dates.append(date.start())
            numbers = list(_NUM.finditer(line))
            if len(numbers) < 2:
                # Not enough to place a column. The row itself is left to the
                # parse loop, which reports it as `unreadable_row`.
                continue
            amount_ends.append(numbers[-2].end())
            amount_starts.append(numbers[-2].start())
            balance_ends.append(numbers[-1].end())
            after_date = _row_text_after_date(line)
            if after_date is not None:
                text_starts.append(after_date)
        elif line.strip() and not _NUM.search(line):
            # A wrapped narration: it prints in the narration column too, and
            # on this layout it is the only other thing on the page.
            first = _first_text(line, 0)
            if first is not None:
                text_starts.append(first)
    if not amount_ends:
        return None

    balance_groups = _cluster(balance_ends)
    if len(balance_groups) != 1:
        return None
    # Every member of a cluster sits within `_CLUSTER_GAP` of its neighbours
    # and `_COL_SLACK` is more than twice that, so which member stands for the
    # column cannot change what any number is read as. Take the edge the column
    # is aligned on: the right for money, the left for the date.
    balance_end = max(balance_groups[0])
    amount_groups = _cluster(amount_ends)
    if len(amount_groups) > 2:
        return None
    directional = len(amount_groups) == 2
    debit_end = max(amount_groups[0])
    credit_end = max(amount_groups[-1])

    date_start = min(dates)
    narration_end = min(amount_starts)
    floor = date_start + _DATE_WIDTH
    inside = [start for start in text_starts if floor <= start < narration_end]
    narration_start = min(inside) if inside else floor
    return _Columns(
        date_start=date_start,
        narration_start=narration_start,
        narration_end=max(narration_start, narration_end),
        debit_end=debit_end,
        credit_end=credit_end,
        balance_end=balance_end,
        directional=directional,
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
    the row reads as unparseable rather than as half of itself. On a page whose
    one amount column has no known side the amount comes back under `amount`,
    never under a guessed `debit`.
    """
    targets = (
        {"debit": cols.debit_end, "credit": cols.credit_end, "balance": cols.balance_end}
        if cols.directional
        else {"amount": cols.debit_end, "balance": cols.balance_end}
    )
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
    #: The original amount of a transaction charged abroad, where the bank
    #: printed it (§8.5). Only the card layout does — `( USD 5.89 )` beside the
    #: rupee charge — and it is the exact figure the journal block holds, so
    #: keeping it saves the matcher from converting a rupee amount back through
    #: a rate production does not have. `amount` stays the rupee amount that
    #: hit the account; these two are never a substitute for it, and they are
    #: deliberately NOT part of `row_id` (§8.3 fixes what is).
    fx_currency: str | None = None
    fx_amount: Decimal | None = None


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
                # Optional, and absent from every record the two bank-account
                # parsers build: only a card prints the original of a foreign
                # charge (§8.5).
                fx_currency=rec.get("fx_currency"),
                fx_amount=rec.get("fx_amount"),
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
    inferred_pages: list[int] = []
    ambiguous_pages: list[int] = []
    index = -1
    stopped = False

    # The column map is PER PAGE. `pdftotext -layout` sizes each column to the
    # widest thing on that page, so the offsets move from page to page — the
    # mailed layout reprints its header on every page and is read page by page.
    # A page that prints no header of its own is never read through the
    # PREVIOUS page's map, which is how a credit becomes a debit: its offsets
    # are inferred from its own rows, and a page whose geometry will not resolve
    # is refused exactly as it was before.
    for page_number, page in enumerate(text.split("\f")):
        if stopped:
            break
        lines = page.splitlines()
        cols: _Columns | None = None
        if not any(_column_map(line) is not None for line in lines):
            cols = _infer_columns(page)
            if cols is not None:
                inferred_pages.append(page_number)
                if not cols.directional:
                    ambiguous_pages.append(page_number)
        for line in lines:
            index += 1
            mapped = _column_map(line)
            if mapped is not None:
                cols = _widen_narration(mapped, line, page)
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
            balance = numbers.get("balance")
            direction: str | None
            if cols.directional:
                debit, credit = numbers.get("debit"), numbers.get("credit")
                if (debit is None) == (credit is None) or balance is None:
                    unreadable.append(index)
                    continue
                amount = debit if debit is not None else credit
                direction = "out" if debit is not None else "in"
            else:
                # One amount column, side unknown. The direction is left for
                # the running balance to settle, below.
                amount = numbers.get("amount")
                if amount is None or balance is None:
                    unreadable.append(index)
                    continue
                direction = None
            if amount < 0:
                unreadable.append(index)
                continue
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

    # A row off an ambiguous page arrives with no direction, because its page
    # prints one amount column and `pdftotext` lays a debit-only page out
    # exactly like a credit-only one. The running balance is the only thing on
    # such a page that can say which side a row is, so it decides.
    #
    # And BECAUSE it decides, the §6.2 identity below stops being a check on
    # these rows: derive each direction from the balances and the balances
    # agree by construction, so a check that can never fail would sit in the
    # code reading like safety. The bank's own printed debit and credit totals
    # are the independent evidence, and a statement that carries none is
    # refused rather than trusted — see `no_independent_totals`.
    inferred_directions = 0
    undetermined: list[int] = []
    if opening is not None:
        running = opening
        for position, rec in enumerate(records):
            if rec["direction"] is None:
                inferred_directions += 1
                delta = rec["balance_after"] - running
                if rec["amount"] > 0 and delta == rec["amount"]:
                    rec["direction"] = "in"
                elif rec["amount"] > 0 and delta == -rec["amount"]:
                    rec["direction"] = "out"
                else:
                    # A zero amount reads both ways and a balance that moved by
                    # something else reads neither. Both are refusals: a
                    # direction is posted money, never a guess.
                    undetermined.append(position)
            running = rec["balance_after"]

    deposits = sum((r["amount"] for r in records if r["direction"] == "in"), Decimal("0"))
    withdrawals = sum((r["amount"] for r in records if r["direction"] == "out"), Decimal("0"))
    diagnostics: dict[str, Any] = {
        "rows_read": len(records),
        "reprinted_rows": len(reprinted),
        "deposits": str(deposits),
        "withdrawals": str(withdrawals),
        "layout": header.layout,
        # Which pages were read through a map inferred from their own rows
        # rather than one the bank printed, and which of those could not be
        # told debit from credit by position alone.
        "inferred_column_pages": sorted(set(inferred_pages)),
        "ambiguous_column_pages": sorted(set(ambiguous_pages)),
        "inferred_direction_rows": inferred_directions,
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
    if undetermined:
        return refuse("undetermined_direction", undetermined_rows=undetermined[:10])
    outside = [r["occurred_on"].isoformat() for r in records
               if not header.period_start <= r["occurred_on"] <= header.period_end]
    if outside:
        return refuse("row_outside_period", outside_dates=sorted(set(outside))[:10])
    # The gate on the inference above. A direction taken from the running
    # balance makes both balance checks below tautological for that row, so a
    # statement holding one has to be checked against a figure the bank printed
    # itself. Axis prints `TRANSACTION TOTAL` with separate debit and credit
    # sides on both layouts, including the last page of a 48-page netbanking
    # statement; a statement that does not is refused rather than read on the
    # strength of a check that cannot fail.
    if inferred_directions and not printed_totals:
        return refuse("no_independent_totals")
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
