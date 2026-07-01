# AEGIS

**A flow-first, self-hosted personal AI orchestration platform.** A small fleet
of named agents run scheduled and event-driven workflows over your own data —
GTD/tasks, money, knowledge, homelab alerts — and ask you for a decision only
when they actually need one. Local-LLM-first: it runs against your own models
through a LiteLLM proxy, and can reach for Claude or OpenAI when you want the
extra horsepower.

> This is a personal project, built to be **forked and configured for your own
> life** — not a multi-tenant SaaS. The shipped agents, schedules, and
> personalities are the maintainer's working example; you replace them with
> yours. MIT licensed.

## What it does

- **Agents, not a chatbot.** Four personalities (an assistant, a researcher, a
  money agent, an infra/ops agent) each own a slice of your life. Routing
  between them is data-driven (per-agent keywords/tools in the DB, not hardcoded).
- **Flows do the work.** ~30 Temporal workflows on a schedule or trigger:
  triage email, mirror your task manager, sweep subscriptions, watch a Google
  Drive folder, investigate alerts, build a daily brief.
- **Human-in-the-loop, budgeted.** When an agent needs a decision it sends a
  card to your chat channel; you Approve / Edit / Reject. A daily
  **notification budget** keeps proactive pings from becoming noise.
- **Memory that learns.** When you correct an agent (resolve a card with a
  reason), that correction becomes a durable lesson surfaced in the agent's
  next prompt.
- **Your knowledge, local.** A native Postgres + pgvector RAG store. Seed it
  from URLs, uploads, server folders, or a watched Drive folder. Embeddings run
  on a free local model — no per-token cost.

## Architecture

Three Python packages in one repo:

| Package | Role |
|---|---|
| `aegis-core` | FastAPI API (port 8080) + the admin SPA, chat, knowledge, connectors |
| `aegis-worker` | Temporal worker — all the flows and activities |
| `aegis-comms` | Chat channel bot + delivery server (Slack adapter) |

Backed by **Postgres 16 + pgvector** (migrations auto-apply on core startup),
**Temporal** for durable workflows, and a **LiteLLM proxy** that resolves
`fast` / `balanced` / `smart` model tiers to whatever models you point it at.

Full design: [`docs/architecture/overview.md`](docs/architecture/overview.md).

## Quick start

One command brings up Postgres+pgvector, Temporal, and the core API + worker:

```bash
cp config/.env.example config/.env   # set AEGIS_ADMIN_USERNAME / _PASSWORD
docker compose up -d                 # core (:8080) + worker + postgres + temporal
```

Then open **http://localhost:8080** (the admin panel) and:

1. **Models & Providers** → pick your LLM backend — a hosted key (Claude / OpenAI /
   OpenRouter) or a local one. For fully-local, run `docker compose --profile
   local-llm up -d ollama` then `docker exec aegis-ollama ollama pull <model>` and
   point the base URL at `http://ollama:11434/v1`.
2. **Personalities** → tweak the agents (or use **Draft with AI**).
3. **Flows & Config** → enable the scheduled flows you want.

Cards that need a decision land in the **Interactions** inbox — no chat app
required. Slack is optional: set `AEGIS_CHANNEL=slack` + the `AEGIS_SLACK_*`
tokens and `docker compose --profile slack up -d`.

For Python development (running the services from source) see
[`AGENTS.md`](AGENTS.md).

## Configure it for yourself

The system is seeded from plain YAML + Markdown — edit these, not code:

- `config/seed/agents.yaml` — your agents (names, model tier, routing metadata, channel)
- `config/seed/activities.yaml` — the scheduled flows (cron, agent, config) — also editable live from the admin panel at `/admin/flows`
- `config/seed/{channels,resources,todoist}.yaml` — channels, tracked resources, task projects
- `personalities/<agent>/{SOUL,AGENTS,USER,MEMORY}.md` — each agent's persona and what it knows about you
- `config/.env` — secrets and endpoints (gitignored; never commit real keys)

The admin panel (served by core at `/`) is the visibility surface: flows,
interactions, knowledge, Google integrations, notification budget.

## Tests

```bash
docker compose up -d postgres        # real Postgres on :25432 for tests
pytest                               # asyncio_mode=auto
ruff check .
```

## Deployment

CI/CD is wired for the maintainer's Docker Swarm (GitHub Actions build + deploy
on merge to `main`). The deploy jobs are gated to the upstream repository, so a
**fork's CI only builds and tests** — it won't try to deploy to infrastructure
you don't have. Point the workflows at your own registry/runner to deploy your
fork. Infra/ops runbook: [`docs/production.md`](docs/production.md).

## License

MIT — see [`LICENSE`](LICENSE).
