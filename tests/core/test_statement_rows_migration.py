"""Migrations 032 and 043 — `finance.statement_rows` has the shape spec §7
gives it, plus the two foreign-currency columns §8.5 earned."""

import pytest

# The columns and their types, exactly as §7 declares them. A statement row that
# cannot hold `balance_after` (NULL on cards) or `candidates` (the msgids an
# ambiguous row could not choose between) silently loses what the matcher and
# the digest are built on, so this is pinned rather than smoke-tested.
#
# `fx_currency`/`fx_amount` (migration 043) hold the original of a card charge
# made abroad, which only the card layout prints. Without them the matcher has
# to convert a rupee amount back through a rate production does not have.
EXPECTED = {
    "row_id": ("text", "NO"),
    "instrument": ("text", "NO"),
    "occurred_on": ("date", "NO"),
    "narration": ("text", "NO"),
    "ref": ("text", "YES"),
    "direction": ("text", "NO"),
    "amount": ("numeric", "NO"),
    "balance_after": ("numeric", "YES"),
    "statement_id": ("text", "NO"),
    "file_sha256": ("text", "NO"),
    "matched_msgid": ("text", "YES"),
    "candidates": ("jsonb", "YES"),
    "posted_at": ("timestamp with time zone", "YES"),
    "skip_reason": ("text", "YES"),
    "created_at": ("timestamp with time zone", "NO"),
    "fx_currency": ("text", "YES"),
    "fx_amount": ("numeric", "YES"),
}


@pytest.mark.asyncio
async def test_statement_rows_has_the_columns_the_spec_declares(db_pool):
    rows = await db_pool.fetch(
        "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
        "WHERE table_schema='finance' AND table_name='statement_rows'"
    )
    assert {r["column_name"]: (r["data_type"], r["is_nullable"]) for r in rows} == EXPECTED


@pytest.mark.asyncio
async def test_the_money_columns_keep_two_decimal_places(db_pool):
    rows = await db_pool.fetch(
        "SELECT column_name, numeric_precision, numeric_scale FROM information_schema.columns "
        "WHERE table_schema='finance' AND table_name='statement_rows' "
        "AND column_name IN ('amount','balance_after','fx_amount')"
    )
    assert {r["column_name"]: (r["numeric_precision"], r["numeric_scale"]) for r in rows} == {
        "amount": (14, 2),
        "balance_after": (14, 2),
        "fx_amount": (14, 2),
    }


@pytest.mark.asyncio
async def test_the_row_id_is_the_primary_key_so_a_re_import_cannot_duplicate(db_pool):
    keys = await db_pool.fetchval(
        "SELECT array_agg(a.attname ORDER BY a.attname) FROM pg_index i "
        "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
        "WHERE i.indrelid = 'finance.statement_rows'::regclass AND i.indisprimary"
    )
    assert list(keys) == ["row_id"]


@pytest.mark.asyncio
async def test_the_matchers_two_lookups_are_indexed(db_pool):
    names = await db_pool.fetch(
        "SELECT indexname, indexdef FROM pg_indexes "
        "WHERE schemaname='finance' AND tablename='statement_rows'"
    )
    by_name = {r["indexname"]: r["indexdef"] for r in names}
    assert "statement_rows_instrument_day" in by_name
    assert "(instrument, occurred_on)" in by_name["statement_rows_instrument_day"]
    # Partial, like journal_index_ref: most rows carry no reference at all.
    assert "WHERE (ref IS NOT NULL)" in by_name["statement_rows_ref"]
