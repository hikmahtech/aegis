"""Axis credit-card statement parsing — the PDF card layout (spec
`2026-09-07-statement-reconciliation-design.md` §6.1, §6.2, §6.3, §8.3, §8.5).

The card is a different document from the savings/current statement in
`statements.py`, so it gets its own module — one bank *and product* per parser,
the house pattern `statements_hdfc.py` set. It shares the record shape, the
`row_id` construction and the outcome vocabulary with `statements.py`,
read-only, so all three parsers emit one kind of row.

Deterministic and model-free (§6.3): the card prints labelled columns and a
`Dr`/`Cr` suffix on every amount.

**The card is easier than the netbanking layout in one way and harder in
another.** Easier: the suffix states the direction, so nothing has to be
inferred from position or from a running balance — a debit-only page and a
credit-only page are not the same picture here. Harder: there is no running
balance column at all, so the balance chain that catches two cancelling errors
in the other two parsers does not exist. What replaces it is the bank's own
arithmetic, which the card prints in full and the savings statement does not.

Four rules do the load-bearing work.

* **Identify by header anchor only** (§6.1). Two whole-line anchors: the
  `Card No: … Name …` line that opens the table, and the payment-summary column
  labels whose values row carries the `dd/mm/yyyy - dd/mm/yyyy` period. Neither
  can appear in a transaction row, which starts with a single leading date. A
  substring check on `Credit Card` once filed nine Axis *current account*
  statements as credit-card, because a current account carries a
  `CreditCard Payment` narration row — so nothing here scans for a product name,
  and the statement's own boilerplate line `payable to Axis Bank Card No.<n>`
  does not match the anchor either: the anchor needs the colon and the `Name`
  field.
* **The statement proves itself, and a card proves itself harder than a bank
  account does** (§6.2). The card prints its own identity —
  `Previous Balance - Payments - Credits + Purchase + Cash Advance +
  Other Debit&Charges = Total Payment Due` — with the seven figures under it.
  So the parsed `Cr` rows must sum to `Payments + Credits`, the parsed `Dr` rows
  to `Purchase + Cash Advance + Other Debit&Charges`, and the bank's own
  identity must close — if it does not, this parser misread the figures it is
  checking everything else against. Any failure refuses the **whole**
  statement: `rows` comes back empty, because a parser that quietly returns
  fewer rows than the statement holds is the worst outcome in this lane.
  §6.2's "closing minus opening equals the difference" then FOLLOWS from those
  three and is deliberately not written out a fourth time — an `if` that cannot
  fail is not a check. Verified against three real statements, where every
  figure holds to the paisa.
* **A card statement DOES support §9.3's closing-balance check.** The lane
  assumed cards could not, because they carry no per-row running balance. They
  carry the two numbers that matter: `Previous Balance` is the opening and
  `Total Payment Due` is the closing, both printed with a `Dr`/`Cr` suffix. So
  step 5 can assert an Axis card the same way it asserts a bank account —
  `closing_balance` is what the account owed at `period_end`, positive when
  money is owed and negative on a credit balance.
* **`row_id` is layout-independent** (§8.3). A card has no running balance, so
  §8.3's fallback stands: the normalised narration takes that slot. That makes
  the narration load-bearing here in a way it is not for the other two parsers,
  and it is why the narration is read as *content* — everything the bank printed
  between the date and the amount, whitespace collapsed — and never by column
  offset. `pdftotext -layout` gives the card two geometries (page one's amount
  column ends at character 230, page two's at 129, and the merchant-category
  column moves with it), so a narration sliced by offset would hash the same row
  differently on the two pages. Splitting the merchant category into its own
  field was rejected for the same reason: the split would have to come from a
  per-page offset, and getting it wrong moves text between two fields, one of
  which is in the key.

**The foreign-currency bracket is kept** (§8.5). A card row that was charged
abroad prints the original beside the rupee amount — `( USD 5.89 )` — and that
is the exact number the journal holds. §8.5's alternative is to match a `$5.89`
block to a `₹567.87` row through `prices.journal` and a 5% markup band, and
production has no USD or GBP rate at all. So `fx_currency` and `fx_amount` are
parsed onto the row and stored. Nothing here matches on them; it only refuses to
throw them away.

**The printed `Statement Period` is not the transaction window.** All three real
statements run from two days before `period_start` to one day before
`period_end` — the period is the billing cycle, and `period_end` is the
statement generation date. So rows before `period_start` are normal and are not
refused; a row dated *after* `period_end` is refused, because nothing can be
charged after the statement was generated and §9.3 asserts the balance at that
date.
"""

from __future__ import annotations

import re
from collections.abc import Collection
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
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

#: This module's `diagnostics["layout"]`, and the value the intake reports.
LAYOUT = "axis_card"

# ------------------------------------------------------------------- §6.1 anchors

#: The account. Whole-line, and it needs BOTH the colon and the `Name` field:
#: the same page prints `payable to Axis Bank Card No.<number> . Please write
#: your NAME…` in its boilerplate, which is the near-miss this shape excludes.
#: A transaction row starts with its date, so no narration can ever match.
_ACCOUNT = re.compile(r"^\s*Card No\s*:\s*([0-9Xx*]{4,})\s+Name\s+\S.*$")

#: The payment-summary column labels — a whole line, no digits in it. The period
#: is read from the values row printed under it, never from a scan of the whole
#: file: `dd/mm/yyyy - dd/mm/yyyy` is a shape a narration could print one day
#: (`INSURANCE CAMPAIGN CB OFFER 1 JUN26-30 JUN26` already prints a date range in
#: another notation), and the anchored form cannot be reached from a row.
_SUMMARY_LABELS = re.compile(
    r"^\s*Total Payment Due\s+Minimum Payment Due\s+Statement Period"
    r"\s+Payment Due Date\s+Statement Generation Date\s*$"
)

#: The bank's own arithmetic, printed as a label line with its seven figures on
#: a line below. Anchored at the START of the line and not at the end, because
#: Axis prints unrelated marketing text to the right of both lines. A
#: transaction row begins with a date, so the anchor still cannot be a row.
_TOTALS_LABELS = re.compile(
    r"^\s*Previous Balance\s*-\s*Payments\s*-\s*Credits\s*\+\s*Purchase"
    r"\s*\+\s*Cash Advance\s*\+\s*Other Debit&Charges\s*=\s*Total Payment Due\b"
)

#: How far below a label line its values row may sit. Both blocks print theirs
#: within two lines on every real statement; three leaves room for a blank line
#: the bank adds without moving anything else.
_VALUES_WITHIN = 3

_PERIOD = re.compile(r"(\d{2}/\d{2}/\d{4})\s*-\s*(\d{2}/\d{2}/\d{4})")

#: A money token, with the `Dr`/`Cr` suffix the card prints on a balance. The
#: integer part is optional for the same reason as in `statements.py`: Axis
#: prints some sub-rupee figures with no leading zero. The lookbehind stops a
#: token from being read out of the tail of something already read.
_MONEY = re.compile(r"(?<![\d.])((?:\d[\d,]*)?\.\d{2})(?:\s+(Dr|Cr))?")

# --------------------------------------------------------------------- the table

#: One printed transaction: a leading date, everything the bank wrote, then the
#: amount and its direction at the end of the line. Anchored at both ends, so
#: the amount is the LAST token on the line and a number inside the narration
#: (`EMI INTEREST - 2/6, REF# 74830698`, `MAILCHIMP,678-9990141`) can never be
#: read as one.
_ROW = re.compile(r"^\s*(\d{2}/\d{2}/\d{4})\s+(\S.*?)\s+((?:\d[\d,]*)?\.\d{2})\s+(Dr|Cr)\s*$")

#: Row-shaped, for diagnosis only: a line that leads with a date but does not
#: parse is money the statement holds and this parser did not read, so it is
#: named rather than skipped. On all three real statements every date-led line
#: before the end marker is a row, and no line after it leads with a date.
_DATED = re.compile(r"^\s*\d{2}/\d{2}/\d{4}\s")

#: Where the table ends. Belt to the row shape's braces: the pages after it are
#: boilerplate carrying example amounts (`Rs. 25000.00`) and a worked table of
#: `Db` entries. The row shape is what actually excludes them — none of those
#: lines leads with a `dd/mm/yyyy` date — so a statement that stops printing the
#: marker is still read correctly, and the marker only means those pages are
#: never looked at. The `EMI BALANCES` block just above it carries no leading
#: date and is excluded by the same shape.
END_MARKER = "**** End of Statement ****"

#: The original amount of a transaction charged abroad, printed by the bank
#: beside the rupee amount (§8.5).
_FX = re.compile(r"\(\s*([A-Z]{3})\s+((?:\d[\d,]*)?\.\d{2})\s*\)")


@dataclass(frozen=True)
class CardHeader:
    account_tail: str
    period_start: date
    period_end: date


@dataclass(frozen=True)
class CardTotals:
    """The seven figures the bank prints under its own identity line.

    `opening` and `closing` are signed: positive is owed, negative is a credit
    balance on the card. The five in between are magnitudes, as printed.
    """

    opening: Decimal
    payments: Decimal
    credits: Decimal
    purchase: Decimal
    cash_advance: Decimal
    other_charges: Decimal
    closing: Decimal

    @property
    def holds(self) -> bool:
        """Does the bank's own arithmetic close? A `False` means this parser
        misread the figures, not that the bank cannot add up."""
        return (
            self.opening
            - self.payments
            - self.credits
            + self.purchase
            + self.cash_advance
            + self.other_charges
            == self.closing
        )


def _d(text: str) -> date:
    return datetime.strptime(text, "%d/%m/%Y").date()


def _amount(token: str) -> Decimal:
    try:
        return Decimal(token.replace(",", "")).quantize(_CENT)
    except InvalidOperation as exc:  # pragma: no cover — the regex fixes the shape
        raise ValueError(f"unreadable amount: {token!r}") from exc


def _signed(token: str, suffix: str) -> Decimal:
    """A balance the card printed with its side. `Cr` on a balance means the
    card owes the holder, which is the negative of what `Dr` means."""
    return -_amount(token) if suffix == "Cr" else _amount(token)


def _values_line(lines: list[str], label_at: int) -> str | None:
    """The first non-blank line under a label line, within `_VALUES_WITHIN`."""
    for line in lines[label_at + 1 : label_at + 1 + _VALUES_WITHIN]:
        if line.strip():
            return line
    return None


def is_axis_card_statement(text: str) -> bool:
    """Does this text carry the card's account anchor (§6.1)?

    The dispatch question, and the ONLY thing that decides a PDF is a card
    statement — never a filename, an email subject, a folder or a substring of a
    narration. A statement that answers True and then fails to identify is
    reported by this parser with its own reason, rather than falling through to
    the savings parser and coming back `no_header_anchor`.
    """
    return any(_ACCOUNT.match(line) for line in text.splitlines())


def identify_axis_card(text: str) -> tuple[CardHeader | None, str]:
    """(header, reason). Every anchor in the file must agree — a file holding
    two cards or two periods is a misfile, not a thing to guess at."""
    tails = {match.group(1)[-4:] for line in text.splitlines() if (match := _ACCOUNT.match(line))}
    if not tails:
        return None, "no_header_anchor"
    if len(tails) > 1:
        return None, "conflicting_headers"

    lines = text.splitlines()
    periods: set[tuple[date, date]] = set()
    for index, line in enumerate(lines):
        if not _SUMMARY_LABELS.match(line):
            continue
        values = _values_line(lines, index)
        found = _PERIOD.search(values) if values else None
        if found is None:
            continue
        try:
            periods.add((_d(found.group(1)), _d(found.group(2))))
        except ValueError:
            return None, "unreadable_period"
    if not periods:
        return None, "no_period_anchor"
    if len(periods) > 1:
        return None, "conflicting_headers"
    start, end = next(iter(periods))
    if start > end:
        return None, "reversed_period"
    return CardHeader(next(iter(tails)), start, end), ""


def _printed_total_due(text: str) -> Decimal | None:
    """`Total Payment Due` as the PAYMENT SUMMARY block prints it.

    A second, independent reading of the closing balance: the same figure also
    ends the bank's identity line, and the two are compared. Read by count
    within the block — the first money token on the values row is the total due
    and the second is the minimum due — because the block's column offsets move
    between statements while its label order does not. `None` when the block is
    absent or prints something else, which the caller turns into a refusal.
    """
    lines = text.splitlines()
    found: set[Decimal] = set()
    for index, line in enumerate(lines):
        if not _SUMMARY_LABELS.match(line):
            continue
        values = _values_line(lines, index)
        tokens = _MONEY.findall(values) if values else []
        if tokens:
            found.add(_signed(*tokens[0]))
    return found.pop() if len(found) == 1 else None


def _printed_totals(text: str) -> CardTotals | None:
    """The bank's seven figures, in the printed order of its own identity line.

    By count and order rather than by column, exactly as `TRANSACTION TOTAL` is
    read in `statements.py`: the line prints seven money tokens and nothing else
    (the marketing text to its right carries none), and a line holding any other
    count is not the one. `None` when it is absent, and the caller refuses.
    """
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if not _TOTALS_LABELS.match(line):
            continue
        for candidate in lines[index + 1 : index + 1 + _VALUES_WITHIN]:
            tokens = _MONEY.findall(candidate)
            if len(tokens) != 7:
                continue
            return CardTotals(
                opening=_signed(*tokens[0]),
                payments=_amount(tokens[1][0]),
                credits=_amount(tokens[2][0]),
                purchase=_amount(tokens[3][0]),
                cash_advance=_amount(tokens[4][0]),
                other_charges=_amount(tokens[5][0]),
                closing=_signed(*tokens[6]),
            )
    return None


def parse_axis_card_statement(
    text: str,
    *,
    file_sha256: str,
    subject: str = "",
    declared: Collection[str] = (),
) -> ParsedStatement:
    """`pdftotext -layout` text -> a parsed, self-validated Axis card statement.

    `subject` is recorded for the digest and **never read** for the account or
    the period (§6.1), and neither is the filename or the Drive folder — which
    is why neither is a parameter.
    """
    header, reason = identify_axis_card(text)
    if header is None:
        return ParsedStatement(
            status=UNIDENTIFIED, reason=reason, file_sha256=file_sha256, subject_hint=subject
        )

    instrument = books.canonical_instrument(f"axis-cc-{header.account_tail}", declared) or ""
    statement_id = statement_id_for(instrument, header.period_start, header.period_end)
    base = {
        "instrument": instrument,
        "period_start": header.period_start,
        "period_end": header.period_end,
        "statement_id": statement_id,
        "file_sha256": file_sha256,
        "subject_hint": subject,
    }

    totals = _printed_totals(text)
    summary_due = _printed_total_due(text)
    opening = totals.opening if totals else None
    closing = totals.closing if totals else None

    records: list[dict[str, Any]] = []
    unreadable: list[int] = []
    fx_rows = 0
    fx_ambiguous = 0
    for index, line in enumerate(text.split(END_MARKER)[0].splitlines()):
        row = _ROW.match(line)
        if row is None:
            # A date-led line that does not parse is money this parser did not
            # read. Naming it beats dropping it: a dropped row would only ever
            # surface as an arithmetic failure with no line to look at.
            if _DATED.match(line):
                unreadable.append(index)
            continue
        day, narration, token, suffix = row.groups()
        try:
            occurred_on, amount = _d(day), _amount(token)
        except ValueError:
            unreadable.append(index)
            continue
        if amount < 0:
            # Not a shape the card prints — it states the side in a suffix
            # instead. A ZERO is left alone deliberately: its direction is
            # printed like any other row's, so it is readable, and refusing a
            # whole statement over a cosmetic ₹0.00 line would cost far more
            # than carrying it.
            unreadable.append(index)
            continue
        fx = _FX.findall(narration)
        if len(fx) == 1:
            fx_rows += 1
        elif len(fx) > 1:
            # Never seen: one bracket per row on all three real statements. It
            # is counted rather than guessed at, so a layout change that starts
            # printing two is visible instead of quietly dropping both.
            fx_ambiguous += 1
        records.append(
            {
                "occurred_on": occurred_on,
                # The suffix IS the direction (§6.2's arithmetic): `Dr` is a
                # charge and `Cr` a payment or a refund. Nothing is inferred
                # from a running balance, because the card prints none.
                "direction": "out" if suffix == "Dr" else "in",
                "amount": amount,
                "balance_after": None,
                "narration": narration,
                "fx_currency": fx[0][0] if len(fx) == 1 else None,
                "fx_amount": _amount(fx[0][1]) if len(fx) == 1 else None,
            }
        )

    charges = sum((r["amount"] for r in records if r["direction"] == "out"), Decimal("0"))
    paid = sum((r["amount"] for r in records if r["direction"] == "in"), Decimal("0"))
    days = [r["occurred_on"] for r in records]
    diagnostics: dict[str, Any] = {
        "rows_read": len(records),
        # The vocabulary the other two parsers use, so one digest can read all
        # three. On a card `deposits` is what came off the bill — payments and
        # refunds — and `withdrawals` is what was charged to it.
        "deposits": str(paid),
        "withdrawals": str(charges),
        "layout": LAYOUT,
        "fx_rows": fx_rows,
        "fx_ambiguous_rows": fx_ambiguous,
        "row_date_min": min(days).isoformat() if days else "",
        "row_date_max": max(days).isoformat() if days else "",
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

    if unreadable:
        return refuse("unreadable_row", unreadable_lines=unreadable[:10])
    if totals is None or opening is None or closing is None:
        return refuse("missing_balance_anchor")
    if not totals.holds:
        # The bank's own arithmetic does not close, so this parser misread the
        # figures it then checks everything else against. Refuse before those
        # checks run against numbers that are not the bank's.
        return refuse("printed_totals_broken", printed_closing=str(totals.closing))
    if summary_due is None or summary_due != closing:
        # The card prints its closing balance twice — the payment summary and
        # the identity line — and §9.3 will assert the whole journal against it.
        return refuse("conflicting_closing", summary_total_due=str(summary_due))
    # Rows dated BEFORE `period_start` are normal: on all three real statements
    # the transactions run from two days before it. A row after `period_end` is
    # not, because that is the statement generation date.
    late = [d.isoformat() for d in days if d > header.period_end]
    if late:
        return refuse("row_after_period", late_dates=sorted(set(late))[:10])
    # §6.2 — the check that earns the lane. The row sides are read from the
    # printed `Dr`/`Cr` suffix and the bank's figures come from its own summary,
    # so neither is derived from the other: a dropped, double-read or mis-signed
    # row breaks one of these three. Refuse the WHOLE statement; never import
    # part of one.
    if charges != totals.purchase + totals.cash_advance + totals.other_charges:
        return refuse(
            "totals_mismatch",
            side="debit",
            printed=str(totals.purchase + totals.cash_advance + totals.other_charges),
            parsed=str(charges),
        )
    if paid != totals.payments + totals.credits:
        return refuse(
            "totals_mismatch",
            side="credit",
            printed=str(totals.payments + totals.credits),
            parsed=str(paid),
        )
    # §6.2's third sentence — closing minus opening equals the difference — is
    # now true by construction, and deliberately NOT written out as a fourth
    # `if`. The bank's identity has been checked (`printed_totals_broken`) and
    # both sides of it match the rows, so the subtraction can no longer
    # disagree: an `if` here could never fail, and a check that cannot fail is
    # the thing this lane keeps finding in its own tests. What makes the
    # implication safe is `printed_totals_broken`, which has its own test.

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


__all__ = [
    "END_MARKER",
    "LAYOUT",
    "CardHeader",
    "CardTotals",
    "identify_axis_card",
    "is_axis_card_statement",
    "parse_axis_card_statement",
]
