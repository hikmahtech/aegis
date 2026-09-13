"""The chart of accounts the books tests run against (#560).

The money lane used to carry this in Python (`books.UNKNOWN`,
`books._ACCOUNT_MAP`, `books._INCOME_CATEGORIES`). It is now a settings row, so
the tests need their own copy — and a copy that only ever agreed with itself
would be worthless. `tests/core/services/test_books_chart.py` reads the MIGRATED
database and asserts migration `049_books_chart.sql` seeds exactly this, which
is what makes it evidence rather than a restatement.

It is deliberately this deployment's real chart, two entities and all: the books
tests are written against the real journal (`expenses:hikmah:*`,
`assets:bank:hdfc:1225`, the two journal directories), and the whole point of
the entity split is what most of them exercise. A test about what a FORK gets
builds its own from `books_chart.merge(None)` and says so.
"""

from __future__ import annotations

from aegis.services import books_chart

OPERATOR_CHART: dict = {
    "default_entity": "personal",
    "income_categories": ["interest", "refund", "salary"],
    "entities": {
        "personal": {
            "label": "Personal",
            "segment": "",
            "unknown": {"in": "income:unknown", "out": "expenses:unknown"},
            "categories": {
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
        },
        "hikmah": {
            "label": "Hikmah",
            "segment": "hikmah",
            "unknown": {"in": "income:hikmah:other", "out": "expenses:hikmah:unknown"},
            "categories": {
                "saas": "expenses:hikmah:saas",
                "media": "expenses:hikmah:saas",
                "infra": "expenses:hikmah:infra",
                "internet": "expenses:hikmah:internet",
                "fees": "expenses:hikmah:fees:bank",
                "tax": "expenses:hikmah:tax",
                "professional": "expenses:hikmah:professional",
                "ads": "expenses:hikmah:ads",
            },
        },
    },
}

#: Ready to pass wherever a `Chart` is wanted.
CHART = books_chart.Chart.from_dict(books_chart.merge(OPERATOR_CHART))

#: What a fork gets before anyone configures anything: one entity, no segment,
#: nobody's company.
DEFAULT_CHART = books_chart.Chart.from_dict(books_chart.merge(None))
