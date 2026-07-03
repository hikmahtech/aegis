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
    # v3 model tiers — match config/models.yaml
    model_fast: str = "gemma4:e2b"  # quick replies, low latency
    model_balanced: str = "gpt-oss:20b"  # default chat + most flows (qwen3:14b retired)
    model_smart: str = "gpt-oss:20b"  # long-context synthesis, Raphael (qwen3:32b retired)
    # Active-work guard: lookback window for open-PR / recent-push / in-flight signals.
    active_work_lookback_hours: int = 48
    # Path to config/models.yaml — loaded at startup by app.lifespan.
    # Override via AEGIS_MODELS_YAML_PATH if running from a non-standard layout.
    models_yaml_path: str = "config/models.yaml"

    # Temporal. temporal_ui_url is just the "open in Temporal UI" link target.
    temporal_host: str = "localhost:7233"
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

    # Timezone
    timezone: str = "Asia/Kolkata"

    # Connectors
    github_token: str = ""
    vercel_token: str = ""
    vercel_team_id: str = ""
    sentry_url: str = ""
    sentry_token: str = ""
    sentry_org: str = ""
    sentry_projects: str = ""  # comma-separated Sentry project IDs; empty = all
    miniflux_url: str = ""
    miniflux_api_key: str = ""
    searxng_url: str = "http://localhost:8888"
    gmail_accounts: str = ""  # "name1:email1,name2:email2"
    gmail_credentials_file: str = "config/google_credentials.json"
    gmail_token_dir: str = "config/"
    # Remote script / coding agents — ENV FALLBACK ONLY. The preferred way to
    # configure the coding host is the admin Infra page: an infra registry row
    # with a `coding` block (enabled=true) supplies the SSH identity (host,
    # user, port, encrypted key — materialized to a temp file per call, no key
    # file on any volume) plus repo_base/engines/routing/tmux/kimi-host. These
    # env settings apply only while no such row exists. See docs/infrastructure.md.
    remote_script_host: str = ""
    remote_script_user: str = "deploy"
    remote_script_key_file: str = "~/.ssh/id_ed25519"
    remote_script_known_hosts: str | None = None  # if set, passed to ssh via UserKnownHostsFile
    remote_script_repo_base: str = ""
    # Preferred host for the kimi lifecycle (e.g. "buildhost"). Empty ⇒ kimi runs on
    # remote_script_host with today's detached nohup. When set AND reachable,
    # runs are wrapped in a tmux session for live attach; unreachable ⇒ falls
    # back to remote_script_host. Hostname comes from env only (no committed default).
    remote_script_kimi_host: str = ""
    remote_script_tmux_session: str = "remote"
    remote_script_tmux_window_cap: int = 10
    # Comma-separated GitHub orgs whose repos must be worked on with the claude
    # CLI on remote_script_host (the base host), NOT kimi — that host's claude
    # login belongs to the org, so org-repo work runs under the org's account.
    # Matched case-insensitively against the org part of a resource's
    # metadata.github_repo. Empty (default) ⇒ everything uses kimi.
    remote_script_claude_orgs: str = ""
    # Todoist (GTD task management)
    todoist_api_key: str = ""
    todoist_webhook_secret: str = ""
    # Social publishing — BYO X (Twitter) OAuth 2.0 app (developer.x.com), same
    # rationale as the Google client: the maintainer's app can't be committed
    # and wouldn't authorize forkers. Editable from the admin Integrations page.
    x_client_id: str = ""
    x_client_secret: str = ""
    # Kimi CLI — the remote coding-CLI used by alert_investigation for auto-fix proposals.
    kimi_cli_binary_path: str = "/usr/local/bin/kimi"
    # Claude CLI on remote_script_host — used instead of kimi for repos whose
    # GitHub org is listed in remote_script_claude_orgs.
    claude_cli_binary_path: str = "/usr/local/bin/claude"
    # CLAUDE_CONFIG_DIR for the claude CLI when it runs as the kimi fallback on a
    # NON-org repo. The default ~/.claude login belongs to an org (acme);
    # the fallback runs under the personal account instead. Empty ⇒ default config.
    claude_personal_config_dir: str = ""
    # AEGIS self-healing — workspace-relative path (under
    # `remote_script_repo_base`) of AEGIS's own checkout. Pandora's
    # `aegis_self_diagnose` tool runs kimi against this checkout to
    # investigate / propose fixes to AEGIS itself. The checkout is part of
    # the fixed workspace hierarchy maintained by WorkspaceRepoSyncFlow.
    aegis_self_repo_path: str = "aegis"
    # Per-alert runbook directory — baked into the worker image at /app/runbooks.
    runbooks_dir: str = "/app/runbooks"

    # Knowledge subsystem (native pgvector — no external service).
    # embedding_model must be served by litellm_url's /embeddings; its vector dim
    # must match the knowledge_chunks.embedding column (768 for nomic-embed-text).
    embedding_model: str = "nomic-embed-text"
    knowledge_ui_url: str = ""  # admin-panel link target (now the in-app /admin/knowledge page)

    # Web finance data (FinanceConnector) — provider-agnostic quotes for Maou's
    # market tools. Built-in keyless providers: "yahoo" (default) and "stooq".
    # finance_api_key is unused by the built-ins; it's the seam for future
    # API-key providers. finance_indices drives get_market_overview.
    finance_provider: str = "yahoo"
    finance_api_key: str = ""
    finance_indices: str = "^GSPC,^IXIC,^NSEI"

    # Chat tool-calling
    tool_calling_enabled: bool = True
    tool_max_iterations: int = 5
    tool_result_max_bytes: int = 4096
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
    github_webhook_secret: str = ""  # X-Hub-Signature-256
    sentry_webhook_secret: str = ""  # Sentry's HMAC header

    # MCP
    mcp_servers: dict = {}

    # Worker -> Core API
    core_api_url: str = "http://localhost:8080"

    # Content extraction
    content_extraction_enabled: bool = True
    raindrop_api_token: str = ""

    # ElevenLabs (separate vendor — NOT the LiteLLM proxy). Empty key = kill
    # switch for media transcription.
    elevenlabs_api_key: str = ""
    elevenlabs_stt_model: str = "scribe_v1"

    # Outbound per-persona TTS voice notes (opt-in, off by default). Worker
    # flows that explicitly call send_voice still no-op unless this is true.
    tts_enabled: bool = False

    # AEGIS admin UI base URL (used for reauth links in chat cards)
    aegis_ui_url: str = Field(default="", validation_alias="AEGIS_UI_URL")

    # Knowledge auto-extraction
    chat_auto_extract_enabled: bool = True

    # v3 seed directory (YAML files for agents, channels, resources, activities)
    seed_dir: str = "./config/seed"

    # Homelab Guardian
    # NOTE: URL/key fields default to "" so the app can boot with
    # homelab_enabled=False. Bootstrap instantiates HomelabConnector only
    # when homelab_enabled=True AND required URLs are non-empty.
    homelab_enabled: bool = False
    homelab_docker_context: str = ""
    homelab_nfs_base_path: str = "/mnt/General/NFS/swarm-backups"
    homelab_dagster_graphql_url: str = ""
    homelab_traefik_api_url: str = ""
    homelab_restore_drill_host: str = ""
    homelab_restore_drill_disk_min_gb: int = 50
    # NoDecode: skip pydantic-settings' JSON decoding so the raw env/dotenv
    # string reaches _parse_homelab_domains, which splits it on commas.
    homelab_public_domains: Annotated[list[str], NoDecode] = Field(default_factory=list)
    homelab_drift_sustained_minutes: int = 30

    # Money Hygiene (Maou)
    money_hygiene_enabled: bool = False
    money_hygiene_haiku_batch: int = 10
    money_hygiene_inr_fallback_rates: dict[str, float] = Field(
        default_factory=lambda: {"USD": 84.5, "EUR": 92.0, "GBP": 108.0, "SGD": 63.0}
    )
    money_hygiene_cancellation_threshold_multiplier: float = 2.0
    # NoDecode: see homelab_public_domains — raw string is comma-split by
    # _parse_money_hygiene_thresholds.
    money_hygiene_alert_thresholds_days: Annotated[list[int], NoDecode] = Field(
        default_factory=lambda: [30, 14, 7, 0]
    )

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
    def _parse_homelab_domains(cls, data: Any) -> Any:
        """Parse comma-separated homelab_public_domains into list."""
        if isinstance(data, dict) and "homelab_public_domains" in data:
            domains = data["homelab_public_domains"]
            if isinstance(domains, str):
                data["homelab_public_domains"] = [
                    s.strip() for s in domains.split(",") if s.strip()
                ]
        return data

    @model_validator(mode="before")
    @classmethod
    def _parse_money_hygiene_thresholds(cls, data: Any) -> Any:
        """Parse comma-separated money_hygiene_alert_thresholds_days into list[int]."""
        if isinstance(data, dict) and "money_hygiene_alert_thresholds_days" in data:
            value = data["money_hygiene_alert_thresholds_days"]
            if isinstance(value, str):
                data["money_hygiene_alert_thresholds_days"] = [
                    int(s.strip()) for s in value.split(",") if s.strip()
                ]
        return data
