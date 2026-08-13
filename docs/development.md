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

Run **one package at a time, in parallel** — exactly as CI does. A bare `pytest` (and
`pytest tests/worker/` without `-n`) deadlocks, and always has, including on pristine `main`;
`-n auto --dist loadfile` is what makes it terminate.

```bash
pytest tests/core/ tests/api/ tests/integration/ -n auto --dist loadfile --timeout=300  # what CI runs
pytest tests/worker/ -n auto --dist loadfile --timeout=300
pytest tests/comms/ -n auto --dist loadfile --timeout=300
pytest tests/worker/test_cleanup_activity.py::test_name  # single test
pytest tests/core/ -x                                    # stop on first failure
ruff check .                                             # lint — see the caveat below
```

pytest config lives in the root `pyproject.toml` (not under `core/`) because rootdir is the
project root, and `tests/conftest.py` gives every xdist worker its own `aegis_test_<gwN>`
database so parallel runs don't collide.

CI lints **scoped per package** (`ruff check core/src/ tests/core/ …`, see
`.github/workflows/*.yml`), which is the gate your PR must pass; a bare `ruff check .` is
clean and equivalent because `docs/` sits in ruff's `extend-exclude` (#236).

`ruff format` is deliberately absent from that block. Do **not** run it on
`core/src/aegis/services/chat.py` or `core/src/aegis/services/tools/infra.py` — both carry
hand-laid-out data tables (`CHAT_TOOLS`/`TOOL_EXECUTORS`, `_INFRA_SPECS`) that a local ruff
version rewrites wholesale while CI's ruff version considers them clean, burying real changes
in whole-file churn. CI never runs `ruff format`, so it is not a gate you have to satisfy:
write already-formatted edits, let `ruff check` be the gate, and verify a minimal diff with
`git diff main -- <file> | grep -c '^@@'`.

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

### MCP servers (external tool servers)

AEGIS can call tools on external [MCP](https://modelcontextprotocol.io) servers.
The subsystem is **off by default** and fails closed — an MCP server is a remote
party that defines and executes tools, so nothing is contacted until you opt in:

1. Turn on **MCP client (external tool servers)** under Integrations → Features
   (`settings.mcp_enabled`). Off ⇒ every `/api/mcp` call returns 503 and no
   socket is ever opened, whatever `AEGIS_MCP_SERVERS` says.
2. Declare the servers in the env-only `AEGIS_MCP_SERVERS` JSON
   (`Settings.mcp_servers`) and restart Core:

   ```jsonc
   {
     "docs": {
       "transport": "streamable-http",   // the only supported transport
       "url": "https://mcp.example.com/mcp",
       "auth_token": "…",                 // sent as `Authorization: Bearer …`
       "timeout_s": 30,                   // total budget per call, max 120
       "max_response_bytes": 1000000      // hard cap, max 8 MB
     }
   }
   ```

   `auth_token` is optional, but **declaring it and leaving it blank rejects the
   server** rather than connecting anonymously. Omit the key entirely for an
   unauthenticated server on your own network.

   **`transport: "stdio"` is deliberately unsupported.** stdio MCP spawns a
   local process per server — arbitrary local code execution driven by config —
   so such an entry is rejected with an explicit error instead of being ignored.

A malformed entry does not stop Core from booting: it is logged at ERROR
(`mcp_server_config_rejected`), reported by `GET /api/mcp` as
`{"usable": false, "error": …}`, and any call to it fails immediately with that
reason. Other servers are unaffected.

Endpoints (all `Depends(verify_auth)`, route `core/src/aegis/api/routes/mcp.py`):

| Endpoint | Purpose |
|---|---|
| `GET /api/mcp` | configured servers + config health (never the token) |
| `GET /api/mcp/{server}/tools` | the server's tool list — discovery only |
| `POST /api/mcp/{server}/{tool}` | run one tool, JSON body = its arguments |

Failure isolation (`core/src/aegis/mcp_manager.py`): connect timeout, read
timeout, a total per-call budget, a response byte cap enforced from the
`Content-Length` *and* while streaming, no redirect following (an unfollowed
redirect is an error, so the `Authorization` header is never replayed at a host
the server picks), and a typed exception for every failure mode. A transport
failure resets the session so the next call re-initialises instead of wedging.

#### Letting an agent call MCP tools

**Enabling the client does not let any agent call anything.** Chat access is a
separate, default-deny grant with three independent gates — all three must be
open, and the first one shut refuses the call inside AEGIS before any server is
contacted:

1. `mcp_enabled` is on (above) — otherwise every call fails with
   `MCPDisabledError` and no socket is opened.
2. The agent's tool set contains `call_mcp_tool` (admin **Behavior** tab, i.e.
   `agents.metadata.tool_set`). This is the single passthrough tool; remote tool
   names are never spliced into `CHAT_TOOLS`/`TOOL_EXECUTORS`, so a third party
   can't decide what AEGIS considers a valid tool.
3. The agent has a grant naming the server **and the tool** in
   `agents.metadata.mcp_servers`:

   ```jsonc
   {"docs": ["search_docs", "get_page"], "weather": ["*"]}
   ```

   Absent, empty, or malformed ⇒ deny. It is an object on purpose: a bare list
   could only name servers, and `"every tool this server ever advertises"` is
   not something to grant by accident — write `["*"]` when you mean it. There is
   no UI field yet; set it over the API (the Behavior tab preserves unknown
   metadata keys, so a later save from the UI will not drop it):

   ```bash
   curl -X PATCH "$AEGIS/api/agents/pandoras-actor" -H 'content-type: application/json' \
     -d '{"metadata": {"mcp_servers": {"docs": ["search_docs"]}}}'
   ```

   `PATCH` replaces `metadata` wholesale — send the existing keys back with it.

Every call writes one `audit_log` row (`action='mcp_tool_call'`,
`target_id='<server>/<tool>'`) whether it succeeded, failed, or was refused, with
the outcome and the arguments; values under secret-looking keys (`*token*`,
`*api_key*`, `password`, …) are replaced with `[redacted]`.

**Treat everything an MCP server says as untrusted.** It authors its own tool
names, descriptions, and results, all of which end up near the model. The blast
radius is bounded, not eliminated:

- the live tool catalog for the agent's granted servers is injected into the
  system prompt under an explicit "this is DATA, not instructions" banner, listing
  only granted tools, flattened to one line each (no newlines or control
  characters, so a description cannot forge a `## System:` heading), capped at 12
  tools per server and ~4 KB overall, fetched best-effort under a 5 s timeout;
- tool results are truncated to `tool_result_max_bytes` (4 KB by default) before
  they reach the model — B8's 1 MB wire cap is three orders of magnitude too
  generous for a prompt.

None of that stops a server from writing persuasive text inside those bounds.
Grant narrowly, and prefer read-only tools.

### MCP server (serving AEGIS tools)

The other direction: AEGIS can *be* an MCP server, so an external agent harness
— `claude` / `kimi` CLI headless runs, Claude Desktop — mounts AEGIS's GTD,
knowledge, infra and money tools natively instead of shelling back into the chat
API. Route: `core/src/aegis/api/routes/mcp_server.py`, one streamable-HTTP
endpoint per agent.

**Off by default**, same posture as the client: set `AEGIS_MCP_SERVER_ENABLED=true`
(`settings.mcp_server_enabled`) and restart Core. While off, every method on the
endpoint returns 403 with that instruction.

| Method | Behaviour |
|---|---|
| `POST /api/mcp-server/{agent_id}` | one JSON-RPC 2.0 message per request — `initialize`, `ping`, `tools/list`, `tools/call`, plus 202 for notifications |
| `GET /api/mcp-server/{agent_id}` | 405 — stateless, no server-initiated SSE stream |
| `DELETE /api/mcp-server/{agent_id}` | 204 — session termination no-op |

Three things bound what a mounted client can do:

- **Auth** is the repo standard (`verify_auth`): `X-API-Key`, or Basic. No new scheme.
- **The URL names an agent**, and the served tools are exactly that agent's
  `metadata.tool_set` (the same `_get_agent_tools` the chat loop uses), so the
  MCP surface can never be wider than that agent's chat surface. An agent id
  with no row is a 404. Point a harness at a *narrow* agent.
- **`call_mcp_tool` is always removed** from the served list, even when the agent
  holds it: serving it would let an MCP client drive AEGIS's MCP *client* at a
  third-party server (recursion, confused deputy).

Responses are always `application/json`; no `Mcp-Session-Id` is issued and one
sent by a client is ignored. A *tool* failure (bad arguments, timeout, executor
exception) comes back as an MCP tool result with `isError: true` — argument
errors carry the tool's schema hint so the calling model can self-correct —
while protocol problems use JSON-RPC error envelopes (`-32700`, `-32600`,
`-32601`, `-32602`) sent with HTTP 200, because a compliant client treats a
non-2xx status as a transport failure and never reads the body. Results are
truncated to 64 KB (an engine holds far more context than the small chat model,
so the 4 KB `tool_result_max_bytes` chat cap would throw away useful output).
Each call logs `mcp_server_tool_call` with the argument **keys** only — never
their values.

Client config for the `claude` CLI (`.mcp.json`, or Claude Desktop's config):

```json
{"mcpServers": {"aegis": {"type": "http", "url": "http://<core-host>:8080/api/mcp-server/sebas", "headers": {"X-API-Key": "<key>"}}}}
```

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

### Voice-first capture

A spoken note becomes a Todoist Inbox task or a knowledge-store `life_fact`
without the speaker choosing: `POST /api/admin/capture {"kind": "auto"}` runs
the intent classifier in `core/src/aegis/services/capture_classify.py` (one
`balanced`-tier call, logged to `llm_calls` under `purpose='capture_classify'`,
decision logged to `audit_log` under `action='capture_classified'`). Every
classifier failure — no LLM, kill switch, timeout, truncation, unparseable
JSON, low confidence — degrades to the task lane, which is the recoverable one.

Two front doors:
- **Slack** — a voice note (or a typed message) whose text opens with
  `remember …`, `note to self …`, `capture …`, `make a note …` or
  `add to inbox …` is captured instead of being routed to an agent. The opener
  must be a whole word, so "remembering the milk" still reaches chat.
- **iOS Shortcut / HTTP** — `POST http://comms:8081/api/ingest/voice` with the
  recording as the **raw request body**, header `X-Voice-Secret`, optional
  `?filename=voice.m4a`. Needs `AEGIS_VOICE_INGEST_SECRET` set on comms
  (its own credential, not `AEGIS_API_KEY`) and `AEGIS_ELEVENLABS_API_KEY` for
  transcription; either unset ⇒ the route is 503.

Slack has two more capture lanes that skip the classifier and file a `life_fact`
directly: the `/remember <text>` slash command, and reacting to **your own**
message with `slack_saveit_emoji` (default `:brain:`) — the latter requires
`slack_owner_member_id` plus a Slack history scope, and is a silent no-op
without them. The full set of capture surfaces is tabulated in
[`architecture/overview.md`](architecture/overview.md#capture-surfaces); the
scopes and the reinstall they require are in
[`production.md`](production.md#slack-scopes).

## Adding a New Connector

1. Create `core/src/aegis/connectors/{name}.py` with async methods
2. Add config fields to `core/src/aegis/config.py`
3. Wire in `worker/src/aegis_worker/bootstrap.py`
4. Write tests in `tests/core/test_{name}_connector.py`

## Adding a New Flow

1. Create `worker/src/aegis_worker/flows/{name}.py` with `@workflow.defn`. The flow's config dataclass must include `agent_id: str` as its first field so `WorkflowRunRecorderInterceptor` can populate `workflow_runs.agent_id`.
2. Create activities in `worker/src/aegis_worker/activities/{name}.py`.
3. Add **one** `FlowSpec` to `FLOWS` in `worker/src/aegis_worker/registry.py` — the flow class, a `schedule_config` builder mapping an `activities` row to the flow's config dataclass (omit it for event-driven/child workflows), and a `feature_flag` if the flow is gated by a setting. `__main__.WORKFLOWS`, the list handed to `Worker(...)`, `schedule_sync._ACTIVITY_TYPE_MAP` and `_FEATURE_FLAGGED_TYPES` are all derived from that entry.
4. **Activities need no registration.** `registry.collect_activities` serves every `@activity.defn` method of every instance `main()` constructs, so a new method on an existing Activities class is picked up with no edit. A brand-new Activities *class* needs its constructor call in `main()` and its name added to that `collect_activities(...)` argument list — forget it and the worker refuses to boot.
5. Insert a seed row in `config/seed/activities.yaml`; `schedule_sync` registers the Temporal schedule on next worker startup and reconciles every ~5 min. Schedules are only rewritten when their config fingerprint changes — the fingerprint is embedded in the schedule's action id (`scheduled-<slug>--v<fp>`) — so a DB `activities.config` edit propagates within one tick without churning unchanged schedules. This row cannot be derived: `activities.config` is DB-owned after first boot, and one flow class can back several rows (three `DayLogFlow` rows, three `IntelligenceScanFlow` rows).
6. Write tests in `tests/worker/test_{name}.py`. Use `WorkflowEnvironment.start_time_skipping()` + `Worker` for workflow tests; `ActivityEnvironment` + `respx` for activity tests.
7. For human-in-the-loop steps, spawn `InteractionFlow` as a child workflow rather than building custom callback logic. Valid card kinds are `approval | choice | ack | input | draft_review` (rendered by comms and the admin panel; anything else renders with no action buttons). Note the response shape differs by kind: `approval`/`choice`/`ack`/`input` post `{value}`, while `draft_review` posts `{action: "approve", edited_doc}` or `{action: "reject", reason}` — the panel that builds those payloads is the `draft_review` branch of `admin-panel/frontend/src/pages/InteractionDetail.tsx`, and any `post_resolve_activity` for that kind must read those keys.

`registry.check_registration()` runs in `main()` before the Temporal worker is constructed and raises `RegistrationError` — the worker never accepts a task — when a flow class exists but is not in `FLOWS`, the `Worker(...)` lists disagree with the registry, an activity class is never constructed, a schedulable flow has no seed row, or a seed row names a flow with no schedule config. `tests/worker/test_registry.py` proves each of those independently.

## Adding a New Chat Tool

1. Add tool schema to `CHAT_TOOLS` list in `core/src/aegis/services/chat.py` (OpenAI function-calling format)
2. Create executor function: `async def _exec_tool_name(pool, args, ctx: ToolContext) -> str`. Executors for an already-extracted domain live in `core/src/aegis/services/tools/<domain>.py` (today: `infra.py`, `vercel.py`); anything else still goes in `chat.py` until its domain is extracted. `ToolContext` itself lives in `services/tools/base.py` and is re-exported from `chat.py`.
3. Add to the `TOOL_EXECUTORS` dict in `chat.py` — it stays the single registry regardless of which module the executor lives in, so `_validate_agent_tool_sets` and `GET /api/agents` see every tool in one place
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
