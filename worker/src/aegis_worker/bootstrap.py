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


class ConnectorUnavailableError(RuntimeError):
    """A connector was configured but could not be constructed.

    Raised at *use* time by :class:`_UnavailableConnector`, so an activity that
    depends on a broken connector fails with the boot error that caused it
    instead of an ``AttributeError: 'NoneType' object has no attribute ...``
    three frames away (issue #205).
    """


class _UnavailableConnector:
    """Stand-in for a connector whose constructor raised.

    Mirrors ``MCPManager``'s handling of a bad server entry (B8,
    ``core/src/aegis/mcp_manager.py``): log at ERROR once, keep booting, record
    why, and raise a typed error the moment anyone actually reaches for it.

    Deliberately **truthy**, which is the whole behaviour change. Activities
    guard with ``if not self.remote_script: return``, meaning a falsy stand-in
    would restore exactly the silent no-op this exists to remove.
    """

    def __init__(self, name: str, error: str):
        self._name = name
        self._error = error

    def __getattr__(self, item: str) -> Any:
        raise ConnectorUnavailableError(
            f"connector '{self._name}' failed to initialise at worker boot and is "
            f"unavailable ({self._error}); fix the configuration and restart the worker"
        )

    def __bool__(self) -> bool:
        return True

    def __repr__(self) -> str:
        return f"<UnavailableConnector {self._name}: {self._error}>"

    async def close(self) -> None:
        """No-op. Defined explicitly because ``WorkerDeps.close`` probes with
        ``hasattr``, which only swallows ``AttributeError`` — a ``__getattr__``
        raising anything else would turn shutdown into a crash."""
        return None


def _register_connector(
    connectors: dict[str, Any],
    errors: dict[str, str],
    name: str,
    factory: Any,
    *,
    fatal: bool = False,
) -> None:
    """Construct one connector, deciding what a failure means.

    ``fatal=True`` refuses to boot, naming the connector: reserved for
    connectors that are not optional integrations at all. ``fatal=False``
    boots degraded — ERROR log, an entry in ``errors`` (so the failure is
    visible at boot rather than inferred from silence), and an
    :class:`_UnavailableConnector` in its slot so dependent activities fail
    fast with the reason attached.

    Note the difference from a connector that is simply *not configured*:
    that one is absent from ``connectors`` entirely, stays ``None`` at the
    call site, and keeps its existing "skip quietly" behaviour. Only a
    connector we tried and failed to build gets a stand-in.
    """
    try:
        connectors[name] = factory()
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
        logger.error("connector_init_failed", connector=name, error=detail, fatal=fatal)
        if fatal:
            raise RuntimeError(
                f"worker cannot start: required connector '{name}' failed to "
                f"initialise ({detail})"
            ) from exc
        errors[name] = detail
        connectors[name] = _UnavailableConnector(name, detail)
    else:
        logger.info("connector_ready", connector=name)


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
        connector_errors: dict[str, str] | None = None,
    ):
        self.pool = pool
        self.llm = llm
        self.settings = settings
        self.connectors = connectors or {}
        # name -> why it failed to construct. Empty on a healthy boot.
        self.connector_errors = connector_errors or {}
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
    # Cap the fast tier at 2 concurrent calls — it typically shares a GPU with
    # everything else aegis hosts, and bursts serialise through ollama
    # compounding tail latency.
    from aegis.llm import set_model_tiers
    from aegis.services.llm_backend import get_llm_backend

    backend = await get_llm_backend(pool, settings)
    set_model_tiers(backend["tiers"])
    llm = LLMClient(
        base_url=backend["base_url"],
        api_key=backend["api_key"],
        timeout=settings.litellm_timeout,
        concurrency_limits={settings.model_fast: 2},
        db_pool=pool,
    )

    # Connectors. Not configured ⇒ absent from the dict ⇒ `None` at the call
    # site ⇒ the dependent activity skips, which is a legitimate steady state.
    # *Configured but broken* is not: see `_register_connector`.
    connectors: dict[str, Any] = {}
    connector_errors: dict[str, str] = {}

    # Search (SearxNG)
    searxng_url = getattr(settings, "searxng_url", "")
    if searxng_url:
        _register_connector(
            connectors,
            connector_errors,
            "search",
            lambda: SearchConnector(base_url=searxng_url),
        )

    # RemoteScript — always constructed: config resolves DB-first from the
    # infra registry (coding.enabled entry), with the env settings as fallback,
    # so the coding host can be configured entirely from the admin UI.
    def _remote_script() -> Any:
        # Imported inside the factory so an ImportError (a missing optional
        # dependency) degrades exactly like a constructor failure instead of
        # taking the whole worker down.
        from aegis.connectors.remote_script import RemoteScriptConnector

        return RemoteScriptConnector(
            host=getattr(settings, "remote_script_host", ""),
            user=getattr(settings, "remote_script_user", "deploy"),
            key_file=getattr(settings, "remote_script_key_file", "~/.ssh/id_ed25519"),
            repo_base=getattr(settings, "remote_script_repo_base", ""),
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
            secret_key=getattr(settings, "secret_key", ""),
        )

    _register_connector(connectors, connector_errors, "remote_script", _remote_script)

    # Knowledge subsystem — native pgvector over our own pool. The ONE fatal
    # connector: it is not an optional external integration but a thin wrapper
    # over the pool and LLM client this function just built, so a failure here
    # means the worker's own substrate is broken, not that the operator hasn't
    # configured something. There is no "knowledge disabled" state to degrade
    # to, and clarify/briefing/curiosity/content all read through it.
    def _knowledge() -> Any:
        from aegis.services.knowledge import KnowledgeStore

        return KnowledgeStore(
            db_pool=pool, llm=llm, embedding_model=settings.embedding_model
        )

    _register_connector(connectors, connector_errors, "knowledge", _knowledge, fatal=True)

    # Social publishing — always constructed; it only acts when social_accounts
    # rows exist (connected from the admin page) and the settings kill switch is on.
    def _social() -> Any:
        from aegis.connectors.social import SocialConnector

        return SocialConnector(db_pool=pool, settings=settings)

    _register_connector(connectors, connector_errors, "social", _social)

    import httpx

    http_client = httpx.AsyncClient(
        headers={"X-API-Key": getattr(settings, "api_key", "")}
        if getattr(settings, "api_key", "")
        else {},
        timeout=60.0,
    )

    # Homelab Guardian connector (Docker Swarm drift + TLS cert radar).
    # An empty docker_context relies on the DOCKER_HOST env var (preferred
    # inside the worker container where no local contexts exist).
    if getattr(settings, "homelab_enabled", False):

        def _homelab() -> Any:
            from aegis.connectors.homelab import HomelabConnector

            return HomelabConnector(docker_context=settings.homelab_docker_context)

        _register_connector(connectors, connector_errors, "homelab", _homelab)

    if connector_errors:
        logger.error(
            "worker_bootstrap_degraded",
            unavailable=sorted(connector_errors),
            errors=connector_errors,
        )
    logger.info(
        "worker_bootstrap_complete",
        connectors=list(connectors.keys()),
        unavailable=sorted(connector_errors),
    )
    return WorkerDeps(
        pool=pool,
        llm=llm,
        settings=settings,
        connectors=connectors,
        http_client=http_client,
        model_tiers=backend["tiers"],
        connector_errors=connector_errors,
    )
