# Production Deployment

AEGIS is built to be **forked and self-hosted**. This repo's CI is deliberately
**test-only** (lint + tests on every push/PR) — it never builds images and never
deploys, so a fork's CI can't touch infrastructure you don't own. Image build and
deploy belong to *your* infrastructure (the maintainer keeps them in a separate,
private Ansible repo); this page describes what that side needs to do.

## Building the images

Three images, one Dockerfile each: `core/Dockerfile`, `worker/Dockerfile`,
`comms/Dockerfile`, all built from the repo root as context. The core image
serves the admin SPA — `core/Dockerfile` builds it itself in a Node stage
(`admin-panel/frontend`, `npm ci && npm run build`), so no separate frontend
build step is needed before building images.

```bash
docker build -f core/Dockerfile   -t <registry>/aegis-core:latest .
docker build -f worker/Dockerfile -t <registry>/aegis-worker:latest .
docker build -f comms/Dockerfile  -t <registry>/aegis-comms:latest .
```

**Cloud CLIs** (`kind=k8s` exec-plugin kubeconfigs and `kind=cloud` accounts in the
infrastructure registry need them):

```bash
docker build --build-arg EXTRA_CLOUD_CLIS="aws gcloud" -f core/Dockerfile .
```

The default is empty (slim image). Supported values: `aws`, `gcloud` — see
[`infrastructure.md`](infrastructure.md) for when you need which.

## Running

Any orchestrator works — the maintainer runs Docker Swarm (`docker stack deploy`,
then `docker service update --force` to pick up a freshly-pushed `:latest`), but
nothing in the images assumes Swarm. Required backing services: Postgres 16 with
pgvector, Redis, and Temporal (see `docker-compose.yml` for a working reference
topology and ports).

- **Migrations auto-apply on Core startup** (`migrations/NNN_*.sql`, tracked in
  `schema_migrations`). There is no separate migration job.
- **Deploy core and worker together.** The worker imports `aegis.*` and both sides
  read the same tables — after a migration that renames or drops columns, an old
  worker against a new schema (or vice versa) fails at runtime. Roll all three
  services on the same commit.
- Comms runs with or without Slack — it idles as a no-op until Slack tokens are
  configured. Core reaches it via `AEGIS_COMMS_URL`.

## Configuration in production

The admin UI is the configuration surface; very little belongs in the environment.

**Env (bootstrap only):** `AEGIS_DATABASE_URL`, `AEGIS_ADMIN_USERNAME` /
`AEGIS_ADMIN_PASSWORD` (required unless `AEGIS_AUTH_DISABLED=true` — proxy-fronted
deployments only, see [`development.md`](development.md)), `AEGIS_SECRET_KEY`
(**set it in production** — it encrypts every DB-stored secret), `AEGIS_COMMS_URL`,
`AEGIS_TEMPORAL_HOST`, and your LLM gateway settings if not configured from the UI.

**Admin UI / DB (everything else):** integration secrets (Slack, Todoist, GitHub,
Postiz, finance), generated API keys, the LLM backend (Models & Providers page),
agents + personalities, channels, flow schedules, and the infrastructure registry
(SSH hosts / swarm / k8s clusters / cloud accounts / the coding host) with per-entry
encrypted credentials. Secrets are entered once in the UI and stored encrypted with
`AEGIS_SECRET_KEY` — they are **not** baked into images or committed to config.

**Seed data is first-boot-only.** `config/seed/*.yaml` and
`personalities/<agent>/*.md` are baked into the images as starter examples; the
seed loader inserts rows only when they don't exist yet and never clobbers or
prunes operator rows. After first boot the DB owns agents, personalities, channels
and schedules — edit them in the admin UI, not by copying YAML onto a volume.
(A DB `activities.config` change propagates to the live Temporal schedule within
~300s, no redeploy.)

## Installing the admin panel on a phone

The admin panel is an installable PWA, so you can run AEGIS from a home-screen icon
rather than a browser tab. Nothing needs enabling — it ships installable.

**Neither platform pops up an offer**, which is the usual reason people think it is
broken:

- **Android** (Chrome, Brave): ⋮ menu → **Install app** / *Add to Home screen*.
  Automatic install banners were retired years ago; the menu entry is the way in.
- **iOS / iPadOS**: Safari → Share → **Add to Home Screen**. iOS has never
  implemented an install prompt, and every iOS browser — Brave and Chrome included —
  is WebKit underneath, so none of them will ever offer one. Use Safari to install;
  the installed app runs standalone regardless of which browser you normally use.

Two things worth knowing before you install:

**HTTPS with a valid certificate is required.** A service worker will not register
without it, and no service worker means no install offer. This is why a bare
`https://<ip>` cannot work — a certificate can't be issued for a private IP. If you
reach AEGIS over a LAN-only address, give it a real hostname and a real certificate.

**The installed app is permanently bound to the origin you installed it from.**
`start_url` and `scope` are `/`, and the API client uses same-origin relative paths,
so the app never names a host — it talks to whoever served it. If you run AEGIS on
two hostnames (say a public one and an internal split-horizon one), installing from
each gives you two *separate* apps: two icons, two service workers, two local
storages. They are not a fallback pair, and nothing warns you when the internal one
stops resolving off-network. **Install from the hostname that works everywhere.**

### Behind an authenticating proxy

If you front AEGIS with an auth proxy (Cloudflare Access, oauth2-proxy, Authelia…),
the installed app handles the login redirect correctly: launching it is a top-level
navigation, so the browser follows the redirect to your identity provider, you
authenticate, and it returns. This works because `sw.js` deliberately has no fetch
handler — see [`development.md`](development.md#pwa-assets-and-the-one-rule-that-must-not-be-broken).

An expiry that happens **while the app is already open** is the awkward case, and it
is handled explicitly. Such a session does not expire as a `401`: the proxy answers
with a redirect to its own login host, `fetch()` follows it, CORS blocks reading the
cross-origin response, and the caller gets an opaque `TypeError`. The API client
(`admin-panel/frontend/src/api/client.ts`) catches that and reloads the page **once**,
which converts the request into a navigation the browser is allowed to follow — so you
land on the login page instead of a dead screen.

It reloads only once per page-load on purpose: a flaky mobile connection raises the
identical `TypeError`, and reloading on every blip would thrash. The second failure is
reported normally, which is also what lets a genuinely offline device settle on the
browser's offline page. There is no way to tell the two cases apart — the fetch spec
gives an opaque error for both by design.

Set a generous proxy session lifetime and this is rare regardless.

**Older iOS:** below iOS 16.4 the manifest's `display: standalone` is ignored and the
app opens wrapped in Safari's UI. Add `<meta name="apple-mobile-web-app-capable"
content="yes">` to `index.html` if you need to support those versions.

## Features that stay inert until you act

Several subsystems ship `active: true` but deliberately do nothing until an operator
turns a key. Their runs complete green in the meantime, which is the system's
signature failure mode
([`how-it-works.md`](how-it-works.md#10-failure-modes-worth-recognising)) — so this is
the checklist to work through after a deploy.

| Subsystem | What it needs | Where |
|---|---|---|
| Social publishing | `social_publishing_enabled` + a connected account | Integrations / Settings |
| LLM spend governor | a non-zero `daily_token_budget` (defaults to 0) | Settings |
| Drive sync | a `folder_id` on `drive-sync-raphael` | admin **Flows** |
| Wearable ingest | `oura_api_token` **and** an active `wearable` channel row | Integrations + Channels |
| Expiry radar | at least one `life.expiring_items` row (empty registry = silent) | admin **Expiring Items** / **Assets** |
| Life-data webhook | `life_webhook_secret` (unset ⇒ every request 503s, including correctly signed ones) | Integrations → Life data |
| `location` place names | one or more `place` channel rows; with none, every push stores `elsewhere` | Channels → *place* |
| Passive people enrichment | `people_enrichment_enabled` **and** `owner_emails` — the calendar lane refuses entirely without the latter, because Google lists you among your own events' attendees. Worker restart required | Integrations → Features / Owner |
| Curiosity's calendar lane | `owner_emails`, same reason | Integrations → Owner |
| Slack reaction capture | `slack_owner_member_id` (unset ⇒ the lane is a no-op) + the scopes below | Integrations + the Slack app |
| Voice ingest on comms | `AEGIS_VOICE_INGEST_SECRET` (its own credential, **not** `AEGIS_API_KEY`) **and** `AEGIS_ELEVENLABS_API_KEY` for transcription — either unset ⇒ 503, never an open endpoint | comms env |
| MCP client | `mcp_enabled`, `AEGIS_MCP_SERVERS`, **and** a per-agent `metadata.mcp_servers` grant — three independent gates, all default-deny. Core restart required | [`development.md`](development.md#mcp-servers-external-tool-servers) |
| Life chat tools | `query_observations` / `last_contact_with_person` are in `AGENT_TOOL_SETS` but **not** in `config/seed/agents.yaml`, so no agent gets them from a seed — grant them explicitly | Agents → **Behavior** |
| Memory consolidation (A4) | **two independent keys**: `dry_run: false` on the `memory-reflection-nightly` row *and* `AEGIS_MEMORY_CONSOLIDATION_APPLY_ENABLED=true` in the worker env. Either one alone writes nothing | admin **Flows** + worker env |
| Persona self-editing (A2/A5) | nothing to enable — but nothing is written to a persona doc until a human approves the `draft_review` card. A resolve whose acknowledged base fingerprint has drifted is refused with **409** and must be resubmitted with a matching `base_ack` | Interactions |

### Slack scopes

The Slack app needs a bot token with scopes covering everything the adapter subscribes
to: `message` / `app_mention` events, the `/capture`, `/remember` and `/status` slash
commands, `file_shared`, and — added by the self-signal capture lane —
**`reactions:read`** plus the `reaction_added` event subscription.

Reading the reacted-to message additionally needs a **history** scope matching the
channel type: `groups:history` (private channels — the natural home for notes, and the
scope the app historically shipped without), `channels:history` (public), `im:history`
(DMs). Without it the `conversations.history` fetch 403s and the whole lane is a silent
no-op; comms logs `slack_reaction_history_missing_scope` with the remedy, which is the
one signal to grep for.

**Adding a scope requires reinstalling the app, and reinstalling reissues the bot
token** — update it on the admin **Slack** page afterwards, or comms authenticates with
a dead token.

## Alert routing (inbound webhooks)

Point your alert sources at Core (all HMAC/secret-verified, auth-exempt):

- `POST /api/webhooks/sentry` — Sentry (plus the scheduled `SentryPollFlow`)
- `POST /api/webhooks/alert` — Grafana / Alertmanager-shaped payloads
- `POST /api/webhooks/github` — PR notifications (`GitHubAlertFlow`)
- `POST /api/webhooks/todoist` — Todoist sync events
- **AEGIS heartbeat (2-min poll)** → `InfraHeartbeatFlow` → `AlertInvestigationFlow` on
  node/service transitions (source `aegis-heartbeat`)

All of them feed `AlertInvestigationFlow` / the flows described in
[`architecture/overview.md`](architecture/overview.md). Per-alert runbooks live in
`runbooks/<AlertName>.md`, baked into the worker image.

## Life-data push (`POST /api/webhooks/life/{source}`)

One signed endpoint for phone / watch / home-automation clients to push personal
data. It is **off until you configure a secret** — with `life_webhook_secret`
unset every request is rejected with 503, including correctly signed ones. An
empty secret never means "skip verification".

Generate and store the secret (admin → Integrations → *Life data → Webhook
secret*, or `AEGIS_LIFE_WEBHOOK_SECRET`):

```bash
openssl rand -hex 32
```

**Signing.** The client HMAC-SHA256s `"{source}.{unix_seconds}." + raw_body`
with that secret and sends the lowercase hex digest:

| Header | Value |
|---|---|
| `X-Aegis-Timestamp` | Unix seconds, **required**, must be within ±5 min |
| `X-Aegis-Signature` | `hex(hmac_sha256(secret, "{source}.{ts}." + body))` |

Both headers are required. The timestamp and the source slug are inside the
signed input, so a captured request expires after 5 minutes and cannot be
re-pointed at a different source. Bodies over 64 KiB are refused (413). Every
authentication failure answers `401 {"detail":"unauthorized"}` — the response
never says *why*, and never echoes the signature.

Shell client (the shape an iOS Shortcut's *Run Script over SSH* or any
home-automation `command:` block needs):

```bash
SRC=observation
TS=$(date +%s)
BODY='{"source":"scale","metric":"weight_kg","value":80.5,"metadata":{"unit":"kg"}}'
SIG=$(printf '%s.%s.%s' "$SRC" "$TS" "$BODY" \
      | openssl dgst -sha256 -hmac "$AEGIS_LIFE_WEBHOOK_SECRET" -r | cut -d' ' -f1)
curl -sS -X POST "https://<aegis>/api/webhooks/life/$SRC" \
  -H "X-Aegis-Timestamp: $TS" -H "X-Aegis-Signature: $SIG" \
  -H 'Content-Type: application/json' --data-raw "$BODY"
```

From an **iOS Shortcut** the same three steps are: *Current Date → Format as Unix
timestamp*, *Text* = `{source}.{ts}.{json}` piped into a base64/HMAC action (the
built-in *Hash* action does not do HMAC — use a Shortcut HMAC helper, Scriptable,
or an `openssl` step on a jump host), then *Get Contents of URL* with the two
headers. TLS in front of Core is assumed and required.

Sources are declared in `LIFE_SOURCES` (`core/src/aegis/api/routes/webhooks.py`).
`observation` writes one `life.observations` row per push (pruned at 365 days by
`CleanupFlow`); a source mapped to a workflow name starts that Temporal workflow
instead. An unknown slug is a 404 — but only for a *correctly signed* request, so
the endpoint cannot be used to enumerate which sources exist. Replays are
additionally collapsed by an `ingest_idempotency` claim on
`(life:{source}, payload id | sha256 of the signed input)`.

Free-form text is **not** pushed here: `POST /api/admin/capture` with
`kind="life_fact"` already owns that lane.

### `location` — place inference from a phone push

`source=location` accepts an OwnTracks / Home Assistant shaped body
(`{"lat":…, "lon":…}` or `{"latitude":…, "longitude":…}`, optional `tst` unix
seconds and OwnTracks' `t` trigger code) and resolves it to a **named place**.

**What is stored is a label, not a position.** The pushed coordinate is used
once — to pick which configured place contains it — and is then discarded. The
observation row is `source='location'`, `metric='place'`, `value=NULL`,
`metadata={"place": "home", "trigger": "p"}`. No coordinate reaches
`life.observations`, `settings`, a log line, the response body, or Temporal
(the lane is inline precisely so a raw payload is never persisted in workflow
history). A push outside every configured place stores `"elsewhere"`.

**Places are yours to configure, never inferred.** Add them on admin →
**Channels** → *place*: identifier = the label that gets stored, config =
`{lat, lon, radius_m}` (radius defaults to 150 m). These few user-typed centres
are the only coordinates AEGIS keeps. Overlapping circles resolve to the
smallest, so an `office` inside a wider `city` circle wins. With no place rows
configured every push stores `elsewhere` — the endpoint still works, it just
has nothing to name.

The newest fix also updates a `settings.current_place` pointer
(`{"place","at"}`, label only), which the daily briefing reports as a
*Location* line and **drops once it is more than 12 hours old** rather than
asserting a place the owner may have left.

Retention: location observations age out with everything else in
`life.observations` — 365 days by `observed_at`, so a push queued on an offline
phone ages from when the fix was taken, not from when it arrived. `CleanupFlow`
prunes per table, so a shorter window for location alone is not expressible
today; the mitigation is that what survives a year is a coarse presence
timeline of names the owner chose, not a track.

### `health` — Apple Health Auto Export

`source=health` accepts the JSON [Health Auto Export](https://www.healthyapps.dev)
posts (`{"data": {"metrics": [{"name", "units", "data": [{"date", "qty"|…}]}]}}`)
and stores **five** metrics. Everything else HealthKit offers is counted and
dropped.

| Health Auto Export metric | Stored as | Notes |
|---|---|---|
| `sleep_analysis` | `sleep_minutes` | `totalSleep` (else `asleep`/`inBed`); `hr`/`min`/`s` → minutes |
| `heart_rate_variability` | `hrv_ms` | |
| `resting_heart_rate` | `resting_hr` | |
| `step_count` | `steps` | |
| `active_energy` | `active_energy_kcal` | `kJ` → kcal when the phone exports kJ |

Sleep and active energy are stored **only** when the export names a unit the
allowlist knows. Anything else — an absent unit, or one Health Auto Export has
not been seen to send — is counted into `skipped` and logged as
`health_push_unreadable_unit` with the offending unit, rather than assumed. An
assumed unit would be a silent 60× (sleep) or 4× (energy) error, and because
the first write for a (metric, instant) wins, a corrected re-export could never
overwrite it; a skipped sample is simply re-offered by the next export. If that
warning appears, fix the phone's units and re-export the window.

Widening that list is a deliberate edit to `_METRIC_ALLOWLIST`
(`core/src/aegis/services/health.py`). It is short on purpose: a store of every
HealthKit sample the phone holds — glucose, cycle, blood oxygen, ECG — is a
liability that grows on its own, and nothing in AEGIS reads it.

**Set up the automation like this**, or the push will be refused:

- **Selected metrics:** the five above and nothing else.
- **Aggregation:** daily, over *completed* days (yesterday, or the last 7 days).
- **Not** per-sample export of a wide window: 64 KiB is the body cap on every
  life source and it is a security control, not a tuning knob. Five metrics ×
  daily × 30 days is ~18 KiB and fits easily; five metrics × hourly × 7 days is
  ~100 KiB and gets a `413 body_too_large`. Narrow the export, don't raise the
  cap. A single push also processes at most 500 samples and reports
  `"truncated": true` if it had more.
- Aggregate *completed* days because the first write for a given (metric,
  instant) wins: steps exported at noon and again at midnight share the day's
  timestamp, and the noon figure is the one kept.

Every sample is deduplicated on `(source='health', metric, instant-in-UTC)`, so
a re-export of an overlapping window — which Health Auto Export does by design
whenever the phone has been offline — writes only the genuinely new samples.
The 202 response reports `{"stored", "duplicate", "skipped", "truncated"}`.
A malformed envelope (no `metrics` list) is a `422` and releases its
idempotency claim so the same batch id can be retried once the export is fixed.

**No value is ever logged.** Log lines from this lane carry counts only, never
a metric name or a reading, and the per-sample device name Health Auto Export
attaches is not stored. The one place a health value is rendered is the daily
briefing's *Health* line — the newest reading of each metric, dropped once it
is more than 36 hours old — which goes to the owner's own channel.

**Health values are also withheld from the briefing's framing model.** That
model is `model_balanced`, which may resolve to a hosted API, so the *Health*
line is formatted deterministically and appended after the narrative rather
than being handed to the LLM to phrase — the same treatment the workflow-failure
block gets, and for the second reason too: an appended block cannot be dropped
by a model asked for 2-5 sentences.

Ingest is inline, like `location`: a Temporal workflow argument is persisted
verbatim in workflow history, and handing a health batch to a flow would copy
body data into a second store with its own retention and its own web UI.

Retention: 365 days by `observed_at`, shared with the rest of
`life.observations`. `CleanupFlow` prunes per table, so a shorter window for
health alone is not expressible today.

### Infra heartbeat & escalation

`InfraHeartbeatFlow` (schedule `infra-heartbeat-2m`, gated by `homelab_enabled`) polls
`docker node ls` + `docker service ls` every 2 min and spawns investigations on state
transitions only. Recovery transitions write an `alert_received` / `resolved=true` audit
row; the `/api/webhooks/alert` handler writes the identical row for alertmanager
`status=resolved` payloads, so `check_alert_resolved` (self-resolve) works for both
heartbeat and alertmanager alerts. Those recovery rows also **re-arm dedup**: a class that
flapped and recovered is no longer treated as a duplicate, so a genuine later outage of the
same class still spawns a fresh investigation (`check_dedup` only suppresses when the
latest investigation has no recovery after it).

**Cross-source dedup invariant:** a heartbeat-detected outage and an alertmanager-pushed
one collapse onto ONE signature (`infra-class:<cluster>:<alertname>`) only when the
`infra_cluster` setting equals the Prometheus `cluster` label — otherwise they key on
different clusters and dedup won't merge them.

Configure on the admin Integrations page (worker restart required):

- **Heartbeat dead-man ping URL** — healthchecks.io check pinged on every successful tick.
- **Slack member id for escalation mentions** — critical infra Gate-2 cards re-ping with
  an @-mention every 3 min (max 10) until acked or self-resolved.

**Known limitation:** a service held below desired replicas for ~9+ min by a slow deploy
can trip a heartbeat `DockerServiceDown` and get force-restarted (transient deploys usually
converge before the 2-tick debounce, so this is rare).

Infra Gate-2 cards can carry a **Run fix** option (kimi's `PROPOSED_COMMANDS:` footer);
approval executes the commands on the coding host via SSH (refused if the infra row is
`read_only`), posts outputs to the task, and re-verifies. Approving with a note runs the
note's lines instead. To route hand-captured Todoist tasks ("noon is down") into the
same pipeline, add a content route with `alert_overrides`, e.g.
`{"source": "todoist-infra", "alertname": "NodeDown", "severity": "critical"}`.

## Debugging comms/Slack

- `GET /api/health` on the comms port (8081) reports inbound Socket Mode liveness;
  `DeliveryWatchdogFlow` polls it and captures a Todoist Inbox task on outage.
- Undelivered cards: interactions with no `delivery_ref` past the grace window are
  flagged by the same flow.
- Slack tokens/channel mapping are on the admin **Slack** page; per-agent channels
  come from `agents.slack_channel_id` (falls back to resolving `#aegis-<short>`).
