"""One slug rule, and one way to make a slug unique in its table.

Four modules had grown their own copy of the same two lines (assets, infra,
social channels, the coding lane's branch names), and two had a byte-identical
`_unique_slug`. They differ only in what an empty slug falls back to, which
characters survive, and which table the uniqueness is against — so those are
the arguments.
"""

from __future__ import annotations

import re
from typing import Any


def slugify(text: str, *, fallback: str = "", keep: str = "") -> str:
    """`text` as a slug: lowercase, `a-z0-9` plus `keep`, every run of
    anything else collapsed to one dash, no dash at either end.

    `fallback` is what a text with nothing sluggable in it becomes; with no
    fallback that is the empty string.
    """
    pattern = f"[^a-z0-9{re.escape(keep)}]+" if keep else "[^a-z0-9]+"
    return re.sub(pattern, "-", (text or "").strip().lower()).strip("-") or fallback


async def unique_slug(pool: Any, base: str, *, table: str) -> str:
    """`base`, else `base-2`, `base-3`, … until `table.slug` is free.

    `table` is a literal named by the caller, never user input. The column is
    UNIQUE and the create routes do not catch `asyncpg.UniqueViolationError`,
    so two assets both called "Fridge" would otherwise be an HTTP 500 instead
    of `fridge` and `fridge-2`.
    """
    slug = base
    n = 2
    while await pool.fetchval(f"SELECT 1 FROM {table} WHERE slug = $1", slug):
        slug = f"{base}-{n}"
        n += 1
    return slug
