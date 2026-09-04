"""The one money formatter. Every human-facing amount goes through `fmt_money`.

Major units in, grouped string out. Indian grouping for INR (₹1,00,308.53),
thousands grouping for the symbol currencies, ISO suffix for the rest.
`amount_cents` never reaches a title, message or report (spec §5.1).
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

_SYMBOL = {"INR": "₹", "USD": "$", "GBP": "£", "EUR": "€"}
_CENT = Decimal("0.01")


def _indian_group(whole: str) -> str:
    """'100308' -> '1,00,308'; '1234' -> '1,234'; '999' -> '999'."""
    if len(whole) <= 3:
        return whole
    head, tail = whole[:-3], whole[-3:]
    parts: list[str] = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return ",".join(parts) + "," + tail


def fmt_money(amount: Decimal | int | float | str | None, currency: str | None) -> str:
    # A receipt with no parsable amount formats to nothing. MoneyEvent.amount
    # is `Decimal | None`, so this is normal input, not a caller bug.
    if amount is None or amount == "":
        return ""
    q = Decimal(str(amount)).quantize(_CENT, rounding=ROUND_HALF_UP)
    sign = "-" if q < 0 else ""
    whole, frac = f"{abs(q):.2f}".split(".")
    code = (currency or "").upper()
    grouped = _indian_group(whole) if code == "INR" else f"{int(whole):,}"
    sym = _SYMBOL.get(code)
    if sym:
        return f"{sign}{sym}{grouped}.{frac}"
    if code:
        return f"{sign}{grouped}.{frac} {code}"
    return f"{sign}{grouped}.{frac}"
