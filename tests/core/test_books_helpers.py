from decimal import Decimal

import pytest
from aegis.api.models.money import MoneyEvent, payee_key
from aegis.services.books import account_for, fmt_money, instrument_account, render_amount


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
