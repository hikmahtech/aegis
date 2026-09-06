from decimal import Decimal

import pytest
from aegis.api.models.money import MoneyEvent, payee_key
from aegis.services.books import (
    account_for,
    canonical_instrument,
    fmt_money,
    instrument_account,
    render_amount,
)


def test_payee_key_normalises():
    assert payee_key("Jai shree nakoda") == "jai shree nakoda"
    assert payee_key("  AMAZON  WEB-SERVICES (India) ") == "amazon web services india"
    assert payee_key("q203028199@ybl") == "q203028199 ybl"
    assert payee_key("") == ""


def test_money_event_defaults_and_validation():
    e = MoneyEvent(kind="transaction", direction="out", amount=Decimal("10"), currency="INR")
    assert e.entity == "personal" and e.channel == "other" and e.parser == "llm"
    assert e.source_class == "other" and e.confidence == 1.0
    with pytest.raises(ValueError):
        MoneyEvent(kind="bogus")


def test_money_event_amount_quantised_to_two_places():
    e = MoneyEvent(kind="transaction", amount="199")
    assert e.amount == Decimal("199.00")
    assert e.model_dump(mode="json")["amount"] == "199.00"


def test_render_amount_no_grouping():
    assert render_amount(Decimal("100308.53"), "INR") == "₹100308.53"
    assert render_amount(Decimal("5.89"), "USD") == "$5.89"
    assert render_amount(Decimal("6285.01"), "GBP") == "£6285.01"
    assert render_amount(Decimal("10"), "EUR") == "€10.00"
    assert render_amount(Decimal("12"), "SGD") == "12.00 SGD"
    assert render_amount(Decimal("150"), "INR", negative=True) == "-₹150.00"
    # The amount is a magnitude — the sign is the caller's `negative` flag, never the input.
    assert render_amount(Decimal("-150"), "INR", negative=True) == "-₹150.00"
    assert render_amount(Decimal("-150"), "INR") == "₹150.00"


def test_fmt_money_is_reexported():
    assert fmt_money(Decimal("1234.5"), "INR") == "₹1,234.50"


@pytest.mark.parametrize(
    "category,direction,entity,expected",
    [
        ("saas", "out", "personal", "expenses:saas"),
        ("saas", "out", "hikmah", "expenses:hikmah:saas"),
        ("infra", "out", "hikmah", "expenses:hikmah:infra"),
        ("infra", "out", "personal", "expenses:saas"),
        ("electricity", "out", "personal", "expenses:utilities:electricity"),
        ("electricity", "out", "hikmah", "expenses:hikmah:unknown"),
        ("groceries", "out", "personal", "expenses:groceries"),
        ("fees", "out", "hikmah", "expenses:hikmah:fees:bank"),
        ("professional", "out", "hikmah", "expenses:hikmah:professional"),
        ("professional", "out", "personal", "expenses:unknown"),
        ("salary", "in", "personal", "income:salary"),
        ("salary", "in", "hikmah", "income:hikmah:other"),
        ("refund", "in", "personal", "income:refunds"),
        ("refund", "in", "hikmah", "income:hikmah:other"),
        ("other", "out", "personal", "expenses:unknown"),
        (None, "in", "hikmah", "income:hikmah:other"),
        (None, None, "personal", "expenses:unknown"),
    ],
)
def test_account_for(category, direction, entity, expected):
    assert account_for(category, direction, entity) == expected


def test_instrument_account():
    declared = {"liabilities:card:axis:1313", "liabilities:card:axis:1747", "assets:bank:hdfc:1225"}
    assert instrument_account("hdfc-1225") == "assets:bank:hdfc:1225"
    assert instrument_account("axis-cc-1313") == "liabilities:card:axis:1313"
    assert instrument_account("nkgsb-843") == "assets:bank:nkgsb:843"
    assert instrument_account("axis-9640") == "assets:bank:axis:9640"
    assert instrument_account("card-1313", declared) == "liabilities:card:axis:1313"
    assert instrument_account("card-9999", declared) == "assets:unknown"
    # With a declared set, a computed account that is not declared is unknown.
    assert instrument_account("hdfc-1225", declared) == "assets:bank:hdfc:1225"
    assert instrument_account("hdfc-0000", declared) == "assets:unknown"
    assert instrument_account("axis-upi", declared) == "assets:unknown"
    assert instrument_account(None) == "assets:unknown"
    assert instrument_account("") == "assets:unknown"


def test_instrument_account_tolerates_a_zero_padded_tail():
    """`nkgsb-0843` and `nkgsb-843` are one account, and the padded spelling
    used to resolve to `assets:unknown` — where ₹53,774.56 of real money sits.

    The chart still decides. An account the user has not declared stays
    unknown, because `hledger check --strict` rejects an undeclared account,
    and the DECLARED spelling is what comes back for the same reason.
    """
    declared = {
        "assets:bank:nkgsb:843",
        "assets:bank:icici:143",
        "assets:bank:hdfc:1225",
        "assets:bank:axis:9640",
        "liabilities:card:axis:1313",
    }
    assert instrument_account("nkgsb-0843", declared) == "assets:bank:nkgsb:843"
    assert instrument_account("icici-0143", declared) == "assets:bank:icici:143"
    assert instrument_account("card-01313", declared) == "liabilities:card:axis:1313"
    assert instrument_account("hdfc-0325", declared) == "assets:unknown"
    assert instrument_account("icici-9954", declared) == "assets:unknown"
    # Leading zeros ONLY. A comparison that ignores length is a different bug:
    # a one-digit tail must never reach a three-digit account.
    assert instrument_account("axis-1", declared) == "assets:unknown"
    assert instrument_account("icici-43", declared) == "assets:unknown"
    assert instrument_account("card-313", declared) == "assets:unknown"


def test_canonical_instrument_gives_one_spelling_per_account():
    """One card, one spelling. `card-1313` (a receipt, which knows the digits
    but not the bank) and `axis-cc-1313` (the bank's own alert) are the same
    card, and both must group as one.
    """
    declared = {
        "assets:bank:nkgsb:843",
        "assets:bank:icici:143",
        "assets:bank:hdfc:1225",
        "liabilities:card:axis:1313",
    }
    assert canonical_instrument("card-1313", declared) == "axis-cc-1313"
    assert canonical_instrument("axis-cc-1313", declared) == "axis-cc-1313"
    assert canonical_instrument("nkgsb-0843", declared) == "nkgsb-843"
    assert canonical_instrument("hdfc-1225", declared) == "hdfc-1225"
    # Whatever comes back still resolves to the account it was derived from.
    for raw in ("card-1313", "axis-cc-1313", "nkgsb-0843", "icici-0143", "hdfc-1225"):
        assert instrument_account(canonical_instrument(raw, declared), declared) == (
            instrument_account(raw, declared)
        )
    # An instrument the chart cannot resolve is kept EXACTLY as parsed, never
    # rewritten and never dropped: `hdfc-0325` is a real account the user has
    # not declared yet, and losing it would hide that from them.
    assert canonical_instrument("hdfc-0325", declared) == "hdfc-0325"
    assert canonical_instrument("axis-upi", declared) == "axis-upi"
    assert canonical_instrument("card-9999", declared) == "card-9999"
    # No chart to hand (books disabled) changes nothing.
    assert canonical_instrument("card-1313") == "card-1313"
    assert canonical_instrument("nkgsb-0843") == "nkgsb-0843"
    assert canonical_instrument(None, declared) is None
    # A derived spelling that would read back as a DIFFERENT account is
    # refused: the bank segment here carries the "-" the spelling splits on.
    assert canonical_instrument("card-77", {"liabilities:card:hdfc-bank:77"}) == "card-77"


HOSTILE_CURRENCY = "\u20b9\n    ; hijacked: true"


def _currency(value):
    return MoneyEvent(kind="transaction", currency=value).currency


def test_currency_accepts_only_iso_4217_codes():
    """`currency` is in `_LLM_EVENT_FIELDS`, so a steered email chooses it, and
    it reaches a posting line through `render_amount`. ISO-4217 is the whole
    vocabulary and `_SYMBOL` is keyed on exactly that, so anything else is
    coerced to "" — one bad extraction degrades that item rather than failing
    the whole batch."""
    assert _currency("USD") == "USD"
    assert _currency("inr") == "INR"
    assert _currency(" gbp ") == "GBP"
    assert _currency("") == ""
    assert _currency(None) is None
    assert _currency(HOSTILE_CURRENCY) == ""
    for bad in ("US", "USDD", "US1", "\u20b9", "US-"):
        assert _currency(bad) == "", bad


def test_render_amount_cannot_be_injected_through_the_currency_code():
    """The writer is the last gate: `books.py` is a public service module, so a
    caller that never passed through `MoneyEvent` must not be able to put a
    second line on the posting either. Letters only, capped at 3."""
    out = render_amount(Decimal("10"), HOSTILE_CURRENCY)
    assert "\n" not in out and ";" not in out
    assert out == "10.00 HIJ"
    # ...and the legitimate codes are untouched.
    assert render_amount(Decimal("10"), "inr") == "\u20b910.00"
    assert render_amount(Decimal("10"), "") == "10.00"
    assert render_amount(Decimal("10"), "USD", negative=True) == "-$10.00"
