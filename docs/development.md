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
- tool results are truncated to `tool_result_max_bytes` (16 KB by default) before
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

**It also refuses to run unauthenticated.** `AEGIS_AUTH_DISABLED=true` makes
`verify_auth` a no-op, which is the documented posture for a deployment behind
an authenticating proxy (Cloudflare Access, an OAuth2 proxy) — but the URL an
agent run mounts is deliberately a LAN/overlay address that *bypasses* that
proxy, so the two together would serve every agent's tool set (`restart_service`,
`run_infra_script`, money and GTD writes) to anything that can open a socket to
Core. Enabled + `auth_disabled` is therefore a 403 naming both settings, unless
the operator accepts it explicitly with
`AEGIS_MCP_SERVER_ALLOW_UNAUTHENTICATED=true`. The fix in almost every case is
the other direction: unset `AEGIS_AUTH_DISABLED` and give the endpoint a key.

| Method | Behaviour |
|---|---|
| `POST /api/mcp-server/{agent_id}` | one JSON-RPC 2.0 message per request — `initialize`, `ping`, `tools/list`, `tools/call`, plus 202 for notifications |
| `POST /api/mcp-server/{agent_id}/gated` | identical, except a tool outside `_READ_ONLY_TOOLS` needs an operator approval first — see [Gated runs](#gated-runs-human-in-the-loop) |
| `GET /api/mcp-server/{agent_id}[/gated]` | 405 — stateless, no server-initiated SSE stream |
| `DELETE /api/mcp-server/{agent_id}[/gated]` | 204 — session termination no-op |

Three things bound what a mounted client can do:

- **Auth** is the repo standard (`verify_auth`): `X-API-Key`, or Basic. No new scheme.
- **The URL names an agent**, and the served tools are exactly that agent's
  `metadata.tool_set` (the same `_get_agent_tools` the chat loop uses), so the
  MCP surface can never be wider than that agent's chat surface. An agent id
  with no row is a 404. Point a harness at a *narrow* agent.
- **`_UNSERVED_TOOLS` is always removed** from the served list, even when the
  agent holds those tools. `call_mcp_tool` would let an MCP client drive AEGIS's
  MCP *client* at a third-party server (confused deputy). `dispatch_agent_run`,
  `aegis_self_diagnose` and `investigate_resource` each **start another CLI
  run** — which mounts this same endpoint with the same tool set, so serving
  them is unbounded recursion with no depth counter anywhere in the loop (the
  only brake is the coding host's tmux window cap, past which launches fall
  through to detached `nohup` and stop being bounded at all). The exclusion is
  applied where the served set is *derived*, so an excluded tool is neither
  listed nor callable — `tools/call` authorizes against that same list.

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

## Interactive API docs (`/docs`) are off by default

`/docs`, `/redoc` and `/openapi.json` are **not registered** unless you opt in:

```bash
AEGIS_EXPOSE_API_DOCS=true python -m aegis
```

FastAPI mounts those routes itself, so they never pick up the `verify_auth`
dependency that every `/api` router carries — leaving them on hands an anonymous
caller a complete map of every endpoint, parameter and schema (#305). Gating them
behind auth instead would be no protection at all in the common
`AEGIS_AUTH_DISABLED=true` topology, which is why this is an explicit switch
rather than something derived from the auth posture.

`tests/core/test_route_auth_coverage.py` asserts both directions: absent by
default, and present when the flag is on (a switch nobody can turn on gets
deleted).

That same file is the auth audit for **every** registered route, not just `/api`
ones. Being reachable anonymously is an explicit `_ALLOWLIST_*` entry — `/health`,
`/api/webhooks/*` (each verifies its own HMAC), and the SPA shell plus its
`/assets`. It previously skipped anything outside `/api`, which is how the docs
routes stayed anonymous while the test reported full coverage (#306). Note that
in a test environment `/health` is the *only* non-`/api` route registered, so one
test deliberately injects a route outside `/api` to prove the audit can still see
it — without that, narrowing the walk back again would break nothing visibly.

## Admin Panel Development

```bash
cd admin-panel/frontend
npm install
npm run dev     # Dev server on port 5173
npm run build   # Build for production (served by Core)
```

### PWA assets and the one rule that must not be broken

The panel is an installable PWA. The pieces live in `admin-panel/frontend/public/`
(`manifest.json`, `sw.js`, `icon-192.png`, `icon-512.png`, `icon-maskable-512.png`);
Vite copies `public/*` to the `dist/` root and Core's SPA catch-all
(`api/app.py::serve_spa`) serves them, so adding a file there needs no route change.

**`sw.js` must not register a `fetch` handler.** Chrome requires a *registered* service
worker before it offers "Install app", but it does not require the worker to do anything —
so this one does nothing on purpose.

The reason is that AEGIS is typically deployed behind an authenticating proxy. A service
worker that answers navigation requests from cache turns an expired proxy session into a
bricked app: the shell boots from cache, its API calls hit the proxy's cross-origin redirect
to a login page, that redirect fails silently inside `fetch()`, and the user has no route
back short of uninstalling. Letting navigations reach the network means the browser follows
the redirect normally and the user logs in.

If offline caching is ever genuinely wanted, it must pass navigations straight through:

```js
if (event.request.mode === 'navigate') return;  // never cache navigations
```

`tests/core/test_pwa_manifest.py` enforces this, along with the manifest fields Chrome needs
to offer an install. It asserts on the *source* files rather than a build, because `dist/` is
gitignored and CI never runs `vite build` — a test that needed the bundle would pass by
skipping. Note that its inputs sit outside `admin-panel/frontend/src/**`, so `core.yml`'s
`paths:` filter also lists `public/**` and `index.html`; without those, editing `sw.js` would
not trigger the job that guards it.

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

## Agent runs (the heavy lane)

Agent work runs in two lanes.

| | Light lane | Heavy lane |
|---|---|---|
| What | Core's in-process chat loop (`services/chat.py`) | `AgentRunFlow` — a headless claude/kimi CLI session on the coding host |
| Where | Core, one LLM call + a bounded tool loop | `RemoteScriptConnector.start_kimi_run`, in a per-run git worktree, tmux window when the host is reachable |
| How long | Seconds; answers in the same turn | Minutes to tens of minutes; the result is delivered to the agent's channel when it lands |
| Dispatch | the user talks to the agent | the `dispatch_agent_run` chat tool, or `start_workflow("AgentRunFlow", {...})` |
| Permissions | every tool call is AEGIS's own, already scoped to the agent | full-auto by default; `gated: true` puts a human on every non-allowed action (see [Gated runs](#gated-runs-human-in-the-loop)) |

An **agent run** is deliberately not "a coding task ending in a PR". It is the
same machinery `activities/agent_task.py` uses for the coding lane with the
Todoist coupling removed, so investigation, research and analysis are equally
valid asks — the run gets a filesystem, a full tool budget and its own
time, and AEGIS gets the transcript tail back in chat.

Three invariants worth knowing before you touch `flows/agent_run.py`:

- **The launch activity is `NO_RETRY`.** Launching is not idempotent — a retry
  is a second CLI session on a second worktree, burning tokens and racing the
  first one's writes. Polling (`check_agent_run`) is a `cat` plus a `fuser`,
  so it retries freely.
- **A timeout does not kill the run.** The process may be minutes from a good
  answer and the operator can attach to its tmux window, so the flow reports
  where the run is (`tmux window <name> on <host>`, plus the output file) and
  exits with `status: "timeout"`.
- **Every terminal path calls `cleanup_agent_run`.** Nothing else removes a
  run's worktree, so a missing call leaks one directory and one
  `git worktree list` registration per run, for ever (#300). The activity
  probes liveness before removing anything and the flow passes `output_file`
  only on the timeout path — where the process was deliberately *not* killed,
  so a live run keeps its worktree (leaking it is better than deleting the cwd
  it is writing to). `start_kimi_run` cleans up after its own failed launches
  for the same reason, except a launch that TIMED OUT, which may already have
  forked the agent.

Completion is detected by **process exit**, not by the `STATUS:` footer
`alerts._kimi_output_complete` looks for: that regex accepts a closed
vocabulary of alert-RCA / Jira-scoping verbs a general run has no reason to
emit.

### What a run gets in its workspace

A **claude-engine** run is not a bare CLI session — the connector mounts two
things into it before launch. Both are best-effort: neither can fail a launch,
and a run that gets neither is degraded, not broken.

**SKILL.md runbooks.** `config/skills/*/SKILL.md` in this repo are copied into
`<worktree>/.claude/skills` as part of the worktree-creation command. The source
is AEGIS's own checkout on the coding host — `coding.self_repo_path` (or
`AEGIS_SELF_REPO_PATH`), resolved against `repo_base`, which
`WorkspaceRepoSyncFlow` keeps current, so editing a skill and merging it is
enough to change what runs see. With `self_repo_path` unset, or the directory
missing on the host, the copy fragment is skipped (`[ -d … ] && … || true`) and
the launch proceeds. The copy happens only when the per-run worktree was
actually created: the shared clone is long-lived, and seeding `.claude/skills`
into it would leave untracked files behind forever.

**AEGIS's own tools over MCP.** The run mounts `POST /api/mcp-server/{agent_id}`
(PR #284) as an MCP server named `aegis`, so it can read the GTD projection,
capture tasks, search knowledge and query infra with the *same* tools the
dispatching agent has in chat — its `metadata.tool_set`, no wider. The gate is a
chain, and every link must be open:

| Link | Where | If missing |
|---|---|---|
| `mcp_server_enabled` | `AEGIS_MCP_SERVER_ENABLED` | endpoint 403s; the run mounts a server that refuses everything |
| not (`auth_disabled` without `mcp_server_allow_unauthenticated`) | `AEGIS_AUTH_DISABLED` / `AEGIS_MCP_SERVER_ALLOW_UNAUTHENTICATED` | endpoint 403s (see the MCP-server section above) |
| `mcp_server_external_url` | `AEGIS_MCP_SERVER_EXTERNAL_URL`, or infra `coding.mcp_server_url` (DB wins) | no config written, run launches toolless |
| an API key | `AEGIS_API_KEY`, **or** the admin-generated key in `settings` (env first, DB fallback — `_resolve_mount_api_key`) | no config written + a `mcp_mount_skipped` WARNING |
| engine == `claude` | routing / `engine_override` | kimi runs never mount (see below) |
| `agent_id` | `AgentRunInput.agent_id`, threaded through `launch_agent_run` | no per-agent endpoint to point at |

The URL must be reachable **from the coding host** — an internal address like
`http://10.0.0.5:8080`, not the browser-facing hostname, which is typically
behind an authenticating proxy a headless CLI cannot traverse.

**Security posture.** The mounted key is full API access to AEGIS, so:

- It is written by piping the config through the **SSH channel's stdin**, never
  as a command argument. argv is world-readable via `ps` on a shared coding host
  and lands in shell audit logs; a heredoc would be equivalent but leaves the
  content in the command string too. The content is never logged — only
  `mcp_config_written agent_id=… path=…`.
- The file lives at `$HOME/.aegis/mcp-<agent_id>.json` (a gated run gets its own
  `mcp-<agent_id>-gated.json`, pointed at the enforcing endpoint), written under
  `umask 077` (0600, in a 0700 directory) and deliberately **outside the run's
  worktree**, so the agent it authenticates cannot commit or push its own
  credential.
- The launch adds `--strict-mcp-config`, which makes that file the *only* server
  list. Without it, a `.mcp.json` checked into the target repo could add servers
  of its own to an unattended, full-auto run.
- The write is `cat > <path>.$$.tmp && mv <path>.$$.tmp <path>`, not a plain
  `cat >`: two launches for the same agent target the same path, and a
  truncate-in-place would be read half-written by the other run's CLI.

**Per-agent tool scoping is not a security boundary against a hostile run.**
The mount key is one shared AEGIS API key, and the per-agent scoping is only
the URL a run was *handed*. A run that goes off the rails can read the other
agents' config files on the same coding host (or simply guess the path — it is
`$HOME/.aegis/mcp-<agent_id>.json`) and drive `/api/mcp-server/<other-agent>`
with the same key, which is every tool any agent holds. Per-run scoped tokens
are issue **#288**; until then the real containment is (a) gated runs, which
put a human on every action, and (b) the `_UNSERVED_TOOLS` exclusions above,
which keep the recursion-capable tools off the mount entirely. Treat "agent X's
mount" as "AEGIS's whole tool surface, addressed conveniently", not as a
sandbox.

**Kimi runs get neither (v1).** The kimi CLI has no `--mcp-config` /
`--strict-mcp-config` pair and no skills convention, so a kimi run is a plain
CLI session exactly as before. Force `engine: "claude"` on a dispatch that needs
AEGIS's tools.

### Gated runs (human-in-the-loop)

A normal run launches with `--dangerously-skip-permissions`: nobody is sitting
at the terminal, so nothing can prompt. A **gated** run
(`gated: true` on `dispatch_agent_run` / `AgentRunInput`) replaces that flag with
`--permission-prompt-tool mcp__aegis__approve_tool_use` and turns every action
the CLI would otherwise auto-allow into a question for a human.

```
run wants to use Bash/Write/…
  └─ CLI calls mcp__aegis__approve_tool_use {tool_name, input, tool_use_id}
       └─ core (routes/mcp_server.py) starts an InteractionFlow
            └─ approval card lands in the agent's channel
                 └─ ✅ Approve → {"behavior":"allow","updatedInput":<the input, verbatim>}
                    ⛔ Deny    → {"behavior":"deny","message":"Denied by operator: …"}
  └─ run continues either way (a deny is a tool result, not a crash)
```

**That path covers the CLI's BUILT-IN tools only.** Live E2E on 2026-08-13
(issue **#294**) caught claude 2.1.231 executing `mcp__aegis__capture_to_inbox`
in a gated run with zero cards: tools that arrive through an explicitly-passed
`--mcp-config` are trusted in `-p` mode and never reach
`--permission-prompt-tool` (Bash *does* still reach it). So AEGIS's own tools are
gated **server-side** instead, by the URL the gated run mounts:

```
POST /api/mcp-server/{agent_id}/gated        ← what a gated run mounts
  read-only tool (_READ_ONLY_TOOLS)   → executes, no card
  anything else                       → approve-then-retry:
     1st call  → raise an InteractionFlow card, wait mcp_gate_wait_seconds (40),
                 then answer isError "…retry this exact call in ~60 seconds…"
     retry     → find the approval by (agent, tool, sha256(canonical args)),
                 claim it single-use, execute, return the real result
     denied    → permanent deny for those arguments; archived → "expired"
```

40 seconds is deliberate: the CLI was measured to abandon an MCP tool call at
~60s regardless of `MCP_TOOL_TIMEOUT`, so the gate cannot hold a call open until
a human answers — it has to come back in time to *instruct the retry*. An
approval lives 15 minutes, covers exactly the arguments the operator read
(canonical-JSON hash, so key order is not a new call) and authorises exactly one
execution: the claim is an atomic `UPDATE … WHERE metadata->>'gate_consumed_at'
IS NULL`, so two concurrent retries cannot both run. `_READ_ONLY_TOOLS`
(`api/routes/mcp_server.py`) is v1 of the classification issue **#289** asks for
— an ALLOW-list, so a tool added later is gated until someone classifies it.

This is AEGIS's **Rule-of-Two** posture in one flag: a run that reads untrusted
content *and* can mutate state should not also be unsupervised. Give up any one
of the three and you are back in safe territory — a gated run keeps the first
two and hands the third to a person.

**The gate fails closed.** Only a human resolving the card with an approve value
produces an `allow`. Every other outcome is a deny: no Temporal client, a
malformed request, a workflow that will not start, an exception mid-flight, a
timeout, an archived card, or a response value the gate does not positively
recognise. That is deliberate and worth preserving — a permission gate that
opens when it breaks is not a gate. The verdict always comes back as a normal
(`isError: false`) MCP result, because an error result is a broken permission
check rather than a decision.

**Two timeouts, and their order matters.** Core holds the CLI's permission call
open for `AGENT_RUN_APPROVAL_TIMEOUT_S` (**9 min**,
`api/routes/mcp_server.py`) and the launch exports `MCP_TOOL_TIMEOUT=600000`
(**10 min**, `connectors/remote_script.py`). The nine must stay under the ten: if
the CLI gave up first, a slow operator would surface inside the run as a
transport failure and their answer would land nowhere. Raise one and raise the
other. The card's own `timeout_policy` is `archive`, so an unanswered card stops
being pending instead of holding a workflow open forever.

**A third clock: the flow's watch window.** `AgentRunInput.timeout_minutes`
(default 30) is how long `AgentRunFlow` keeps polling; it never kills the run,
it only stops watching and reports where the process is. A gated run spends most
of that window *blocked on a human* (up to 9 min per card), so three or four
questions exhaust 30 minutes while the CLI is still working and still raising
cards. `dispatch_agent_run` therefore takes an optional `timeout_minutes`
(5–240) and defaults a **gated** dispatch to **120** when the caller omits it.

**Gated has two hard preconditions**, both checked at launch and both returned
as a normal `{"status": "failed", ...}`:

| Precondition | Why |
|---|---|
| engine is `claude` | kimi has no `--permission-prompt-tool` equivalent |
| the MCP mount succeeded | it is both where the approval tool lives and what points the run at the enforcing `/gated` endpoint — an unmounted gated run is an ungated run |

Neither degrades to an ungated run. A request for a human in the loop that
quietly became a full-auto session is the one failure mode this feature cannot
have, so it fails the launch instead and the flow delivers the reason.

`approve_tool_use` is served by `POST /api/mcp-server/{agent_id}` to **every**
agent regardless of `metadata.tool_set` — `--permission-prompt-tool` only
resolves a tool the server advertises, and an agent whose gate is invisible
could not run gated at all. It is deliberately **not** a `CHAT_TOOLS` entry: it
is a transport-level permission gate, not something an agent may call in chat,
and calling it grants nothing anyway.

### Provisioning the aegis-scratch workspace (once)

`start_kimi_run` never JIT-clones — a missing checkout is a deliberate hard
failure. A run dispatched without a `repo` uses the fixed `aegis-scratch`
checkout, which the operator creates once on the coding host. The name is
AEGIS-prefixed deliberately (#292): `repo_base` is a real workspace root where a
plain `scratch/` usually already exists as a personal, non-git folder.

```bash
mkdir -p <repo_base>/aegis-scratch && cd <repo_base>/aegis-scratch && \
  git init && git commit --allow-empty -m init
```

The empty commit is load-bearing: the launch adds a **detached worktree**, which
needs a `HEAD` to detach from. If it is missing, the flow delivers the failure
with this exact command in it.

### Granting the tool on an existing deployment

`agents.metadata.tool_set` is **DB-owned once non-empty**, so adding
`dispatch_agent_run` to `config/seed/agents.yaml` grants it on a *fresh* boot
only. On a running deployment, add it on the admin **Behavior** tab or:

```bash
curl -X PATCH "$AEGIS_URL/api/agents/pandoras-actor" \
  -H 'content-type: application/json' \
  -d '{"metadata": {"tool_set": [<existing tools...>, "dispatch_agent_run"]}}'
```

`tool_set` is replaced wholesale, so send the full list, not just the new entry.

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
