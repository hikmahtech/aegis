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
