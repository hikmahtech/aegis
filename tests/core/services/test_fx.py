import pytest
from aegis.services.fx import to_monthly_inr


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
def test_to_monthly_inr(amount, currency, cadence, expected):
    rates = {"USD": 84.5, "EUR": 92.0, "GBP": 108.0, "SGD": 63.0}
    result = to_monthly_inr(amount, currency, cadence, rates)
    assert abs(result - expected) < 0.01


def test_to_monthly_inr_unknown_currency_returns_zero():
    assert to_monthly_inr(10, "XYZ", "monthly", {"USD": 84.5}) == 0.0
