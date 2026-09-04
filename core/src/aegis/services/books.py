"""The books: journal helpers, rules, git sync and the hledger runner.

Spec: docs/superpowers/specs/2026-09-05-maou-books-design.md §1, §4, §5.
This module is shared by core (tools) and worker (flows). Amounts are
`Decimal` major units; `render_amount` is the journal form (no grouping),
`fmt_money` the human form.
"""

from __future__ import annotations

from decimal import Decimal

from aegis.services.money_format import fmt_money  # noqa: F401 — re-export (spec §5.1)

_SYMBOL = {"INR": "₹", "USD": "$", "GBP": "£", "EUR": "€"}
_CENT = Decimal("0.01")

UNKNOWN = {
    "personal": {"out": "expenses:unknown", "in": "income:unknown"},
    "hikmah": {"out": "expenses:hikmah:unknown", "in": "income:hikmah:other"},
}

# spec §4 — category → account, per entity. Missing key ⇒ the unknown account.
_ACCOUNT_MAP: dict[str, dict[str, str]] = {
    "personal": {
        "saas": "expenses:saas",
        "media": "expenses:media",
        "infra": "expenses:saas",
        "internet": "expenses:utilities:internet",
        "electricity": "expenses:utilities:electricity",
        "mobile": "expenses:utilities:mobile",
        "groceries": "expenses:groceries",
        "food": "expenses:food",
        "transport": "expenses:transport",
        "shopping": "expenses:shopping",
        "health": "expenses:health",
        "insurance": "expenses:insurance",
        "fees": "expenses:fees:bank",
        "tax": "expenses:tax",
        "people": "expenses:people",
        "salary": "income:salary",
        "interest": "income:interest",
        "refund": "income:refunds",
    },
    "hikmah": {
        "saas": "expenses:hikmah:saas",
        "media": "expenses:hikmah:saas",
        "infra": "expenses:hikmah:infra",
        "internet": "expenses:hikmah:internet",
        "fees": "expenses:hikmah:fees:bank",
        "tax": "expenses:hikmah:tax",
        "professional": "expenses:hikmah:professional",
        "ads": "expenses:hikmah:ads",
    },
}

_INCOME_CATEGORIES = frozenset({"salary", "interest", "refund"})


def render_amount(amount: Decimal, currency: str, *, negative: bool = False) -> str:
    """Journal amount: symbol-prefixed, no digit grouping, ISO suffix otherwise.

    `amount` is a magnitude — the sign comes from `negative` alone, as in
    `fmt_money`, so a negative input never double-negates.
    """
    q = abs(Decimal(amount).quantize(_CENT))
    code = (currency or "").upper()
    sign = "-" if negative else ""
    sym = _SYMBOL.get(code)
    return f"{sign}{sym}{q}" if sym else f"{sign}{q} {code}".strip()


def account_for(category: str | None, direction: str | None, entity: str) -> str:
    """Counter account for an event (spec §4). Unknown ⇒ the entity's unknown account."""
    ent = "hikmah" if entity == "hikmah" else "personal"
    side = "in" if direction == "in" or (category or "") in _INCOME_CATEGORIES else "out"
    if side == "in" and ent == "hikmah":
        return "income:hikmah:other"
    mapped = _ACCOUNT_MAP[ent].get((category or "").lower())
    if mapped and (mapped.startswith("income:") == (side == "in")):
        return mapped
    return UNKNOWN[ent][side]


def instrument_account(
    instrument: str | None, declared: set[str] | frozenset[str] = frozenset()
) -> str:
    """`hdfc-1225` → `assets:bank:hdfc:1225`, `axis-cc-1313` → `liabilities:card:axis:1313`,
    `card-1313` → the declared `liabilities:card:*:1313`, else `assets:unknown`."""
    if not instrument:
        return "assets:unknown"
    parts = instrument.lower().split("-")
    if len(parts) == 2 and parts[0] == "card":
        tail = parts[1]
        for acct in sorted(declared):
            if acct.startswith("liabilities:card:") and acct.endswith(f":{tail}"):
                return acct
        return "assets:unknown"
    if len(parts) == 3 and parts[1] == "cc":
        computed = f"liabilities:card:{parts[0]}:{parts[2]}"
    elif len(parts) == 2:
        computed = f"assets:bank:{parts[0]}:{parts[1]}"
    else:
        return "assets:unknown"
    # An empty `declared` means "no chart to check against" (unit tests);
    # with a chart, an undeclared instrument must not break `check --strict`.
    if declared and computed not in declared:
        return "assets:unknown"
    return computed
