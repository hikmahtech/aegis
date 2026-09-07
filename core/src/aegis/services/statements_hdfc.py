"""HDFC SmartStatement parsing — the HTML lane (spec
`2026-09-07-statement-reconciliation-design.md` §6.1, §6.2, §8.3; retrieval and
table shape in `hdfc-smartstatement-recipe.md`).

HDFC is the easier bank. The statement is **HTML**, not PDF: the transactions
are a real table with labelled columns, so there is no `pdftotext`, no column
reconstruction and no coordinate arithmetic. Everything that made the Axis
parser in `statements.py` hard — a per-page column map, right-aligned numbers
matched to header offsets, narration continuation lines — simply does not arise
here. What survives is the part that is about *trust*, and it is the same three
rules:

* **Identify by header anchor only** (§6.1). The account and the period come
  from the statement's own header block, never from a filename, a folder name
  or an email subject. Anchors are matched against text **outside** the
  transaction table, so no narration can ever move the account or the period.
  Anything matching no anchor is `UNIDENTIFIED` and stays visible.
* **The statement proves itself** (§6.2). Closing minus opening must equal
  deposits minus withdrawals, and HDFC prints its own `Debit Amount` /
  `Credit Amount` / `Debit Count` / `Credit Count` summary, which must agree
  too. A statement failing any of them is **refused whole** — `rows` comes back
  empty. There is no partial import to be had.
* **`row_id` is layout-independent** (§8.3), and is built from the
  **transaction date**, never the value date. HDFC prints both, in adjacent
  labelled columns, and the running balance is in transaction-date order.

Model-free by design (§6.3): the columns are labelled, so nothing here needs a
language model and none may be added.

Two choices worth knowing before editing.

**The table is found by its column header text, never by `id="Table76"`.**
Every observed file carries that id and it would be the shortest possible
selector — which is exactly the problem. It is a generated identifier from
HDFC's JSP templating, not a contract: it moves when the page's element
ordering moves, and when it does, an id-keyed parser finds no table and returns
an empty statement that reads as a quiet success. The column labels are what a
human reads the statement by, so they are what the bank has to keep stable, and
a change to them is a change the arithmetic check will see too.

**`Transaction details` is the column, not `Narration`.** `Narration` is the PDF
statement's wording for the same thing. Matching the wrong label finds zero
rows, and a zero-row statement whose arithmetic is then trivially true is the
silent-wrong-number failure this lane exists to prevent — so a table whose
header does not carry every required label is **refused by name**
(`unknown_columns`, with the missing labels in the diagnostics), never read
past.

This module deliberately neither creates nor edits `statements.py`: it imports
the record shape, the `row_id` construction and the outcome vocabulary from it
read-only, so both banks produce one kind of row.
"""

from __future__ import annotations

import re
from collections.abc import Collection
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from typing import Any

from aegis.services import books
from aegis.services.statements import (
    PARSED,
    REFUSED,
    UNIDENTIFIED,
    ParsedStatement,
    assign_row_ids,
    statement_id_for,
)

_CENT = Decimal("0.01")

# ------------------------------------------------------------------ HTML reading


@dataclass(frozen=True)
class _Text:
    """One text node, and whether it came from inside a table cell.

    The flag exists for exactly one reason: a header anchor may only be matched
    against text from OUTSIDE the transaction table (§6.1). `Closing Balance` is
    both a summary label and a column header in this document, so a label/value
    pairing that ignored the flag would read the table's header cell as the
    summary label and take the first transaction's date as the closing balance.
    """

    in_cell: bool
    text: str


_CELL_TAGS = frozenset({"td", "th"})
#: Tags that end a run of text inside a cell. A `<br>` between two words must
#: not glue them together: the narration is displayed and rule-matched, so
#: "ABCDEF" where the bank wrote "ABC DEF" is a silent corruption.
_BREAKING_TAGS = frozenset({"br", "p", "div", "tr", "td", "th", "li", "table"})


class _Document(HTMLParser):
    """One pass over the page: the text nodes in order, and every table's rows.

    `html.parser` is the standard library's tolerant tokenizer, which is the
    right tool for a JSP's output — unclosed `<p>`, stray `<font>`, attribute
    quoting nobody validates — and it keeps this lane free of another
    dependency. Tables are collected through a stack so a nested table cannot
    merge its rows into its parent's.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.texts: list[_Text] = []
        self.tables: list[list[list[str]]] = []
        # Stacks throughout, so a table nested inside a cell keeps its own rows
        # AND leaves the enclosing cell intact. Without that the outer row comes
        # back one cell short, every column after it shifts, and a value date is
        # read as a closing balance.
        self._open_tables: list[list[list[str]]] = []
        self._rows: list[list[str]] = []
        self._cells: list[list[str]] = []

    def _feed_cells(self, text: str) -> None:
        for cell in self._cells:
            cell.append(text)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _BREAKING_TAGS:
            self._feed_cells(" ")
        if tag == "table":
            table: list[list[str]] = []
            self._open_tables.append(table)
            self.tables.append(table)
        elif tag == "tr":
            self._rows.append([])
        elif tag in _CELL_TAGS:
            self._cells.append([])

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _BREAKING_TAGS:
            self._feed_cells(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag == "table":
            if self._open_tables:
                self._open_tables.pop()
        elif tag in _CELL_TAGS:
            if self._cells:
                cell = self._cells.pop()
                if self._rows:
                    self._rows[-1].append(collapse("".join(cell)))
        elif tag == "tr" and self._rows:
            row = self._rows.pop()
            if self._open_tables:
                self._open_tables[-1].append(row)

    def handle_data(self, data: str) -> None:
        self._feed_cells(data)
        text = collapse(data)
        if text:
            self.texts.append(_Text(bool(self._cells), text))


def collapse(text: str) -> str:
    r"""Whitespace-collapsed. `str.split()` counts `\xa0` (from `&nbsp;`) as
    whitespace, so a non-breaking space never survives into a narration."""
    return " ".join(text.split())


# ------------------------------------------------------------------ §6.1 anchors

#: The statement's own period line. Observed verbatim as
#: `Statement Period:From 12/07/2026 To 11/08/2026` — no space after the colon,
#: which is why the spec's spelling of it must not be matched literally.
#: Anchored at the start of a text node and read only from outside the
#: transaction table, so nothing a narration can hold is ever read as a period.
_PERIOD = re.compile(
    r"^Statement\s*Period\s*:?\s*From\s*(\d{2}[/-]\d{2}[/-]\d{4})"
    r"\s*To\s*(\d{2}[/-]\d{2}[/-]\d{4})",
    re.I,
)

#: A list, not one string (§6.1: each bank gets a *list* of header patterns), so
#: a second HDFC layout is a line here rather than a rewrite. The label node is
#: followed by the account number inside a `<select><option>`, so the value is
#: taken from the next text node whatever tag produced it.
_ACCOUNT_LABELS = ("account number", "account no", "a/c no", "account no.", "a/c no.")

#: The printed summary block: a label node, then its value node.
_OPENING_LABEL = "opening balance"
_CLOSING_LABEL = "closing balance"
_DEBIT_TOTAL_LABEL = "debit amount"
_CREDIT_TOTAL_LABEL = "credit amount"
_DEBIT_COUNT_LABEL = "debit count"
_CREDIT_COUNT_LABEL = "credit count"

_ACCOUNT_VALUE = re.compile(r"^[Xx*]*(\d{4,})$")
_AMOUNT = re.compile(r"^-?\d[\d,]*(?:\.\d{1,2})?$")
_COUNT = re.compile(r"^\d{1,6}$")


@dataclass(frozen=True)
class HdfcHeader:
    account_tail: str
    period_start: date
    period_end: date


def _d(text: str) -> date:
    return datetime.strptime(text.replace("-", "/"), "%d/%m/%Y").date()


def _label(text: str) -> str:
    return text.strip().rstrip(":").strip().casefold()


def _outside_table(texts: list[_Text]) -> list[_Text]:
    """Header anchors are read only from here — never from a `<td>`/`<th>`."""
    return [t for t in texts if not t.in_cell]


def identify_hdfc(texts: list[_Text]) -> tuple[HdfcHeader | None, str]:
    """(header, reason) from the statement's own header block.

    Every anchor in the file must agree. A document naming two accounts or two
    periods is a misfile, or a page that bundled somebody else's statement, and
    §6.1 is explicit that such a thing is reported and never guessed at.
    """
    outside = _outside_table(texts)

    periods: set[tuple[date, date]] = set()
    for node in outside:
        match = _PERIOD.match(node.text)
        if match:
            try:
                periods.add((_d(match.group(1)), _d(match.group(2))))
            except ValueError:
                return None, "unreadable_period"

    accounts: set[str] = set()
    for position, node in enumerate(outside):
        if _label(node.text) not in _ACCOUNT_LABELS:
            continue
        # The value follows the label — one `<option>` per account in every
        # observed file. Everything account-shaped up to the next non-matching
        # node is collected, so a select listing two accounts CONFLICTS (and is
        # reported) rather than silently handing over the first.
        for following in outside[position + 1 :]:
            value = _ACCOUNT_VALUE.match(following.text)
            if value is None:
                break
            accounts.add(value.group(1))

    if not periods and not accounts:
        return None, "no_header_anchor"
    if not periods:
        return None, "no_period_anchor"
    if not accounts:
        return None, "no_account_anchor"
    if len(periods) > 1:
        return None, "conflicting_periods"
    if len({a[-4:] for a in accounts}) > 1:
        return None, "conflicting_accounts"
    start, end = next(iter(periods))
    if start > end:
        return None, "reversed_period"
    return HdfcHeader(next(iter(accounts))[-4:], start, end), ""


def _paired_value(texts: list[_Text], label: str, shape: re.Pattern[str]) -> Decimal | None:
    """The value printed beside a summary label.

    None when the label is absent, when nothing value-shaped follows it, or when
    the page prints it twice with different values — each of which the caller
    turns into a refusal rather than a guess. Read from outside the table only:
    `Closing Balance` is a column header as well as a summary label here.
    """
    found: set[Decimal] = set()
    outside = _outside_table(texts)
    for position, node in enumerate(outside):
        if _label(node.text) != label:
            continue
        for following in outside[position + 1 :]:
            if _label(following.text) == label:
                continue
            if shape.match(following.text):
                found.add(_amount(following.text))
            break
    return found.pop() if len(found) == 1 else None


def _amount(token: str) -> Decimal:
    try:
        return Decimal(token.replace(",", "")).quantize(_CENT)
    except InvalidOperation as exc:  # pragma: no cover — the regex fixes the shape
        raise ValueError(f"unreadable amount: {token!r}") from exc


# -------------------------------------------------------------------- the table

#: The seven labelled columns of the SmartStatement table, casefolded. HDFC
#: prints two more after them (`COL8`, `COL9`) that carry no header meaning;
#: keying on labels means extra columns are ignored, where a positional parser
#: would shift every value one place the day a column is added.
COL_DATE = "date"
COL_NARRATION = "transaction details"
COL_REF = "cheque/ref no"
COL_VALUE_DATE = "value date"
COL_WITHDRAWAL = "withdrawal"
COL_DEPOSIT = "deposit"
COL_BALANCE = "closing balance"
REQUIRED_COLUMNS = (
    COL_DATE,
    COL_NARRATION,
    COL_REF,
    COL_VALUE_DATE,
    COL_WITHDRAWAL,
    COL_DEPOSIT,
    COL_BALANCE,
)

#: `Narration` is the PDF statement's word for `Transaction details`. Named here
#: so a statement carrying it is refused with a reason a human can act on,
#: instead of matching nothing and reading as an empty statement.
PDF_NARRATION_LABEL = "narration"

#: What the bank prints in a column that does not apply to the row.
_BLANK_CELLS = frozenset({"", "-", "--", "n/a", "na", "nil"})

#: A reference is at least nine digits: a UPI RRN is twelve, an IMPS reference
#: twelve, a NEFT/RTGS UTR longer still. A cheque number is six, and a cheque
#: number read as a reference would mis-attribute a payment on pass 1 of the
#: matcher, which is an exact join — so the floor excludes them by construction.
_REF_DIGITS = re.compile(r"\d{9,}")

_ROW_DATE = re.compile(r"^(\d{2}[/-]\d{2}[/-]\d{4})$")


@dataclass(frozen=True)
class TransactionTable:
    """The transaction table, and where each labelled column sits in its rows."""

    columns: dict[str, int]
    rows: list[list[str]]


def find_transaction_table(
    tables: list[list[list[str]]],
) -> tuple[TransactionTable | None, str, list[str]]:
    """(table, reason, missing_labels), keyed on the column header TEXT.

    Never on `id="Table76"`: that id is generated by HDFC's templating, and an
    id-keyed parser that stops matching returns an empty statement that reads as
    a quiet success.
    """
    best_missing: list[str] = []
    for table in tables:
        for index, row in enumerate(table):
            labels = [_label(cell) for cell in row]
            missing = [c for c in REQUIRED_COLUMNS if c not in labels]
            if not missing:
                return (
                    TransactionTable(
                        columns={c: labels.index(c) for c in REQUIRED_COLUMNS},
                        rows=table[index + 1 :],
                    ),
                    "",
                    [],
                )
            # A near miss is worth naming. A header carrying the money columns
            # but not the narration one is the `Narration`/`Transaction details`
            # trap, and refusing it BY NAME is the whole point of the check.
            if len(missing) < len(REQUIRED_COLUMNS) - 2 and (
                not best_missing or len(missing) < len(best_missing)
            ):
                best_missing = missing
    if best_missing:
        return None, "unknown_columns", best_missing
    return None, "no_transaction_table", []


def _cell(row: list[str], columns: dict[str, int], name: str) -> str:
    index = columns[name]
    return row[index] if index < len(row) else ""


def _cell_amount(text: str) -> Decimal | None:
    """An amount cell, or None where the bank printed a blank (`-`) instead."""
    cleaned = collapse(text)
    if cleaned.casefold() in _BLANK_CELLS:
        return None
    if not _AMOUNT.match(cleaned):
        raise ValueError(f"unreadable amount cell: {text!r}")
    return _amount(cleaned)


def extract_ref(ref_cell: str, narration: str) -> str | None:
    """The bank's own reference for the row, or None.

    HDFC prints a dedicated `Cheque/Ref No` column, so unlike Axis there is no
    need to mine prose: a labelled field the bank fills is better evidence than
    a regex over a narration. The narration stays the fallback, because that
    column is `-` on cash and charge rows where the narration can still carry a
    reference.

    Only a run of nine or more digits counts, and only when the source holds
    exactly one. `journal_index.ref` stores the bare digits a bank alert quoted,
    so this has to be the same shape for pass 1 of the matcher to join at all;
    anything shorter is a cheque number, not a reference; and two candidates in
    one cell is an ambiguity, which §8.1 says never to resolve by guessing.
    """
    for source in (ref_cell, narration):
        digits = _REF_DIGITS.findall(collapse(source))
        if len(digits) == 1:
            return digits[0]
    return None


# ------------------------------------------------------------------- the parser


def parse_hdfc_statement(
    html: str,
    *,
    file_sha256: str,
    subject: str = "",
    declared: Collection[str] = (),
) -> ParsedStatement:
    """One HDFC SmartStatement page -> a parsed, self-validated statement.

    `subject` is recorded for the digest and **never read** for the account or
    the period (§6.1) — and neither is the filename, nor the Drive folder the
    file came out of, which is why neither is a parameter.
    """
    document = _Document()
    document.feed(html)

    header, reason = identify_hdfc(document.texts)
    if header is None:
        return ParsedStatement(
            status=UNIDENTIFIED, reason=reason, file_sha256=file_sha256, subject_hint=subject
        )

    instrument = books.canonical_instrument(f"hdfc-{header.account_tail}", declared) or ""
    statement_id = statement_id_for(instrument, header.period_start, header.period_end)
    base = {
        "instrument": instrument,
        "period_start": header.period_start,
        "period_end": header.period_end,
        "statement_id": statement_id,
        "file_sha256": file_sha256,
        "subject_hint": subject,
    }
    opening = _paired_value(document.texts, _OPENING_LABEL, _AMOUNT)
    closing = _paired_value(document.texts, _CLOSING_LABEL, _AMOUNT)
    printed_debit = _paired_value(document.texts, _DEBIT_TOTAL_LABEL, _AMOUNT)
    printed_credit = _paired_value(document.texts, _CREDIT_TOTAL_LABEL, _AMOUNT)
    printed_debit_count = _paired_value(document.texts, _DEBIT_COUNT_LABEL, _COUNT)
    printed_credit_count = _paired_value(document.texts, _CREDIT_COUNT_LABEL, _COUNT)
    diagnostics: dict[str, Any] = {}

    def refuse(why: str, **extra: Any) -> ParsedStatement:
        return ParsedStatement(
            status=REFUSED,
            reason=why,
            opening_balance=opening,
            closing_balance=closing,
            diagnostics={**diagnostics, **extra},
            **base,
        )

    table, table_reason, missing = find_transaction_table(document.tables)
    if table is None:
        return refuse(table_reason, missing_columns=missing)

    records: list[dict[str, Any]] = []
    refs: list[str | None] = []
    unreadable: list[int] = []
    value_date_differs = 0
    for position, row in enumerate(table.rows):
        if not any(collapse(cell) for cell in row):
            continue  # a spacer row: entirely empty, and so carries no money
        date_cell = collapse(_cell(row, table.columns, COL_DATE))
        # A row with no date but something in it is NOT a spacer. Skipping one
        # silently would drop money the arithmetic check cannot then see, since
        # the check only ever sums the rows that were read.
        matched = _ROW_DATE.match(date_cell)
        if matched is None:
            unreadable.append(position)
            continue
        try:
            # The TRANSACTION date (§8.3). `Value date` is a separate labelled
            # column printed right beside it that this parser never reads for
            # `occurred_on`: the running balance is in transaction-date order,
            # so keying on the value date would break the balance chain and the
            # row ids at once.
            occurred_on = _d(matched.group(1))
            withdrawal = _cell_amount(_cell(row, table.columns, COL_WITHDRAWAL))
            deposit = _cell_amount(_cell(row, table.columns, COL_DEPOSIT))
            balance = _cell_amount(_cell(row, table.columns, COL_BALANCE))
        except ValueError:
            unreadable.append(position)
            continue
        if (withdrawal is None) == (deposit is None) or balance is None:
            unreadable.append(position)
            continue
        amount = withdrawal if withdrawal is not None else deposit
        if amount is None or amount < 0:
            unreadable.append(position)
            continue
        narration = _cell(row, table.columns, COL_NARRATION)
        value_date = collapse(_cell(row, table.columns, COL_VALUE_DATE))
        if _ROW_DATE.match(value_date) and _d(value_date) != occurred_on:
            value_date_differs += 1
        records.append(
            {
                "occurred_on": occurred_on,
                "direction": "out" if withdrawal is not None else "in",
                "amount": amount,
                "balance_after": balance,
                "narration": narration,
            }
        )
        refs.append(extract_ref(_cell(row, table.columns, COL_REF), narration))

    deposits = sum((r["amount"] for r in records if r["direction"] == "in"), Decimal("0"))
    withdrawals = sum((r["amount"] for r in records if r["direction"] == "out"), Decimal("0"))
    debit_rows = sum(1 for r in records if r["direction"] == "out")
    diagnostics.update(
        {
            "rows_read": len(records),
            "deposits": str(deposits),
            "withdrawals": str(withdrawals),
            "value_date_differs": value_date_differs,
            "refs_found": sum(1 for r in refs if r),
        }
    )

    if unreadable:
        return refuse("unreadable_row", unreadable_rows=unreadable[:10])
    if opening is None or closing is None:
        return refuse("missing_balance_anchor")
    outside = [
        r["occurred_on"].isoformat()
        for r in records
        if not header.period_start <= r["occurred_on"] <= header.period_end
    ]
    if outside:
        return refuse("row_outside_period", outside_dates=sorted(set(outside))[:10])
    # §6.2 — the statement's own proof, and the check that earns the lane: it
    # fails whenever a row was dropped, mis-columned or read twice. Refuse the
    # WHOLE statement; never import part of one.
    if closing - opening != deposits - withdrawals:
        return refuse("arithmetic", difference=str((closing - opening) - (deposits - withdrawals)))
    # HDFC prints its own totals and counts, which state the same fact a second
    # and independent time — so they catch the one thing the identity cannot: a
    # debit and a credit of equal size dropped together.
    if (printed_debit is not None and printed_debit != withdrawals) or (
        printed_credit is not None and printed_credit != deposits
    ):
        return refuse(
            "totals_mismatch", printed_debit=str(printed_debit), printed_credit=str(printed_credit)
        )
    if (printed_debit_count is not None and int(printed_debit_count) != debit_rows) or (
        printed_credit_count is not None and int(printed_credit_count) != len(records) - debit_rows
    ):
        return refuse(
            "count_mismatch",
            printed_debit_count=str(printed_debit_count),
            printed_credit_count=str(printed_credit_count),
        )
    # The one failure an aggregate cannot see: two errors that cancel. Every
    # row's running balance must move by exactly its own amount, in its own
    # direction. Runs AFTER the §6.2 check so that stays the primary guard and
    # this stays the belt-and-braces one.
    running = opening
    chain_breaks: list[int] = []
    for position, rec in enumerate(records):
        signed = rec["amount"] if rec["direction"] == "in" else -rec["amount"]
        if rec["balance_after"] - running != signed:
            chain_breaks.append(position)
        running = rec["balance_after"]
    if chain_breaks:
        return refuse("balance_chain", chain_breaks=chain_breaks[:10])

    rows = assign_row_ids(
        records, instrument=instrument, statement_id=statement_id, file_sha256=file_sha256
    )
    # `assign_row_ids` mines the narration for a reference, which is right for
    # Axis and second best here: HDFC prints a labelled `Cheque/Ref No` column,
    # so the column wins and the mined value stays the fallback.
    rows = tuple(replace(row, ref=ref or row.ref) for row, ref in zip(rows, refs, strict=True))

    return ParsedStatement(
        status=PARSED,
        opening_balance=opening,
        closing_balance=closing,
        rows=rows,
        diagnostics=diagnostics,
        **base,
    )


__all__ = [
    "COL_BALANCE",
    "COL_DATE",
    "COL_DEPOSIT",
    "COL_NARRATION",
    "COL_REF",
    "COL_VALUE_DATE",
    "COL_WITHDRAWAL",
    "PDF_NARRATION_LABEL",
    "REQUIRED_COLUMNS",
    "HdfcHeader",
    "TransactionTable",
    "collapse",
    "extract_ref",
    "find_transaction_table",
    "identify_hdfc",
    "parse_hdfc_statement",
]
