"""Content routing config — compile/match/validate + DB round-trip."""

from __future__ import annotations

import re

import pytest
import pytest_asyncio
from aegis.services.content_routes import (
    active_patterns,
    compile_pattern,
    get_content_routes,
    match_route,
    save_content_routes,
    validate_routes,
)


def test_compile_prefix_escapes_metachars():
    # "APP-" prefix → anchored. `-` is not an ERE metacharacter outside a bracket
    # expression, so it stays literal (only true metachars get a backslash).
    pat = compile_pattern("prefix", "APP-")
    assert pat == "^APP-"
    assert re.search(pat, "APP-11175: broke")
    assert not re.search(pat, "xAPP-1")  # anchored
    # A metachar in the literal IS escaped so it matches literally in both engines.
    assert compile_pattern("prefix", "a.b(") == r"^a\.b\("


def test_compile_contains_and_regex():
    assert re.search(compile_pattern("contains", "[bug]"), "urgent [bug] here")
    assert re.search(compile_pattern("regex", r"^BUG-\d+"), "BUG-42: down")
    assert not re.search(compile_pattern("regex", r"^BUG-\d+"), "nope")


def test_compile_unknown_mode_raises():
    with pytest.raises(ValueError):
        compile_pattern("glob", "x")


def test_match_route_first_wins():
    routes = validate_routes(
        [
            {"key": "app", "match": "prefix", "value": "APP-", "assignee": "@pandora"},
            {"key": "bug", "match": "regex", "value": r"^\w+-\d+", "assignee": "@raphael"},
        ]
    )
    assert match_route("APP-1: x", routes)["key"] == "app"  # both match; first wins
    assert match_route("BUG-9: y", routes)["key"] == "bug"
    assert match_route("just a note", routes) is None
    assert match_route("", routes) is None


def test_validate_defaults_and_rejections():
    r = validate_routes([{"key": "k", "match": "prefix", "value": "X-"}])[0]
    assert r["assignee"] == "@pandora" and r["gate"] is True and r["contexts"] == []
    assert r["area_label"] is None and r["service"] is None and r["resource_tags"] == []

    with pytest.raises(ValueError):  # bad match mode
        validate_routes([{"key": "k", "match": "nope", "value": "x"}])
    with pytest.raises(ValueError):  # missing key
        validate_routes([{"match": "prefix", "value": "x"}])
    with pytest.raises(ValueError):  # duplicate key
        validate_routes(
            [
                {"key": "k", "match": "prefix", "value": "a"},
                {"key": "k", "match": "prefix", "value": "b"},
            ]
        )
    with pytest.raises(ValueError):  # invalid regex
        validate_routes([{"key": "k", "match": "regex", "value": "("}])


def test_active_patterns_drops_nothing_for_valid():
    routes = validate_routes(
        [
            {"key": "app", "match": "prefix", "value": "APP-"},
            {"key": "bug", "match": "regex", "value": r"^BUG-\d+"},
        ]
    )
    assert active_patterns(routes) == ["^APP-", r"^BUG-\d+"]


@pytest_asyncio.fixture(loop_scope="function")
async def clean_routes(db_pool):
    await db_pool.execute("DELETE FROM settings WHERE key = 'content_routes'")
    yield db_pool
    await db_pool.execute("DELETE FROM settings WHERE key = 'content_routes'")


async def test_get_empty_when_unset(clean_routes):
    assert await get_content_routes(clean_routes) == []


async def test_save_and_get_roundtrip(clean_routes):
    saved = await save_content_routes(
        clean_routes,
        [
            {
                "key": "jira-app",
                "match": "prefix",
                "value": "APP-",
                "assignee": "@pandora",
                "contexts": ["@code", "@deep"],
                "area_label": "@area/acme",
                "gate": True,
                "service": "acme",
                "resource_tags": ["acme"],
            }
        ],
    )
    assert saved[0]["service"] == "acme"
    got = await get_content_routes(clean_routes)
    assert got == saved
    assert match_route("APP-999: x", got)["key"] == "jira-app"


async def test_save_rejects_bad_regex(clean_routes):
    with pytest.raises(ValueError):
        await save_content_routes(clean_routes, [{"key": "k", "match": "regex", "value": "["}])
