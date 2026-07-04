"""BYO integration config — connector tokens + webhook secrets, editable from the
admin UI. Stored in the ``settings`` table (secrets encrypted via Phase A crypto)
under ``integration:<field>`` keys.

A boot-time overlay (`apply_config_overrides`) mutates the Settings singleton so
every connector built afterwards, and every ``Depends(get_settings)`` route,
sees the DB values with the env vars as the fallback. Connector-token changes
apply on the next core/worker restart (the connector is built once); webhook
secrets are read per-request so they go live when the overlay is re-applied on
save.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog

from aegis.crypto import decrypt_secret, encrypt_secret

logger = structlog.get_logger()

_PREFIX = "integration:"


@dataclass(frozen=True)
class ConfigKey:
    key: str  # Settings field name
    label: str
    group: str
    secret: bool


# The user-facing integration config. Infra/bootstrap fields (db/temporal/admin/
# paths/homelab/remote-script) are deliberately NOT here — they're env-only.
CONFIG_REGISTRY: list[ConfigKey] = [
    ConfigKey("github_token", "API token", "GitHub", True),
    ConfigKey("github_webhook_secret", "Webhook secret", "GitHub", True),
    ConfigKey("sentry_url", "Base URL", "Sentry", False),
    ConfigKey("sentry_token", "API token", "Sentry", True),
    ConfigKey("sentry_org", "Org slug", "Sentry", False),
    ConfigKey("sentry_projects", "Project ids (comma-sep, blank = all)", "Sentry", False),
    ConfigKey("sentry_webhook_secret", "Webhook secret", "Sentry", True),
    ConfigKey("todoist_webhook_secret", "Webhook secret", "Todoist", True),
    ConfigKey("x_client_id", "OAuth client id", "X (Twitter)", False),
    ConfigKey("x_client_secret", "OAuth client secret", "X (Twitter)", True),
    ConfigKey("postiz_url", "Base URL", "Postiz", False),
    ConfigKey("postiz_api_key", "API key", "Postiz", True),
    ConfigKey("postiz_public_url", "Web UI URL (browser-facing)", "Postiz", False),
    ConfigKey("vercel_token", "API token", "Vercel", True),
    ConfigKey("vercel_team_id", "Team id", "Vercel", False),
    ConfigKey("elevenlabs_api_key", "API key", "Voice (ElevenLabs)", True),
    ConfigKey("raindrop_api_token", "API token", "Raindrop", True),
    ConfigKey("miniflux_url", "Base URL", "RSS (Miniflux)", False),
    ConfigKey("miniflux_api_key", "API key", "RSS (Miniflux)", True),
    ConfigKey("searxng_url", "Base URL", "Search (SearXNG)", False),
    ConfigKey("finance_provider", "Provider (yahoo | stooq)", "Finance", False),
    ConfigKey("finance_api_key", "API key (optional, future providers)", "Finance", True),
    ConfigKey("finance_indices", "Overview indices (comma-sep symbols)", "Finance", False),
    ConfigKey("aegis_stack_name", "Swarm stack name (blank = show all services)", "System Monitoring", False),
]
_BY_KEY = {c.key: c for c in CONFIG_REGISTRY}


def _skey(field: str) -> str:
    return _PREFIX + field


def _resolve(spec: ConfigKey, stored: dict, secret_key: str) -> str:
    if spec.secret:
        return decrypt_secret(stored.get("enc"), secret_key)
    return str(stored.get("val") or "")


async def apply_config_overrides(settings: Any, pool: Any) -> Any:
    """Overlay DB integration config onto the Settings object (mutates in place).
    Called at boot after the pool is up, and re-applied on save. Never raises."""
    try:
        rows = await pool.fetch(
            "SELECT key, value FROM settings WHERE key LIKE $1", _PREFIX + "%"
        )
    except Exception as exc:  # noqa: BLE001 — config overlay must never break boot
        logger.warning("config_overrides_read_failed", error=str(exc)[:200])
        return settings
    for r in rows:
        field = r["key"][len(_PREFIX):]
        spec = _BY_KEY.get(field)
        if not spec or not r["value"]:
            continue
        val = _resolve(spec, r["value"], getattr(settings, "secret_key", ""))
        if val:
            setattr(settings, field, val)
    return settings


async def get_integrations(pool: Any, settings: Any) -> list[dict]:
    """Registry + current state for the admin UI (secret values never returned)."""
    rows = await pool.fetch("SELECT key, value FROM settings WHERE key LIKE $1", _PREFIX + "%")
    db = {r["key"][len(_PREFIX):]: r["value"] for r in rows if r["value"]}
    out: list[dict] = []
    for spec in CONFIG_REGISTRY:
        in_db = spec.key in db
        env_val = getattr(settings, spec.key, "") or ""
        if spec.secret:
            db_has = in_db and bool(decrypt_secret(db[spec.key].get("enc"), settings.secret_key))
            out.append({
                "key": spec.key, "label": spec.label, "group": spec.group, "secret": True,
                "set": db_has or bool(env_val), "value": None,
                "source": "db" if db_has else ("env" if env_val else "none"),
            })
        else:
            display = (db[spec.key].get("val") if in_db else "") or env_val or ""
            out.append({
                "key": spec.key, "label": spec.label, "group": spec.group, "secret": False,
                "set": bool(display), "value": display,
                "source": "db" if in_db else ("env" if env_val else "none"),
            })
    return out


async def save_integration(pool: Any, settings: Any, key: str, value: str) -> None:
    spec = _BY_KEY.get(key)
    if spec is None:
        raise ValueError(f"unknown integration key: {key}")
    stored = {"enc": encrypt_secret(value, settings.secret_key)} if spec.secret else {"val": value}
    await pool.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES ($1, $2, NOW()) "
        "ON CONFLICT (key) DO UPDATE SET value = $2, updated_at = NOW()",
        _skey(key),
        stored,
    )
