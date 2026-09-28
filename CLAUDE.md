# CLAUDE.md

Guidance for Claude Code in this repository. Keep this file short: it loads into every session.
Detail lives in `docs/`, and the last section says which file to read and when.

## What AEGIS is

A self-hosted personal AI platform built around workflows. Named agents run scheduled and
event-driven [Temporal](https://temporal.io) workflows over your own data (tasks, money,
knowledge, homelab alerts). They ask you for a decision only when they need one. Models go
through a LiteLLM proxy, local first.

| Package | Path | Role |
|---|---|---|
| `aegis-core` | `core/` | FastAPI API (port 8080), admin SPA, chat, knowledge (Postgres + pgvector), connectors |
| `aegis-worker` | `worker/` | Temporal worker: all flows and activities (task queue `aegis-main`) |
| `aegis-comms` | `comms/` | Slack bot (Socket Mode) and delivery server |
| admin panel | `admin-panel/frontend/` | React + Vite SPA, built into the core image |

Backing services: Postgres 16 + pgvector, Temporal, and a LiteLLM proxy that maps the model
tiers `fast` / `balanced` / `smart` to real models.

This is a public repo, built to be forked. Never add host, registry or personal details to it.

## Commands

```bash
# Setup
python -m venv .venv && source .venv/bin/activate
pip install -e "core[dev,google]" -e "worker[dev]" -e "comms[dev]"

# Local infrastructure
docker compose up -d postgres temporal temporal-ui   # Postgres on :25432

# Run services, one shell each
python -m aegis          # core on :8080; applies migrations, serves the admin panel
python -m aegis_worker   # Temporal worker
python -m aegis_comms    # Slack bot + delivery server

# Tests: one package at a time, the way CI runs them
pytest tests/core/ tests/api/ tests/integration/ -n auto --dist loadfile --timeout=300
pytest tests/worker/ -n auto --dist loadfile --timeout=300
pytest tests/comms/ -n auto --dist loadfile --timeout=300
pytest tests/worker/test_cleanup.py::test_name       # one test

# Lint: scoped per package, as CI does
ruff check core/src/ tests/core/

# Admin panel
cd admin-panel/frontend && npm ci && npm run build && npm test
```

The full stack is `docker compose up -d`. Add `--profile slack` for comms and
`--profile local-llm` for a bundled Ollama. The exact CI lines are in `.github/workflows/`.

## Rules that cause damage or lost time if you miss them

**Tests and lint**
- Never run the whole suite in one process. Bare `pytest`, or `pytest tests/worker/` without
  `-n`, deadlocks. Always use `-n auto --dist loadfile`.
- Tests need a real Postgres (`docker compose up -d postgres`). No DB mocks. Each xdist worker
  gets its own database, so two runs on one host do not collide.
- Never `ruff format` `core/src/aegis/services/chat.py` or `core/src/aegis/services/tools/infra.py`.
  Local ruff rewrites their hand-laid tables. Run `ruff check` only, and keep diffs minimal.
- CI test workflows are `paths:`-filtered. When tests start reading a new path, add it to the
  filter, or the job silently stops guarding it. `ci-grep-guard.yml` fails the build if n8n-era
  files come back.

**Schema and config**
- Migrations are `migrations/NNN_*.sql`. Core applies them at startup, tracked by **filename**.
  Renaming an applied migration runs it again, so write idempotent DDL (`IF NOT EXISTS`). If two
  PRs claim the same number, renumber the loser before it merges.
- Seed config is `config/seed/*.yaml`. Once a DB row exists, the DB wins: editing the YAML or a
  Python default does not change a running deployment. Agent `capabilities` and `metadata`
  (including `tool_set`) are DB-owned after first boot.
- A new user-facing `settings` row ships with a service module (lenient `merge` on read, strict
  `validate` on write), a GET/PUT route, and a field on the admin page for its domain.
- Secrets go in `config/.env` (gitignored; copy `config/.env.example`). Refer to them by name only.

**Flows, agents and tools**
- New scheduled flow: add one `FlowSpec` to `FLOWS` in `worker/src/aegis_worker/registry.py`
  and a seed row in `config/seed/activities.yaml`. Activities need no registration, except a new
  Activities class, which needs its constructor call in `main()`. `check_registration()` refuses to
  start a half-wired worker.
- Every flow config dataclass has `agent_id: str` as its first field.
- Never branch on a literal agent id. Resolve by capability tag (`gtd`, `finance`, `research`,
  `infra`) with `services/agents.py::resolve_tag` in core or `AgentRegistryActivities.resolve_agents`
  in the worker. No holder means skip and warn, never crash.
- New chat tool: a typed executor decorated with `@aegis_tool` under `services/tools/`. A new
  module needs its import added in `services/chat.py`. Regenerate
  `tests/core/fixtures/chat_tools_golden.json` when a schema changes on purpose. Grant the tool
  through the agent's `metadata.tool_set` (a DB write). `AGENT_TOOL_SETS` is not read at runtime.
- A chat-tool executor reports failure by returning an error envelope, not by raising.
- `interactions` / `InteractionFlow` is the one human-in-the-loop primitive. Do not add
  per-domain decision tables.

**Models**
- Resolve models through the tier map (`services/llm_backend.py`: the `settings.llm_backend` row
  first, then `config/models.yaml`). Never read `settings.model_*` directly.
- The worker picks up a changed model or route config only on restart.
- A new reasoning model must be added to `_REASONING_MODELS` in `core/src/aegis/llm/__init__.py`,
  or it gets no token floor and can return empty content.
- `llm_calls.status` has three values: `success`, `clipped`, `error`. A NULL `cost_usd` means
  unpriced, not zero.

**Data you must write through its owner**
- `agent_memory` is soft-retired: every new read needs `AND superseded_at IS NULL`. Only
  `services/memory.py::apply_consolidation` writes it.
- Automated persona edits go through `personalities.py::apply_profile_patch`, never
  `set_personality`. Only the `user` kind may be written automatically.
- The `life` schema is written only through its services. External readings go through
  `observations.record_external_observation`; a `None` return means "already ingested".
- The problem hub owns alert identity. A new producer builds an `Event`, calls
  `hub.ingest_event`, and adds its source to `SOURCES`. A Todoist task is a projection of a
  problem, never its identity.
- The hledger journal is the money record; Postgres is only its index. Writes go through
  `services/books.py` and `services/ledger_write.py`. Keep `run_hledger`'s exact-match allowlist.
- The Obsidian vault is written only by `services/notes.py`: insert-only, never force-push, and
  encrypted blocks are stripped before anything is indexed or sent to a model.
- Every fetch of an untrusted URL goes through `services/url_guard.py`.

**Todoist**
- Check per-command `sync_status` on Todoist Sync API calls (`check_sync_status`). Retryable
  failures go to the outbox; permanent ones are logged and dropped.
- Every terminal clarify outcome leaves a GTD state label (`_GTD_STATE_FOR` in
  `activities/clarify.py`). Clarify never stamps `@waiting`.

## Deployment

GitHub Actions here run tests only. Image build and deploy live in a separate private
infrastructure repo (Ansible), so no registry or host details land in this public tree. Do not
add a build or deploy job; a fork wires up its own. Fork-facing notes: `docs/production.md`.

## Where to read more

- `docs/README.md`: index of all docs.
- `docs/development.md`: local setup, ports, adding flows, connectors and chat tools.
- `docs/architecture/overview.md`: services, flows, agents, connectors, schema.
- `docs/architecture/conventions.md`: the full reasoning behind the rules above, plus tests, CI,
  models, clarify, email triage, settings rows and observability. Read it before changing any of them.
- `docs/architecture/domain-rules.md`: GTD and Todoist, knowledge ranking, `life`, interactions,
  meeting notes.
- `docs/architecture/problem-hub.md`: read before touching `hub*.py`, alert producers or outages.
- `docs/architecture/money-and-trading-desk.md`: read before touching the books, bank parsers,
  money email or the trading desk.
- `docs/architecture/research-lane.md`: read before touching research, feeds, Calibre, topics,
  areas, or the world, GitHub and tender watches.
- `docs/architecture/vault.md`: read before touching `notes.py`, the journal or `vault_layout`.
- `docs/infrastructure.md`: the infra registry, the coding host, and setup guides per lane.
- `docs/superpowers/specs/`: design specs.

## Issue tracking

Repo work (bugs, tasks, improvements, deferred fixes) goes in GitHub Issues: `gh issue list`,
`gh issue create`. Close them from the fixing PR with `Fixes #N`. Dated follow-ups that are not
repo work go to Todoist. Notion is not used. Never put secret values in an issue.
