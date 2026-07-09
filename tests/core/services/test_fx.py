import pytest
from aegis.services.fx import to_monthly_home


@pytest.mark.parametrize(
    "amount,currency,cadence,expected",
    [
        (12.99, "USD", "yearly", 12.99 * 84.5 / 12),  # ≈ 91.45
        (10.00, "USD", "monthly", 10.00 * 84.5),  # 845
        (300.00, "USD", "quarterly", 300.00 * 84.5 / 3),  # 8450
        (1000.00, "INR", "monthly", 1000.00),  # no conversion
        (50.00, "USD", "unknown", 0.0),  # unknown → 0 (don't aggregate)
    ],
)
def test_to_monthly_home(amount, currency, cadence, expected):
    rates = {"USD": 84.5, "EUR": 92.0, "GBP": 108.0, "SGD": 63.0}
    result = to_monthly_home(amount, currency, cadence, rates, "INR")
    assert abs(result - expected) < 0.01


def test_to_monthly_home_unknown_currency_returns_zero():
    assert to_monthly_home(10, "XYZ", "monthly", {"USD": 84.5}, "INR") == 0.0


def test_to_monthly_home_non_inr_home_currency():
    """A self-hoster who sets home_currency to something other than INR gets
    fx=1.0 for that currency (it's the new pivot), and other currencies still
    convert via the configured rates."""
    assert to_monthly_home(10, "USD", "monthly", {}, "USD") == 10.0
    assert to_monthly_home(10, "INR", "monthly", {"INR": 0.012}, "USD") == round(
        10 * 0.012, 2
    )
