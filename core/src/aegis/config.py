"""AEGIS v3 configuration.

All secrets via environment variables with AEGIS_ prefix.

Required (no defaults — must be set via env or .env):
    - database_url
    - admin_username, admin_password (unless auth_disabled=true — see below)

The LLM backend (litellm_url/key/models) is configured from the admin UI
(Phase A) and optional here; temporal_ui_url is just a UI link with a default.

Sensible defaults are kept ONLY for non-sensitive values (port numbers,
local-only hostnames like ``localhost``, database/feature names, etc).
"""

from typing import Annotated, Any

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    """AEGIS configuration."""

    model_config = SettingsConfigDict(
        env_prefix="AEGIS_",
        env_file="config/.env",
        extra="ignore",
        settings_json_schema_extra={},
    )

    # Database (REQUIRED — no default, must be set via AEGIS_DATABASE_URL)
    database_url: str = Field(...)

    # LLM backend base URL. Optional — configure the provider/key/models from the
    # admin "Models & Providers" page (Phase A); this env value is the fallback.
    litellm_url: str = ""
    litellm_api_key: str = ""
    litellm_timeout: int = 300
    # Optional app secret for encrypting BYO provider keys stored in the DB
    # (Phase A). Unset → secrets stored plaintext (single-user self-hosted).
    secret_key: str = ""
    # v3 model tiers — LAST-RESORT defaults; they must match config/models.yaml,
    # which is itself only the fallback under the `settings.llm_backend` DB row.
    # These read as dead config right up until the moment a yaml/DB lookup is
    # missing and one of them silently becomes the live model, so a
    # decommissioned name here is a live hazard: both of these said
    # `gpt-oss:20b` for weeks after its host (ollama-2 on asif) left the swarm.
    model_fast: str = "bedrock-glm-4.7-flash"  # quick replies, low latency (Bedrock via LiteLLM)
    model_balanced: str = "bedrock-kimi-k2.5"  # default chat + most flows (Bedrock via LiteLLM)
    model_smart: str = "bedrock-kimi-k2.5"  # long-context synthesis, Raphael (Bedrock via LiteLLM)
    # Path to config/models.yaml — loaded at startup by app.lifespan.
    # Override via AEGIS_MODELS_YAML_PATH if running from a non-standard layout.
    models_yaml_path: str = "config/models.yaml"

    # Temporal. temporal_ui_url is just the "open in Temporal UI" link target.
    temporal_host: str = "localhost:7233"
    # One namespace per app on a shared cluster (AEGIS_TEMPORAL_NAMESPACE).
    temporal_namespace: str = "default"
    temporal_api_url: str = "http://localhost:8233"
    temporal_ui_url: str = "http://localhost:8233"

    # Active comms channel (AEGIS_CHANNEL). "web" = human-in-the-loop cards land
    # in the admin inbox, no external chat service needed (the OSS default).
    # "slack" routes cards/notifications through the aegis_comms service.
    channel: str = "web"

    # Comms delivery server (aegis-comms) base URL, e.g. http://comms:8081.
    # Empty = no external chat delivery (web channel only).
    comms_url: str = ""

    # Auth (REQUIRED unless auth_disabled — no defaults; admin/admin is unsafe
    # and must not ship). Set AEGIS_AUTH_DISABLED=true ONLY when the API is
    # fronted by an authenticating proxy (e.g. Cloudflare Access) and port 8080
    # is not otherwise reachable — it turns off basic auth + API-key checks
    # entirely (webhook HMAC verification is separate and stays on).
    auth_disabled: bool = False
    admin_username: str = ""
    admin_password: str = ""
    api_key: str = ""

    # CORS. Defaults to empty (no cross-origin allowed): this is a
    # single-origin self-hosted deployment where the admin-panel SPA is
    # served from the same origin as the API, so CORS should never need to
    # apply in production. Set AEGIS_CORS_ALLOWED_ORIGINS (comma-separated)
    # only for a deployment topology that genuinely serves the SPA from a
    # different origin than the API.
    # NoDecode: skip pydantic-settings' JSON decoding so the raw env/dotenv
    # string reaches _parse_cors_allowed_origins, which splits it on commas.
    cors_allowed_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)

    # Expose FastAPI's interactive docs — /docs, /redoc, /openapi.json. OFF by
    # default (#305): FastAPI mounts those itself, so they carry none of the
    # `verify_auth` dependencies every /api router gets, and an anonymous caller
    # gets a complete map of every endpoint and schema. Gating them behind auth
    # instead would be no protection at all in the common `auth_disabled=true`
    # topology, so the switch is explicit rather than tied to the auth posture.
    # Turn on for local development: AEGIS_EXPOSE_API_DOCS=true.
    expose_api_docs: bool = False
    # Run uvicorn with its file-watching reloader. Local development only (#620);
    # the production image runs the same `python -m aegis`. AEGIS_RELOAD=true.
    reload: bool = False

    # Connectors
    searxng_url: str = "http://localhost:8888"
    gmail_accounts: str = ""  # "name1:email1,name2:email2"
    gmail_credentials_file: str = "config/google_credentials.json"
    gmail_token_dir: str = "config/"
    # Todoist (GTD task management)
    todoist_api_key: str = ""
    todoist_webhook_secret: str = ""
    # Social publishing — BYO X (Twitter) OAuth 2.0 app (developer.x.com), same
    # rationale as the Google client: the maintainer's app can't be committed
    # and wouldn't authorize forkers. Editable from the admin Integrations page.
    x_client_id: str = ""
    x_client_secret: str = ""
    # Self-hosted Postiz instance — an alternate posting backend that holds the
    # platform OAuth itself; aegis mirrors its channels and posts through its
    # public API instead of doing native per-platform OAuth (mixed mode: native
    # X above keeps working for accounts connected via /connect).
    postiz_url: str = ""
    postiz_api_key: str = ""
    # Browser-facing Postiz URL for admin-UI links — distinct from postiz_url,
    # which may be an internal-only address the browser can't reach.
    postiz_public_url: str = ""
    slack_owner_member_id: str = ""  # the owner's Slack member id ("" = unset)
    # Curated self-signal ingest (comms reads these over /api/internal/slack-config).
    # Reaction names (no colons, comma-separated) that file YOUR OWN message as a
    # life_fact; and a channel id where every message you post is filed the same
    # way. Both are inert unless slack_owner_member_id is set — AEGIS never
    # ingests anyone else's message.
    slack_saveit_emoji: str = "brain"
    slack_note_to_self_channel: str = ""
    # Your own email addresses (comma-separated, matched case-insensitively).
    # Google lists the calendar owner among an event's attendees, so without
    # this the curiosity gap-finder can ask you who you are. Empty = no
    # exclusion. Editable from the admin Integrations page.
    owner_emails: str = ""
    # Passive people enrichment (C2) — keep life.people current from the mail
    # and meetings already flowing through AEGIS. Off by default: it writes
    # information about real third parties. Email only ever ENRICHES an
    # existing person; the calendar lane, which may create, additionally
    # refuses while owner_emails above is unset.
    people_enrichment_enabled: bool = False
    # Memory consolidation (A4) — the deployment-level kill switch for letting
    # an LLM plan MUTATE agent_memory (the user's accumulated corrections).
    # False = the nightly pass plans and logs but writes nothing, whatever
    # /admin/flows says. Enabling apply needs BOTH this env var on the worker
    # AND `dry_run: false` in the memory-reflection-nightly activities.config;
    # two keys in two systems, so neither a misclick in the admin UI nor a
    # stray env can grant write access on its own. Turning this back off kills
    # writes fleet-wide on the next worker restart, no DB edit needed.
    memory_consolidation_apply_enabled: bool = False
    # Knowledge subsystem (native pgvector — no external service).
    # embedding_model must be served by litellm_url's /embeddings; its vector dim
    # must match the knowledge_chunks.embedding column (768 for nomic-embed-text).
    embedding_model: str = "nomic-embed-text"
    knowledge_ui_url: str = ""  # admin-panel link target (now the in-app /admin/knowledge page)

    # Web finance data (FinanceConnector) — provider-agnostic quotes for Maou's
    # market tools. Built-in keyless providers: "yahoo" (default) and "stooq".
    # finance_indices drives get_market_overview.
    finance_provider: str = "yahoo"
    finance_indices: str = "^GSPC,^IXIC,^NSEI"

    # Chat tool-calling. 5 iterations (~4 tool steps) was the binding
    # constraint on multi-step agent work; the repeat-signature guard in
    # services/chat.py (chat_tool_repeat_stop) already stops degenerate
    # loops, so raising the cap doesn't reopen that failure mode. 4096 bytes
    # was starving the model of tool output; balanced-tier models have large
    # contexts, so the truncation cap can afford to be generous too.
    tool_calling_enabled: bool = True
    tool_max_iterations: int = 15
    tool_result_max_bytes: int = 16384
    tool_timeout_seconds: int = 30

    # Notification budget (Phase 5) — cap daily proactive FYI pushes. Disabled =
    # record-only (measures volume without suppressing); enable to defer
    # over-budget pushes to the daily digest.
    notification_budget_enabled: bool = False
    notification_daily_budget: int = 8

    # Proactive knowledge context
    knowledge_context_enabled: bool = True
    knowledge_context_score_threshold: float = 0.3
    knowledge_context_max_results: int = 5
    knowledge_context_max_chars: int = 2000
    knowledge_context_timeout_seconds: float = 5.0

    # v3 per-source webhook signing secrets. Each source verifies its own HMAC.
    # Kept as env vars (not settings table) per spec §15 resolution.
    # Optional read-only token for GitHub search (#677). Empty = unauthenticated.
    github_token: str = ""
    # /api/webhooks/life/{source} — signed push from phones/watches/home
    # automation. Empty = the endpoint rejects EVERYTHING (503). Never treat
    # an unset secret as "skip verification": this door writes into the
    # owner's personal data store.
    life_webhook_secret: str = ""  # X-Aegis-Signature + X-Aegis-Timestamp

    # Worker -> Core API
    core_api_url: str = "http://localhost:8080"

    # Content extraction
    content_extraction_enabled: bool = True
    raindrop_api_token: str = ""

    # Calibre library (#510). No default address: the URL, user and password
    # come from the Integrations page (DB-first), and any of them blank = the
    # library tools say "not configured" and CalibreSyncFlow reports
    # not_configured. The URL must reach calibre-web directly, never a host
    # behind an SSO login page (every redirect is an error). The two caps are
    # numbers the page stores as strings; `library.calibre_limits` coerces.
    calibre_url: str = ""
    calibre_user: str = ""
    calibre_password: str = ""
    calibre_max_book_mb: str = ""  # blank = 80
    calibre_max_books: str = ""  # blank = 3000

    # The research lane (#509). A Semantic Scholar key lifts paper_search off
    # the public rate limit; the contact URL goes into the bot User-Agent
    # (`services/user_agent.py`), falling back to aegis_ui_url. Both DB-first.
    semantic_scholar_api_key: str = ""
    bot_contact_url: str = ""

    # Wearables (B7). Blank = WearableIngestFlow reports `token_missing` and
    # never issues a request. Oura personal access token.
    oura_api_token: str = ""

    # ElevenLabs (separate vendor — NOT the LiteLLM proxy). Empty key = kill
    # switch for media transcription.
    elevenlabs_api_key: str = ""
    elevenlabs_stt_model: str = "scribe_v1"

    # Outbound per-persona TTS voice notes (opt-in, off by default). Worker
    # flows that explicitly call send_voice still no-op unless this is true.
    tts_enabled: bool = False

    # AEGIS admin UI base URL: where links in chat cards, tasks and mail send a
    # person. May be a LAN/VPN-only host.
    aegis_ui_url: str = Field(default="", validation_alias="AEGIS_UI_URL")
    # The host OAuth providers redirect back to (Gmail re-auth, X). Must be the
    # one registered with the provider, which is usually the public host — so it
    # is separate from the link host. Blank = aegis_ui_url.
    aegis_public_url: str = Field(default="", validation_alias="AEGIS_PUBLIC_URL")

    # v3 seed directory (YAML files for agents, channels, resources, activities)
    seed_dir: str = "./config/seed"

    # Money Hygiene (Maou)
    money_hygiene_enabled: bool = False
    # The books cutover switch (#703); read per run from the DB by the worker. Declared so the
    # boot overlay (integrations_config.apply_config_overrides) has a field to set.
    money_fanout_enabled: bool = True
    # Maou's paper trading desk. Its own flag, not money_hygiene_enabled: the desk
    # stays in v1 when the books lane leaves it. On by default.
    trading_desk_enabled: bool = True
    # Currency the books report in; drives the money brief's symbol.
    home_currency: str = "INR"

    # The books — Maou's hledger journal (spec 2026-09-05-maou-books-design.md §10).
    # books_repo_url empty ⇒ books disabled: money events are still indexed,
    # never posted. The three list-ish knobs are strings so the admin
    # Integrations page can set them (DB-configured, no redeploy).
    books_path: str = "/app/config/books"
    books_repo_url: str = ""
    books_deploy_key: str = ""  # private ed25519 deploy key, PEM or base64 PEM; never logged
    books_ignored_mailboxes: str = ""  # comma-separated mailbox labels whose money is not ours
    # "label=entity,..." — mailbox → an entity from `settings.books_chart`; an
    # unlisted mailbox belongs to the chart's default entity.
    books_mailbox_entities: str = ""
    books_todoist_projects: str = ""  # "<entity>=<todoist project id>,..." for dues

    # Raphael's notes — the user's Obsidian vault (#514, spec
    # 2026-09-12-raphael-notes-design.md). The checkout sits beside the books
    # in the config volume core and worker share. Both keys empty ⇒ the vault
    # is not configured: nothing writes or indexes it and the daylog keeps
    # filing knowledge rows.
    notes_path: str = "/app/config/notes"
    notes_repo_url: str = ""
    notes_deploy_key: str = ""  # private ed25519 deploy key, PEM or base64 PEM; never logged
    # Maou's trading desk (spec 2026-09-12-maou-trading-desk-design.md): the
    # ansaar-data API serving the trading system's decisions. Either empty ⇒ off.
    ansaar_url: str = ""
    ansaar_service_secret: str = ""
    # Raphael's world watch (#676): the Quantamentry API. Either empty ⇒ off.
    quantamentry_url: str = ""
    quantamentry_api_key: str = ""

    @model_validator(mode="after")
    def _require_admin_credentials(self) -> "Settings":
        """admin_username/admin_password are required unless auth_disabled."""
        if not self.auth_disabled and not (self.admin_username and self.admin_password):
            raise ValueError(
                "admin_username and admin_password are required "
                "(set AEGIS_ADMIN_USERNAME / AEGIS_ADMIN_PASSWORD), "
                "unless AEGIS_AUTH_DISABLED=true"
            )
        return self

    @model_validator(mode="before")
    @classmethod
    def _parse_cors_allowed_origins(cls, data: Any) -> Any:
        """Parse comma-separated cors_allowed_origins into a list."""
        if isinstance(data, dict) and "cors_allowed_origins" in data:
            origins = data["cors_allowed_origins"]
            if isinstance(origins, str):
                data["cors_allowed_origins"] = [
                    s.strip() for s in origins.split(",") if s.strip()
                ]
        return data

