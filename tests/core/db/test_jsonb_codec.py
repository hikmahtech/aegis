"""Real-Postgres tests for the jsonb codec in aegis/db/pool.py.

The pool's `_init_connection` registers a jsonb codec whose encoder applies
`json.dumps`. Passing an already-serialized dict/list string as a jsonb
parameter used to silently double-encode it into a jsonb *string scalar*
instead of an object (`col->>'key'` then reads NULL) — this bit repeatedly
(issue #37, PR #79). The encoder now rejects that specific shape (a string
that itself parses as a JSON object/array) with a loud error instead of
corrupting data.

Bare scalar strings are NOT rejected — jsonb columns legitimately store them
(e.g. `settings.value = "UTC"`), so `_encode_jsonb` must keep encoding those
as plain JSON string scalars.

Note: asyncpg wraps any exception raised inside a custom codec's encoder in
`asyncpg.exceptions.DataError`, chaining the original as `__cause__` — so the
raised type observed here is `DataError`, not the `TypeError` `_encode_jsonb`
itself raises.
"""

from __future__ import annotations

import json

import asyncpg
import pytest


@pytest.mark.asyncio
async def test_dict_round_trips_as_object(db_pool):
    value = {"a": 1, "b": {"nested": True}}
    row = await db_pool.fetchrow("SELECT $1::jsonb AS v", value)
    assert row["v"] == value


@pytest.mark.asyncio
async def test_list_round_trips_as_array(db_pool):
    value = [1, "two", {"three": 3}]
    row = await db_pool.fetchrow("SELECT $1::jsonb AS v", value)
    assert row["v"] == value


@pytest.mark.asyncio
async def test_scalar_string_round_trips_as_json_string(db_pool):
    """A bare string is a legitimate jsonb scalar (e.g. settings.value = "UTC")."""
    row = await db_pool.fetchrow("SELECT $1::jsonb AS v", "UTC")
    assert row["v"] == "UTC"


@pytest.mark.asyncio
async def test_pre_dumped_dict_string_raises(db_pool):
    """A pre-json.dumps'd dict/list must fail loudly, not double-encode."""
    pre_dumped = json.dumps({"a": 1})
    with pytest.raises(asyncpg.exceptions.DataError) as exc_info:
        await db_pool.fetchrow("SELECT $1::jsonb AS v", pre_dumped)
    assert isinstance(exc_info.value.__cause__, TypeError)


@pytest.mark.asyncio
async def test_pre_dumped_list_string_raises(db_pool):
    pre_dumped = json.dumps([1, 2, 3])
    with pytest.raises(asyncpg.exceptions.DataError) as exc_info:
        await db_pool.fetchrow("SELECT $1::jsonb AS v", pre_dumped)
    assert isinstance(exc_info.value.__cause__, TypeError)


@pytest.mark.asyncio
async def test_bytes_raises(db_pool):
    with pytest.raises(asyncpg.exceptions.DataError) as exc_info:
        await db_pool.fetchrow("SELECT $1::jsonb AS v", b'{"a": 1}')
    assert isinstance(exc_info.value.__cause__, TypeError)
