# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims
to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Auth-disabled deployments are no longer silent** (#88): `AEGIS_AUTH_DISABLED=true`
  now logs a `CRITICAL` `auth_disabled_active` event on every Core boot and shows a
  red banner on the admin System-monitoring page (`auth_mode` in
  `GET /api/admin/system/status`: `disabled` | `basic` | `api_key` | `basic+api_key`).
  The flag is only safe behind a proxy that fully fronts port 8080 — combined with a
  host-published port it grants full admin access to anyone on the network.
- **Route auth-coverage regression test** (#88):
  `tests/core/test_route_auth_coverage.py` walks every registered `/api` route and
  fails if one answers an anonymous request, so a new router can't forget
  `dependencies=[Depends(verify_auth)]`. Only `/health` and `/api/webhooks/*` are
  allowlisted.
- **Optional `X-Alert-Token` on `/api/webhooks/alert`** (#88): Alertmanager/Grafana
  don't sign payloads, so this route had no verification at all. Setting
  `AEGIS_ALERT_WEBHOOK_SECRET` now requires a matching header (constant-time compare);
  unset keeps the previous open behaviour.
- **LinkedIn first-comment link support** (#83): `SocialConnector._post_postiz`
  now sends a LinkedIn post's external link as a second `value` item instead
  of appending it in-body — Postiz's LinkedIn provider posts `value[1:]` as a
  comment on the main post, avoiding LinkedIn's reach penalty for in-body
  links. Other platforms are unaffected.
- **LLM spend governor + kill switch**: `LLMSpendGuardFlow` (every 15 min) sums
  the rolling-24h `llm_calls` token usage and trips a DB kill switch when it
  exceeds `settings.llm_governor.daily_token_budget`, posting a Slack system
  event. While `settings.llm_kill_switch.active` is true, `LLMClient.think` /
  `chat` (and `extract_receipts_batch` via `think`) raise `LLMKillSwitchError`
  instead of calling the model — embeddings stay exempt so knowledge search
  keeps working. Ships **active but inert**: the budget defaults to `0`
  (disabled), so it is a no-op until configured on the admin Settings page.
  `model_filter` (comma-separated substrings, e.g. `claude`) scopes the budget
  to the paid models. The governor auto-clears only a switch it set itself
  (`set_by == "governor"`); a manual kill stays until manually cleared, and
  alerts fire only on a state transition, never on every tick.
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
