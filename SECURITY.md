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

## Operator hardening notes

- Always set `AEGIS_SECRET_KEY` — without it, integration secrets are stored in
  the DB in plaintext.
- Don't expose the admin panel or Temporal UI to the public internet; keep them
  behind a VPN / IP allowlist / auth proxy.
- The webhook routes authenticate by HMAC only — keep the signing secrets
  secret and rotate them if leaked.

## Supported versions

Only the latest `main` receives fixes. Pin a release tag for stability, but
expect security fixes to land on `main` first.
