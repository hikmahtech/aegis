"""Test for the v3 seed loader.

load_seeds(pool, seed_dir) reads every YAML in seed_dir and upserts rows
into the matching table. Upsert is idempotent — re-running must not
duplicate rows and must update changed fields.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from aegis.db import run_migrations
from aegis.seed import _load_agents, load_seeds

REPO_ROOT = Path(__file__).parent.parent.parent
SEED_DIR = REPO_ROOT / "config" / "seed"


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
    # Superset (not equality): channels are DB-owned after first boot, so
    # operator-added rows may legitimately coexist with the seeded ones.
    assert expected <= identifiers


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
    kinds the YAML doesn't own. `repository` (via WorkspaceRepoSyncFlow + the
    resolve_alert_resource auto-register path) is the real example;
    `test_other_kind` stands in for "any kind outside yaml_managed_kinds" to
    prove this is an allow-list, not a block-list. Only kinds the YAML
    actually owns (connector/runbook/endpoint/mcp_server) are eligible for
    orphan-delete.
    """
    await run_migrations(db_pool)
    await load_seeds(db_pool, SEED_DIR)
    async with db_pool.acquire() as conn:
        # Insert sync-managed rows (slugs that don't appear in resources.yaml)
        await conn.execute(
            "INSERT INTO resources (kind, slug, title) VALUES "
            "('repository','test-sync-managed-repo','test sync repo'),"
            "('test_other_kind','test-sync-managed-other','test sync other')"
        )
        # Re-run the loader; the new rows must survive.
        await load_seeds(db_pool, SEED_DIR)
        repo_survives = await conn.fetchval(
            "SELECT 1 FROM resources WHERE slug='test-sync-managed-repo'"
        )
        other_survives = await conn.fetchval(
            "SELECT 1 FROM resources WHERE slug='test-sync-managed-other'"
        )
        await conn.execute(
            "DELETE FROM resources WHERE slug LIKE 'test-sync-managed-%'"
        )
    assert repo_survives == 1
    assert other_survives == 1


_PHASE3_ACTIVITY_SLUGS = [
    "gmail-ingest-hourly",
    "calendar-ingest-daily",
    "raindrop-ingest-2h",
    "rss-ingest-hourly",
    "intel-scan-hn",
    "intel-scan-news",
    "intel-scan-finance",
]


@pytest.mark.asyncio
async def test_phase3_activities_loaded(db_pool):
    """The 7 Phase 3 activity rows still seeded are upserted with correct
    workflow_type and agent_id (receipt-ingest-weekly and sentry-poll-30m left
    in the v1 removal prep)."""
    await run_migrations(db_pool)
    await load_seeds(db_pool, SEED_DIR)

    slugs_sql = ", ".join(f"'{s}'" for s in _PHASE3_ACTIVITY_SLUGS)
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT slug, workflow_type, agent_id, schedule_cron FROM activities "
            f"WHERE slug IN ({slugs_sql}) ORDER BY slug"
        )

    assert len(rows) == 7, f"Expected 7 activity rows, got {len(rows)}"
    slugs_found = {r["slug"] for r in rows}
    assert "gmail-ingest-hourly" in slugs_found
    assert "intel-scan-hn" in slugs_found

    intel_rows = [r for r in rows if r["slug"].startswith("intel-scan-")]
    assert len(intel_rows) == 3
    assert all(r["workflow_type"] == "IntelligenceScanFlow" for r in intel_rows)

    by_slug = {r["slug"]: r for r in rows}
    assert by_slug["gmail-ingest-hourly"]["agent_id"] == "sebas"
    assert by_slug["raindrop-ingest-2h"]["agent_id"] == "raphael"


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
    # No `active` assertion: the row is DB-owned after first insert — a UI
    # deactivation must survive re-seeds, so active may legitimately be False.

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


# ---------------------------------------------------------------------------
# Channels seed — first-boot starter examples only (insert-when-missing;
# never UPDATE a UI-edited row, never DELETE an operator-added row).
# ---------------------------------------------------------------------------


async def _run_channels_loader(db_pool, channels: list[dict]) -> None:
    """Run _load_channels against a temp yaml containing exactly `channels`."""
    import tempfile

    import yaml as _yaml
    from aegis.seed import _load_channels

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "channels.yaml"
        path.write_text(_yaml.dump({"channels": channels}))
        await _load_channels(db_pool, path)


@pytest.mark.asyncio
async def test_channels_seed_inserts_when_missing(db_pool):
    await run_migrations(db_pool)
    identifier = "https://seed-test.example/feed"
    await db_pool.execute("DELETE FROM channels WHERE identifier = $1", identifier)
    try:
        await _run_channels_loader(
            db_pool,
            [
                {
                    "kind": "rss",
                    "identifier": identifier,
                    "config": {"label": "seed-test", "agent_id": "raphael"},
                    "active": True,
                }
            ],
        )
        row = await db_pool.fetchrow(
            "SELECT config, active FROM channels WHERE kind = 'rss' AND identifier = $1",
            identifier,
        )
        assert row is not None, "seed loader must insert a missing channel"
        assert row["config"]["label"] == "seed-test"
        assert row["active"] is True
    finally:
        await db_pool.execute("DELETE FROM channels WHERE identifier = $1", identifier)


@pytest.mark.asyncio
async def test_channels_seed_does_not_clobber_existing_row(db_pool):
    """A UI-edited config and a UI-deactivated channel must survive a re-seed —
    the yaml must never overwrite an existing (kind, identifier) row."""
    await run_migrations(db_pool)
    identifier = "https://seed-clobber-test.example/feed"
    await db_pool.execute("DELETE FROM channels WHERE identifier = $1", identifier)
    try:
        # Operator state: edited config + deactivated from the admin UI.
        await db_pool.execute(
            "INSERT INTO channels (kind, identifier, config, active) VALUES ('rss', $1, $2, false)",
            identifier,
            {"label": "ui-edited", "agent_id": "sebas"},
        )
        # Re-seed with the same (kind, identifier) but different config/active.
        await _run_channels_loader(
            db_pool,
            [
                {
                    "kind": "rss",
                    "identifier": identifier,
                    "config": {"label": "yaml-clobber", "agent_id": "raphael"},
                    "active": True,
                }
            ],
        )
        row = await db_pool.fetchrow(
            "SELECT config, active FROM channels WHERE kind = 'rss' AND identifier = $1",
            identifier,
        )
        assert row["config"] == {"label": "ui-edited", "agent_id": "sebas"}
        assert row["active"] is False, "re-seed must not resurrect a deactivated channel"
    finally:
        await db_pool.execute("DELETE FROM channels WHERE identifier = $1", identifier)


@pytest.mark.asyncio
async def test_channels_seed_does_not_prune_operator_rows(db_pool):
    """Regression for the live incident: a channel added directly by the
    operator (not present in the yaml) must survive the next Core boot."""
    await run_migrations(db_pool)
    operator_identifier = "operator-added@example.com"
    await db_pool.execute("DELETE FROM channels WHERE identifier = $1", operator_identifier)
    try:
        await db_pool.execute(
            "INSERT INTO channels (kind, identifier, config, active) VALUES ('email', $1, $2, true)",
            operator_identifier,
            {"label": "operator", "token_path": "config/credentials/operator.json"},
        )
        # Seed yaml knows nothing about the operator's channel.
        await _run_channels_loader(
            db_pool,
            [{"kind": "raindrop", "identifier": "default", "config": {}, "active": True}],
        )
        survives = await db_pool.fetchval(
            "SELECT 1 FROM channels WHERE kind = 'email' AND identifier = $1",
            operator_identifier,
        )
        assert survives == 1, "seed loader must never delete rows missing from the yaml"
    finally:
        await db_pool.execute("DELETE FROM channels WHERE identifier = $1", operator_identifier)


def test_the_journal_rows_belong_to_the_gtd_holder():
    """The journal moved from the research agent to the GTD one (spec
    2026-09-22 §1). Read through the capability, not the id, so renaming the
    example agent in a fork does not need this test edited. The index row
    stays where it was: an index has no author."""
    import yaml

    activities = yaml.safe_load((SEED_DIR / "activities.yaml").read_text())["activities"]
    agents = yaml.safe_load((SEED_DIR / "agents.yaml").read_text())["agents"]
    holds = {
        tag: {a["id"] for a in agents if tag in (a.get("capabilities") or [])}
        for tag in ("gtd", "research")
    }
    by_slug = {r["slug"]: r for r in activities}

    for slug in (
        "daylog-nightly", "daylog-weekly", "daylog-monthly", "notes-backfill-weekly",
        "journal-prompt-daily",
    ):
        assert by_slug[slug]["agent_id"] in holds["gtd"], slug
    assert by_slug["notes-sync-hourly"]["agent_id"] in holds["research"]


# --- v1 removal prep (migration 054) ----------------------------------------

_REMOVED_SLUGS = {
    "infra-heartbeat-2m", "service-drift-4h", "cert-radar-daily",
    "profile-reflection-weekly-pandoras-actor", "memory-reflection-nightly-pandoras-actor",
    "sentry-poll-30m", "jira-sync-30m", "workspace-repo-sync-daily",
    "money-statements-reconcile", "receipt-ingest-weekly", "money-brief-weekly",
    "money-close-monthly",
}
_REASSIGNED_SLUGS = {
    "hub-sweep-5m", "llm-spend-guard-15min", "flow-health-watchdog-30m",
    "delivery-watchdog-hourly", "cleanup-daily", "agent-task-15min",
}


def _seed_rows(name: str, key: str) -> list[dict]:
    return yaml.safe_load((SEED_DIR / name).read_text())[key]


def test_the_seed_no_longer_carries_the_removed_schedules():
    slugs = {r["slug"] for r in _seed_rows("activities.yaml", "activities")}
    assert not (slugs & _REMOVED_SLUGS)
    assert all(
        r["agent_id"] != "pandoras-actor" for r in _seed_rows("activities.yaml", "activities")
    )


def test_the_shared_schedules_belong_to_sebas_and_no_coding_runs():
    rows = {r["slug"]: r for r in _seed_rows("activities.yaml", "activities")}
    for slug in _REASSIGNED_SLUGS:
        assert rows[slug]["agent_id"] == "sebas", slug
    assert rows["agent-task-15min"]["config"]["max_coding"] == 0


def test_the_infra_agent_ships_inactive():
    """`seed.py` writes `active` on every boot, so the YAML must agree with 054."""
    agents = {a["id"]: a for a in _seed_rows("agents.yaml", "agents")}
    assert agents["pandoras-actor"]["active"] is False


@pytest.mark.asyncio
async def test_migration_054_leaves_the_db_as_the_seed_says(db_pool):
    await run_migrations(db_pool)
    await load_seeds(db_pool, SEED_DIR)
    async with db_pool.acquire() as conn:
        gone = await conn.fetch(
            "SELECT slug FROM activities WHERE slug = ANY($1::text[])", list(_REMOVED_SLUGS)
        )
        owners = await conn.fetch(
            "SELECT slug, agent_id FROM activities WHERE slug = ANY($1::text[])",
            list(_REASSIGNED_SLUGS),
        )
        active = await conn.fetchval("SELECT active FROM agents WHERE id = 'pandoras-actor'")
    assert gone == []
    assert {r["slug"]: r["agent_id"] for r in owners} == dict.fromkeys(_REASSIGNED_SLUGS, "sebas")
    assert active is False


@pytest.mark.asyncio
async def test_migration_054_strips_removed_tools_and_is_idempotent(db_pool):
    """Run the 054 SQL over a DB tool_set that still lists removed tools."""
    sql = (REPO_ROOT / "migrations" / "054_v1_removal_prep.sql").read_text()
    await run_migrations(db_pool)
    await load_seeds(db_pool, SEED_DIR)
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO agents (id, name, role, system_prompt_path, metadata) "
            "VALUES ('zz-054', 'zz', 'test', 'personalities/zz', $1) "
            "ON CONFLICT (id) DO UPDATE SET metadata = EXCLUDED.metadata",
            {"tool_set": ["search_knowledge", "dispatch_agent_run", "ledger_query",
                          "capture_to_inbox", "list_nodes", "task_context"]},
        )
        await conn.execute(
            "UPDATE activities SET agent_id = 'pandoras-actor', config = '{}'::jsonb "
            "WHERE slug = 'agent-task-15min'"
        )
        await conn.execute(sql)
        await conn.execute(sql)  # a re-run is a no-op
        tools = await conn.fetchval("SELECT metadata->'tool_set' FROM agents WHERE id = 'zz-054'")
        await conn.execute("DELETE FROM agents WHERE id = 'zz-054'")
        row = await conn.fetchrow(
            "SELECT agent_id, config FROM activities WHERE slug = 'agent-task-15min'"
        )
    assert tools == ["search_knowledge", "capture_to_inbox"]
    assert row["agent_id"] == "sebas"
    assert row["config"]["max_coding"] == 0


@pytest.mark.asyncio
async def test_migration_056_clears_the_infra_lane_rows_and_is_idempotent(db_pool):
    """056 deletes the infra lane's settings rows and seeded runbook, drops the
    content routes and verb overrides that pointed at it, and strips the sweep's
    alertmanager keys. Everything else in those rows stays."""
    sql = (REPO_ROOT / "migrations" / "056_v1_removal_infra.sql").read_text()
    await run_migrations(db_pool)
    await load_seeds(db_pool, SEED_DIR)
    keys = ["alert_remediation", "infra_alert_routing", "hub_settle_seconds", "infra_heartbeat_state"]
    async with db_pool.acquire() as conn:
        for key in keys:
            await conn.execute(
                "INSERT INTO settings (key, value) VALUES ($1, '{}'::jsonb) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                key,
            )
        await conn.execute(
            "INSERT INTO resources (kind, slug, title) "
            "VALUES ('runbook', 'homelab-service-restart', 'restart') ON CONFLICT (slug) DO NOTHING"
        )
        await conn.execute(
            "INSERT INTO settings (key, value) VALUES ('content_routes', $1) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            [
                {"key": "jira-app", "match": "prefix", "value": "APP-", "assignee": "@pandora"},
                {"key": "bug", "match": "contains", "value": "[bug]", "assignee": "@raphael"},
                {"key": "infra", "match": "contains", "value": "down", "gate": True},
            ],
        )
        await conn.execute(
            "INSERT INTO settings (key, value) VALUES ('agent_task_verbs', $1) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            {"#alert": "infra", "#calendar": None, "#chat": "research"},
        )
        await conn.execute(
            "UPDATE activities SET config = $1 WHERE slug = 'hub-sweep-5m'",
            {"alertmanager_url": "http://am:9093", "alertmanager_min_uptime_seconds": 900,
             "group_min_members": 4},
        )
        await conn.execute(sql)
        await conn.execute(sql)  # a re-run is a no-op
        left = await conn.fetch("SELECT key FROM settings WHERE key = ANY($1::text[])", keys)
        runbook = await conn.fetchval(
            "SELECT 1 FROM resources WHERE slug = 'homelab-service-restart'"
        )
        routes = await conn.fetchval("SELECT value FROM settings WHERE key = 'content_routes'")
        verbs = await conn.fetchval("SELECT value FROM settings WHERE key = 'agent_task_verbs'")
        sweep = await conn.fetchval("SELECT config FROM activities WHERE slug = 'hub-sweep-5m'")
        await conn.execute("DELETE FROM settings WHERE key IN ('content_routes', 'agent_task_verbs')")
        await conn.execute("UPDATE activities SET config = '{}'::jsonb WHERE slug = 'hub-sweep-5m'")
    assert left == []
    assert runbook is None
    # A route with no assignee was the old `@pandora` default too.
    assert [r["key"] for r in routes] == ["bug"]
    assert verbs == {"#calendar": None, "#chat": "research"}
    assert sweep == {"group_min_members": 4}
