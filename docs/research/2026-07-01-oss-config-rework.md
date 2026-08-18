# OSS config rework — DB-as-config, UI-as-editor, low-footprint deploy

**Status:** design approved 2026-07-01. Phased build in the (private) repo via the
normal PR flow; the fresh-public-repo single-commit cutover is the final step.

## Problem

The repo *is* the maintainer's deployment. Config lives as committed files —
`config/seed/*.yaml`, `personalities/*/*.md`, `config/.env`, `config/models.yaml`
— loaded at boot. That couples three problems:

1. **Personal config is in git.** Channel emails, agent personas, voice ids, the
   LiteLLM proxy URL — all committed. Can't go public cleanly.
2. **A forker can't actually run it.** The LLM backend is hardwired to the
   maintainer's LiteLLM proxy; personalities are hand-edited markdown; the
   channel requires a Slack app.
3. **UI config is raw JSON.** `/admin/flows` edits a JSON textarea; agents are
   read-only.

## Principle

**The database is the source of truth; the UI is the editor; the repo ships a
generic first-boot bootstrap; env/files are the fallback.** This is already true
for `activities` (DB-owned, edited at `/admin/flows`) and `agents.metadata`
(PR #371). Extend it to the LLM backend, personalities, and channel — so nothing
personal is committed, and a forker configures everything in the UI.

**Non-breaking rule:** every phase reads DB-first and falls back to the
maintainer's current env/files. Your deployment keeps working until you move each
piece into the UI; the repo's seeds only populate an *empty* table on first boot.

## What the code already gives us

- `LLMClient` wraps `AsyncOpenAI(base_url, api_key)` (`core/src/aegis/llm/__init__.py`)
  — already OpenAI-compatible. OpenAI, OpenRouter, LiteLLM, Ollama all speak that
  shape; Anthropic has an OpenAI-compat endpoint. BYO-backend = make
  `base_url`/`api_key`/the tier→model map configurable, not a rewrite.
- `docker compose up -d` already brings up Postgres+pgvector, Temporal, and the
  three services. Gaps: drop legacy `n8n`, bundle/optionalize an LLM, trim env.
- A schemaless `settings` KV table + `/api/settings` already exist — the home for
  provider/channel config. No new infra.

## Phases

OSS-ready core = **A–D**. **E** is polish.

### A — Configurable LLM backend (BYO key + backend) — *the linchpin*

- Store backend config in `settings` under `llm_backend`:
  `{ provider, base_url, api_key (encrypted), tiers: {fast, balanced, smart} }`.
  One endpoint + per-tier model names (covers LiteLLM / OpenRouter / OpenAI /
  Ollama; mixing raw providers = put a proxy in front, which is what proxies are
  for).
- `get_llm_backend(pool)` → DB row, **falling back to env** (`litellm_url`,
  `litellm_api_key`, `config/models.yaml`). Core + worker build their `LLMClient`
  + tier map from it at boot; tier→model is cached with a short TTL so model
  tweaks go live without a restart; core rebuilds its client on UI save.
- **Secrets:** `api_key` encrypted with `AEGIS_SECRET_KEY` (Fernet) if set,
  plaintext otherwise — single-user self-hosted default, full configure-in-UI UX
  with one optional bootstrap secret.
- **UI:** "Models & Providers" page — provider preset dropdown (prefills
  base_url), key (write-only field), per-tier model, **Test connection** button.
- No migration (uses `settings`). Maintainer's env stays the fallback.

### B — Personalities as DB config

- Persona text → `agents` columns (soul / operating_notes / user_context);
  `_build_agent_system_prompt` reads DB-first, the `.md` files become the
  first-boot seed. Make `agents` **DB-owned on reseed** (preserve UI edits, like
  `activities`).
- Agent **PATCH** endpoint + a real editor (the Personalities page is read-only
  today). A **"draft with AI"** button uses the now-configured LLM to generate a
  persona from a one-line description.

### C — Web inbox as the default channel (Slack optional)

- A `web` delivery adapter that just persists interactions (the
  `DeliveryRef`/`CardSpec` types are already channel-neutral); flows deliver to
  the admin inbox, which renders the card + collects the response. `AEGIS_CHANNEL`
  defaults to `web`; Slack/comms becomes an optional add-on. Runs with zero Slack
  setup.

### D — Turnkey + low footprint

- Drop `n8n` from compose; add an optional Ollama profile; trim required env
  (default `TEMPORAL_UI_URL`, make `LITELLM_URL` optional given A). Generic
  bootstrap seeds + a one-command quickstart. Prod (ansible/swarm) unchanged.

### E — Config forms + setup wizard *(polish)*

- Typed forms replacing JSON textareas (per-activity, like the Drive folder
  form). A first-run `/admin/setup` wizard: pick LLM backend → test → generate
  agents → done.

## Decisions

- **Encrypted key in DB** (Fernet via optional `AEGIS_SECRET_KEY`, plaintext
  fallback) — not env-only — for the full configure-in-UI experience.
- **Temporal stays, bundled in compose** — replacing it is a months-long rewrite
  for no user-facing win.
