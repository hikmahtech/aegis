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

All of them feed `AlertInvestigationFlow` / the flows described in
[`architecture/overview.md`](architecture/overview.md). Per-alert runbooks live in
`runbooks/<AlertName>.md`, baked into the worker image.

## Debugging comms/Slack

- `GET /api/health` on the comms port (8081) reports inbound Socket Mode liveness;
  `DeliveryWatchdogFlow` polls it and captures a Todoist Inbox task on outage.
- Undelivered cards: interactions with no `delivery_ref` past the grace window are
  flagged by the same flow.
- Slack tokens/channel mapping are on the admin **Slack** page; per-agent channels
  come from `agents.slack_channel_id` (falls back to resolving `#aegis-<short>`).
