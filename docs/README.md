# AEGIS Documentation

**Autonomous Executive Guild Intelligence System** — a flow-first personal AI orchestration platform.

| Document | Covers |
|----------|--------|
| [How it works](how-it-works.md) | **Operator's guide** — the mental model, what runs when, GTD, the agent task executor, interactions, the problem hub, and the failure modes worth recognising |
| [Architecture overview](architecture/overview.md) | Services, personalities, flows, activities, connectors, chat tools, capture surfaces, primitives, schema, API |
| [SDK stubs](architecture/sdk-stubs/README.md) | **Target-state reference**: the plugin contract and provider ports for a future kernel + SDK + capability-plugin redesign (non-running stubs) |
| [Todoist sync protocol](architecture/todoist-sync-protocol.md) | Per-command status checks, outbox, comment-loop guard, watermark invariant |
| [Engineering conventions](architecture/conventions.md) | Test and CI rules, key paths, adding flows and chat tools, capability tags, model tiers and token budgets, memory and persona writes, clarify, email triage, settings rows, observability |
| [Domain rules](architecture/domain-rules.md) | GTD and Todoist, knowledge ranking, the `life` schema, interactions, meeting notes |
| [Problem hub](architecture/problem-hub.md) | Problem identity, v1's producers, groups and the Todoist projection (the infra lane moved to DevOps) |
| [Money and trading desk](architecture/money-and-trading-desk.md) | Maou's hledger books, bank parsers, dues, the chart of accounts, the paper trading desk |
| [Research lane](architecture/research-lane.md) | Research flow, RSS feeds, Calibre, tracked topics, areas, story feedback, world/GitHub/tender watches |
| [Obsidian vault](architecture/vault.md) | The notes writer's rules, `vault_layout`, the journal prompt, the `me/` record |
| [Lanes, setup and operations](infrastructure.md) | The problem hub, the books, the research lane, feeds, Calibre, tracked topics, the vault; what moved out of v1 (the infra lane to DevOps, a2-devops; the development lane, the repo registry and the infra registry to Development, a2-development) |
| [Social publishing](social-publishing.md) | Todoist-scheduled social posting with approval cards; native X OAuth + Postiz transport |
| [Local development](development.md) | Docker Compose, setup, config, auth, adding flows/tools |
| [Production](production.md) | Fork-owned image build + deploy, migrations, config plane, features that stay inert until you act, Slack scopes, inbound webhooks, the signed life-data webhook, comms/Slack debugging |

Starter persona examples live in `personalities/<id>/` (imported into the DB on first boot).
