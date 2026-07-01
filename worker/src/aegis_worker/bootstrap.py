"""Worker bootstrap — create all service dependencies for activities."""

from __future__ import annotations

from typing import Any

import asyncpg
import structlog
from aegis.config import Settings
from aegis.connectors.search import SearchConnector
from aegis.db import create_pool
from aegis.llm import LLMClient

logger = structlog.get_logger()


class WorkerDeps:
    """Container for worker service dependencies."""

    def __init__(
        self,
        pool: asyncpg.Pool,
        llm: LLMClient,
        settings: Settings,
        connectors: dict[str, Any] | None = None,
        http_client: Any = None,
        model_tiers: dict[str, str] | None = None,
    ):
        self.pool = pool
        self.llm = llm
        self.settings = settings
        self.connectors = connectors or {}
        self.http_client = http_client
        # Resolved tier→model map from the configurable LLM backend (Phase A).
        self.model_tiers = model_tiers or {}

    async def close(self):
        await self.llm.close()
        for c in self.connectors.values():
            if hasattr(c, "close"):
                await c.close()
        if self.http_client:
            await self.http_client.aclose()
        await self.pool.close()


async def bootstrap(settings: Settings | None = None) -> WorkerDeps:
    """Create all service dependencies for worker activities."""
    if settings is None:
        settings = Settings()

    logger.info("worker_bootstrap_starting")

    # Database pool (with JSONB codec)
    pool = await create_pool(settings.database_url)
    logger.info("worker_db_pool_created")

    # Overlay UI-set integration config (tokens/secrets) over env before the
    # connectors below are built from `settings`.
    from aegis.services.integrations_config import apply_config_overrides

    await apply_config_overrides(settings, pool)

    # LLM client + tier map from the configurable backend (DB → env fallback).
    # Cap gemma4:e2b at 2 concurrent calls — it shares node-a's RTX 2070 with
    # everything else aegis hosts on that node, and bursts serialise through
    # ollama compounding tail latency.
    from aegis.llm import set_model_tiers
    from aegis.services.llm_backend import get_llm_backend

    backend = await get_llm_backend(pool, settings)
    set_model_tiers(backend["tiers"])
    llm = LLMClient(
        base_url=backend["base_url"],
        api_key=backend["api_key"],
        timeout=settings.litellm_timeout,
        concurrency_limits={"gemma4:e2b": 2},
    )

    # Connectors (created if configured, None if not)
    connectors: dict[str, Any] = {}

    # Search (SearxNG)
    searxng_url = getattr(settings, "searxng_url", "")
    if searxng_url:
        connectors["search"] = SearchConnector(base_url=searxng_url)
        logger.info("connector_ready", connector="search")

    # RemoteScript (needs SSH config)
    remote_host = getattr(settings, "remote_script_host", "")
    if remote_host:
        try:
            from aegis.connectors.remote_script import RemoteScriptConnector

            connectors["remote_script"] = RemoteScriptConnector(
                host=remote_host,
                user=getattr(settings, "remote_script_user", "deploy"),
                key_file=getattr(settings, "remote_script_key_file", "~/.ssh/id_ed25519"),
                repo_base=getattr(settings, "remote_script_repo_base", ""),
                kimi_host=getattr(settings, "remote_script_kimi_host", ""),
                tmux_session=getattr(settings, "remote_script_tmux_session", "remote"),
                tmux_window_cap=getattr(settings, "remote_script_tmux_window_cap", 10),
                claude_orgs=getattr(settings, "remote_script_claude_orgs", ""),
                claude_binary=getattr(settings, "claude_cli_binary_path", ""),
            )
            logger.info("connector_ready", connector="remote_script")
        except Exception as exc:
            logger.warning("remote_script_init_failed", error=str(exc))

    # Knowledge subsystem — native pgvector over our own pool, always available.
    from aegis.services.knowledge import KnowledgeStore

    connectors["knowledge"] = KnowledgeStore(
        db_pool=pool, llm=llm, embedding_model=settings.embedding_model
    )
    logger.info("connector_ready", connector="knowledge")

    import httpx

    http_client = httpx.AsyncClient(
        headers={"X-API-Key": getattr(settings, "api_key", "")}
        if getattr(settings, "api_key", "")
        else {},
        timeout=60.0,
    )

    # Homelab Guardian connector — only when fully configured.
    # Dagster URL is optional: ScheduleHealthFlow errors cleanly at call time if unset.
    if getattr(settings, "homelab_enabled", False):
        if not settings.homelab_traefik_api_url:
            logger.warning(
                "homelab_init_skipped_missing_settings",
                missing=["traefik_api_url"],
                hint="set AEGIS_HOMELAB_TRAEFIK_API_URL or flip AEGIS_HOMELAB_ENABLED=false",
            )
        else:
            try:
                from aegis.connectors.homelab import HomelabConnector

                connectors["homelab"] = HomelabConnector(
                    docker_context=settings.homelab_docker_context,
                    dagster_graphql_url=settings.homelab_dagster_graphql_url,
                    traefik_api_url=settings.homelab_traefik_api_url,
                    ssh_host=getattr(settings, "remote_script_host", ""),
                    ssh_user=getattr(settings, "remote_script_user", "deploy"),
                    ssh_key_file=getattr(settings, "remote_script_key_file", "~/.ssh/id_ed25519"),
                    http_client=http_client,
                )
                logger.info("connector_ready", connector="homelab")
            except Exception as exc:
                logger.warning("homelab_init_failed", error=str(exc))

    logger.info("worker_bootstrap_complete", connectors=list(connectors.keys()))
    return WorkerDeps(
        pool=pool,
        llm=llm,
        settings=settings,
        connectors=connectors,
        http_client=http_client,
        model_tiers=backend["tiers"],
    )
