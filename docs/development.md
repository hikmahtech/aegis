# Local Development

## Prerequisites

- Python 3.12+
- Docker + Docker Compose
- Node.js 18+ (for admin panel)
- **hledger** for the books tests (`tests/core/test_books.py`,
  `tests/worker/activities/test_money_v2.py` skip without it): run
  `sudo ./docker/install-hledger.sh`. That script pins the version and its
  checksum, and both images and both CI jobs run the same one — so your local
  binary is the one production writes the journal with.

## Quick Start

```bash
# 1. Clone and setup
python -m venv .venv && source .venv/bin/activate
pip install -e "core[dev]" -e "worker[dev]" -e "comms[dev]"

# 2. Start infrastructure
docker compose up -d postgres temporal temporal-ui

# 3. Start Core API (runs migrations + serves admin panel)
#    AEGIS_RELOAD=true restarts it on code changes; reload is off by default
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
project root.

Each pytest run gets its own test databases on the shared Postgres, so you can run several at
once on one host (#325). `tests/conftest.py` and `tests/pg_test_db.py` handle it:

- **Names.** An xdist worker uses `aegis_test_<run_id>_<gwN>`; a run without xdist uses
  `aegis_test_<run_id>`. The run id is `<controller pid>_<host tag>`. The controller makes it
  once and hands it to its workers through xdist's `workerinput`.
- **Clean-up.** A run drops its own databases when it ends, including after a failure or
  Ctrl-C. A killed run leaves them behind, so every run starts by sweeping them. The sweep
  drops a database only when all of these hold: the name is a generated one, it was made on this
  host, no process with that pid exists here, and nobody is connected. The drop never uses
  `FORCE`, so a client that connects in between keeps it alive.
- **Fixed names.** `AEGIS_TEST_RUN_ID=<letters and digits>` pins the name. Two runs that share
  it do collide; the second stops with one clear message instead of hundreds of errors.
- **Keeping them.** `AEGIS_TEST_KEEP_DB=1` leaves this run's databases in place to inspect.
  (A later run's sweep removes them once this run's process is gone.)
- **Old names.** Databases named `aegis_test` or `aegis_test_gwN` come from the old scheme.
  The sweep never touches them; drop them by hand once no checkout runs the old code.
- **`TEST_DATABASE_URL`** still means a caller-managed database: nothing is created, migrated,
  swept, reset or dropped.

Every test file also starts from the seeded `settings` table (#569). A row one file leaves
behind would otherwise break whichever file `--dist loadfile` happens to put after it on the
same worker. Set `AEGIS_TEST_SETTINGS_LEAKS=<path>` to log each file that needed a reset.

CI lints **scoped per package** (`ruff check core/src/ tests/core/ …`, see
`.github/workflows/*.yml`), which is the gate your PR must pass; a bare `ruff check .` is
clean and equivalent because `docs/` sits in ruff's `extend-exclude` (#236).

`ruff format` is deliberately absent from that block. Do **not** run it on
`core/src/aegis/services/chat.py` — it carries
hand-laid-out data tables (`CHAT_TOOLS`/`TOOL_EXECUTORS`) that a local ruff
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
- `AEGIS_SECRET_KEY` — Fernet key encrypting DB-stored secrets (integration tokens, API keys); unset = plaintext-with-flag, fine for local dev only
- `AEGIS_LITELLM_URL` + `AEGIS_LITELLM_API_KEY` — LLM gateway (or configure the backend from the admin **Models & Providers** page)
- `AEGIS_COMMS_URL` — how Core reaches the comms delivery server (e.g. `http://localhost:8081`)
- `AEGIS_SLACK_BOT_TOKEN` + `AEGIS_SLACK_APP_TOKEN` — Slack (comms); can also be set from the admin UI (stored encrypted in the DB)
- `AEGIS_GMAIL_ACCOUNTS` — Gmail OAuth (format: `name:email,name:email`)

Most integration secrets (Todoist, Slack, Postiz, finance provider keys, API
keys) are entered in the admin UI and stored encrypted in the DB —
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
its behavior tags — closed vocab `gtd` / `finance` / `research` from
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
briefings, money processing, Slack @-addressing, chat routing) follows the
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
thing standing between the internet/LAN and full admin access to your data and
credentials. Keep `AEGIS_AUTH_DISABLED` unset (or `false`) and set both:

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

1. Write the executor in its domain's module under `core/src/aegis/services/tools/` and decorate it with `@aegis_tool`: `async def _exec_tool_name(pool, ctx: ToolContext, *, arg: str) -> str`. The first two parameters are always `pool` and `ctx`; every keyword-only parameter after them is a tool argument, and the schema the model sees is GENERATED from the annotations plus the docstring's first paragraph and its Google-style `Args:` section — so that wording IS the contract. Import `ToolContext` from `services/tools/base.py`, never from `chat` (`chat.py` imports these modules; the reverse is a cycle). A brand-new module needs its import adding to `chat.py`, or nothing registers.
2. Add `_registry_schema("tool_name")` to the `CHAT_TOOLS` list in `core/src/aegis/services/chat.py`, in the position you want it advertised — the list IS the LLM's prompt order. Never a hand-written schema dict.
3. Add to the `TOOL_EXECUTORS` dict in `chat.py` — it stays the single registry regardless of which module the executor lives in, so `_validate_agent_tool_sets` and `GET /api/agents` see every tool in one place
4. Grant it to agents via their `metadata.tool_set` — set it on the admin **Behavior** tab (runtime source of truth) and/or in `config/seed/agents.yaml`. The shipped `AGENT_TOOL_SETS` dict is now only a seed-time default for the four example agents; an agent's DB `metadata.tool_set` overrides it, and an unconfigured agent falls back to a small read-only `_FALLBACK_TOOL_SET` (not Sebas's full surface). `_validate_agent_tool_sets` refuses to boot on a tool name with no executor, and Core additionally warns at startup on any DB `metadata.tool_set` entry that references a missing executor.
5. If the tool needs new connectors on `ToolContext`, add the field and wire it in `send_message()`
6. Write tests in `tests/core/test_{tool_name}_tool.py`
7. If the tool can legitimately run longer than `tool_timeout_seconds` (default 30s), add an entry to `_TOOL_TIMEOUT_OVERRIDES` in `chat.py` — otherwise the executor cancels it mid-flight and the model retries, orphaning whatever the tool started

## Coding runs and the MCP server moved out

Agent runs on a coding host (`AgentRunFlow`, claude/kimi), task sessions and
the MCP server (`/api/mcp-server/*`) left v1 with the development lane. They
live in the Development vertical (a2-development) now.

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
