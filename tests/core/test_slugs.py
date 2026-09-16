"""`slugify`/`unique_slug`, and that each caller's own rule survived them.

The four copies differed in exactly two ways — what an unsluggable name falls
back to, and whether underscores survive — so those are what this pins.
"""

from __future__ import annotations

import pytest
from aegis.services.assets import slugify as asset_slug
from aegis.services.infra import _slugify as infra_slug
from aegis.services.social_channels import slugify_label
from aegis.services.tools.agents import _slugify_issue
from aegis.slugs import slugify, unique_slug


def test_the_shared_rule():
    assert slugify("  Bosch Washing Machine  ") == "bosch-washing-machine"
    assert slugify("Volvo XC90 (2019)") == "volvo-xc90-2019"
    assert slugify("a___b") == "a-b"
    assert slugify("") == "" and slugify("!!!") == ""


def test_each_caller_keeps_its_own_fallback():
    assert asset_slug("!!!") == "asset"
    assert infra_slug("!!!") == "infra"
    assert _slugify_issue("!!!") == "issue"
    # A Postiz label has no fallback: an unnamed channel falls back to its id.
    assert slugify_label("!!!") == ""


def test_a_postiz_label_keeps_its_underscores():
    """A label is an id, not a URL segment — the one call site that differs."""
    assert slugify_label("My_Page (LinkedIn)") == "my_page-linkedin"
    assert asset_slug("My_Page") == "my-page"


def test_an_issue_slug_is_capped_and_never_ends_in_a_dash():
    assert _slugify_issue("a" * 40) == "a" * 32
    # The cap must not leave the dash it cut on.
    assert _slugify_issue("aaaa bbbb", max_len=5) == "aaaa"
    assert _slugify_issue("-", max_len=5) == "issue"


@pytest.mark.asyncio
async def test_unique_slug_suffixes_until_the_table_is_free():
    taken = {"fridge", "fridge-2"}

    class Pool:
        def __init__(self):
            self.tables = []

        async def fetchval(self, sql, slug):
            self.tables.append(sql)
            return 1 if slug in taken else None

    pool = Pool()
    assert await unique_slug(pool, "fridge", table="life.assets") == "fridge-3"
    assert await unique_slug(pool, "boiler", table="life.assets") == "boiler"
    assert all("FROM life.assets" in sql for sql in pool.tables)
