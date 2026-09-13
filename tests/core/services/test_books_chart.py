"""The books' entities and chart of accounts are configuration, not code (#560).

Five things this file is here to prove, because each has a way of going wrong
that nothing else would catch:

* a second entity's categories and unknown accounts actually reach the posting
  decision, rather than every transaction quietly taking the default's;
* `entity_of` reads the entity out of the account NAME, and returns None for
  the trees every set of books shares — the guard four cross-entity checks rest
  on;
* the strict write refuses a chart that could not work, and refuses it BEFORE
  anything is stored;
* the lenient read never raises, whatever is in the row, because the money lane
  reads it on every post;
* migration 049 seeds exactly the chart the deleted constants carried, so the
  day after the deploy posts where the day before did.
"""

from __future__ import annotations

import pytest
from aegis.services import books, books_chart

from tests.books_chart_data import CHART, OPERATOR_CHART

#: A fork's chart plus one segmented entity of its own. Nobody's real company:
#: the point is that a SECOND entity works, not which one.
TWO_ENTITY: dict = {
    "default_entity": "personal",
    "income_categories": ["salary"],
    "entities": {
        "personal": {
            "label": "Personal",
            "segment": "",
            "unknown": {"in": "income:unknown", "out": "expenses:unknown"},
            "categories": {
                "rent": "expenses:rent",
                "salary": "income:salary",
                # An income account whose category is NOT an income category, so
                # the direction alone decides its side.
                "bonus": "income:bonus",
            },
        },
        "acme": {
            "label": "Acme Ltd",
            "segment": "acme",
            "unknown": {"in": "income:acme:other", "out": "expenses:acme:unknown"},
            "categories": {"rent": "expenses:acme:rent"},
        },
    },
}


def _chart(data: dict) -> books_chart.Chart:
    return books_chart.Chart.from_dict(books_chart.merge(data))


# ------------------------------------------------------------------ the chart


def test_a_configured_entity_files_into_its_own_accounts():
    """FALSIFIABILITY (1/3). The whole point of a second set of books: its
    mapped category goes to ITS account, and a category it does not map goes to
    ITS unknown account — never to the default entity's.

    Break `Chart.account_for` to ignore the entity (resolve everything to
    `default_entity`) and both assertions here fail: the first reads
    `expenses:rent`, the second `expenses:unknown`.
    """
    chart = _chart(TWO_ENTITY)
    assert chart.account_for("rent", "out", "acme") == "expenses:acme:rent"
    assert chart.account_for("stationery", "out", "acme") == "expenses:acme:unknown"
    # And the default entity is untouched by the second one existing.
    assert chart.account_for("rent", "out", "personal") == "expenses:rent"
    assert chart.account_for("stationery", "out", "personal") == "expenses:unknown"


def test_an_entity_with_no_income_categories_files_credits_to_its_unknown_in():
    """`acme` maps no income account, so money IN has no income side to land on.
    It goes to its unknown-IN rather than to `expenses:acme:rent` — which is
    what the old `if side == "in" and ent == "hikmah"` branch did, generalised."""
    chart = _chart(TWO_ENTITY)
    assert chart.account_for("rent", "in", "acme") == "income:acme:other"
    assert chart.account_for(None, "in", "acme") == "income:acme:other"


def test_a_mapped_account_on_the_wrong_side_falls_back_to_unknown():
    """A category that means an expense cannot absorb a credit. Sending it to
    the unknown account keeps the money visible instead of silently reversing
    the sign of an account nobody reviews."""
    chart = _chart(TWO_ENTITY)
    assert chart.account_for("rent", "in", "personal") == "income:unknown"
    assert chart.account_for("bonus", "out", "personal") == "expenses:unknown"


def test_an_income_category_decides_the_side_when_the_direction_is_missing():
    chart = _chart(TWO_ENTITY)
    assert chart.account_for("salary", None, "personal") == "income:salary"
    assert chart.account_for("rent", None, "personal") == "expenses:rent"


def test_an_unconfigured_entity_resolves_to_the_default():
    """`MoneyEvent.entity` is shape-checked and never checked against the chart
    — the worker, the chat tools and the tests all build events with no pool.
    So a name nothing configures arrives here and has to mean something."""
    chart = _chart(TWO_ENTITY)
    assert chart.resolve("nobody") == "personal"
    assert chart.resolve(None) == "personal"
    assert chart.account_for("rent", "out", "nobody") == "expenses:rent"


def test_entity_of_reads_the_entity_out_of_the_account_name():
    """FALSIFIABILITY (2/3). The segment decides, and only inside the expense
    and income trees. Assets, liabilities and equity are entity-NEUTRAL by
    design — `post_event` writes `assets:bank:*` into either set of books
    through `instrument_account` — and four cross-entity guards return early on
    that None.

    Break `entity_of` to return the default entity for assets (drop the
    `startswith` early return) and the second half of this fails.
    """
    chart = _chart(TWO_ENTITY)
    assert chart.entity_of("expenses:acme:rent") == "acme"
    assert chart.entity_of("income:acme:other") == "acme"
    # The segment may also END the account name.
    assert chart.entity_of("expenses:acme") == "acme"
    # Anything the segments do not claim belongs to the default entity.
    assert chart.entity_of("expenses:rent") == "personal"
    assert chart.entity_of("income:salary") == "personal"
    # Entity-neutral: shared by every set of books.
    assert chart.entity_of("assets:bank:x") is None
    assert chart.entity_of("liabilities:card:axis:1313") is None
    assert chart.entity_of("equity:transfers") is None


def test_a_segment_is_matched_whole_never_as_a_prefix():
    """`acme` must not claim `expenses:acmecorp:rent` — they are different
    accounts and the second belongs to whoever declared it."""
    chart = _chart(TWO_ENTITY)
    assert chart.entity_of("expenses:acmecorp:rent") == "personal"


def test_the_books_delegates_take_the_chart_and_agree_with_it():
    """`books.account_for` / `books.account_entity` are what the call sites
    use; they must be the same answer, not a second one."""
    chart = _chart(TWO_ENTITY)
    assert books.account_for(chart, "rent", "out", "acme") == chart.account_for(
        "rent", "out", "acme"
    )
    assert books.account_entity(chart, "assets:bank:x") is None


# ------------------------------------------------------------- the code default


def test_the_code_default_names_nobody():
    """A fork gets one entity, no segment, and generic categories. Not this
    operator's company, and not this operator's accounts."""
    default = _chart(None)
    assert default.ids == ("personal",)
    assert default.default_entity == "personal"
    assert default.segment("personal") == ""
    accounts = set(books_chart.DEFAULT_CHART["entities"]["personal"]["categories"].values())
    assert all(a.startswith(("expenses:", "income:")) for a in accounts)
    # Every expense and income account is the default entity's, because nothing
    # else claims one.
    assert default.entity_of("expenses:anything") == "personal"


# ------------------------------------------------------------------ the reading


@pytest.mark.parametrize(
    "row",
    [
        None,
        {},
        "not an object",
        {"entities": "not a map"},
        {"entities": {}},
        {"entities": {"personal": "not an object"}},
        {"default_entity": "ghost", "entities": {"a": {"segment": "a"}}},
        {"entities": {"a": {"segment": "a", "unknown": "not a map", "categories": 7}}},
        {"income_categories": 42, "entities": {"personal": {"segment": ""}}},
    ],
)
def test_a_broken_row_still_reads_as_a_usable_chart(row):
    """The money lane reads this on every post. A row somebody hand-edited into
    nonsense must cost the wrong accounts on a few transactions, never the
    whole lane."""
    chart = _chart(row)
    assert chart.ids
    assert chart.default_entity in chart.ids
    for ent in chart.ids:
        assert chart.unknown(ent, "in") and chart.unknown(ent, "out")
    # And it can still answer the question every post asks.
    assert chart.account_for("whatever", "out", "whoever")


def test_a_partial_entity_borrows_the_default_unknown_accounts():
    chart = _chart({"default_entity": "p", "entities": {"p": {"segment": ""}}})
    assert chart.unknown("p", "out") == "expenses:unknown"
    assert chart.unknown("p", "in") == "income:unknown"
    assert chart.label("p") == "P"


# ------------------------------------------------------------------ the writing


def test_validate_accepts_a_real_chart_unchanged():
    stored = books_chart.validate(TWO_ENTITY)
    assert stored["default_entity"] == "personal"
    assert set(stored["entities"]) == {"personal", "acme"}
    assert stored["entities"]["acme"]["unknown"]["out"] == "expenses:acme:unknown"
    assert stored["income_categories"] == ["salary"]


@pytest.mark.parametrize(
    "bad,says",
    [
        ({"default_entity": "p", "entities": {"P ltd": {"segment": "p"}}}, "entity id"),
        ({"default_entity": "ghost", "entities": {"p": {"segment": ""}}}, "default_entity"),
        ({"default_entity": "p", "entities": {}}, "at least one entity"),
        (
            {
                "default_entity": "p",
                "entities": {
                    "p": {"segment": "", "unknown": {"in": "income:u", "out": "expenses:u"}},
                    "q": {"segment": "", "unknown": {"in": "income:u", "out": "expenses:u"}},
                },
            },
            "needs an account segment",
        ),
        (
            {
                "default_entity": "p",
                "entities": {
                    "p": {
                        "segment": "",
                        "unknown": {"in": "expenses:oops", "out": "expenses:u"},
                    }
                },
            },
            "unknown-IN",
        ),
        (
            {
                "default_entity": "p",
                "entities": {
                    "p": {"segment": "", "unknown": {"in": "income:u", "out": "income:oops"}}
                },
            },
            "unknown-OUT",
        ),
        (
            {
                "default_entity": "p",
                "entities": {
                    "p": {
                        "segment": "",
                        "unknown": {"in": "income:u", "out": "expenses:u"},
                        "categories": {"rent": "Expenses Rent"},
                    }
                },
            },
            "not an account name",
        ),
    ],
)
def test_validate_refuses_what_could_not_work(bad, says):
    with pytest.raises(ValueError, match=says):
        books_chart.validate(bad)


def test_validate_refuses_two_entities_sharing_a_segment():
    """FALSIFIABILITY (3/3), the check half. With one segment on two entities,
    `entity_of` answers by whichever the chart happens to list first — an
    account name could not say which set of books it belongs to.

    Drop the `by_segment` check from `validate` and this passes silently.
    """
    both = {
        "default_entity": "p",
        "entities": {
            "p": {"segment": "", "unknown": {"in": "income:u", "out": "expenses:u"}},
            "a": {"segment": "acme", "unknown": {"in": "income:a", "out": "expenses:a"}},
            "b": {"segment": "acme", "unknown": {"in": "income:b", "out": "expenses:b"}},
        },
    }
    with pytest.raises(ValueError, match="both claim the segment"):
        books_chart.validate(both)


# ------------------------------------------------------------ against the DB


@pytest.mark.asyncio
async def test_the_migration_seeds_the_chart_the_code_used_to_carry(db_pool):
    """Migration 049 must reproduce `books.UNKNOWN`, `books._ACCOUNT_MAP` and
    `books._INCOME_CATEGORIES` exactly — the deploy is a configuration change,
    not a behaviour change.

    This is also what makes `tests/books_chart_data.py` evidence rather than a
    restatement: every books test posts against that literal, and this is the
    only thing tying it to what a real deployment gets.
    """
    stored = await books_chart.get_chart(db_pool)
    assert stored.as_dict() == books_chart.merge(OPERATOR_CHART)
    assert stored.as_dict() == CHART.as_dict()


@pytest.mark.asyncio
async def test_a_save_replaces_the_row_and_a_refused_save_leaves_it_alone(db_pool):
    """FALSIFIABILITY (3/3), the storage half: a 400 must write NOTHING. A
    validate that ran after the INSERT would leave the books filing against a
    chart the operator was told had been rejected."""
    before = (await books_chart.read(db_pool))["chart"]
    with pytest.raises(ValueError):
        await books_chart.save_chart(
            db_pool,
            {
                "default_entity": "p",
                "entities": {
                    "p": {"segment": "", "unknown": {"in": "income:u", "out": "expenses:u"}},
                    "a": {"segment": "acme", "unknown": {"in": "income:a", "out": "expenses:a"}},
                    "b": {"segment": "acme", "unknown": {"in": "income:b", "out": "expenses:b"}},
                },
            },
        )
    assert (await books_chart.read(db_pool))["chart"] == before

    try:
        saved = await books_chart.save_chart(db_pool, TWO_ENTITY)
        assert saved["stored"] is True
        assert set(saved["chart"]["entities"]) == {"personal", "acme"}
        # A REPLACEMENT: the entity the new chart does not name is gone, which
        # is what makes removing one possible at all.
        assert "hikmah" not in saved["chart"]["entities"]
        assert (await books_chart.get_chart(db_pool)).entity_of("expenses:acme:rent") == "acme"
    finally:
        await books_chart.save_chart(db_pool, before)
