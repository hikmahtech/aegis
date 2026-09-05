"""Table-driven tests for the deterministic parsers (spec §3). Fixture text is
the real 2026-09 mail with names, tails, refs and amounts altered."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from aegis.services import bank_parsers as bp

HDFC = "HDFC Bank InstaAlerts <alerts@hdfcbank.bank.in>"
AXIS = "Axis Bank Alerts <alerts@axis.bank.in>"

FIX = {
    "hdfc_upi_debit": (
        HDFC,
        "❗  You have done a UPI txn. Check details!",
        "Dear Customer, Greetings from HDFC Bank! Rs.245.50 is debited from your account "
        "ending 4321 towards VPA shop77@ybl (Corner Store) on 02-09-26. UPI transaction "
        "reference no.: 100000000001. If you did not authorize this transaction, please "
        "report it immediately.",
    ),
    "hdfc_upi_credit": (
        HDFC,
        "❗  You have done a UPI txn. Check details!",
        "Dear Customer, Rs.1,500.00 is credited to your account ending 4321 from VPA "
        "friend@okaxis (A Friend) on 03-09-26. UPI transaction reference no.: 100000000002.",
    ),
    "hdfc_imps": (
        HDFC,
        "View: Account update for your HDFC Bank A/c",
        "Dear Customer, Greetings from HDFC Bank! INR 10,000.00 has been debited from your "
        "account ending xxxxxxxxxx4321 on 21-08-26 and credited to the account ending "
        "xxxxxxxxxx8765 via IMPS. IMPS Reference No: 600000000003 Available Balance: INR 90,957.97",
    ),
    "nkgsb_credit": (
        "alerts@nkgsb-bank.com",
        "NKGSB Bank - Transaction Alert",
        "Dear Sir/ Madam, Received Rs.150.00 in NKGSB Bank A/C X999 on 31-08-26 "
        "UPI/CREDIT/660000000004/someone-1@okicici/N.Clr bal Rs.5923.21.",
    ),
    "axis_card_spend": (
        '"alerts@axis.bank.in" <alerts@axis.bank.in>',
        "USD 5.89 spent on credit card no. XX9876",
        "18-08-2026 Dear Customer, Here's the summary of your Axis Bank Credit Card "
        "Transaction: Transaction Amount: USD 5.89 Merchant Name: 1PASSWORD Axis Bank Credit "
        "Card No. XX9876 Date & Time: 18-08-2026, 16:37:46 IST Available Limit*: INR 157592.24",
    ),
    "axis_autopay_done": (
        AXIS,
        "AutoPay for GitHubInc: ACTIVATED",
        "21-08-2026 Dear Customer, Here's the summary of your successful AutoPay transaction: "
        "Transaction Amount: USD 4.00 Merchant Name: GitHubInc AutoPay ID: Yb0000 Axis Bank "
        "Card No. XX9876 Max Limit: USD 15000.00",
    ),
    "axis_autopay_reminder": (
        AXIS,
        "Upcoming AutoPay txn. reminder",
        "02-09-2026 Dear Customer, Here's the summary of your upcoming AutoPay transaction: "
        "Transaction Amount: INR 262.30 Merchant Name: AMAZON WEB SERVICES To be debited by: "
        "04-09-2026 Max Limit: INR 262.30 Axis Bank card No. XX5555 AutoPay ID: Xf0000",
    ),
    "axis_cc_statement": (
        "cc.statements@axis.bank.in",
        "Your Axis Bank My Zone Credit Card Statement ending XX76 - August 2026",
        "Axis Bank Dear Customer, Please find enclosed your credit card statement for AUGUST "
        "2026. Total Amount Due INR Minimum Amount Due (INR) Payment Due Date (DD-MM-YYYY) "
        "100308.53 Dr 25113 Dr 07/09/2026 How do I access my statement?",
    ),
    "axis_remittance": (
        "eforexservices@axis.bank.in",
        "Inward Remittance Notification",
        "Dear Sir/Madam, This email is to inform you that we have received foreign currency "
        "funds of GBP 6285.01 from overseas bearing reference numbers SWIFT Reference "
        "Number:GBC00000000000. ARC Reference Number:030926ARC00000. Following are the details "
        "of the transaction: Amount and Currency (F32A) : GBP 6285.01 Value Date (F32A) : "
        "02-SEP-26 Ordering Customer - Account and Name and Address (F50F) : 1/Stockopedia Ltd "
        "3/GB/OXFORD Beneficiary Customer - Account and name and address (F59): 000000000 "
        "Hikmah Technologies",
    ),
    "gpay_bill": (
        "Google Pay <google-pay-noreply@google.com>",
        "New bill from Mahavitaran - Maharashtra Electricity (MSEDCL). Pay now on Google Pay",
        "New bill from Mahavitaran - Maharashtra Electricity (MSEDCL)\nBill Amount:\nRs. 7170.00\n"
        "Bill Category:\nElectricity\nAccount Name:\nSuncity 501\nDue Date:\nSep 15, 2026\nPay Now",
    ),
    "stripe_receipt": (
        '"Eleven Labs Inc." <invoice+statements+acct_1M0000@stripe.com>',
        "Your receipt from Eleven Labs Inc. #2537-8261-6429",
        "Eleven Labs Inc.\nReceipt from Eleven Labs Inc. ₹1,936.00 Paid August 25, 2026 "
        "Download invoice Download receipt Receipt number 2537-8261-6429 Invoice number "
        "WHFZ3G4O-0008 Payment method - 9876\nReceipt #2537-8261-6429 Jul 24–Aug 24, 2026 "
        "Creator (per subscription) Qty 1 ₹1,936.00 Total ₹1,936.00 Amount paid ₹1,936.00",
    ),
    "apple_receipt": (
        "Apple <no_reply@email.apple.com>",
        "Your receipt from Apple.",
        "Tax Invoice 3 September 2026 Sequence: 3-000 Order ID: MN0000 Document: 000 Apple "
        "Account: someone@example.com Apple Music Individual (Monthly) SAC: 998432 Renews 4 "
        "October 2026 ₹119.00 Billing and Payment Someone Subtotal ₹100.85 IGST charged at 18% "
        "₹18.15 someone-1@okaxis ₹119.00 You can turn off renewal receipts",
    ),
    "airtel_bill": (
        "ebill@airtel.com",
        "Bill for your Airtel Xstream Fiber  02200000000_wifi - Aug'26 is generated",
        "airtel Airtel number 9100000000 This Month's Charges (Rs.) 5306.46 Relationship number "
        "2004XXXXX Due amount* (Rs.) 5306.46 Bill period 17-Jul-2026 to 16-Aug-2026 Due date* "
        "06-Sep-2026 View bill Pay bill",
    ),
    "airtel_receipt": (
        "update@airtel.com",
        "Here's your Airtel payment receipt!",
        "Payment Reciept Dear SOMEONE . Thank you for choosing Airtel. We have received a payment "
        "of Rs 5306.46 for your Bill Payment. Please find the payment receipt attached.",
    ),
}


# Parser names are direction-neutral: direction lives in MoneyEvent.direction.
# Fixture keys that name a direction map back to the one parser that owns them;
# every other key already equals its parser name.
_PARSER_FOR = {
    "hdfc_upi_debit": "hdfc_upi",
    "hdfc_upi_credit": "hdfc_upi",
    "nkgsb_credit": "nkgsb",
}


def _ev(name):
    sender, subject, body = FIX[name]
    ev = bp.parse_any(sender, subject, body)
    assert ev is not None, name
    assert ev.parser == _PARSER_FOR.get(name, name)
    assert ev.payee_key, name
    return ev


def test_hdfc_upi_debit():
    ev = _ev("hdfc_upi_debit")
    assert (ev.kind, ev.direction, ev.channel) == ("transaction", "out", "upi")
    assert ev.amount == Decimal("245.50") and ev.currency == "INR"
    assert ev.payee == "Corner Store" and ev.instrument == "hdfc-4321"
    assert ev.occurred_on == date(2026, 9, 2) and ev.ref == "100000000001"
    assert ev.source_class == "bank" and ev.entity == "personal"


def test_hdfc_upi_credit():
    ev = _ev("hdfc_upi_credit")
    assert ev.direction == "in" and ev.amount == Decimal("1500.00")
    assert ev.payee == "A Friend" and ev.occurred_on == date(2026, 9, 3)


def test_hdfc_imps():
    ev = _ev("hdfc_imps")
    assert (ev.kind, ev.direction, ev.channel) == ("transaction", "out", "imps")
    assert ev.amount == Decimal("10000.00") and ev.instrument == "hdfc-4321"
    assert ev.payee == "a/c ••8765" and ev.account == "equity:transfers"
    assert ev.occurred_on == date(2026, 8, 21) and ev.ref == "600000000003"


def test_nkgsb_credit():
    ev = _ev("nkgsb_credit")
    assert ev.direction == "in" and ev.amount == Decimal("150.00")
    assert ev.instrument == "nkgsb-999" and ev.payee == "someone-1@okicici"
    assert ev.ref == "660000000004" and ev.occurred_on == date(2026, 8, 31)


def test_axis_card_spend():
    ev = _ev("axis_card_spend")
    assert (ev.channel, ev.direction) == ("card", "out")
    assert ev.amount == Decimal("5.89") and ev.currency == "USD"
    assert ev.payee == "1PASSWORD" and ev.instrument == "axis-cc-9876"
    assert ev.occurred_on == date(2026, 8, 18)


def test_axis_autopay_done():
    ev = _ev("axis_autopay_done")
    assert (ev.kind, ev.channel, ev.direction) == ("transaction", "autopay", "out")
    assert ev.amount == Decimal("4.00") and ev.currency == "USD"
    assert ev.payee == "GitHubInc" and ev.instrument == "axis-cc-9876"
    assert ev.occurred_on == date(2026, 8, 21) and ev.is_recurring is True


def test_axis_autopay_reminder_is_a_due():
    ev = _ev("axis_autopay_reminder")
    assert ev.kind == "due" and ev.channel == "autopay"
    assert ev.amount == Decimal("262.30") and ev.currency == "INR"
    assert ev.payee == "AMAZON WEB SERVICES" and ev.due_on == date(2026, 9, 4)
    assert ev.instrument == "axis-cc-5555"


def test_axis_cc_statement_is_a_due():
    ev = _ev("axis_cc_statement")
    assert ev.kind == "due" and ev.channel == "statement"
    assert ev.amount == Decimal("100308.53") and ev.currency == "INR"
    assert ev.due_on == date(2026, 9, 7)
    assert ev.payee == "Axis credit card XX76" and ev.instrument == "axis-cc-76"


def test_axis_remittance():
    ev = _ev("axis_remittance")
    assert (ev.kind, ev.direction, ev.channel) == ("transaction", "in", "remittance")
    assert ev.amount == Decimal("6285.01") and ev.currency == "GBP"
    assert ev.payee == "Stockopedia Ltd" and ev.entity == "hikmah"
    assert ev.account == "income:hikmah:stockopedia" and ev.instrument == "axis-9640"
    assert ev.occurred_on == date(2026, 9, 2) and ev.ref == "GBC00000000000"


def test_gpay_bill_is_a_due():
    ev = _ev("gpay_bill")
    assert ev.kind == "due" and ev.channel == "bill"
    assert ev.amount == Decimal("7170.00") and ev.currency == "INR"
    assert ev.payee == "Mahavitaran - Maharashtra Electricity (MSEDCL) Suncity 501"
    assert ev.due_on == date(2026, 9, 15)


def test_stripe_receipt():
    ev = _ev("stripe_receipt")
    assert (ev.kind, ev.channel, ev.direction) == ("transaction", "receipt", "out")
    assert ev.amount == Decimal("1936.00") and ev.currency == "INR"
    assert ev.payee == "Eleven Labs Inc." and ev.instrument == "card-9876"
    assert ev.occurred_on == date(2026, 8, 25) and ev.source_class == "receipt"


def test_apple_receipt():
    ev = _ev("apple_receipt")
    assert ev.kind == "transaction" and ev.channel == "receipt"
    assert ev.amount == Decimal("119.00") and ev.currency == "INR"
    assert ev.payee == "Apple Music Individual" and ev.is_recurring is True
    assert ev.occurred_on == date(2026, 9, 3)


def test_airtel_bill_is_a_due():
    ev = _ev("airtel_bill")
    assert ev.kind == "due" and ev.amount == Decimal("5306.46")
    assert ev.due_on == date(2026, 9, 6) and ev.payee == "Airtel Xstream Fiber"


def test_airtel_receipt():
    ev = _ev("airtel_receipt")
    assert ev.kind == "transaction" and ev.channel == "receipt"
    assert ev.amount == Decimal("5306.46") and ev.payee == "Airtel"
    # The body carries no date, so this parser is the one transaction parser
    # that depends on `parse_money_email` back-filling `occurred_on` from
    # `received_at`. Without that, `post_event` raises BooksError.
    assert ev.occurred_on is None


@pytest.mark.parametrize(
    "sender,subject,body",
    [
        (
            HDFC,
            "⚠️ Non-maintenance charges may apply on A/c XX0236",
            "Keep AMB of Rs.5000 to avoid charges.",
        ),
        (AXIS, "Reminder to update your KYC information", "Dear Customer, please update your KYC."),
        (AXIS, "Pay your GST with Axis Bank on the go!", "Pay GST of Rs 10,000 easily."),
        (
            "Axis Bank Cards <info@digital.axisbankmail.bank.in>",
            "Manage your expenses smartly with FLEXI EMI!",
            "Convert Rs 50,000 to EMI",
        ),
        ("Apple <no_reply@email.apple.com>", "Your Apple ID was used to sign in", "A new sign in."),
    ],
)
def test_marketing_and_notices_are_not_parsed(sender, subject, body):
    assert bp.parse_any(sender, subject, body) is None


def test_amount_and_currency_helpers():
    assert bp.amount_from("1,00,308.53") == Decimal("100308.53")
    assert bp.amount_from("245.5") == Decimal("245.50")
    assert bp.currency_from("Rs.") == "INR" and bp.currency_from("₹") == "INR"
    assert bp.currency_from("USD") == "USD" and bp.currency_from("$") == "USD"
    assert bp.currency_from("GBP") == "GBP" and bp.currency_from("£") == "GBP"
    assert bp.currency_from("€") == "EUR" and bp.currency_from("xyz") is None

def test_is_autopay_reads_the_phrasings_real_mail_uses():
    """Grounded in the four autopay notices that became chores on 2026-09-05.

    Each string below is copied from a live email body or subject; the
    non-autopay ones are the real bills that must keep their task.
    """
    from aegis.services.bank_parsers import is_autopay

    assert is_autopay("Confirmation required to process Auto Pay")
    assert is_autopay("your auto debit payment is due. Amount to be debited: USD 200.00")
    assert is_autopay("your subscription automatically renews for Rs 149.00/month")
    assert is_autopay("scheduled to automatically renew on 2026-07-20")
    assert is_autopay("your 200 GB storage plan automatically renews monthly")
    assert is_autopay("We automatically renewed registration for the domain")

    # The real bills, which still need a task.
    assert not is_autopay("New bill from Axis Bank Credit Card. Pay now on Google Pay")
    assert not is_autopay("Bill Amount: Rs. 55275.34 Due Date: Aug 4, 2026 Pay Now")
    assert not is_autopay("Your electricity bill for August is ready. Pay by 11-08-2026")
    assert not is_autopay("")


def test_is_autopay_is_false_when_the_mail_says_autopay_is_off():
    """The phrase can carry the opposite meaning, and that is a real bill.

    Caught in prod minutes after the first version shipped: GitHub's monthly
    bill says "auto-pay for recurring payments is currently disabled for your
    account due to the new RBI regulation" — autopay words, autopay switched
    OFF, $4.00 somebody has to pay by hand. The two mistakes do not cost the
    same: a phantom task is noise, a suppressed bill is a missed payment, so
    anything negated stays a chore.
    """
    from aegis.services.bank_parsers import is_autopay

    assert not is_autopay(
        "Your bill for usage on GitHub is available to pay. Bill amount: $4.00 "
        "Payment due by September 2, 2026. Please note that auto-pay for recurring "
        "payments is currently disabled for your account due to the new RBI regulation."
    )
    assert not is_autopay("We have turned off auto-renew for this subscription")
    assert not is_autopay("automatic payment is not enabled on your account")

    # "until cancelled" is subscription boilerplate, NOT a statement that
    # autopay is off — Apple's iCloud+ renewal notice carries it verbatim and
    # must stay suppressed.
    assert is_autopay(
        "your 200 GB storage plan automatically renews monthly for Rs 219 starting "
        "2026-08-06 12:53:33 America/Los_Angeles until cancelled"
    )
