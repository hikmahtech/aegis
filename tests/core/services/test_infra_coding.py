"""Infra `coding` block — CRUD, validation, single-enabled enforcement.

Real-Postgres tests (db_pool fixture) mirroring test_infra_credentials.py:
the jsonb round-trip and the at-most-one-enabled invariant are what matter.
"""

from __future__ import annotations

import pytest
from aegis.db import run_migrations
from aegis.services import infra as infra_service

SECRET_KEY = "test-secret-key"

CODING = {
    "enabled": True,
    "repo_base": "/home/deploy/Workspace",
    "engines": {
        "claude": {
            "binary_path": "/usr/local/bin/claude",
            "config_dirs": {"work": "/home/deploy/.claude-work", "personal": "/home/deploy/.claude-personal"},
            "default_account": "personal",
        },
        "kimi": {"binary_path": "/usr/local/bin/kimi"},
    },
    "routing": {
        "orgs": {"Acme": {"engine": "claude", "account": "work"}},
        "default_engine": "kimi",
    },
    "tmux": {"session": "remote", "window_cap": 8},
    "kimi_host_slug": "node-b",
    "self_repo_path": "personal/aegis",
    "runbooks_dir": "/app/runbooks",
}


async def _prepare(db_pool):
    await run_migrations(db_pool)
    await db_pool.execute("DELETE FROM infra WHERE slug LIKE 'test-coding-%'")
    # Neutralize any coding-enabled leftovers from other test modules — the
    # single-enabled invariant is DB-global.
    await db_pool.execute(
        "UPDATE infra SET coding = '{}'::jsonb WHERE coding->>'enabled' = 'true'"
    )


async def _create(db_pool, **overrides):
    data = {
        "name": "test-coding-host",
        "slug": "test-coding-host",
        "kind": "ssh_host",
        "host": "10.20.0.5",
        "ssh_user": "deploy",
        "ssh_private_key": "fake-key-material",
        **overrides,
    }
    return await infra_service.create_infra(db_pool, data, SECRET_KEY)


async def test_create_with_coding_round_trips_and_normalizes(db_pool):
    await _prepare(db_pool)
    row = await _create(db_pool, coding=CODING)

    # coding is non-secret — returned as-is through the public read...
    got = await infra_service.get_infra(db_pool, row["id"])
    assert got["coding"]["enabled"] is True
    assert got["coding"]["repo_base"] == "/home/deploy/Workspace"
    assert got["coding"]["tmux"] == {"session": "remote", "window_cap": 8}
    # ...with org keys normalized to lowercase for the routing lookup.
    assert got["coding"]["routing"]["orgs"] == {"acme": {"engine": "claude", "account": "work"}}
    # secrets still never leak
    assert "credentials" not in got


async def test_update_coding_alone_persists(db_pool):
    await _prepare(db_pool)
    row = await _create(db_pool)
    assert (await infra_service.get_infra(db_pool, row["id"]))["coding"] == {}

    updated = await infra_service.update_infra(db_pool, row["id"], {"coding": CODING}, SECRET_KEY)
    assert updated["coding"]["enabled"] is True

    # Disabling via an updated block persists too.
    disabled = dict(CODING, enabled=False)
    updated = await infra_service.update_infra(db_pool, row["id"], {"coding": disabled}, SECRET_KEY)
    assert updated["coding"]["enabled"] is False


async def test_single_enabled_enforced_on_create_and_update(db_pool):
    await _prepare(db_pool)
    first = await _create(db_pool, coding=CODING)

    # Second enabled row is refused.
    with pytest.raises(ValueError, match="already the coding host"):
        await _create(db_pool, name="test-coding-two", slug="test-coding-two", coding=CODING)

    # A disabled block on another row is fine...
    second = await _create(
        db_pool,
        name="test-coding-two",
        slug="test-coding-two",
        coding=dict(CODING, enabled=False),
    )
    # ...but flipping it to enabled while the first is still enabled is refused.
    with pytest.raises(ValueError, match="already the coding host"):
        await infra_service.update_infra(db_pool, second["id"], {"coding": CODING}, SECRET_KEY)

    # Re-saving the SAME enabled row must not trip the check (exclude_id).
    updated = await infra_service.update_infra(
        db_pool, first["id"], {"coding": dict(CODING, repo_base="/srv/ws")}, SECRET_KEY
    )
    assert updated["coding"]["repo_base"] == "/srv/ws"

    # Disable the first → enabling the second now succeeds.
    await infra_service.update_infra(
        db_pool, first["id"], {"coding": dict(CODING, enabled=False)}, SECRET_KEY
    )
    updated = await infra_service.update_infra(db_pool, second["id"], {"coding": CODING}, SECRET_KEY)
    assert updated["coding"]["enabled"] is True


async def test_get_coding_host_returns_enabled_row_with_credentials(db_pool):
    await _prepare(db_pool)
    assert await infra_service.get_coding_host(db_pool) is None
    row = await _create(db_pool, coding=CODING)

    got = await infra_service.get_coding_host(db_pool)
    assert got["id"] == row["id"]
    # Server-side lookup includes credentials (the connector decrypts the key).
    assert got["credentials"]["ssh_private_key_enc"]["value"]

    await infra_service.update_infra(
        db_pool, row["id"], {"coding": dict(CODING, enabled=False)}, SECRET_KEY
    )
    assert await infra_service.get_coding_host(db_pool) is None


async def test_get_infra_by_slug(db_pool):
    await _prepare(db_pool)
    row = await _create(db_pool)
    got = await infra_service.get_infra_by_slug(db_pool, row["slug"])
    assert got["id"] == row["id"]
    assert "credentials" not in got
    assert await infra_service.get_infra_by_slug(db_pool, "test-coding-ghost") is None


async def test_coding_validation_rejects_bad_shapes(db_pool):
    await _prepare(db_pool)
    cases = [
        ({"enabled": True, "engines": {"gpt": {}}}, "unknown engine"),
        ({"enabled": True, "routing": {"default_engine": "gpt"}}, "unknown engine"),
        ({"enabled": True, "tmux": {"window_cap": "lots"}}, "window_cap"),
        ({"enabled": True, "tmux": {"window_cap": 0}}, "window_cap"),
        ({"enabled": True, "repo_base": 42}, "repo_base"),
        (
            {
                "enabled": True,
                "engines": {"claude": {"binary_path": "/b", "default_account": "ghost"}},
            },
            "default_account",
        ),
        (
            {
                "enabled": True,
                "routing": {"orgs": {"acme": {"engine": "claude", "account": "ghost"}}},
            },
            "config_dirs",
        ),
    ]
    for coding, match in cases:
        with pytest.raises(ValueError, match=match):
            await _create(db_pool, name="test-coding-bad", slug="test-coding-bad", coding=coding)

    # enabled coding on a k8s entry makes no sense (no SSH identity).
    with pytest.raises(ValueError, match="k8s"):
        await _create(
            db_pool,
            name="test-coding-k8s",
            slug="test-coding-k8s",
            kind="k8s",
            coding={"enabled": True},
        )
