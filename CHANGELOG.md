# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims
to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
