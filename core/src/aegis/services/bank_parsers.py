"""Deterministic parsers for the bank and vendor mail formats we see (spec §3).

Each parser takes (sender, subject, body) and returns a MoneyEvent or None.
Anchors are the literal phrases the real emails use; a parser that matches
its anchors but cannot read an amount returns None so the LLM gets a try.
`parse_any` tries them in PARSERS order.

Every regex here runs against `_clean(body)` — whitespace collapsed to single
spaces — so patterns are written against one long line, never against the
original line breaks.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from aegis.api.models.money import MoneyEvent, payee_key

_CENT = Decimal("0.01")
_CURRENCY = {
    "rs": "INR", "rs.": "INR", "inr": "INR", "₹": "INR", "rupees": "INR",
    "usd": "USD", "$": "USD", "gbp": "GBP", "£": "GBP", "eur": "EUR", "€": "EUR",
}
_AMT = r"([\d,]+(?:\.\d{1,2})?)"


def amount_from(text: str) -> Decimal:
    """Indian-grouped or plain digits to a 2-place Decimal ("1,00,308.53")."""
    return Decimal(text.replace(",", "")).quantize(_CENT)


def currency_from(token: str) -> str | None:
    """Symbol or code to an ISO currency; None when the token isn't one."""
    return _CURRENCY.get((token or "").strip().lower())


# An email that says the money moves on its own. A due built from one is a
# heads-up, not a chore — there is nothing for anyone to do, and if the debit
# fails the bank says so in a separate mail that becomes a `failed` event with
# its own task. Anchored on the phrasings the real mail uses: Axis autopay
# notices ("Auto Pay", "auto debit payment is due"), Apple renewals
# ("automatically renews monthly") and AWS ("scheduled to automatically
# renew"). Deliberately NOT model-supplied — `_LLM_EVENT_FIELDS` must never
# gain this, or a crafted body could silence a real bill.
_AUTOPAY_RE = re.compile(
    r"auto[\s-]?(?:pay|debit|renew)"
    r"|automatic(?:ally)?[\s-]+(?:renew|debit|charg|pay)",
    re.I,
)


def is_autopay(text: str) -> bool:
    """True when the mail says the charge happens without the user acting."""
    return bool(_AUTOPAY_RE.search(text or ""))


def _date_dmy2(s: str) -> date:  # 02-09-26
    return datetime.strptime(s, "%d-%m-%y").date()


def _date_dmy4(s: str) -> date:  # 18-08-2026 or 07/09/2026
    return datetime.strptime(s.replace("/", "-"), "%d-%m-%Y").date()


def _date_dmon2(s: str) -> date:  # 02-SEP-26
    return datetime.strptime(s.title(), "%d-%b-%y").date()


def _date_dmon4(s: str) -> date:  # 06-Sep-2026
    return datetime.strptime(s.title(), "%d-%b-%Y").date()


def _date_mdy_text(s: str) -> date:  # Sep 15, 2026 / August 25, 2026
    for fmt in ("%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            continue
    raise ValueError(s)


def _date_dmy_text(s: str) -> date:  # 3 September 2026
    return datetime.strptime(s.strip(), "%d %B %Y").date()


def _from(sender: str, needle: str) -> bool:
    return needle.lower() in (sender or "").lower()


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def _event(name: str, **kw) -> MoneyEvent:
    ev = MoneyEvent(parser=name, **kw)
    ev.payee_key = payee_key(ev.payee)
    return ev


# ---------------------------------------------------------------- banks

_HDFC_UPI = re.compile(
    rf"Rs\.?\s*{_AMT} is (debited from|credited to) your account ending (\d{{4}})\s+"
    r"(?:towards|from) VPA (\S+?)(?: \(([^)]*)\))? on (\d{2}-\d{2}-\d{2})\."
    r".*?UPI transaction reference no\.?:\s*(\d+)",
    re.S,
)


def parse_hdfc_upi(sender: str, subject: str, body: str) -> MoneyEvent | None:
    if not _from(sender, "alerts@hdfcbank.bank.in"):
        return None
    m = _HDFC_UPI.search(_clean(body))
    if not m:
        return None
    amt, verb, tail, vpa, name, d, ref = m.groups()
    out = verb.startswith("debited")
    return _event(
        "hdfc_upi",
        kind="transaction",
        direction="out" if out else "in",
        amount=amount_from(amt),
        currency="INR",
        payee=_clean(name) or vpa,
        channel="upi",
        instrument=f"hdfc-{tail}",
        occurred_on=_date_dmy2(d),
        ref=ref,
        source_class="bank",
    )


_HDFC_IMPS = re.compile(
    rf"INR\s*{_AMT} has been debited from your account ending x*(\d{{4}}) "
    r"on (\d{2}-\d{2}-\d{2}) and credited to the account ending x*(\d{4}) "
    r"via IMPS\. IMPS Reference No:\s*(\d+)"
)


def parse_hdfc_imps(sender: str, subject: str, body: str) -> MoneyEvent | None:
    if not _from(sender, "alerts@hdfcbank.bank.in"):
        return None
    m = _HDFC_IMPS.search(_clean(body))
    if not m:
        return None
    amt, tail, d, to_tail, ref = m.groups()
    return _event(
        "hdfc_imps",
        kind="transaction",
        direction="out",
        amount=amount_from(amt),
        currency="INR",
        payee=f"a/c ••{to_tail}",
        channel="imps",
        instrument=f"hdfc-{tail}",
        occurred_on=_date_dmy2(d),
        ref=ref,
        account="equity:transfers",
        source_class="bank",
    )


_NKGSB = re.compile(
    rf"(Received|Paid|Debited)\s+Rs\.?\s*{_AMT} (?:in|from) NKGSB Bank A/C X(\d+) "
    r"on (\d{2}-\d{2}-\d{2})\s+UPI/(?:CREDIT|DEBIT)/(\d+)/([^/\s]+)/"
)


def parse_nkgsb(sender: str, subject: str, body: str) -> MoneyEvent | None:
    if not _from(sender, "alerts@nkgsb-bank.com"):
        return None
    m = _NKGSB.search(_clean(body))
    if not m:
        return None
    verb, amt, tail, d, ref, vpa = m.groups()
    incoming = verb == "Received"
    return _event(
        "nkgsb",
        kind="transaction",
        direction="in" if incoming else "out",
        amount=amount_from(amt),
        currency="INR",
        payee=vpa,
        channel="upi",
        instrument=f"nkgsb-{tail}",
        occurred_on=_date_dmy2(d),
        ref=ref,
        source_class="bank",
    )


_AXIS_SPEND_SUBJ = re.compile(rf"^(\w+) {_AMT} spent on credit card no\. XX(\d+)", re.I)
_AXIS_MERCHANT = re.compile(r"Merchant Name:\s*(.+?)\s+(?:Axis Bank|AutoPay ID|To be debited)")
_AXIS_DATETIME = re.compile(r"Date & Time:\s*(\d{2}-\d{2}-\d{4})")


def parse_axis_card_spend(sender: str, subject: str, body: str) -> MoneyEvent | None:
    if not _from(sender, "alerts@axis.bank.in"):
        return None
    m = _AXIS_SPEND_SUBJ.search(_clean(subject))
    if not m:
        return None
    cur, amt, tail = m.groups()
    text = _clean(body)
    merchant = _AXIS_MERCHANT.search(text)
    when = _AXIS_DATETIME.search(text)
    if not merchant or not when:
        return None
    return _event(
        "axis_card_spend",
        kind="transaction",
        direction="out",
        amount=amount_from(amt),
        currency=currency_from(cur) or cur.upper(),
        payee=merchant.group(1).strip(),
        channel="card",
        instrument=f"axis-cc-{tail}",
        occurred_on=_date_dmy4(when.group(1)),
        source_class="bank",
    )


_AXIS_AMOUNT = re.compile(rf"Transaction Amount:\s*(\w+)\s*{_AMT}")
_AXIS_CARD = re.compile(r"[Cc]ard No\.?\s*XX(\d+)")
_AXIS_DATE_HEAD = re.compile(r"^(\d{2}-\d{2}-\d{4})")
_AXIS_DEBIT_BY = re.compile(r"To be debited by:\s*(\d{2}-\d{2}-\d{4})")


def parse_axis_autopay_done(sender: str, subject: str, body: str) -> MoneyEvent | None:
    text = _clean(body)
    if not _from(sender, "alerts@axis.bank.in") or "successful AutoPay transaction" not in text:
        return None
    amt = _AXIS_AMOUNT.search(text)
    merchant = _AXIS_MERCHANT.search(text)
    card = _AXIS_CARD.search(text)
    head = _AXIS_DATE_HEAD.search(text)
    if not (amt and merchant and card and head):
        return None
    return _event(
        "axis_autopay_done",
        kind="transaction",
        direction="out",
        amount=amount_from(amt.group(2)),
        currency=currency_from(amt.group(1)) or amt.group(1).upper(),
        payee=merchant.group(1).strip(),
        channel="autopay",
        instrument=f"axis-cc-{card.group(1)}",
        occurred_on=_date_dmy4(head.group(1)),
        is_recurring=True,
        source_class="bank",
    )


def parse_axis_autopay_reminder(sender: str, subject: str, body: str) -> MoneyEvent | None:
    if not _from(sender, "alerts@axis.bank.in") or "Upcoming AutoPay" not in subject:
        return None
    text = _clean(body)
    amt = _AXIS_AMOUNT.search(text)
    merchant = _AXIS_MERCHANT.search(text)
    due = _AXIS_DEBIT_BY.search(text)
    card = _AXIS_CARD.search(text)
    if not (amt and merchant and due):
        return None
    return _event(
        "axis_autopay_reminder",
        kind="due",
        direction="out",
        amount=amount_from(amt.group(2)),
        currency=currency_from(amt.group(1)) or amt.group(1).upper(),
        payee=merchant.group(1).strip(),
        channel="autopay",
        instrument=f"axis-cc-{card.group(1)}" if card else None,
        due_on=_date_dmy4(due.group(1)),
        is_recurring=True,
        source_class="bank",
    )


_AXIS_STMT = re.compile(
    rf"Total Amount Due.*?Payment Due Date.*?{_AMT}\s*Dr\s*{_AMT}\s*Dr"
    r"\s*(\d{2}/\d{2}/\d{4})"
)
_AXIS_STMT_TAIL = re.compile(r"ending XX(\d+)")


def parse_axis_cc_statement(sender: str, subject: str, body: str) -> MoneyEvent | None:
    if not _from(sender, "cc.statements@axis.bank.in"):
        return None
    m = _AXIS_STMT.search(_clean(body))
    tail = _AXIS_STMT_TAIL.search(subject)
    if not m:
        return None
    total, _minimum, due = m.groups()
    t = tail.group(1) if tail else "????"
    return _event(
        "axis_cc_statement",
        kind="due",
        direction="out",
        amount=amount_from(total),
        currency="INR",
        payee=f"Axis credit card XX{t}",
        channel="statement",
        instrument=f"axis-cc-{t}",
        due_on=_date_dmy4(due),
        source_class="bank",
    )


_REMIT_AMT = re.compile(r"received foreign currency funds of (\w{3}) " + _AMT)
_REMIT_DATE = re.compile(r"Value Date \(F32A\)\s*:\s*(\d{2}-[A-Za-z]{3}-\d{2})")
_REMIT_ORDER = re.compile(r"\(F50F\)\s*:\s*1/([^/]+?)\s+(?:3/|2/|\(F59\))")
_REMIT_SWIFT = re.compile(r"SWIFT Reference Number:\s*([A-Z0-9]+)")


def parse_axis_remittance(sender: str, subject: str, body: str) -> MoneyEvent | None:
    if not _from(sender, "eforexservices@axis.bank.in"):
        return None
    if "Inward Remittance Notification" not in subject:
        return None
    text = _clean(body)
    amt = _REMIT_AMT.search(text)
    when = _REMIT_DATE.search(text)
    who = _REMIT_ORDER.search(text)
    swift = _REMIT_SWIFT.search(text)
    if not (amt and when):
        return None
    payee = who.group(1).strip() if who else "Inward remittance"
    account = (
        "income:hikmah:stockopedia"
        if "stockopedia" in payee.lower()
        else "income:hikmah:other"
    )
    return _event(
        "axis_remittance",
        kind="transaction",
        direction="in",
        amount=amount_from(amt.group(2)),
        currency=amt.group(1).upper(),
        payee=payee,
        channel="remittance",
        instrument="axis-9640",
        occurred_on=_date_dmon2(when.group(1)),
        ref=swift.group(1) if swift else None,
        entity="hikmah",
        account=account,
        source_class="bank",
    )


# ---------------------------------------------------------------- bills & receipts

_GPAY_SUBJ = re.compile(r"^New bill from (.+?)\. Pay now", re.I)
_GPAY_AMT = re.compile(rf"Bill Amount:\s*Rs\.?\s*{_AMT}")
_GPAY_ACCT = re.compile(r"Account Name:\s*(.+?)\s*(?:Due Date:|$)")
_GPAY_DUE = re.compile(r"Due Date:\s*([A-Za-z]{3,9} \d{1,2}, \d{4})")


def parse_gpay_bill(sender: str, subject: str, body: str) -> MoneyEvent | None:
    if not _from(sender, "google-pay-noreply@google.com"):
        return None
    s = _GPAY_SUBJ.search(_clean(subject))
    text = _clean(body)
    amt = _GPAY_AMT.search(text)
    due = _GPAY_DUE.search(text)
    if not (s and amt and due):
        return None
    acct = _GPAY_ACCT.search(text)
    biller = s.group(1).strip()
    payee = f"{biller} {acct.group(1).strip()}" if acct else biller
    return _event(
        "gpay_bill",
        kind="due",
        direction="out",
        amount=amount_from(amt.group(1)),
        currency="INR",
        payee=payee,
        channel="bill",
        due_on=_date_mdy_text(due.group(1)),
        is_recurring=True,
        source_class="receipt",
    )


_STRIPE_HEAD = re.compile(
    rf"Receipt from (.+?) ([₹$£€]|Rs\.?|USD|INR|GBP|EUR)\s?{_AMT} "
    r"Paid ([A-Za-z]+ \d{1,2}, \d{4})"
)
_STRIPE_PM = re.compile(r"Payment method - (\d{4})")


def parse_stripe_receipt(sender: str, subject: str, body: str) -> MoneyEvent | None:
    if "invoice+statements" not in (sender or "").lower():
        return None
    text = _clean(body)
    m = _STRIPE_HEAD.search(text)
    if not m:
        return None
    vendor, cur, amt, paid = m.groups()
    pm = _STRIPE_PM.search(text)
    return _event(
        "stripe_receipt",
        kind="transaction",
        direction="out",
        amount=amount_from(amt),
        currency=currency_from(cur) or "INR",
        payee=vendor.strip(),
        channel="receipt",
        instrument=f"card-{pm.group(1)}" if pm else None,
        occurred_on=_date_mdy_text(paid),
        is_recurring=True,
        source_class="receipt",
    )


_APPLE_DATE = re.compile(r"Tax Invoice (\d{1,2} [A-Za-z]+ \d{4})")
_APPLE_PRODUCT_LINE = re.compile(r"Apple Account: \S+ (.+?) \((Monthly|Yearly|Weekly)\)")
_APPLE_AMT = re.compile(r"₹" + _AMT)


def parse_apple_receipt(sender: str, subject: str, body: str) -> MoneyEvent | None:
    if not _from(sender, "no_reply@email.apple.com") or "receipt from Apple" not in subject:
        return None
    text = _clean(body)
    when = _APPLE_DATE.search(text)
    product = _APPLE_PRODUCT_LINE.search(text)
    amounts = _APPLE_AMT.findall(text)
    if not (when and product and amounts):
        return None
    return _event(
        "apple_receipt",
        kind="transaction",
        direction="out",
        # The Apple invoice repeats the total: line item, subtotal, tax, then
        # the charged total last. The last ₹ figure is the one actually paid.
        amount=amount_from(amounts[-1]),
        currency="INR",
        payee=product.group(1).strip(),
        channel="receipt",
        occurred_on=_date_dmy_text(when.group(1)),
        is_recurring=True,
        category="media",
        source_class="receipt",
    )


_AIRTEL_DUE_AMT = re.compile(rf"Due amount\*? \(Rs\.\)\s*{_AMT}")
_AIRTEL_DUE_DATE = re.compile(r"Due date\*?\s*(\d{2}-[A-Za-z]{3}-\d{4})")


def parse_airtel_bill(sender: str, subject: str, body: str) -> MoneyEvent | None:
    if not _from(sender, "ebill@airtel.com"):
        return None
    text = _clean(body)
    amt = _AIRTEL_DUE_AMT.search(text)
    due = _AIRTEL_DUE_DATE.search(text)
    if not (amt and due):
        return None
    return _event(
        "airtel_bill",
        kind="due",
        direction="out",
        amount=amount_from(amt.group(1)),
        currency="INR",
        payee="Airtel Xstream Fiber" if "xstream" in subject.lower() else "Airtel",
        channel="bill",
        due_on=_date_dmon4(due.group(1)),
        is_recurring=True,
        source_class="receipt",
    )


_AIRTEL_PAID = re.compile(rf"received a payment of Rs\.?\s*{_AMT} for your Bill Payment")


def parse_airtel_receipt(sender: str, subject: str, body: str) -> MoneyEvent | None:
    """The Airtel payment receipt, which carries NO date anywhere in the body.

    DEPENDENCY: this is the one `kind="transaction"` parser that returns no
    `occurred_on`, and it works only because `MoneyActivities.parse_money_email`
    back-fills the date from the email's `received_at` before anything writes
    the journal. A future caller of `parse_any` that skips that step hands
    `post_event` a dateless transaction and gets `BooksError`. Documented here
    rather than guessed at: inventing a date would silently misdate the ledger.
    """
    if not _from(sender, "update@airtel.com"):
        return None
    m = _AIRTEL_PAID.search(_clean(body))
    if not m:
        return None
    return _event(
        "airtel_receipt",
        kind="transaction",
        direction="out",
        amount=amount_from(m.group(1)),
        currency="INR",
        payee="Airtel",
        channel="receipt",
        is_recurring=True,
        source_class="receipt",
    )


Parser = Callable[[str, str, str], MoneyEvent | None]

PARSERS: tuple[Parser, ...] = (
    parse_hdfc_upi,
    parse_hdfc_imps,
    parse_nkgsb,
    parse_axis_card_spend,
    parse_axis_autopay_done,
    parse_axis_autopay_reminder,
    parse_axis_cc_statement,
    parse_axis_remittance,
    parse_gpay_bill,
    parse_stripe_receipt,
    parse_apple_receipt,
    parse_airtel_bill,
    parse_airtel_receipt,
)


def parse_any(sender: str, subject: str, body: str) -> MoneyEvent | None:
    """First parser whose anchors match wins; None means "hand it to the LLM"."""
    for parser in PARSERS:
        try:
            ev = parser(sender or "", subject or "", body or "")
        except (ValueError, InvalidOperation):
            ev = None
        if ev is not None:
            return ev
    return None
