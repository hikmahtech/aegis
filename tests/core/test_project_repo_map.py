"""project_repo_map — the DB-configured tier-1 repo resolver (issue #345).

Read is lenient, write is strict, mirroring email_rules and content_routes: a
malformed row must never stop a task resolving (the resolver falls through to
its later tiers), but a typo must not save silently and then do nothing forever.
"""

from __future__ import annotations

import pytest
from aegis.services.project_repo_map import (
    SETTINGS_KEY,
    get_project_repo_map,
    lookup,
    save_project_repo_map,
    validate_map,
)

# ── validation (write path: strict) ───────────────────────────────────────


def test_empty_is_valid_and_is_the_shipped_default():
    """A fork must inherit nobody's Todoist layout."""
    assert validate_map(None) == {}
    assert validate_map({}) == {}


def test_names_are_normalised_for_case_insensitive_matching():
    out = validate_map({"  Home Infra  ": "acme/infra"})
    assert out == {"home infra": "acme/infra"}


def test_repo_must_be_owner_slash_name():
    for bad in ["justname", "too/many/parts", "owner/", "/name", "own er/name"]:
        with pytest.raises(ValueError, match="owner/name"):
            validate_map({"p": bad})


def test_rejects_non_object():
    with pytest.raises(ValueError, match="must be an object"):
        validate_map([("a", "b")])


def test_rejects_empty_name_or_repo():
    with pytest.raises(ValueError, match="must not be empty"):
        validate_map({"   ": "acme/app"})
    with pytest.raises(ValueError, match="repo required"):
        validate_map({"p": ""})


def test_rejects_names_that_collide_after_normalising():
    with pytest.raises(ValueError, match="duplicate"):
        validate_map({"App": "acme/app", "app": "acme/other"})


# ── lookup ────────────────────────────────────────────────────────────────


def test_lookup_is_case_and_space_insensitive():
    mapping = validate_map({"Home Infra": "acme/infra"})
    for probe in ["Home Infra", "home infra", "  HOME INFRA  "]:
        assert lookup(probe, mapping) == "acme/infra"


def test_lookup_misses_are_empty_not_errors():
    assert lookup("unknown", {"a": "x/y"}) == ""
    assert lookup(None, {"a": "x/y"}) == ""
    assert lookup("a", {}) == ""


# ── persistence (read path: lenient) ──────────────────────────────────────


async def test_round_trip(db_pool):
    await db_pool.execute("DELETE FROM settings WHERE key = $1", SETTINGS_KEY)
    saved = await save_project_repo_map(db_pool, {"BCP": "Acme/bcp"})
    assert saved == {"bcp": "Acme/bcp"}
    assert await get_project_repo_map(db_pool) == {"bcp": "Acme/bcp"}
    await db_pool.execute("DELETE FROM settings WHERE key = $1", SETTINGS_KEY)


async def test_unset_reads_as_empty(db_pool):
    await db_pool.execute("DELETE FROM settings WHERE key = $1", SETTINGS_KEY)
    assert await get_project_repo_map(db_pool) == {}


async def test_a_malformed_stored_row_reads_as_empty_rather_than_raising(db_pool):
    """The resolver must degrade to its later tiers, never break the coding lane."""
    await db_pool.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES ($1, $2, NOW()) "
        "ON CONFLICT (key) DO UPDATE SET value = $2",
        SETTINGS_KEY,
        {"broken": "not-a-repo"},
    )
    assert await get_project_repo_map(db_pool) == {}
    await db_pool.execute("DELETE FROM settings WHERE key = $1", SETTINGS_KEY)


async def test_saving_a_malformed_entry_raises_so_the_put_can_400(db_pool):
    with pytest.raises(ValueError):
        await save_project_repo_map(db_pool, {"p": "not-a-repo"})
