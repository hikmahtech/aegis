# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims
to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Learning-loop input on Slack cards** (#71): approval/choice/ack interaction
  cards carry an optional "Why?" free-text input; a note typed with a button
  tap lands as `response.note` and becomes a durable `agent_memory` lesson.
- **Per-tool chat executor timeouts** (#73): `_TOOL_TIMEOUT_OVERRIDES` in
  `services/chat.py` lets long-running tools exceed the 30s default —
  `aegis_self_diagnose` (remote coding-CLI run, up to 8 min) could previously
  never complete and each retry orphaned another run on the coding host.

### Changed

- **`schedule_sync` converges** (#72, fixes #11): schedules are rewritten only
  when their config fingerprint changes (embedded in the action id
  `scheduled-<slug>--v<fp>`), instead of unconditionally every ~5-min tick.
- Default `smart` tier example points at a tool-calling-capable proxy alias
  (`claude-sonnet-5`); receipt extraction (`MoneyActivities.extract_model`) now
  runs on the smart tier instead of the fast local model (#69).
- Gmail classification token budget raised 768 → 2048 (reasoning-token
  truncation) (#69).
- `SocialPublishFlow` summary keys renamed `posted`/`post_failed` →
  `drain_posted`/`drain_failed` — the flow's drain step is only the retry net,
  so the old key read as "nothing posted" while approval-path posts flowed (#69).

### Fixed

- **Google Calendar 410 poison loop** (#69): a quiet calendar's cursor
  (max event `updated`) ages past Google's `updatedMin` horizon and every daily
  fetch 410s without advancing it. On 410 the fetch now retries the full window
  and bumps the cursor to fetch time.

### Removed

- **Dead-code cleanup (repo-wide over-engineering audit).** Telegram-era seams
  that survived the Slack migration: `reply_markup`/`parse_mode` delivery
  plumbing, `CardSpec.target`, the worker `send_document` activity and
  `update_interaction_message_id` (cards record `delivery_ref` only). The
  never-consumed Slack signing-secret config (comms authenticates via Socket
  Mode bot+app tokens) was dropped end-to-end (admin UI field, encrypted
  storage, internal API). Also removed: `preview_retention`, six
  `@activity.defn` registrations only ever called in-process,
  `load_model_tiers` (superseded by `set_model_tiers` + the LLM backend
  resolver), the unused `finance_api_key` seam, two Settings fields shadowed
  by direct env reads, the speculative `arch-guard` CI job for a package
  layout that doesn't exist yet, the dev docker-compose `redis` service
  nothing connects to, and unused dependencies (redis/asyncpg/requests OTel
  instrumentations, `google-auth-oauthlib` in worker, `google-auth-httplib2`,
  transitively-satisfied `pydantic`/`pydantic-settings` in worker).

## [0.1.0] — 2026-07-10

First public release. AEGIS has been running as the maintainer's private
personal-orchestration platform; `0.1.0` marks the point where it was cleaned
up, decoupled from the maintainer's own setup, and opened for others to fork.

### Added

- **Fork-and-run quickstart.** The core image now builds the admin SPA itself
  (multi-stage Docker build), so a fresh clone comes up with
  `cp config/.env.example config/.env && docker compose up -d` — no manual
  frontend pre-build step.
- **Agent behavior is data-driven**, keyed on capability tags
  (`gtd`/`finance`/`research`/`infra`) in `agents.capabilities` and per-agent
  `agents.metadata`, editable from the admin **Behavior** tab — not hardcoded to
  the four example personalities.
- New configurable knobs so the platform isn't tied to one deployment:
  - `AEGIS_INFRA_CLUSTER` — Prometheus/Alertmanager `cluster` label that marks
    an alert as infra (default blank).
  - `AEGIS_SCRIPT_HOST_K8S_CONTEXTS` — k8s context names on the remote script
    host; k8s/ArgoCD chat tools + the admin Live Inspector now resolve contexts
    from this plus registered `kind=k8s` infra entries, instead of hardcoded
    cluster names.
  - `AEGIS_HOME_CURRENCY` + `AEGIS_MONEY_HYGIENE_FX_RATES` — Money Hygiene is
    now currency-neutral (digests render the configured currency's symbol).
  - `AEGIS_BANK_ALERT_SENDERS` — configurable bank-alert sender guard (was a
    hardcoded list).
- `CONTRIBUTING.md`, `SECURITY.md`, and this changelog.

### Changed

- Default seeded timezone is now `UTC` (was the maintainer's timezone).
- Money-model schema is currency-neutral: `recurring_charge.monthly_inr_equivalent`
  → `monthly_home_equivalent`; `fx.to_monthly_inr` → `to_monthly_home`.
- Worker fast-tier LLM concurrency limit follows `AEGIS_MODEL_FAST` instead of a
  hardcoded model id.

### Security

- No secrets in the source tree or git history (verified). Infrastructure
  credentials and integration tokens are stored encrypted at rest
  (`AEGIS_SECRET_KEY`).

[Unreleased]: https://github.com/hikmahtech/aegis/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/hikmahtech/aegis/releases/tag/v0.1.0
