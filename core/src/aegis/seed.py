"""v3 seed loader.

Reads YAML files under seed_dir (expects agents.yaml, channels.yaml,
resources.yaml, activities.yaml) and upserts each row. Called from the
FastAPI lifespan after run_migrations.

Upserts are keyed by the table's natural unique: agents.id, channels.(kind,identifier),
resources.slug, activities.slug. Re-running on the same DB is a no-op unless a
YAML field changed — in which case DO UPDATE overwrites the row.

JSONB columns receive Python dicts/lists directly — the pool's jsonb codec
(db/pool.py::_init_connection) encodes them. Calling json.dumps here would
double-encode and store a JSON-literal string.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import asyncpg
import structlog
import yaml

logger = structlog.get_logger()


async def load_seeds(pool: asyncpg.Pool, seed_dir: str | Path) -> None:
    """Load all seed YAMLs under seed_dir into the database."""
    seed_path = Path(seed_dir)
    if not seed_path.exists():
        logger.warning("seed_dir_not_found", path=str(seed_path))
        return

    await _load_agents(pool, seed_path / "agents.yaml")
    await _load_channels(pool, seed_path / "channels.yaml")
    await _load_resources(pool, seed_path / "resources.yaml")
    await _load_activities(pool, seed_path / "activities.yaml")


def _read_yaml(path: Path, top_key: str) -> list[dict[str, Any]]:
    if not path.exists():
        logger.warning("seed_file_missing", path=str(path))
        return []
    data = yaml.safe_load(path.read_text()) or {}
    return data.get(top_key, []) or []


def _persona_base() -> Path:
    """Where personalities/<id>/ live — env override, repo dir, or container path."""
    import os

    env = os.environ.get("AEGIS_PERSONALITY_DIR")
    if env and Path(env).is_dir():
        return Path(env)
    repo = Path(__file__).resolve().parents[3] / "personalities"
    if repo.is_dir():
        return repo
    return Path("/app/personalities")


def _read_persona_files(agent_id: str) -> tuple[str | None, str | None, str | None]:
    """Read (SOUL, AGENTS, USER).md for an agent — the first-boot seed for the
    soul/operating_notes/user_context columns. Returns None per missing file."""
    agent_dir = _persona_base() / agent_id

    def _rd(name: str) -> str | None:
        f = agent_dir / name
        return f.read_text().strip() if f.exists() else None

    return _rd("SOUL.md"), _rd("AGENTS.md"), _rd("USER.md")


async def _load_agents(pool: asyncpg.Pool, path: Path) -> None:
    rows = _read_yaml(path, "agents")
    if not rows:
        return
    async with pool.acquire() as conn:
        for r in rows:
            soul, operating_notes, user_context = _read_persona_files(r["id"])
            await conn.execute(
                """
                INSERT INTO agents (
                    id, name, role, system_prompt_path, capabilities,
                    model_tier, interaction_timeout_default, telegram_topic_id,
                    slack_channel_id, elevenlabs_voice_id, active, metadata,
                    soul, operating_notes, user_context
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)
                ON CONFLICT (id) DO UPDATE SET
                    name = EXCLUDED.name,
                    role = EXCLUDED.role,
                    system_prompt_path = EXCLUDED.system_prompt_path,
                    capabilities = EXCLUDED.capabilities,
                    -- model_tier + persona are DB-owned once set (edited in the
                    -- admin UI); the yaml/.md files only seed an empty value.
                    model_tier = COALESCE(NULLIF(agents.model_tier, ''), EXCLUDED.model_tier),
                    interaction_timeout_default = EXCLUDED.interaction_timeout_default,
                    telegram_topic_id = EXCLUDED.telegram_topic_id,
                    slack_channel_id = COALESCE(
                        NULLIF(EXCLUDED.slack_channel_id, ''),
                        agents.slack_channel_id
                    ),
                    elevenlabs_voice_id = COALESCE(
                        NULLIF(EXCLUDED.elevenlabs_voice_id, ''),
                        agents.elevenlabs_voice_id
                    ),
                    active = EXCLUDED.active,
                    -- agent metadata (routing config) is seed-owned for now — no
                    -- UI editor yet, so the yaml is the source of truth.
                    metadata = EXCLUDED.metadata,
                    soul = COALESCE(NULLIF(agents.soul, ''), EXCLUDED.soul),
                    operating_notes = COALESCE(
                        NULLIF(agents.operating_notes, ''), EXCLUDED.operating_notes
                    ),
                    user_context = COALESCE(
                        NULLIF(agents.user_context, ''), EXCLUDED.user_context
                    ),
                    updated_at = now()
                """,
                r["id"],
                r["name"],
                r["role"],
                r["system_prompt_path"],
                r.get("capabilities", []),
                r.get("model_tier", "balanced"),
                r.get("interaction_timeout_default", "archive"),
                r.get("telegram_topic_id"),
                r.get("slack_channel_id") or None,
                r.get("elevenlabs_voice_id") or None,
                r.get("active", True),
                r.get("metadata", {}),
                soul,
                operating_notes,
                user_context,
            )
        # Deactivate agents no longer in the YAML (FK constraints prevent deletion).
        yaml_ids = [r["id"] for r in rows]
        status = await conn.execute(
            "UPDATE agents SET active=FALSE WHERE id <> ALL($1::text[]) AND active=TRUE",
            yaml_ids,
        )
        # status is "UPDATE N"
        n = int(status.split()[-1]) if status else 0
        if n:
            logger.info("seeds_deactivated_orphans", kind="agents", count=n)
    logger.info("seeds_loaded", kind="agents", count=len(rows))


async def _load_channels(pool: asyncpg.Pool, path: Path) -> None:
    rows = _read_yaml(path, "channels")
    if not rows:
        return
    async with pool.acquire() as conn:
        for r in rows:
            await conn.execute(
                """
                INSERT INTO channels (kind, identifier, config, active)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (kind, identifier) DO UPDATE SET
                    config = EXCLUDED.config,
                    active = EXCLUDED.active
                """,
                r["kind"],
                r["identifier"],
                r.get("config", {}),
                r.get("active", True),
            )
        # Delete channels no longer in the YAML (no FK references).
        yaml_kinds = [r["kind"] for r in rows]
        yaml_identifiers = [r["identifier"] for r in rows]
        status = await conn.execute(
            """
            DELETE FROM channels
            WHERE (kind, identifier) NOT IN (
                SELECT unnest($1::text[]), unnest($2::text[])
            )
            """,
            yaml_kinds,
            yaml_identifiers,
        )
        n = int(status.split()[-1]) if status else 0
        if n:
            logger.info("seeds_deleted_orphans", kind="channels", count=n)
    logger.info("seeds_loaded", kind="channels", count=len(rows))


async def _load_resources(pool: asyncpg.Pool, path: Path) -> None:
    rows = _read_yaml(path, "resources")
    if not rows:
        return
    async with pool.acquire() as conn:
        for r in rows:
            await conn.execute(
                """
                INSERT INTO resources (kind, slug, title, content, url, tags, metadata)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                ON CONFLICT (slug) DO UPDATE SET
                    kind = EXCLUDED.kind,
                    title = EXCLUDED.title,
                    content = EXCLUDED.content,
                    url = EXCLUDED.url,
                    tags = EXCLUDED.tags,
                    metadata = EXCLUDED.metadata,
                    updated_at = now()
                """,
                r["kind"],
                r["slug"],
                r["title"],
                r.get("content"),
                r.get("url"),
                r.get("tags", []),
                r.get("metadata", {}),
            )
        # Delete orphans only among kinds the YAML actually owns. The sync
        # flows (WorkspaceRepoSyncFlow, VercelProjectSyncFlow) and the reactive
        # auto-register path in `worker/.../activities/alerts.py::
        # resolve_alert_resource` add rows of kind `repository` and
        # `vercel_project` that intentionally aren't tracked in the YAML —
        # without this scope, every Core restart wiped 248 GitHub repos.
        yaml_slugs = [r["slug"] for r in rows]
        yaml_managed_kinds = ("connector", "runbook", "endpoint", "mcp_server")
        status = await conn.execute(
            "DELETE FROM resources WHERE slug <> ALL($1::text[]) "
            "AND kind = ANY($2::text[])",
            yaml_slugs,
            list(yaml_managed_kinds),
        )
        n = int(status.split()[-1]) if status else 0
        if n:
            logger.info("seeds_deleted_orphans", kind="resources", count=n)
    logger.info("seeds_loaded", kind="resources", count=len(rows))


async def _load_activities(pool: asyncpg.Pool, path: Path) -> None:
    rows = _read_yaml(path, "activities")
    if not rows:
        logger.info("seeds_loaded", kind="activities", count=0)
        return
    async with pool.acquire() as conn:
        for r in rows:
            await conn.execute(
                """
                INSERT INTO activities (
                    slug, workflow_type, agent_id, schedule_cron, config, active
                ) VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (slug) DO UPDATE SET
                    -- The seed yaml is INITIAL defaults: first insert sets
                    -- everything, but later re-seeds only refresh the
                    -- code-structural fields. schedule_cron / config / active are
                    -- DB-owned (editable from /admin/flows) and must NOT be
                    -- clobbered, else a core restart would revert UI edits.
                    workflow_type = EXCLUDED.workflow_type,
                    agent_id = EXCLUDED.agent_id,
                    updated_at = now()
                """,
                r["slug"],
                r["workflow_type"],
                r["agent_id"],
                r["schedule_cron"],
                r.get("config", {}),
                r.get("active", True),
            )
        # Hard-delete activities no longer in the YAML so schedule_sync prunes
        # their orphan Temporal schedules on the next worker startup.
        yaml_slugs = [r["slug"] for r in rows]
        status = await conn.execute(
            "DELETE FROM activities WHERE slug <> ALL($1::text[])",
            yaml_slugs,
        )
        n = int(status.split()[-1]) if status else 0
        if n:
            logger.info("seeds_deleted_orphans", kind="activities", count=n)
    logger.info("seeds_loaded", kind="activities", count=len(rows))
