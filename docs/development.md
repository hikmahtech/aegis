# Local Development

## Prerequisites

- Python 3.12+
- Docker + Docker Compose
- Node.js 18+ (for admin panel)

## Quick Start

```bash
# 1. Clone and setup
python -m venv .venv && source .venv/bin/activate
pip install -e "core[dev]" -e "worker[dev]" -e "comms[dev]"

# 2. Start infrastructure
docker compose up -d postgres temporal temporal-ui

# 3. Start Core API (runs migrations + serves admin panel)
python -m aegis

# 4. Start Worker (registers schedules, runs flows)
python -m aegis_worker

# 5. Start Comms bot (Slack Socket Mode — needs Slack tokens in config/.env)
python -m aegis_comms
```

## Docker Compose (full stack)

```bash
# Build all images
docker compose build

# Start everything
docker compose up -d

# Check status
docker compose ps

# View logs
docker compose logs core --tail 50 -f
docker compose logs worker --tail 50 -f
```

## Service Ports

| Service | Local Dev | Docker |
|---------|-----------|--------|
| Core API | 8080 (or 8090 if 8080 busy) | 8080 |
| Comms | 8081 | 8081 |
| Postgres | 25432 | 25432 |
| Redis | 26379 | 26379 |
| Temporal | 7233 | 7233 |
| Temporal UI | 8233 | 8233 |

## Testing

```bash
pytest                    # full suite (asyncio_mode=auto)
pytest tests/core/        # core only
pytest tests/worker/      # worker only
pytest tests/comms/       # comms only
pytest -x                 # stop on first failure
ruff check .              # lint
ruff format .             # format
```

pytest config lives in the root `pyproject.toml` (not under `core/`) because rootdir is the project root.

## Configuration

Copy `config/.env.example` to `config/.env` and fill in secrets:

```bash
cp config/.env.example config/.env
# Edit config/.env with your tokens
```

Key settings:
- `AEGIS_DATABASE_URL` — PostgreSQL connection
- `AEGIS_ADMIN_USERNAME` + `AEGIS_ADMIN_PASSWORD` — admin credentials (required unless `AEGIS_AUTH_DISABLED=true`; see the auth section below)
- `AEGIS_SECRET_KEY` — Fernet key encrypting DB-stored secrets (integration tokens, infra credentials, API keys); unset = plaintext-with-flag, fine for local dev only
- `AEGIS_LITELLM_URL` + `AEGIS_LITELLM_API_KEY` — LLM gateway (or configure the backend from the admin **Models & Providers** page)
- `AEGIS_COMMS_URL` — how Core reaches the comms delivery server (e.g. `http://localhost:8081`)
- `AEGIS_SLACK_BOT_TOKEN` + `AEGIS_SLACK_APP_TOKEN` — Slack (comms); can also be set from the admin UI (stored encrypted in the DB)
- `AEGIS_GITHUB_TOKEN` — GitHub API
- `AEGIS_GMAIL_ACCOUNTS` — Gmail OAuth (format: `name:email,name:email`)

Most integration secrets (Todoist, Slack, Postiz, finance provider keys, infra/cloud
credentials, API keys) are entered in the admin UI and stored encrypted in the DB —
env vars exist as bootstrap/fallback for local dev, not as the primary store.

### Agent personalities

Personas live in the `agent_personalities` table — four markdown "kinds" per agent
(`soul` identity, `agents` operational boundaries, `user` user context, `memory`
long-term memory) — and are edited from the admin panel's agent detail page
(GET/PUT `/api/admin/agents/{id}/personality`; service:
`core/src/aegis/services/personalities.py`).

The files under `personalities/<agent>/{SOUL,AGENTS,USER,MEMORY}.md` are
**import-on-first-boot starter examples only**: on Core startup the seed loader
imports each file into its kind *only when that kind has no DB row yet*. After
that the DB owns the content — editing the files has no effect on an existing
install. `AEGIS_PERSONALITY_DIR` overrides where the starter files are read from.

### Agent behavior (tags, tools, routing)

Behavior is data, not code (issue #36). An agent's `capabilities` (JSONB) holds
its behavior tags — closed vocab `gtd` / `finance` / `research` / `infra` from
`core/src/aegis/agent_tags.py` — and `metadata` (JSONB) holds routing knobs:
`tool_set`, `intent_keywords`, `intent_description`, `mention_aliases`,
`async_dispatch`, `knowledge_domains`, `voice_lines`. Flows/routes resolve *who
does X* by tag (`services/agents.py::resolve_tag` in core, the
`AgentRegistryActivities.resolve_agents` activity in the worker), never by a
literal id.

Edit all of this from the admin panel's agent detail **Behavior** tab
(`PATCH /api/agents/{id}`; the tag/tool vocab comes from
`GET /api/agents/meta/options`). `seed.py` treats `capabilities`/`metadata` as
**DB-owned once non-empty** — `config/seed/agents.yaml` only seeds first boot and
merges *new* metadata keys on upgrade, so UI edits survive restarts. Note: a new
capability tag added to the yaml will **not** retroactively apply to an existing
deployment — tick it in the Behavior tab once.

**Adding a new agent:** create it (Agents page or `POST /api/agents`), write its
persona, then check the capability tag(s) that describe its role and pick its tool
set on the Behavior tab. No code changes — every tag-driven feature (GTD reviews,
briefings, money processing, alerts, Slack @-addressing, chat routing) follows the
tags automatically.

### Ingestion channels

Channels (`email` / `rss` / `raindrop` ingestion sources) live in the `channels`
table and are managed from the admin panel's **Channels** page (CRUD API:
`/api/admin/channels`, route: `core/src/aegis/api/routes/channels.py`).
`config/seed/channels.yaml` follows the same import-on-first-boot pattern as
personalities: the seed loader inserts a yaml row only when no `(kind, identifier)`
row exists yet, and never updates or deletes existing rows — after first boot the
DB owns the channels, so UI edits, deactivations, and operator-added channels
(e.g. a new Gmail account) survive Core restarts. Email channels additionally need
the account authorized via the Google accounts re-auth flow (Flows page).

### Authentication (required for non-proxied deployments)

If your deployment is **NOT** behind an authenticating proxy (Cloudflare Access, an
OAuth2 proxy, Tailscale-only access, etc.), basic auth **MUST stay on** — it is the only
thing standing between the internet/LAN and full admin access to your data, credentials
and infrastructure registry. Keep `AEGIS_AUTH_DISABLED` unset (or `false`) and set both:

```bash
# config/.env
AEGIS_ADMIN_USERNAME=<pick-a-username>
AEGIS_ADMIN_PASSWORD=<long-random-password>   # e.g. `openssl rand -base64 24`
```

There are no defaults — Core refuses to boot when they're unset (unless
`AEGIS_AUTH_DISABLED=true`), precisely so an unprotected instance never ships. The admin
SPA prompts for these credentials; API clients can send them as HTTP basic auth or use
an API key via the `X-API-Key` header (generate one from the admin **Integrations**
page, or set `AEGIS_API_KEY` in the env).

### Disabling built-in auth (authenticating-proxy deployments ONLY)

`AEGIS_AUTH_DISABLED=true` turns off the API's basic-auth / `X-API-Key` checks and makes
`AEGIS_ADMIN_USERNAME` / `AEGIS_ADMIN_PASSWORD` optional; the admin SPA detects this and
skips its login prompt. It exists for deployments where the public hostname is already
fronted by an authenticating proxy (e.g. Cloudflare Access with email verification), so a
second basic-auth prompt is redundant. Webhook HMAC verification is unaffected.
**Warning:** with this flag set, *anyone who can reach port 8080* (e.g. any device on the
LAN, or the internet if the port is exposed) has full admin access. Only enable it when the
port is reachable exclusively through the authenticating proxy — no direct port exposure.

Because that mistake is invisible from the outside (the API just answers), an auth-disabled
deployment announces itself in two places:

- **Boot log:** a `CRITICAL` `auth_disabled_active` event on every Core startup
  (`docker service logs aegis_core | grep auth_disabled_active`).
- **Admin UI:** a red *"Authentication is disabled"* banner on the **System monitoring**
  page, driven by `auth_mode` in `GET /api/admin/system/status`
  (`disabled` | `basic` | `api_key` | `basic+api_key`).

To confirm auth is actually on, an anonymous request must be rejected:

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://<host>:8080/api/agents   # expect 401
```

## Admin Panel Development

```bash
cd admin-panel/frontend
npm install
npm run dev     # Dev server on port 5173
npm run build   # Build for production (served by Core)
```

## Content Extraction Setup

The worker's content extraction pipeline requires system-level dependencies for PDF, image, and media processing:

```bash
# macOS
brew install tesseract poppler ffmpeg

# Ubuntu/Debian
apt-get install tesseract-ocr poppler-utils ffmpeg
```

Playwright (Tier 2 article extraction) requires a browser install:

```bash
playwright install chromium
```

**Optional:** Media transcription (fallback when a URL has no captions) uses ElevenLabs Scribe — a hosted vendor (NOT the LiteLLM proxy, which serves text LLMs only). Set `AEGIS_ELEVENLABS_API_KEY` to enable. YouTube captions work without it.

Kill switches:
- `AEGIS_CONTENT_EXTRACTION_ENABLED=false` — disables all content extraction
- `AEGIS_ELEVENLABS_API_KEY=""` (empty) — disables media transcription only
- `AEGIS_TTS_ENABLED=false` (default) — disables outbound per-persona voice notes

## Adding a New Connector

1. Create `core/src/aegis/connectors/{name}.py` with async methods
2. Add config fields to `core/src/aegis/config.py`
3. Wire in `worker/src/aegis_worker/bootstrap.py`
4. Write tests in `tests/core/test_{name}_connector.py`

## Adding a New Flow

1. Create `worker/src/aegis_worker/flows/{name}.py` with `@workflow.defn`. The flow's config dataclass must include `agent_id: str` as its first field so `WorkflowRunRecorderInterceptor` can populate `workflow_runs.agent_id`.
2. Create activities in `worker/src/aegis_worker/activities/{name}.py`.
3. Register in `worker/src/aegis_worker/__main__.py` (workflows + activities lists) — both lists are explicit; nothing is auto-discovered.
4. If scheduled, add a `_ACTIVITY_TYPE_MAP` entry in `worker/src/aegis_worker/schedule_sync.py` keyed by the PascalCase class name (e.g. `"CleanupFlow"`).
5. Insert a seed row in `config/seed/activities.yaml`; `schedule_sync` registers the Temporal schedule on next worker startup and reconciles every ~5 min. Schedules are only rewritten when their config fingerprint changes — the fingerprint is embedded in the schedule's action id (`scheduled-<slug>--v<fp>`) — so a DB `activities.config` edit propagates within one tick without churning unchanged schedules.
6. Write tests in `tests/worker/test_{name}.py`. Use `WorkflowEnvironment.start_time_skipping()` + `Worker` for workflow tests; `ActivityEnvironment` + `respx` for activity tests.
7. For human-in-the-loop steps, spawn `InteractionFlow` as a child workflow rather than building custom callback logic. Valid card kinds are `approval | choice | ack | input | draft_review` (rendered by comms and the admin panel; anything else renders with no action buttons).

## Adding a New Chat Tool

1. Add tool schema to `CHAT_TOOLS` list in `core/src/aegis/services/chat.py` (OpenAI function-calling format)
2. Create executor function: `async def _exec_tool_name(pool, args, ctx: ToolContext) -> str`
3. Add to `TOOL_EXECUTORS` dict
4. Grant it to agents via their `metadata.tool_set` — set it on the admin **Behavior** tab (runtime source of truth) and/or in `config/seed/agents.yaml`. The shipped `AGENT_TOOL_SETS` dict is now only a seed-time default for the four example agents; an agent's DB `metadata.tool_set` overrides it, and an unconfigured agent falls back to a small read-only `_FALLBACK_TOOL_SET` (not Sebas's full surface). `_validate_agent_tool_sets` refuses to boot on a tool name with no executor, and Core additionally warns at startup on any DB `metadata.tool_set` entry that references a missing executor.
5. If the tool needs new connectors on `ToolContext`, add the field and wire it in `send_message()`
6. Write tests in `tests/core/test_{tool_name}_tool.py`
7. If the tool can legitimately run longer than `tool_timeout_seconds` (default 30s), add an entry to `_TOOL_TIMEOUT_OVERRIDES` in `chat.py` — otherwise the executor cancels it mid-flight and the model retries, orphaning whatever the tool started (e.g. `aegis_self_diagnose` gets its full remote coding-run budget there).

## Adding Intelligence Topics

The topics `IntelligenceScanFlow` (Raphael) scans are set **per source** in the flow config: the `topics` list on each `intelligence-scan-*` row in `config/seed/activities.yaml`, also editable live at `/admin/flows`. Change the config and `schedule_sync` propagates it without a redeploy.

> The `track_topic` chat tool writes a separate `settings.intelligence_topics` key that the scan flow does **not** currently read — it has no effect on scanning yet.

## Todoist (local dev)

For local development against the real Todoist API:

1. Personal API key in `config/.env`:
   ```
   AEGIS_TODOIST_API_KEY=<your key>
   AEGIS_TODOIST_WEBHOOK_SECRET=<any string for local — webhooks won't reach localhost anyway>
   ```
2. Boot Core + worker as usual. `TodoistSyncFlow` will fire every 5 minutes against your real Todoist account.
3. For webhook testing without exposing localhost, hand-craft an HMAC-signed request:
   ```bash
   SECRET=<your secret>
   BODY='{"event_name":"item:added","event_data":{"id":1}}'
   SIG=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$SECRET" | awk '{print $2}')
   curl -X POST http://localhost:8080/api/webhooks/todoist \
     -H "X-Todoist-Hmac-SHA256: $SIG" -d "$BODY"
   ```
4. To reset the projection between dev runs:
   ```sql
   TRUNCATE todoist_tasks, todoist_projects, todoist_labels, todoist_webhook_events, todoist_outbox;
   UPDATE todoist_sync_state SET sync_token = '*' WHERE key = 'main';
   DELETE FROM settings WHERE key = 'todoist_managed_project_ids';
   ```
   Then the next sync fires bootstrap + full sync again.

### Phase 2 — local dev

The capture helper reads two `settings` rows: `todoist_capture_enabled` (boolean) and `todoist_managed_project_ids` (JSONB dict with at least `inbox` key). Both are populated by the baseline migration + the Todoist bootstrap.

To exercise the capture path locally without going through a full ingest flow:

```python
import asyncio, os
from aegis.db import create_pool
from aegis.connectors.todoist import TodoistConnector
from aegis_worker.activities.capture import CaptureActivities

async def main():
    pool = await create_pool("postgresql://aegis:aegis_dev@localhost:25432/aegis")
    conn = TodoistConnector(api_key=os.environ["AEGIS_TODOIST_API_KEY"])
    act = CaptureActivities(db_pool=pool, connector=conn)
    ref = await act.capture_to_inbox(
        source_tag="#manual",
        external_id="local-test-1",
        title="Phase 2 local test",
        description="Triggered from a script",
    )
    print("Captured ref:", ref)

asyncio.run(main())
```

To reset capture state between runs:

```sql
TRUNCATE todoist_capture_idempotency;
UPDATE settings SET value = 'true'::jsonb WHERE key = 'todoist_capture_enabled';
```
