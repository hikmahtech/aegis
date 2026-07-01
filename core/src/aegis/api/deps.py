"""FastAPI dependency injection for AEGIS v2."""

from fastapi import HTTPException, Request

from aegis.config import Settings

_settings: Settings | None = None


def get_settings() -> Settings:
    """Return the global Settings instance."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def get_knowledge_connector(request: Request):
    """Return the native pgvector KnowledgeStore from app state, or 503.

    Shared by the knowledge and references routes. The attribute is still named
    `knowledge_connector` for call-site stability after the external
    knowledge-service was replaced by the in-process KnowledgeStore.
    """
    connector = getattr(request.app.state, "knowledge_connector", None)
    if connector is None:
        raise HTTPException(status_code=503, detail="Knowledge subsystem not available")
    return connector
