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
