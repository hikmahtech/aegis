"""Test for the v3 seed loader.

load_seeds(pool, seed_dir) reads every YAML in seed_dir and upserts rows
into the matching table. Upsert is idempotent — re-running must not
duplicate rows and must update changed fields.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from aegis.db import run_migrations
from aegis.seed import _load_agents, load_seeds

SEED_DIR = Path(__file__).parent.parent.parent / "config" / "seed"


@pytest.mark.asyncio
async def test_load_seeds_populates_agents(db_pool):
    await run_migrations(db_pool)
    await load_seeds(db_pool, SEED_DIR)
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT id FROM agents ORDER BY id")
    ids = {r["id"] for r in rows}
    # `system` is a virtual placeholder agent (active=false) that exists only
    # to satisfy the chat_history.agent_id FK for system-level dispatch rows.
    assert ids == {"sebas", "raphael", "maou", "pandoras-actor", "system"}


@pytest.mark.asyncio
async def test_load_seeds_populates_channels(db_pool):
    import yaml

    await run_migrations(db_pool)
    await load_seeds(db_pool, SEED_DIR)
    seed = yaml.safe_load((SEED_DIR / "channels.yaml").read_text())
    expected = {c["identifier"] for c in seed["channels"] if c["kind"] == "email"}
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT identifier FROM channels WHERE kind='email'")
    identifiers = {r["identifier"] for r in rows}
    # No cap on Gmail accounts — the seed may carry any number; assert they all load.
    assert identifiers == expected


@pytest.mark.asyncio
async def test_load_seeds_is_idempotent(db_pool):
    await run_migrations(db_pool)
    await load_seeds(db_pool, SEED_DIR)
    await load_seeds(db_pool, SEED_DIR)  # re-run
    async with db_pool.acquire() as conn:
        agent_count = await conn.fetchval("SELECT count(*) FROM agents")
    assert agent_count == 5


@pytest.mark.asyncio
async def test_load_seeds_populates_resources_and_activities(db_pool):
    await run_migrations(db_pool)
    await load_seeds(db_pool, SEED_DIR)
    async with db_pool.acquire() as conn:
        resource_count = await conn.fetchval("SELECT count(*) FROM resources")
        activity_count = await conn.fetchval("SELECT count(*) FROM activities")
    assert resource_count >= 1
    assert activity_count >= 0  # Phase 1 may seed zero; later phases add rows


@pytest.mark.asyncio
async def test_load_seeds_preserves_sync_managed_resource_kinds(db_pool):
    """Regression: the orphan-delete in _load_resources must NOT touch rows of
    kinds managed by sync flows (`repository` via WorkspaceRepoSyncFlow + the
    resolve_alert_resource auto-register path, `vercel_project` via
    VercelProjectSyncFlow). Only kinds the YAML actually owns
    (connector/runbook/endpoint/mcp_server) are eligible for orphan-delete.
    """
    await run_migrations(db_pool)
    await load_seeds(db_pool, SEED_DIR)
    async with db_pool.acquire() as conn:
        # Insert sync-managed rows (slugs that don't appear in resources.yaml)
        await conn.execute(
            "INSERT INTO resources (kind, slug, title) VALUES "
            "('repository','test-sync-managed-repo','test sync repo'),"
            "('vercel_project','test-sync-managed-vercel','test sync vercel')"
        )
        # Re-run the loader; the new rows must survive.
        await load_seeds(db_pool, SEED_DIR)
        repo_survives = await conn.fetchval(
            "SELECT 1 FROM resources WHERE slug='test-sync-managed-repo'"
        )
        vercel_survives = await conn.fetchval(
            "SELECT 1 FROM resources WHERE slug='test-sync-managed-vercel'"
        )
        await conn.execute(
            "DELETE FROM resources WHERE slug LIKE 'test-sync-managed-%'"
        )
    assert repo_survives == 1
    assert vercel_survives == 1


_PHASE3_ACTIVITY_SLUGS = [
    "gmail-ingest-hourly",
    "calendar-ingest-daily",
    "receipt-ingest-weekly",
    "raindrop-ingest-2h",
    "rss-ingest-hourly",
    "intel-scan-hn",
    "intel-scan-news",
    "intel-scan-finance",
    "sentry-poll-30m",
]


@pytest.mark.asyncio
async def test_phase3_activities_loaded(db_pool):
    """9 Phase 3 activity rows are upserted with correct workflow_type and agent_id."""
    await run_migrations(db_pool)
    await load_seeds(db_pool, SEED_DIR)

    slugs_sql = ", ".join(f"'{s}'" for s in _PHASE3_ACTIVITY_SLUGS)
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT slug, workflow_type, agent_id, schedule_cron FROM activities "
            f"WHERE slug IN ({slugs_sql}) ORDER BY slug"
        )

    assert len(rows) == 9, f"Expected 9 activity rows, got {len(rows)}"
    slugs_found = {r["slug"] for r in rows}
    assert "gmail-ingest-hourly" in slugs_found
    assert "intel-scan-hn" in slugs_found

    intel_rows = [r for r in rows if r["slug"].startswith("intel-scan-")]
    assert len(intel_rows) == 3
    assert all(r["workflow_type"] == "IntelligenceScanFlow" for r in intel_rows)

    by_slug = {r["slug"]: r for r in rows}
    assert by_slug["gmail-ingest-hourly"]["agent_id"] == "sebas"
    assert by_slug["receipt-ingest-weekly"]["agent_id"] == "maou"
    assert by_slug["raindrop-ingest-2h"]["agent_id"] == "raphael"
    assert by_slug["sentry-poll-30m"]["agent_id"] == "pandoras-actor"


@pytest.mark.asyncio
async def test_jsonb_columns_are_not_double_encoded(db_pool):
    """Seed-loaded JSONB columns must store objects/arrays, not scalar strings.

    Regression: passing `json.dumps(dict)` through the pool's jsonb codec
    double-encoded, producing jsonb_typeof='string' and breaking jsonb_set /
    `config->>'key'` readers.
    """
    await run_migrations(db_pool)
    await load_seeds(db_pool, SEED_DIR)
    async with db_pool.acquire() as conn:
        bad = await conn.fetch(
            """
            SELECT 'channels' AS tbl FROM channels WHERE jsonb_typeof(config) = 'string'
            UNION ALL
            SELECT 'agents' FROM agents WHERE jsonb_typeof(capabilities) = 'string'
            UNION ALL
            SELECT 'resources' FROM resources WHERE jsonb_typeof(metadata) = 'string'
            UNION ALL
            SELECT 'activities' FROM activities WHERE jsonb_typeof(config) = 'string'
            """
        )
    assert bad == [], f"Double-encoded JSONB rows found: {[r['tbl'] for r in bad]}"


@pytest.mark.asyncio
async def test_phase3_channels_loaded(db_pool):
    """Raindrop and RSS channel rows are upserted correctly."""
    await run_migrations(db_pool)
    await load_seeds(db_pool, SEED_DIR)

    async with db_pool.acquire() as conn:
        raindrop = await conn.fetchrow(
            "SELECT * FROM channels WHERE kind='raindrop' AND identifier='default'"
        )
        rss = await conn.fetch(
            "SELECT identifier FROM channels WHERE kind='rss' ORDER BY identifier"
        )

    assert raindrop is not None, "raindrop/default channel row missing"
    assert raindrop["active"] is True

    rss_urls = {r["identifier"] for r in rss}
    assert "https://hnrss.org/frontpage" in rss_urls
    assert "https://arxiv.org/rss/cs.AI" in rss_urls


# ---------------------------------------------------------------------------
# Task 6.3 — slack_channel_id seed preservation
# ---------------------------------------------------------------------------

_MINIMAL_AGENT_SEED = [
    {
        "id": "sebas",
        "name": "Sebas Tian",
        "role": "Executive assistant",
        "system_prompt_path": "personalities/sebas",
        "capabilities": ["email"],
        "model_tier": "smart",
        "interaction_timeout_default": "archive",
        "slack_channel_id": "",
        "active": True,
    }
]


@pytest.mark.asyncio
async def test_seed_preserves_provisioned_slack_channel_id(db_pool):
    """An empty slack_channel_id in the seed must NOT overwrite a pre-existing
    non-empty value already in the DB (simulating a provisioned channel id).
    """
    await run_migrations(db_pool)

    # Pre-seed with a provisioned channel id (as if provision script ran first).
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO agents (
                id, name, role, system_prompt_path, capabilities,
                model_tier, interaction_timeout_default,
                slack_channel_id, active
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
            ON CONFLICT (id) DO UPDATE SET slack_channel_id = EXCLUDED.slack_channel_id
            """,
            "sebas",
            "Sebas Tian",
            "Executive assistant",
            "personalities/sebas",
            ["email"],
            "smart",
            "archive",
            "C123",
            True,
        )

    # Now run the seed loader with an empty slack_channel_id — must not wipe C123.
    import tempfile
    from pathlib import Path

    import yaml as _yaml

    seed_content = _yaml.dump({"agents": _MINIMAL_AGENT_SEED})
    with tempfile.TemporaryDirectory() as tmp:
        seed_path = Path(tmp)
        (seed_path / "agents.yaml").write_text(seed_content)
        await _load_agents(db_pool, seed_path / "agents.yaml")

    async with db_pool.acquire() as conn:
        val = await conn.fetchval(
            "SELECT slack_channel_id FROM agents WHERE id = 'sebas'"
        )
    assert val == "C123", f"Expected 'C123', got {val!r}"


@pytest.mark.asyncio
async def test_seed_writes_nonempty_slack_channel_id(db_pool):
    """A non-empty slack_channel_id in the seed IS written to the DB."""
    await run_migrations(db_pool)

    # Ensure the agent exists first (no prior channel id).
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO agents (
                id, name, role, system_prompt_path, capabilities,
                model_tier, interaction_timeout_default,
                slack_channel_id, active
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
            ON CONFLICT (id) DO UPDATE SET slack_channel_id = NULL
            """,
            "raphael",
            "Raphael Ainz Ooal Gown",
            "Research and knowledge",
            "personalities/raphael",
            ["knowledge_search"],
            "smart",
            "hold",
            None,
            True,
        )

    import tempfile
    from pathlib import Path

    import yaml as _yaml

    seed_with_id = [
        {
            "id": "raphael",
            "name": "Raphael Ainz Ooal Gown",
            "role": "Research and knowledge",
            "system_prompt_path": "personalities/raphael",
            "capabilities": ["knowledge_search"],
            "model_tier": "smart",
            "interaction_timeout_default": "hold",
            "slack_channel_id": "C456",
            "active": True,
        }
    ]
    seed_content = _yaml.dump({"agents": seed_with_id})
    with tempfile.TemporaryDirectory() as tmp:
        seed_path = Path(tmp)
        (seed_path / "agents.yaml").write_text(seed_content)
        await _load_agents(db_pool, seed_path / "agents.yaml")

    async with db_pool.acquire() as conn:
        val = await conn.fetchval(
            "SELECT slack_channel_id FROM agents WHERE id = 'raphael'"
        )
    assert val == "C456", f"Expected 'C456', got {val!r}"


@pytest.mark.asyncio
async def test_seed_preserves_provisioned_elevenlabs_voice_id(db_pool):
    """An empty elevenlabs_voice_id in the seed must NOT overwrite a pre-existing
    non-empty value (the owner sets the voice id directly in the DB / volume).
    """
    await run_migrations(db_pool)

    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO agents (
                id, name, role, system_prompt_path, capabilities,
                model_tier, interaction_timeout_default,
                slack_channel_id, elevenlabs_voice_id, active
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
            ON CONFLICT (id) DO UPDATE SET elevenlabs_voice_id = EXCLUDED.elevenlabs_voice_id
            """,
            "sebas",
            "Sebas Tian",
            "Executive assistant",
            "personalities/sebas",
            ["email"],
            "smart",
            "archive",
            "C123",
            "VOICE_SEBAS",
            True,
        )

    import tempfile
    from pathlib import Path

    import yaml as _yaml

    # Minimal seed has NO elevenlabs_voice_id key → must not wipe VOICE_SEBAS.
    seed_content = _yaml.dump({"agents": _MINIMAL_AGENT_SEED})
    with tempfile.TemporaryDirectory() as tmp:
        seed_path = Path(tmp)
        (seed_path / "agents.yaml").write_text(seed_content)
        await _load_agents(db_pool, seed_path / "agents.yaml")

    async with db_pool.acquire() as conn:
        val = await conn.fetchval("SELECT elevenlabs_voice_id FROM agents WHERE id = 'sebas'")
    assert val == "VOICE_SEBAS", f"Expected 'VOICE_SEBAS', got {val!r}"
