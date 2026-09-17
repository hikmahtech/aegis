"""The books' currency is `settings.home_currency`, not a literal (#560).

Five places wrote `"INR"` while `settings.home_currency` existed and exactly one
caller read it. Each of the five fails in a way nothing else would catch, and
none of them raises when it is wrong:

* `render_manual` would write a rupee sign into a block of dollars;
* `cleared_movement_sync` would ask hledger to convert to a commodity the books
  do not use, get one it could not price, and revert every statement;
* `_write_post` would index a payment in the wrong currency;
* `event_for` would stamp a statement row with a currency the bank never
  printed;
* `match_statements` would compare a row's amount against candidates in another
  currency and silently match nothing.

`BooksConfig.currency` is the one seam, so `USD` is what makes these falsifiable
— with `INR` every candidate implementation agrees.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from aegis.services import books, ledger_write, statement_match


def test_a_posting_with_no_currency_takes_the_books_home_currency():
    """`render_manual` has no default: the block's text IS its identity
    (`manual_msgid` digests it), so a guessed currency would give the same
    transaction two ids."""
    postings = [
        {"account": "expenses:groceries", "amount": "10.00"},
        {"account": "assets:bank:hdfc:1225", "amount": "-10.00"},
    ]
    rupees = books.render_manual(date(2026, 9, 2), "Shop", postings, "m/1", currency="INR")
    dollars = books.render_manual(date(2026, 9, 2), "Shop", postings, "m/1", currency="USD")

    assert "₹10.00" in rupees and "$10.00" not in rupees
    assert "$10.00" in dollars and "₹10.00" not in dollars


def test_the_msgid_follows_the_currency_the_block_was_written_in():
    """Same transaction, two currencies, two blocks — so two ids. Digesting the
    rendered block is what makes "same id" mean "same journal entry"."""
    postings = [
        {"account": "expenses:groceries", "amount": "10.00"},
        {"account": "assets:bank:hdfc:1225", "amount": "-10.00"},
    ]
    args = ("personal", date(2026, 9, 2), "Shop", postings, "")
    assert ledger_write.manual_msgid(*args, "INR") != ledger_write.manual_msgid(*args, "USD")
    assert ledger_write.manual_msgid(*args, "USD") == ledger_write.manual_msgid(*args, "USD")


def test_the_config_takes_its_currency_from_the_settings_field():
    """`config_from_settings` is where `settings.home_currency` reaches the
    books; everything downstream reads `cfg.currency`."""
    cfg = books.config_from_settings(
        SimpleNamespace(home_currency="GBP", books_path="/tmp/x", gmail_token_dir="/tmp")
    )
    assert cfg.currency == "GBP"
    # An unset field falls back to the same default `Settings.home_currency`
    # declares, rather than to an empty string that would render no commodity.
    assert books.config_from_settings(SimpleNamespace(books_path="/tmp/x")).currency == "INR"


def test_the_commodity_is_the_symbol_when_there_is_one_and_the_code_otherwise():
    """The rule `render_amount` uses to PUT a commodity in the file, so a query
    asks for exactly what the blocks say."""
    assert books.commodity("INR") == "₹"
    assert books.commodity("USD") == "$"
    assert books.commodity("AED") == "AED"


def test_cleared_movement_asks_hledger_for_the_home_commodity(monkeypatch):
    """The `-X` argument decides what a statement's balance check is measured
    in. Pointing it at a commodity the books do not hold makes every row
    unpriced, which reverts the statement."""
    seen: list[list[str]] = []

    def fake_spawn(argv, **kw):
        seen.append(argv)
        return SimpleNamespace(returncode=0, stdout="account,balance\n", stderr="")

    monkeypatch.setattr(books, "_spawn", fake_spawn)
    cfg = books.BooksConfig(path=Path("/tmp"), currency="USD")

    books.cleared_movement_sync(cfg, "assets:bank:x", date(2026, 7, 1), date(2026, 7, 31))

    assert "-X" in seen[0]
    assert seen[0][seen[0].index("-X") + 1] == "$"


def test_a_cell_in_another_commodity_is_named_against_the_home_one(monkeypatch):
    """The refusal sentence has to name the commodity the conversion targeted,
    or it describes a conversion that never ran."""

    def fake_spawn(argv, **kw):
        return SimpleNamespace(
            returncode=0, stdout='account,balance\n"assets:bank:x","₹-100.00, $-4.00"\n', stderr=""
        )

    monkeypatch.setattr(books, "_spawn", fake_spawn)
    cfg = books.BooksConfig(path=Path("/tmp"), currency="USD")

    try:
        books.cleared_movement_sync(cfg, "assets:bank:x", date(2026, 7, 1), date(2026, 7, 31))
    except books.BooksCheckError as exc:
        assert "is not all $" in str(exc) and "₹" in str(exc)
    else:  # pragma: no cover — the mixed cell must raise
        raise AssertionError("a cell holding two commodities must raise")


def test_the_matcher_compares_against_the_currency_it_is_told():
    """`match_statements` takes the currency with no default. A wrong one does
    not fail — it stops matching every row, which reads as a clean run with a
    whole class of rows missing."""
    from aegis.services.statement_match import Candidate

    candidate = Candidate(
        msgid="m/1",
        entity="personal",
        direction="out",
        amount=Decimal("100.00"),
        currency="USD",
        occurred_on=date(2026, 7, 2),
        instrument="hdfc-1225",
    )
    # Same currency as the candidate: an exact compare, no rate needed.
    assert statement_match._amount_matches(
        Decimal("100.00"), candidate, currency="USD", rates={}, missing_rates=set()
    )
    # Told the books are in rupees, the dollar candidate needs a RATE, and
    # there is none — so it does not match and the missing rate is reported.
    missing: set[str] = set()
    assert not statement_match._amount_matches(
        Decimal("100.00"), candidate, currency="INR", rates={}, missing_rates=missing
    )
    assert missing == {"USD"}
