"""AEGIS v2 FastAPI application."""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles

from aegis.api.deps import get_settings
from aegis.db import create_pool, run_migrations
from aegis.llm import LLMClient, set_model_tiers
from aegis.services.chat import _validate_agent_tool_sets

logger = structlog.get_logger()

# Bounded wait when draining background tasks on shutdown so we don't hang
# the process indefinitely on a stuck extraction.
_SHUTDOWN_DRAIN_TIMEOUT_SECONDS = 10.0


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize and teardown services."""
    settings = get_settings()
    logger.info("aegis_v2_starting")

    # Fail fast if any agent's tool set references a missing executor.
    _validate_agent_tool_sets()

    # Set for fire-and-forget background tasks (e.g. post-chat knowledge
    # extraction). Handlers add their task and register a done_callback that
    # discards it. Shutdown drains whatever is still pending.
    app.state.background_tasks = set()

    # Database
    pool = await create_pool(settings.database_url)
    await run_migrations(pool)

    # Overlay UI-set integration config (tokens/secrets) over env before anything
    # reads them (connectors, seeders, webhook routes all use this settings obj).
    from aegis.services.integrations_config import apply_config_overrides

    await apply_config_overrides(settings, pool)

    from aegis.seed import load_seeds

    await load_seeds(pool, settings.seed_dir)

    try:
        from aegis.services.rss_seeder import seed_rss_from_miniflux

        count = await seed_rss_from_miniflux(pool, settings.miniflux_url, settings.miniflux_api_key)
        logger.info("miniflux_rss_seed_complete", count=count)
    except Exception as exc:
        logger.warning("miniflux_seed_failed", error=str(exc)[:200])

    app.state.db_pool = pool

    # LLM client + tier map from the configurable backend (DB → env fallback).
    from aegis.services.llm_backend import get_llm_backend

    backend = await get_llm_backend(pool, settings)
    set_model_tiers(backend["tiers"])
    logger.info("model_tiers_loaded", tiers=sorted(backend["tiers"]), source=backend["source"])
    app.state.llm_backend = backend
    llm = LLMClient(
        base_url=backend["base_url"],
        api_key=backend["api_key"],
        timeout=settings.litellm_timeout,
    )
    app.state.llm = llm

    from aegis.connectors.search import SearchConnector
    from aegis.mcp_manager import MCPManager
    from aegis.services.knowledge import KnowledgeStore

    # Native pgvector knowledge subsystem — always available (it's just our DB).
    knowledge_connector = KnowledgeStore(
        db_pool=pool, llm=llm, embedding_model=settings.embedding_model
    )
    app.state.knowledge_connector = knowledge_connector

    # Finance connector — web market data (keyless providers, always available).
    from aegis.connectors.finance import FinanceConnector

    finance_connector = FinanceConnector(
        provider=settings.finance_provider,
        api_key=settings.finance_api_key,
        indices=settings.finance_indices,
        db_pool=pool,
    )
    app.state.finance_connector = finance_connector

    search_connector = SearchConnector(base_url=settings.searxng_url)
    app.state.search_connector = search_connector

    # Vercel connector — read-only project/deployment/build-log queries for
    # Pandora's chat tools. Short-circuits to no-op when token is empty.
    from aegis.connectors.vercel import VercelConnector

    vercel_connector = VercelConnector(
        token=settings.vercel_token,
        team_id=settings.vercel_team_id,
        db_pool=pool,
    )
    app.state.vercel_connector = vercel_connector

    # Remote script connector (SSH to the coding host — used by infra chat
    # tools and coding-agent runs). Config is DB-first: an infra registry row
    # with coding.enabled overrides the env settings at call time, so the
    # connector is always constructed (env values are the fallback).
    from aegis.connectors.remote_script import RemoteScriptConnector

    remote_script_connector = RemoteScriptConnector(
        host=settings.remote_script_host,
        user=settings.remote_script_user,
        key_file=settings.remote_script_key_file,
        repo_base=settings.remote_script_repo_base,
        known_hosts=getattr(settings, "remote_script_known_hosts", None),
        kimi_host=getattr(settings, "remote_script_kimi_host", ""),
        tmux_session=getattr(settings, "remote_script_tmux_session", "remote"),
        tmux_window_cap=getattr(settings, "remote_script_tmux_window_cap", 10),
        claude_orgs=getattr(settings, "remote_script_claude_orgs", ""),
        claude_binary=getattr(settings, "claude_cli_binary_path", ""),
        kimi_binary=getattr(settings, "kimi_cli_binary_path", ""),
        self_repo_path=getattr(settings, "aegis_self_repo_path", ""),
        runbooks_dir=getattr(settings, "runbooks_dir", ""),
        db_pool=pool,
        secret_key=settings.secret_key,
    )
    logger.info(
        "remote_script_connector_ready",
        env_host=settings.remote_script_host or None,
        db_first=True,
    )
    app.state.remote_script_connector = remote_script_connector

    mcp_manager = MCPManager(server_configs=settings.mcp_servers or {})
    app.state.mcp_manager = mcp_manager
    app.state.settings = settings

    # Temporal client (best-effort — don't block startup if unreachable)
    temporal_client = None
    try:
        from temporalio.client import Client as TemporalClient

        temporal_client = await TemporalClient.connect(settings.temporal_host)
        logger.info("temporal_client_connected", host=settings.temporal_host)
    except Exception as exc:
        logger.warning("temporal_client_unavailable", host=settings.temporal_host, error=str(exc))
    app.state.temporal_client = temporal_client

    logger.info("aegis_v2_ready")
    yield

    # Drain fire-and-forget tasks (e.g. post-chat knowledge extraction) before
    # tearing down dependencies they may still be using. Bounded wait avoids
    # hanging shutdown on a stuck task; anything still unfinished gets cancelled.
    pending = list(getattr(app.state, "background_tasks", set()))
    if pending:
        logger.info("background_tasks_draining", count=len(pending))
        try:
            await asyncio.wait_for(
                asyncio.gather(*pending, return_exceptions=True),
                timeout=_SHUTDOWN_DRAIN_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            logger.warning("background_tasks_drain_timeout", pending=len(pending))
            for task in pending:
                if not task.done():
                    task.cancel()

    await knowledge_connector.close()
    await vercel_connector.close()
    await finance_connector.close()
    sc = getattr(app.state, "search_connector", None)
    if sc:
        await sc.close()
    await mcp_manager.close()
    await llm.close()
    await pool.close()
    logger.info("aegis_v2_stopped")


def create_app(run_lifespan: bool = True) -> FastAPI:
    """Create the FastAPI application."""
    from aegis.api.routes import (
        activities,
        agents,
        audit,
        capture,
        chat,
        gmail_reauth,
        health,
        homelab,
        infra,
        infra_admin,
        integrations,
        interactions,
        knowledge,
        llm_backend,
        market,
        mcp,
        money,
        observability,
        overview,
        references,
        resources,
        settings,
        slack,
        social_auth,
        system_status,
        temporal,
        todoist,
        webhooks,
    )

    app = FastAPI(
        title="AEGIS v2",
        version="2.0.0",
        lifespan=lifespan if run_lifespan else None,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Compress JSON responses and the admin-panel SPA bundle (~332 KB → ~90 KB).
    app.add_middleware(GZipMiddleware, minimum_size=1024)

    # Unauthenticated routes
    app.include_router(health.router)

    # Authenticated routes
    app.include_router(agents.router)
    app.include_router(agents.admin_router)
    app.include_router(gmail_reauth.router)
    app.include_router(social_auth.router)
    app.include_router(chat.router)
    app.include_router(knowledge.router)
    app.include_router(references.router)
    app.include_router(observability.router)
    app.include_router(audit.router)
    app.include_router(temporal.router)
    app.include_router(settings.router)
    app.include_router(activities.router)
    app.include_router(integrations.router)
    app.include_router(llm_backend.router)
    app.include_router(slack.router)
    app.include_router(slack.internal_router)
    app.include_router(interactions.router)
    app.include_router(webhooks.router)
    app.include_router(capture.router)
    app.include_router(mcp.router)
    app.include_router(market.router)
    app.include_router(overview.router)
    app.include_router(homelab.router)
    app.include_router(money.router)
    app.include_router(infra.router)
    app.include_router(infra_admin.router)
    app.include_router(system_status.router)
    app.include_router(resources.router)
    app.include_router(todoist.router)

    # Serve admin panel SPA (static files from built frontend)
    # Try multiple locations: env override, Docker (/app/admin-panel/...) and local dev.
    # The parents[4] path only resolves correctly under an editable install — the env var
    # is the durable anchor for non-editable container installs (see PR #164 for context).
    admin_dist = None
    _local_admin_dist = Path(
        os.environ.get("AEGIS_ADMIN_DIST_DIR")
        or str(Path(__file__).resolve().parents[4] / "admin-panel" / "frontend" / "dist")
    )
    for candidate in [
        Path("/app/admin-panel/frontend/dist"),  # Docker container
        _local_admin_dist,  # Env override or local dev
        Path.cwd() / "admin-panel" / "frontend" / "dist",  # CWD-based
    ]:
        if candidate.exists() and (candidate / "index.html").exists():
            admin_dist = candidate
            break
    if admin_dist:
        from fastapi.responses import FileResponse

        # Serve static assets
        app.mount("/assets", StaticFiles(directory=str(admin_dist / "assets")), name="admin-assets")

        # SPA fallback — serve index.html for all non-API routes
        @app.get("/{path:path}")
        async def serve_spa(path: str):
            # Don't intercept API routes
            if path.startswith("api/") or path == "health":
                return None
            file_path = admin_dist / path
            if file_path.exists() and file_path.is_file():
                return FileResponse(str(file_path))
            return FileResponse(str(admin_dist / "index.html"))

    return app
