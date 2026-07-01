"""Static FX → INR normalization for Money Hygiene aggregations.

Rates are passed in rather than module-globals so settings owns the
truth, tests can vary, and a quarterly manual update is just a config
change.
"""

from __future__ import annotations

_CADENCE_TO_MONTHLY: dict[str, float] = {
    "monthly": 1.0,
    "quarterly": 1.0 / 3,
    "yearly": 1.0 / 12,
}


def to_monthly_inr(
    amount: float | None,
    currency: str | None,
    cadence: str | None,
    rates: dict[str, float],
) -> float:
    """Convert a per-period charge to its monthly INR equivalent.

    Returns 0.0 when any input is missing or currency is unknown so the
    digest doesn't show garbage totals.
    """
    if amount is None or currency is None or cadence is None:
        return 0.0
    if currency != "INR" and currency not in rates:
        return 0.0
    multiplier = _CADENCE_TO_MONTHLY.get(cadence)
    if multiplier is None:
        # Returns 0.0 for cadence='unknown' (or any unrecognised value) so
        # the monthly digest doesn't aggregate garbage. Upstream is expected
        # to refine the cadence via upsert_charges' upgrade-symmetric merge
        # (see activities/money.py) before the row gets here.
        return 0.0
    fx = 1.0 if currency == "INR" else rates[currency]
    return round(amount * fx * multiplier, 2)
