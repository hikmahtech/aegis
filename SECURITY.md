# Security Policy

AEGIS is self-hosted software that holds sensitive material on your behalf —
**encrypted infrastructure credentials** (SSH keys, kubeconfigs, cloud
credentials), integration tokens (Slack, Todoist, GitHub, Google), and personal
data (tasks, email, money). Please treat security reports accordingly.

## Reporting a vulnerability

**Do not open a public GitHub issue for a security vulnerability.**

Instead, report it privately through
[GitHub Security Advisories](https://github.com/hikmahtech/aegis/security/advisories/new)
("Report a vulnerability"). If you can't use that, open a minimal public issue
that says only "security report — please provide a private contact" with no
details, and the maintainer will follow up.

Please include:

- What the issue is and where (file / endpoint / flow).
- How to reproduce it, and the impact (what an attacker could read or do).
- Any suggested fix, if you have one.

There's no bug-bounty program — this is a personal open-source project — but
genuine reports are appreciated and will be credited if you'd like.

## Scope

This is a single-tenant, self-hosted app: **you** run it, on your own
infrastructure, for yourself. The most valuable reports are ones that would let
an attacker who reaches the admin panel or an exposed endpoint escalate beyond
what the operator intended — for example:

- Auth bypass on the admin API or the webhook endpoints (which are
  deliberately un-IP-whitelisted and rely on per-source HMAC verification).
- Decryption or leakage of the at-rest credential store (`AEGIS_SECRET_KEY`
  Fernet encryption of infra credentials / integration tokens).
- SSRF, command injection, or path traversal via the infra/coding-host tooling
  (which by design runs commands on registered hosts) that escapes the
  per-entry `read_only` gating.
- Prompt-injection paths that turn untrusted input (an email, an RSS item, a
  webhook) into an unintended privileged action.

## Authentication

Every `/api` route sits behind `verify_auth` (`core/src/aegis/api/auth.py`).
Only two paths are deliberately open: `/health` (liveness) and
`/api/webhooks/*` (each verifies its own signature — see below). A regression
test (`tests/core/test_route_auth_coverage.py`) walks every registered route and
fails the build if a new one is added without auth.

A caller can authenticate three ways:

| Credential | How it's sent | Where it's set |
|---|---|---|
| Admin username + password | HTTP Basic | `AEGIS_ADMIN_USERNAME` / `AEGIS_ADMIN_PASSWORD` |
| Env API key | `X-API-Key` header | `AEGIS_API_KEY` |
| Admin-generated API key | `X-API-Key` header | admin **Integrations** page (stored Fernet-encrypted in the DB) |

Core **refuses to boot** without admin credentials, so an unprotected instance
can't ship by accident — with one exception, below.

### `AEGIS_AUTH_DISABLED` — what it actually does

`AEGIS_AUTH_DISABLED=true` makes `verify_auth` return success for **every
request on every route, with no credential of any kind**. It is not a "relaxed"
mode; it is *off*. It exists solely for deployments whose port 8080 is reachable
*exclusively* through an authenticating proxy (e.g. Cloudflare Access), where a
second basic-auth prompt would be redundant.

**Never combine it with a host-published port.** `AEGIS_AUTH_DISABLED=true` plus
a published `8080` means anyone who can route to that host — any device on the
LAN, any guest on the wifi — has full admin access: your infra credentials,
integration tokens, email, tasks and money data, plus command execution on every
registered host. The flag and the exposed port are each individually reasonable;
together they are a full compromise.

Because the API answers identically either way, an auth-disabled instance is
invisible from the outside. It surfaces itself in two places:

- a `CRITICAL` `auth_disabled_active` event in the Core boot log;
- a red banner on the admin **System monitoring** page (`auth_mode` in
  `GET /api/admin/system/status`).

Verify from off-box — anonymous access must be refused:

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://<host>:8080/api/agents   # expect 401
```

## Webhooks

`/api/webhooks/{github,sentry,todoist}` are unauthenticated by design and verify
a per-source HMAC signature instead. Keep those signing secrets secret; rotate
them if leaked.

`/api/webhooks/alert` (Alertmanager/Grafana) is the exception: neither tool signs
its payloads, so there is nothing to verify. Set `AEGIS_ALERT_WEBHOOK_SECRET` to
require a matching `X-Alert-Token` header (add it to the sender's headers config,
e.g. Alertmanager's `webhook_configs.http_config.headers`). Left unset, the
endpoint accepts anything that reaches it, and each forged alert spawns an
investigation flow that consumes LLM budget and posts to Todoist/Slack.

## Operator hardening notes

- Always set `AEGIS_SECRET_KEY` — without it, integration secrets are stored in
  the DB in plaintext.
- Don't expose the admin panel or Temporal UI to the public internet; keep them
  behind a VPN / IP allowlist / auth proxy.
- Set `AEGIS_ADMIN_USERNAME` / `AEGIS_ADMIN_PASSWORD`; treat `AEGIS_AUTH_DISABLED`
  as safe *only* behind a proxy that fully fronts port 8080 (see above).
- Set `AEGIS_ALERT_WEBHOOK_SECRET` unless the alert endpoint is proxy-protected.
- The signed webhook routes authenticate by HMAC only — keep the signing secrets
  secret and rotate them if leaked.

## Supported versions

Only the latest `main` receives fixes. Pin a release tag for stability, but
expect security fixes to land on `main` first.
