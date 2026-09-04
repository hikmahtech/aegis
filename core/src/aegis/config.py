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
    model_fast: str = "gemma4:e2b"  # quick replies, low latency
    model_balanced: str = "kimi-k2.5"  # default chat + most flows
    model_smart: str = "claude-sonnet-5"  # long-context synthesis, Raphael
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

    # Connectors
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
    # Self-hosted Postiz instance — an alternate posting backend that holds the
    # platform OAuth itself; aegis mirrors its channels and posts through its
    # public API instead of doing native per-platform OAuth (mixed mode: native
    # X above keeps working for accounts connected via /connect).
    postiz_url: str = ""
    postiz_api_key: str = ""
    # Browser-facing Postiz URL for admin-UI links — distinct from postiz_url,
    # which may be an internal-only address the browser can't reach.
    postiz_public_url: str = ""
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
    # Prometheus/Alertmanager `cluster` label value that marks an alert as an
    # infra/swarm alert (routed straight to infra-gitops, skipping the LLM
    # repo-match). Blank ⇒ the cluster-label fast path is off; alertname
    # matching (INFRA_ALERTNAMES) still classifies infra alerts. Set this to
    # your own cluster label to also route by cluster. Editable from the
    # admin Integrations page.
    infra_cluster: str = ""
    infra_heartbeat_ping_url: str = ""  # healthchecks.io dead-man URL ("" = off)
    slack_owner_member_id: str = ""  # Slack member id for escalation @-mentions ("" = no mention)
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
    # Bank / card-alert sender domains (comma-separated, case-insensitive
    # substring match). Deterministic guard in Money Hygiene that stops bank
    # statements / autopay reminders from minting fake recurring charges.
    # Empty = guard off. Editable from the admin Integrations page.
    bank_alert_senders: str = ""
    # Per-alert runbook directory — baked into the worker image at /app/runbooks.
    runbooks_dir: str = "/app/runbooks"
    # Swarm stack name AEGIS itself is deployed as. The System Monitoring page
    # filters `docker service ls` to this stack (com.docker.stack.namespace
    # label) so it shows AEGIS's own services, not every stack on the swarm.
    # Blank = no filter (show all services). Editable from the admin UI.
    aegis_stack_name: str = "aegis"
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
    github_webhook_secret: str = ""  # X-Hub-Signature-256
    sentry_webhook_secret: str = ""  # Sentry's HMAC header
    # /api/webhooks/alert has no vendor HMAC to verify (Alertmanager/Grafana
    # don't sign). Set this to require an X-Alert-Token header matching it;
    # empty = unauthenticated (legacy default — anyone who can reach the port
    # can mint alerts and spawn investigation flows).
    alert_webhook_secret: str = ""  # X-Alert-Token
    # /api/webhooks/life/{source} — signed push from phones/watches/home
    # automation. Empty = the endpoint rejects EVERYTHING (503). Never treat
    # an unset secret as "skip verification": this door writes into the
    # owner's personal data store.
    life_webhook_secret: str = ""  # X-Aegis-Signature + X-Aegis-Timestamp

    # MCP — client for EXTERNAL tool servers. Off by default: an MCP server is
    # a remote party that defines and executes tools, so the subsystem stays
    # closed until an operator explicitly opens it. Off = no server is ever
    # contacted, whatever mcp_servers says.
    mcp_enabled: bool = False
    # {"<name>": {"transport": "streamable-http", "url": "https://…/mcp",
    #             "auth_token": "…", "timeout_s": 30, "max_response_bytes": …}}
    # stdio is deliberately unsupported (it would spawn local processes).
    mcp_servers: dict = {}
    # MCP — SERVER side (api/routes/mcp_server.py): serve AEGIS's own chat tools
    # to external MCP clients (claude/kimi CLI, Claude Desktop) at
    # POST /api/mcp-server/{agent_id}. Off by default, same default-deny posture
    # as the client above — this door lets an outside harness run AEGIS tools.
    mcp_server_enabled: bool = False
    # Escape hatch for `mcp_server_enabled` + `auth_disabled` together. That
    # pair serves every agent's tools with NO credential: auth_disabled makes
    # verify_auth a no-op (correct only behind an authenticating proxy), while
    # this endpoint is mounted at a LAN/overlay URL that deliberately bypasses
    # that proxy so a headless CLI can reach it. The endpoint 403s on the
    # combination unless this is explicitly true.
    mcp_server_allow_unauthenticated: bool = False
    # Core's base URL **as reachable from the coding host** (e.g.
    # http://10.0.0.5:8080) — NOT the browser-facing one, which is typically
    # behind an authenticating proxy the CLI can't traverse. Used only to mount
    # AEGIS's tools into a claude-engine agent run (`RemoteScriptConnector`);
    # empty ⇒ runs launch with no AEGIS tools. An infra `coding.mcp_server_url`
    # overrides it. The run authenticates with `api_key`, so both must be set.
    mcp_server_external_url: str = ""
    # How long the GATED endpoint (`/api/mcp-server/{agent_id}/gated`) holds a
    # mutating tool call open waiting for the operator before telling the model
    # to retry. Deliberately well under the ~60s hard cap the claude CLI was
    # measured to impose on an MCP tool call (2.1.231 — MCP_TOOL_TIMEOUT does
    # not lift it), because the gate's contract is "retry and it executes",
    # which only works if OUR answer comes back before the CLI gives up.
    mcp_gate_wait_seconds: int = 40

    # Worker -> Core API
    core_api_url: str = "http://localhost:8080"

    # Content extraction
    content_extraction_enabled: bool = True
    raindrop_api_token: str = ""

    # Jira (JiraSyncFlow). Any of the three blank = the flow reports
    # `not_configured` and issues no request. Basic auth: the Atlassian ACCOUNT
    # EMAIL plus an API token from id.atlassian.com/manage-profile/security/
    # api-tokens — not a password, and not the login you use for SSO.
    jira_base_url: str = ""
    jira_email: str = ""
    jira_api_token: str = ""

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

    # AEGIS admin UI base URL (used for reauth links in chat cards)
    aegis_ui_url: str = Field(default="", validation_alias="AEGIS_UI_URL")

    # v3 seed directory (YAML files for agents, channels, resources, activities)
    seed_dir: str = "./config/seed"

    # Homelab Guardian (Docker Swarm drift + TLS cert radar). When enabled,
    # the worker builds a HomelabConnector; an empty docker_context relies on
    # the DOCKER_HOST env var inside the worker container.
    homelab_enabled: bool = False
    homelab_docker_context: str = ""
    # NoDecode: skip pydantic-settings' JSON decoding so the raw env/dotenv
    # string reaches _parse_homelab_domains, which splits it on commas.
    homelab_public_domains: Annotated[list[str], NoDecode] = Field(default_factory=list)

    # Money Hygiene (Maou)
    money_hygiene_enabled: bool = False
    # Currency all Money-Hygiene charges normalize to; drives the digest
    # symbol. A self-hoster sets this AND matching fx rates below.
    home_currency: str = "INR"
    # Fallback FX rates, foreign currency -> home_currency.
    money_hygiene_fx_rates: dict[str, float] = Field(
        default_factory=lambda: {"USD": 84.5, "EUR": 92.0, "GBP": 108.0, "SGD": 63.0}
    )

    # The books — Maou's hledger journal (spec 2026-09-05-maou-books-design.md §10).
    # books_repo_url empty ⇒ books disabled: money events are still indexed,
    # never posted. The three list-ish knobs are strings so the admin
    # Integrations page can set them (DB-configured, no redeploy).
    books_path: str = "/app/config/books"
    books_repo_url: str = ""
    books_deploy_key: str = ""  # private ed25519 deploy key, PEM or base64 PEM; never logged
    books_ignored_mailboxes: str = ""  # comma-separated mailbox labels whose money is not ours
    # "label=entity,..." — mailbox → personal|hikmah; an unlisted mailbox is personal.
    books_mailbox_entities: str = ""
    books_todoist_projects: str = ""  # "personal=<todoist project id>,hikmah=<id>" for dues

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
    def _parse_cors_allowed_origins(cls, data: Any) -> Any:
        """Parse comma-separated cors_allowed_origins into a list."""
        if isinstance(data, dict) and "cors_allowed_origins" in data:
            origins = data["cors_allowed_origins"]
            if isinstance(origins, str):
                data["cors_allowed_origins"] = [
                    s.strip() for s in origins.split(",") if s.strip()
                ]
        return data

