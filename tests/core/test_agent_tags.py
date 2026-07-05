"""Behavior-tag foundation (issue #36 PR 1): resolver, options endpoint,
PATCH validation, and the seed ownership flip for capabilities/metadata.

DB tests run against the shared dev Postgres — every row they create is
prefixed ``tagtest-`` and deleted in ``finally``; seed tests load a
synthetic yaml from tmp_path so the real seed rows are never mutated.
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import yaml
from aegis.agent_tags import BEHAVIOR_TAGS
from aegis.api.app import create_app
from aegis.api.deps import get_settings
from aegis.config import Settings
from aegis.db import run_migrations
from aegis.seed import _load_agents
from aegis.services.agents import agents_by_tag, resolve_tag, warn_unknown_tool_refs
from fastapi.testclient import TestClient

SEED_DIR = Path(__file__).parent.parent.parent / "config" / "seed"

_TAG = "tagtest-ztag"  # synthetic tag: never collides with shared-DB rows


async def _insert_agent(pool, agent_id, capabilities, metadata=None, active=True):
    await pool.execute(
        """
        INSERT INTO agents (id, name, role, system_prompt_path, capabilities,
                            model_tier, metadata, active)
        VALUES ($1, $1, 'test', '', $2, 'balanced', $3, $4)
        """,
        agent_id,
        capabilities,
        metadata or {},
        active,
    )


async def _delete_agents(pool):
    await pool.execute("DELETE FROM agents WHERE id LIKE 'tagtest-%'")


# --- resolver -----------------------------------------------------------


async def test_agents_by_tag_and_resolve(db_pool, caplog):
    await run_migrations(db_pool)
    try:
        await _insert_agent(db_pool, "tagtest-a", [_TAG, "other"])
        await _insert_agent(db_pool, "tagtest-b", [_TAG])
        await _insert_agent(db_pool, "tagtest-inactive", [_TAG], active=False)

        agents = await agents_by_tag(db_pool, _TAG)
        assert [a["id"] for a in agents] == ["tagtest-a", "tagtest-b"]

        with caplog.at_level(logging.WARNING, logger="aegis.services.agents"):
            assert await resolve_tag(db_pool, _TAG) == "tagtest-a"
            assert await resolve_tag(db_pool, "tagtest-unheld") is None
        messages = " ".join(r.message for r in caplog.records)
        assert "tagtest-unheld" in messages  # zero holders warned
        assert _TAG in messages  # multiple holders warned
    finally:
        await _delete_agents(db_pool)


async def test_warn_unknown_tool_refs(db_pool, caplog):
    await run_migrations(db_pool)
    try:
        await _insert_agent(
            db_pool, "tagtest-typo", [], metadata={"tool_set": ["definitely_not_a_tool"]}
        )
        with caplog.at_level(logging.WARNING, logger="aegis.services.agents"):
            await warn_unknown_tool_refs(db_pool)  # must not raise
        assert "definitely_not_a_tool" in " ".join(r.message for r in caplog.records)
    finally:
        await _delete_agents(db_pool)


# --- seed ownership flip --------------------------------------------------


def _seed_yaml(tmp_path, metadata):
    path = tmp_path / "agents.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "agents": [
                    {
                        "id": "tagtest-seed",
                        "name": "Tagtest",
                        "role": "test",
                        "system_prompt_path": "",
                        "capabilities": ["gtd"],
                        "metadata": metadata,
                        "active": True,
                    }
                ]
            }
        )
    )
    return path


async def test_seed_does_not_clobber_db_edits(db_pool, tmp_path):
    """Re-running seeds must keep UI edits (DB wins) while still delivering
    metadata keys newly added to the yaml."""
    await run_migrations(db_pool)
    path = _seed_yaml(tmp_path, {"tool_set": ["search_knowledge"], "intent_description": "seeded"})
    try:
        await _load_agents(db_pool, path)

        # Simulate admin-UI edits: new capabilities, edited tool_set, extra key,
        # and drop intent_description (as if it never existed in an old deploy).
        await db_pool.execute(
            """
            UPDATE agents
            SET capabilities = '["custom-tag"]'::jsonb,
                metadata = (metadata - 'intent_description')
                           || '{"tool_set": ["remember_this"], "ui_key": true}'::jsonb
            WHERE id = 'tagtest-seed'
            """
        )
        await _load_agents(db_pool, path)

        row = await db_pool.fetchrow("SELECT * FROM agents WHERE id = 'tagtest-seed'")
        assert row["capabilities"] == ["custom-tag"]  # DB-owned once non-empty
        assert row["metadata"]["tool_set"] == ["remember_this"]  # DB key wins
        assert row["metadata"]["ui_key"] is True  # UI-added key survives
        assert row["metadata"]["intent_description"] == "seeded"  # new yaml key arrives
    finally:
        await _delete_agents(db_pool)


async def test_seed_fills_empty_capabilities(db_pool, tmp_path):
    await run_migrations(db_pool)
    path = _seed_yaml(tmp_path, {})
    try:
        await _insert_agent(db_pool, "tagtest-seed", [])
        await _load_agents(db_pool, path)
        row = await db_pool.fetchrow("SELECT capabilities FROM agents WHERE id = 'tagtest-seed'")
        assert row["capabilities"] == ["gtd"]
    finally:
        await _delete_agents(db_pool)


def test_shipped_seeds_carry_behavior_tags():
    """The four example personalities each hold their canonical behavior tag
    (pure yaml check — independent of shared-DB state)."""
    seeds = yaml.safe_load((SEED_DIR / "agents.yaml").read_text())["agents"]
    by_id = {a["id"]: a for a in seeds}
    assert "gtd" in by_id["sebas"]["capabilities"]
    assert "research" in by_id["raphael"]["capabilities"]
    assert "finance" in by_id["maou"]["capabilities"]
    assert "infra" in by_id["pandoras-actor"]["capabilities"]
    pandora_meta = by_id["pandoras-actor"]["metadata"]
    assert pandora_meta["mention_aliases"] == ["pandora"]
    assert pandora_meta["async_dispatch"] is True
    for agent_id in ("sebas", "raphael", "maou", "pandoras-actor"):
        assert by_id[agent_id]["metadata"]["intent_description"]


# --- routes ---------------------------------------------------------------


@pytest.fixture
def settings():
    return Settings(
        database_url="postgresql://test:test@localhost/test",
        litellm_url="https://litellm.test/v1",
        temporal_ui_url="https://temporal.test",
        n8n_ui_url="https://n8n.test",
        admin_username="admin",
        admin_password="admin",
        n8n_webhook_secret="test-secret",
        api_key="test-key",
    )


@pytest.fixture
def client(settings):
    app = create_app(run_lifespan=False)
    app.dependency_overrides[get_settings] = lambda: settings
    app.state.db_pool = AsyncMock()
    app.state.settings = settings
    return TestClient(app, headers={"X-API-Key": "test-key"})


def test_options_endpoint(client):
    resp = client.get("/api/agents/meta/options")
    assert resp.status_code == 200
    body = resp.json()
    assert {t["id"] for t in body["tags"]} == set(BEHAVIOR_TAGS)
    names = [t["name"] for t in body["tools"]]
    assert names == sorted(names) and "search_knowledge" in names
    assert body["model_tiers"] == ["fast", "balanced", "smart"]


def test_patch_rejects_unknown_tool(client):
    resp = client.patch("/api/agents/sebas", json={"metadata": {"tool_set": ["no_such_tool"]}})
    assert resp.status_code == 400
    assert "no_such_tool" in resp.json()["detail"]


def test_patch_rejects_bad_types(client):
    assert client.patch("/api/agents/sebas", json={"capabilities": "gtd"}).status_code == 400
    assert (
        client.patch("/api/agents/sebas", json={"metadata": {"async_dispatch": "yes"}}).status_code
        == 400
    )
    assert (
        client.patch(
            "/api/agents/sebas", json={"metadata": {"tool_set": "search_knowledge"}}
        ).status_code
        == 400
    )
