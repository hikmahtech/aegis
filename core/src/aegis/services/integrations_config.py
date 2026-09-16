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
from aegis.errors import error_text
from aegis.services.settings_store import get_setting, put_setting

logger = structlog.get_logger()

_PREFIX = "integration:"


@dataclass(frozen=True)
class ConfigKey:
    key: str  # Settings field name
    label: str
    group: str
    secret: bool
    boolean: bool = False  # render as an on/off toggle; stored as "true"/"false"
    help: str = ""  # prerequisite config / caveats shown under the field


# The user-facing integration config. Infra/bootstrap fields (db/temporal/admin/
# paths/homelab/remote-script) are deliberately NOT here — they're env-only.
CONFIG_REGISTRY: list[ConfigKey] = [
    ConfigKey("github_webhook_secret", "Webhook secret", "GitHub", True),
    ConfigKey("sentry_url", "Base URL", "Sentry", False),
    ConfigKey("sentry_token", "API token", "Sentry", True),
    ConfigKey("sentry_org", "Org slug", "Sentry", False),
    ConfigKey("sentry_projects", "Project ids (comma-sep, blank = all)", "Sentry", False),
    ConfigKey("sentry_webhook_secret", "Webhook secret", "Sentry", True),
    ConfigKey("todoist_webhook_secret", "Webhook secret", "Todoist", True),
    ConfigKey(
        "life_webhook_secret", "Webhook secret", "Life data", True,
        help="Signs pushes to /api/webhooks/life/{source} from your phone, watch or "
        "home automation. Generate with `openssl rand -hex 32`. UNSET = the endpoint "
        "rejects every request (503) — it never runs unauthenticated. Applies on save "
        "(read per-request).",
    ),
    ConfigKey("x_client_id", "OAuth client id", "X (Twitter)", False),
    ConfigKey("x_client_secret", "OAuth client secret", "X (Twitter)", True),
    ConfigKey("postiz_url", "Base URL", "Postiz", False),
    ConfigKey("postiz_api_key", "API key", "Postiz", True),
    ConfigKey("postiz_public_url", "Web UI URL (browser-facing)", "Postiz", False),
    ConfigKey("vercel_token", "API token", "Vercel", True),
    ConfigKey("vercel_team_id", "Team id", "Vercel", False),
    ConfigKey("elevenlabs_api_key", "API key", "Voice (ElevenLabs)", True),
    ConfigKey(
        "elevenlabs_stt_model", "Speech-to-text model", "Voice (ElevenLabs)", False,
        help="The ElevenLabs Scribe model media transcription uses (default scribe_v1). "
        "Worker restart required.",
    ),
    ConfigKey("raindrop_api_token", "API token", "Raindrop", True),
    ConfigKey(
        "semantic_scholar_api_key", "Semantic Scholar API key", "Research", True,
        help="Sent as x-api-key on paper_search and paper_read. Blank = the public, "
        "rate-limited tier. Core applies a change on save; the worker (ResearchFlow) "
        "on restart.",
    ),
    ConfigKey(
        "bot_contact_url", "Bot contact URL (User-Agent)", "Research", False,
        help="Named in the User-Agent AEGIS sends when it fetches pages and feeds "
        "(AegisBot/2.0 (+<url>)), so a site can see who is reading it. Blank = the "
        "admin UI URL, else no contact. Core applies a change on save; the worker on "
        "restart.",
    ),
    ConfigKey(
        "calibre_url", "calibre-web URL", "Calibre (library)", False,
        help="The address the stack reaches calibre-web at directly, e.g. "
        "http://calibre-web:8083 on the same Docker network. Never a host behind an "
        "SSO login page: it answers every path with a redirect, which AEGIS treats as "
        "an error. Blank = not configured. Core applies a change on save; "
        "CalibreSyncFlow (the worker) on restart.",
    ),
    ConfigKey(
        "calibre_user", "calibre-web user", "Calibre (library)", False,
        help="A dedicated read-only calibre-web user for AEGIS: download allowed, no "
        "upload, edit or delete. Blank = the library tools say not configured and "
        "CalibreSyncFlow reports not_configured.",
    ),
    ConfigKey("calibre_password", "calibre-web password", "Calibre (library)", True),
    ConfigKey(
        "calibre_max_book_mb", "Largest book file read (MB)", "Calibre (library)", False,
        help="A book file over this size is not downloaded (default 80).",
    ),
    ConfigKey(
        "calibre_max_books", "Most books in the catalogue", "Calibre (library)", False,
        help="The paging cap when the catalogue is read (default 3000).",
    ),
    ConfigKey(
        "jira_base_url", "Site URL (https://yours.atlassian.net)", "Jira", False,
        help="JiraSyncFlow closes a Todoist task once its issue has a resolution. "
        "It exists because Jira sends NO notification for a transition you make "
        "yourself — email triage can only ever close tickets somebody else "
        "resolved. Worker restart required.",
    ),
    ConfigKey(
        "jira_email", "Atlassian account email", "Jira", False,
        help="The email half of Basic auth — your Atlassian account, not a "
        "team alias.",
    ),
    ConfigKey(
        "jira_api_token", "API token", "Jira", True,
        help="Create at id.atlassian.com/manage-profile/security/api-tokens. "
        "A password will not work. Any of the three fields blank = the flow "
        "reports not_configured and makes no request.",
    ),
    ConfigKey(
        "oura_api_token", "Oura personal access token", "Wearables", True,
        help="Polled by WearableIngestFlow into life.observations. Also needs a "
        "channel row (kind=wearable, identifier=oura) switched on under Channels. "
        "Blank = the flow reports token_missing and never calls the API. "
        "Worker restart required.",
    ),
    ConfigKey("searxng_url", "Base URL", "Search (SearXNG)", False),
    ConfigKey("finance_provider", "Provider (yahoo | stooq)", "Finance", False),
    ConfigKey("finance_indices", "Overview indices (comma-sep symbols)", "Finance", False),
    ConfigKey("aegis_stack_name", "Swarm stack name (blank = show all services)", "System Monitoring", False),
    ConfigKey(
        "infra_cluster", "Infra cluster label (Prometheus `cluster` label)",
        "System Monitoring", False,
        help="Alerts whose cluster label equals this value route straight to infra-gitops, "
        "skipping the LLM repo-match. Blank = alertname matching only. "
        "Worker restart required; a set env var can only be overridden, not blanked, from here.",
    ),
    ConfigKey(
        "infra_heartbeat_ping_url", "Heartbeat dead-man ping URL (healthchecks.io)",
        "System Monitoring", True,
        help="GET on every successful 2-min heartbeat tick; configure the check to alert "
        "when pings stop. Blank = disabled. Worker restart required.",
    ),
    ConfigKey(
        "slack_owner_member_id", "Slack member id for escalation mentions",
        "System Monitoring", False,
        help="Used to @-mention you on unacked critical infra cards (e.g. U0123456789). "
        "Blank = escalate without mention. Worker restart required.",
    ),
    ConfigKey(
        "slack_saveit_emoji", "Save-it reaction names (comma-sep, no colons)",
        "Slack self-capture", False,
        help="React to YOUR OWN Slack message with one of these (default `brain`) "
        "to file it as a life_fact. Inert unless the Slack member id above is "
        "set; never ingests anyone else's message. Comms restart required.",
    ),
    ConfigKey(
        "slack_note_to_self_channel", "Note-to-self channel id",
        "Slack self-capture", False,
        help="Every message YOU post in this channel (e.g. C0123456789) is filed "
        "as a life_fact instead of being routed to an agent. Blank = disabled. "
        "Inert unless the Slack member id above is set. Comms restart required.",
    ),
    ConfigKey(
        "owner_emails", "Your own email addresses (comma-sep)",
        "Owner", False,
        help="The addresses that are YOU. Google lists the calendar owner among an "
        "event's attendees, so without this AEGIS can ask you who you are. Matched "
        "case-insensitively. Blank = no exclusion. Worker restart required.",
    ),
    # Feature flags — enable/disable whole subsystems. Off by default unless noted.
    # `help` names the extra config a feature needs to actually work.
    ConfigKey(
        "homelab_enabled", "Homelab Guardian (swarm drift + cert radar)", "Features", False,
        boolean=True,
        help="Needs an infra registry entry for your Docker Swarm (Infra page) and, for cert-radar, "
        "public domains (Sentry/Finance-style config or homelab_public_domains). Restart the worker after enabling.",
    ),
    ConfigKey(
        "money_hygiene_enabled", "Money Hygiene (Maou: receipts, subscriptions)", "Features", False,
        boolean=True,
        help="Needs a connected Gmail account (Integrations → Google) for receipt ingestion. "
        "Restart the worker after enabling.",
    ),
    ConfigKey(
        "tts_enabled", "Voice notes (per-persona TTS)", "Features", False,
        boolean=True,
        help="Needs an ElevenLabs API key (Voice section above).",
    ),
    ConfigKey(
        "notification_budget_enabled", "Notification budget (cap proactive pushes)", "Features", False,
        boolean=True,
        help="Uses notification_daily_budget (default 8). Off = record-only, no suppression.",
    ),
    ConfigKey(
        "content_extraction_enabled", "Content extraction (article/bookmark bodies)", "Features", False,
        boolean=True,
        help="For Raindrop bookmarks, also set the Raindrop API token above.",
    ),
    ConfigKey(
        "knowledge_context_enabled", "Proactive knowledge context in chat", "Features", False,
        boolean=True,
        help="Injects relevant knowledge into replies. No extra config. On by default.",
    ),
    ConfigKey(
        "tool_calling_enabled", "Agent tool-calling in chat", "Features", False,
        boolean=True,
        help="Lets agents run tools mid-chat. No extra config. On by default.",
    ),
    ConfigKey(
        "people_enrichment_enabled", "Passive people enrichment (email + calendar)",
        "Features", False,
        boolean=True,
        help="Learns email addresses and last-contact dates for people already on the "
        "People page. Email NEVER creates a person; calendar attendees may, but only "
        "for small meetings and only once Owner → your own email addresses is set. "
        "Restart the worker after enabling.",
    ),
    ConfigKey(
        "books_repo_url", "Repo URL (git@github.com:org/books.git)", "Books", False,
        help="The hledger books repo Maou writes to. Empty = books disabled (money mail is "
        "indexed, never posted). SSH form; the deploy key below must have write access. "
        "Core + worker restart required.",
    ),
    ConfigKey(
        "books_deploy_key", "Deploy key (private, ed25519)", "Books", True,
        help="Paste the PEM (multi-line) or its base64. Written to the credentials dir "
        "with mode 0600 at boot; never logged.",
    ),
    ConfigKey(
        "notes_repo_url", "Vault repo URL (git@github.com:you/vault.git)", "Notes (vault)", False,
        help="The Obsidian vault your research agent reads, indexes and keeps the journal in "
        "(#514). Append-only: it never changes a line you wrote. Needs the deploy key below "
        "with WRITE access. Empty = not configured (the daylog keeps filing knowledge rows). "
        "Where the notes go is the Vault page. Core + worker restart required.",
    ),
    ConfigKey(
        "notes_deploy_key", "Vault deploy key (private, ed25519)", "Notes (vault)", True,
        help="Paste the PEM (multi-line) or its base64. Written to the credentials dir with "
        "mode 0600 at boot; never logged.",
    ),
    ConfigKey(
        "home_currency", "Home currency (ISO code)", "Books", False,
        help="The currency the books report in — INR, USD, GBP, EUR, … It is what hledger "
        "converts to for every balance and check, and what a posting that names no "
        "currency is written in. Core + worker restart required.",
    ),
    ConfigKey(
        "books_ignored_mailboxes", "Ignored mailboxes (comma-separated labels)", "Books", False,
        help="Money mail in these mailboxes is not yours (e.g. an employer's account).",
    ),
    ConfigKey(
        "books_mailbox_entities",
        "Mailbox → entity (label=<entity>, comma-separated)", "Books", False,
        help="Which set of books a mailbox's money belongs to, naming an entity from the "
        "chart of accounts (Money → Its entities). Unlisted = the default entity.",
    ),
    ConfigKey(
        "books_todoist_projects", "Todoist projects for dues (<entity>=<id>, comma-separated)",
        "Books", False,
        help="Bills and failed payments become dated tasks here, and Maou's money problem "
        "tasks (#money) go to the default entity's project. Unset = the Inbox.",
    ),
    ConfigKey(
        "ansaar_url", "ansaar-data URL", "Trading desk", False,
        help="Where Maou's trading desk reads the trading system's decisions, e.g. "
        "http://ansaar-data:3000 on the swarm overlay. Empty = the desk does nothing. "
        "Applies on the next run.",
    ),
    ConfigKey(
        "ansaar_service_secret", "Client-token service secret", "Trading desk", True,
        help="ansaar-data's CLIENT_TOKEN_SECRET, exchanged for a 15-minute token on each run. "
        "Never the admin login.",
    ),
]
_BY_KEY = {c.key: c for c in CONFIG_REGISTRY}


def _skey(field: str) -> str:
    return _PREFIX + field


async def read_integration(pool: Any, settings: Any, key: str) -> str:
    """One integration value, read now: the DB row first (decrypted when it is a
    secret), then the Settings field. For a caller that must see an admin save
    without a restart, since the worker applies the overlay only at boot. Never raises."""
    spec = _BY_KEY[key]
    try:
        stored = await get_setting(pool, _skey(key))
    except Exception as exc:  # noqa: BLE001 — a config read must never break a run
        logger.warning("integration_read_failed", key=key, error=error_text(exc))
        stored = None
    if isinstance(stored, dict):
        val = _resolve(spec, stored, getattr(settings, "secret_key", ""))
        if val:
            return val
    return str(getattr(settings, key, "") or "")


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
        logger.warning("config_overrides_read_failed", error=error_text(exc))
        return settings
    for r in rows:
        field = r["key"][len(_PREFIX):]
        spec = _BY_KEY.get(field)
        if not spec or not r["value"]:
            continue
        if spec.boolean:
            # Always set (even "false") so a DB toggle can override an env True.
            raw = str((r["value"] or {}).get("val") or "").lower()
            setattr(settings, field, raw == "true")
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
        base = {
            "key": spec.key, "label": spec.label, "group": spec.group,
            "boolean": spec.boolean, "help": spec.help,
        }
        if spec.boolean:
            if in_db:
                cur = str(db[spec.key].get("val") or "").lower() == "true"
                source = "db"
            else:
                cur = bool(getattr(settings, spec.key, False))
                source = "env"
            out.append({**base, "secret": False, "set": cur, "value": cur, "source": source})
        elif spec.secret:
            env_val = getattr(settings, spec.key, "") or ""
            db_has = in_db and bool(decrypt_secret(db[spec.key].get("enc"), settings.secret_key))
            out.append({**base, "secret": True,
                "set": db_has or bool(env_val), "value": None,
                "source": "db" if db_has else ("env" if env_val else "none")})
        else:
            env_val = getattr(settings, spec.key, "") or ""
            display = (db[spec.key].get("val") if in_db else "") or env_val or ""
            out.append({**base, "secret": False,
                "set": bool(display), "value": display,
                "source": "db" if in_db else ("env" if env_val else "none")})
    return out


async def save_integration(pool: Any, settings: Any, key: str, value: str) -> None:
    spec = _BY_KEY.get(key)
    if spec is None:
        raise ValueError(f"unknown integration key: {key}")
    if spec.boolean:
        stored = {"val": "true" if str(value).lower() in ("true", "1", "on", "yes") else "false"}
    elif spec.secret:
        stored = {"enc": encrypt_secret(value, settings.secret_key)}
    else:
        stored = {"val": value}
    await put_setting(pool, _skey(key), stored)
