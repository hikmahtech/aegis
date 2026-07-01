"""GTD clarify rules config — merge over defaults, DB round-trip."""

from __future__ import annotations

import pytest_asyncio
from aegis.services.gtd_rules import get_gtd_rules, merge, save_gtd_rules


def test_merge_defaults():
    r = merge(None)
    assert r["assignee"]["#email"] == "@sebas"
    assert r["contexts"]["#email"] == ["@email", "@5min"]
    assert r["skip_inbox"]["#research"] == "reference"


def test_merge_override_keeps_other_defaults():
    r = merge({"assignee": {"#email": "@maou"}, "skip_inbox": {"#chat": "reference"}})
    assert r["assignee"]["#email"] == "@maou"  # overridden
    assert r["assignee"]["#alert"] == "@pandora"  # default kept
    assert r["skip_inbox"]["#chat"] == "reference"
    assert r["skip_inbox"]["#research"] == "reference"  # default kept


@pytest_asyncio.fixture(loop_scope="function")
async def clean_gtd(db_pool):
    await db_pool.execute("DELETE FROM settings WHERE key = 'gtd_rules'")
    yield db_pool
    await db_pool.execute("DELETE FROM settings WHERE key = 'gtd_rules'")


async def test_get_defaults_when_unset(clean_gtd):
    r = await get_gtd_rules(clean_gtd)
    assert r["assignee"]["#email"] == "@sebas"


async def test_save_and_get_merges(clean_gtd):
    r = await save_gtd_rules(
        clean_gtd, {"assignee": {"#email": "@raphael"}, "contexts": {"#chat": ["@5min"]}}
    )
    assert r["assignee"]["#email"] == "@raphael" and r["contexts"]["#chat"] == ["@5min"]
    r2 = await get_gtd_rules(clean_gtd)
    assert r2["assignee"]["#email"] == "@raphael"  # persisted
    assert r2["assignee"]["#alert"] == "@pandora"  # default kept
