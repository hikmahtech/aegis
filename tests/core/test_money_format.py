from decimal import Decimal

from aegis.services.money_format import fmt_money


def test_inr_uses_indian_grouping():
    assert fmt_money(Decimal("100308.53"), "INR") == "₹1,00,308.53"
    assert fmt_money(Decimal("1234.56"), "INR") == "₹1,234.56"
    assert fmt_money(Decimal("10"), "INR") == "₹10.00"
    assert fmt_money(Decimal("12345678.9"), "inr") == "₹1,23,45,678.90"


def test_western_currencies_use_symbol_and_thousands():
    assert fmt_money(Decimal("5.89"), "USD") == "$5.89"
    assert fmt_money(Decimal("6285.01"), "GBP") == "£6,285.01"
    assert fmt_money(10, "EUR") == "€10.00"


def test_unknown_currency_is_iso_suffix():
    assert fmt_money(Decimal("12"), "SGD") == "12.00 SGD"


def test_missing_currency_is_bare_number():
    assert fmt_money(Decimal("12.5"), None) == "12.50"
    assert fmt_money(Decimal("12.5"), "") == "12.50"


def test_negative_and_rounding():
    assert fmt_money(Decimal("-5"), "INR") == "-₹5.00"
    assert fmt_money("1.005", "USD") == "$1.01"
    assert fmt_money(0.1 + 0.2, "USD") == "$0.30"


def test_missing_amount_is_empty_string():
    """MoneyEvent.amount is `Decimal | None` — a receipt with no parsable
    amount formats to nothing, it does not blow up the caller."""
    assert fmt_money(None, "INR") == ""
    assert fmt_money("", "INR") == ""
    assert fmt_money(None, None) == ""
