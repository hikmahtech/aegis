"""Behavior-tab fields added for #556 — `knowledge_domains` and `slack_icon`.

Covers the PATCH validation, the options vocabulary the tab reads, and the
seed: the example agents' icons are yaml metadata now, and `seed.py` merges a
NEW metadata key into an agent that already exists — which is how production's
existing agents get their `slack_icon` on the first boot after deploy.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
import yaml
from aegis.api.app import create_app
from aegis.api.deps import get_settings
from aegis.config import Settings
from aegis.seed import _load_agents
from httpx import ASGITransport, AsyncClient

_SETTINGS = {
    "database_url": "postgresql://test:test@localhost:5432/test",
    "litellm_url": "https://litellm.example.com/v1",
    "temporal_ui_url": "https://temporal.example.com",
    "admin_username": "admin",
    "admin_password": "admin",
}
AUTH = ("admin", "admin")
AGENT = "zztest-behavior"
SEED_AGENTS = Path(__file__).resolve().parents[2] / "config" / "seed" / "agents.yaml"

# What comms' old id-keyed table gave the four example agents.
_ICONS_BEFORE_556 = {
    "sebas": ":bust_in_silhouette:",
    "raphael": ":books:",
    "maou": ":moneybag:",
    "pandoras-actor": ":robot_face:",
}


@pytest_asyncio.fixture(loop_scope="function")
async def client(db_pool):
    await db_pool.execute("DELETE FROM agents WHERE id = $1", AGENT)
    await db_pool.execute(
        "INSERT INTO agents (id, name, role, system_prompt_path, active, metadata) "
        "VALUES ($1, 'Z', 'tester', '', TRUE, '{}'::jsonb)",
        AGENT,
    )
    app = create_app(run_lifespan=False)
    app.state.db_pool = db_pool
    app.state.llm = AsyncMock()
    app.dependency_overrides[get_settings] = lambda: Settings(**_SETTINGS)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    await db_pool.execute("DELETE FROM agents WHERE id = $1", AGENT)


async def test_options_list_the_source_types_knowledge_domains_can_name(client):
    r = await client.get("/api/agents/meta/options", auth=AUTH)
    assert r.status_code == 200
    assert {"chat", "research", "task_outcome"} <= set(r.json()["source_types"])


@pytest.mark.parametrize(
    "metadata",
    [
        {"slack_icon": "books"},
        {"slack_icon": ":two words:"},
        {"slack_icon": 7},
        {"knowledge_domains": "article"},
        {"knowledge_domains": ["article", ""]},
        {"knowledge_domains": [3]},
    ],
)
async def test_patch_400s_on_a_malformed_field(client, db_pool, metadata):
    r = await client.patch(f"/api/agents/{AGENT}", auth=AUTH, json={"metadata": metadata})
    assert r.status_code == 400, r.text
    assert "metadata." in r.json()["detail"]
    assert await db_pool.fetchval("SELECT metadata FROM agents WHERE id = $1", AGENT) == {}


@pytest.mark.parametrize(
    "metadata",
    [
        {"slack_icon": ":books:"},
        {"slack_icon": ":+1:"},
        {"slack_icon": ""},
        {"knowledge_domains": ["article", "feed"]},
        {"knowledge_domains": []},
    ],
)
async def test_patch_saves_a_good_field(client, db_pool, metadata):
    r = await client.patch(f"/api/agents/{AGENT}", auth=AUTH, json={"metadata": metadata})
    assert r.status_code == 200, r.text
    assert await db_pool.fetchval("SELECT metadata FROM agents WHERE id = $1", AGENT) == metadata


def test_the_seed_gives_the_example_agents_the_icons_they_had():
    """Prod looks the same after deploy: each example agent's yaml icon is the
    one the old id-keyed table gave it."""
    rows = {a["id"]: a for a in yaml.safe_load(SEED_AGENTS.read_text())["agents"]}
    assert {aid: rows[aid]["metadata"]["slack_icon"] for aid in _ICONS_BEFORE_556} == (
        _ICONS_BEFORE_556
    )


async def test_the_booted_agents_carry_their_icons(db_pool):
    """The test DB is built by `load_seeds`, as a boot does."""
    rows = await db_pool.fetch(
        "SELECT id, metadata->>'slack_icon' AS icon FROM agents WHERE id = ANY($1)",
        list(_ICONS_BEFORE_556),
    )
    assert {r["id"]: r["icon"] for r in rows} == _ICONS_BEFORE_556


async def test_seed_merges_a_new_key_into_an_existing_agent_and_keeps_edits(db_pool, tmp_path):
    """An agent that predates `slack_icon` (production's four) gains it on the
    next boot; a key the operator already set is never overwritten."""
    seed_id = "zztest-seed-icon"
    path = tmp_path / "agents.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "agents": [
                    {
                        "id": seed_id, "name": "S", "role": "r", "system_prompt_path": "",
                        "metadata": {"slack_icon": ":books:", "intent_keywords": ["seeded"]},
                    }
                ]
            }
        )
    )
    await db_pool.execute("DELETE FROM agents WHERE id = $1", seed_id)
    await db_pool.execute(
        "INSERT INTO agents (id, name, role, system_prompt_path, active, metadata) "
        "VALUES ($1, 'S', 'r', '', TRUE, '{\"intent_keywords\": [\"mine\"]}'::jsonb)",
        seed_id,
    )
    try:
        await _load_agents(db_pool, path)
        md = await db_pool.fetchval("SELECT metadata FROM agents WHERE id = $1", seed_id)
        assert md == {"slack_icon": ":books:", "intent_keywords": ["mine"]}

        await db_pool.execute(
            "UPDATE agents SET metadata = metadata || '{\"slack_icon\": \":fire:\"}'::jsonb "
            "WHERE id = $1",
            seed_id,
        )
        await _load_agents(db_pool, path)
        md = await db_pool.fetchval("SELECT metadata FROM agents WHERE id = $1", seed_id)
        assert md["slack_icon"] == ":fire:"
    finally:
        await db_pool.execute("DELETE FROM agents WHERE id = $1", seed_id)
