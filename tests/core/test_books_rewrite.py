"""`books.rewrite_block` — the promotion path's in-place edit."""

from __future__ import annotations

from aegis.services import books


def test_a_tag_lands_on_a_comment_even_when_the_block_has_only_a_msgid():
    """`add_tags` used to write to line index 2 without looking.

    That is the tags line in every block `render_transaction` writes, so it
    worked — until a block carried only `; msgid:`, and the tag went onto the
    first POSTING instead: `equity:transfers  ₹100000.00, stmt: …`, which
    hledger refuses to parse. `check --strict` catches it and reverts, so no
    ledger is corrupted; what is lost is the whole statement's write, reported
    as a promotion failure that reads like a books problem.
    """
    block = (
        "2026-07-15 ! Transfer\n"
        "    ; msgid: st-imps\n"
        "    equity:transfers          ₹100000.00\n"
        "    assets:bank:axis:9640    ₹-100000.00\n"
    )
    out = books.rewrite_block(block, "st-imps", status="*", add_tags={"stmt": "axis/x"})
    assert "; msgid: st-imps, stmt: axis/x" in out
    assert "₹100000.00, stmt" not in out, out
    # And the postings are untouched.
    assert "    equity:transfers          ₹100000.00\n" in out


def test_cost_notation_replaces_a_previous_cost_rather_than_stacking():
    """A re-run must land on the same text.

    `@@` is the TOTAL cost. `@` would be a unit price, which for
    `$200.00 @ ₹19091.99` claims nineteen thousand rupees per dollar — so the
    stripper has to recognise both spellings or a second promotion appends to
    the first.
    """
    from decimal import Decimal

    assert books.with_cost("$200.00", Decimal("19091.99")) == "$200.00 @@ ₹19091.99"
    once = books.with_cost("$4.00", Decimal("385.65"))
    assert books.with_cost(once, Decimal("385.65")) == once
    # a unit price is stripped too, not appended to
    assert books.with_cost("$4.00 @ ₹96.41", Decimal("385.65")) == "$4.00 @@ ₹385.65"
    # a negative posting keeps its sign; the cost stays positive
    assert (
        books.with_cost("-£5722.20", Decimal("731000.00")) == "-£5722.20 @@ ₹731000.00"
    )


def test_a_promoted_foreign_block_balances_in_rupees():
    """The point of §8.5's cost notation, stated as the property that matters.

    hledger balances a block on its COST, so recording what the bank charged
    makes the other posting rupees — and the instrument account stops holding a
    commodity `-X ₹` cannot value. That is what unblocks §9.3's check; a `P`
    price cannot do it, because it values the dollars at a market rate rather
    than at what was charged.
    """
    block = (
        "2026-08-14 ! Anthropic\n"
        "    ; msgid: a\n"
        "    ; channel: receipt, instrument: card-1313\n"
        "    expenses:hikmah:saas                    $200.00\n"
        "    liabilities:card:axis:1313\n"
    )
    from decimal import Decimal

    out = books.rewrite_block(block, "a", status="*", cost=Decimal("19091.99"))
    assert "$200.00 @@ ₹19091.99" in out
    # the second posting is still blank — hledger infers it, in rupees
    assert out.rstrip().endswith("liabilities:card:axis:1313")
    assert "2026-08-14 * Anthropic" in out


def test_a_rupee_block_is_never_given_a_cost():
    """Only a FOREIGN posting gets one. `₹1936.00 @@ ₹1936.00` is noise at best,
    and wrong the moment the two figures disagree."""
    from decimal import Decimal

    block = (
        "2026-08-25 ! Eleven Labs\n"
        "    ; msgid: b\n"
        "    ; channel: receipt\n"
        "    expenses:hikmah:saas                    ₹1936.00\n"
        "    liabilities:card:axis:1313\n"
    )
    # the caller decides; `rewrite_block` with no cost must not invent one
    out = books.rewrite_block(block, "b", status="*")
    assert "@@" not in out
    assert "₹1936.00" in out
    # and passing one is the caller's error, not silently ignored
    assert "@@" in books.rewrite_block(block, "b", cost=Decimal("1936.00"))
