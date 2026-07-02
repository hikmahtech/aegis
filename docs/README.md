# AEGIS v3 Documentation

**Autonomous Executive Guild Intelligence System** — a flow-first personal AI orchestration platform.

| Document | Covers |
|----------|--------|
| [Architecture overview](architecture/overview.md) | Services, personalities, flows, activities, connectors, chat tools, primitives, schema, API |
| [Productization architecture](architecture/productization.md) | **Target-state design**: kernel + SDK + capability plugins, ports/adapters, event spine + read-models, permission tiers, lifecycle/entitlements, bundles, migration plan |
| [Todoist sync protocol](architecture/todoist-sync-protocol.md) | Per-command status checks, outbox, comment-loop guard, watermark invariant |
| [Infrastructure registry](infrastructure.md) | Registering SSH hosts / the swarm / k8s clusters from the admin UI: encrypted credentials, kubeconfigs (incl. EKS exec-plugin auth + AWS profiles), read-only gating, chat contexts, `EXTRA_CLOUD_CLIS` image build arg |
| [Local development](development.md) | Docker Compose, setup, running services, adding flows/tools |
| [Production](production.md) | Docker Swarm, CI/CD, alert routing, runbooks, comms/Slack debugging |

The engineering rulebook for AI agents working in this repo lives at [`AGENTS.md`](../AGENTS.md). Per-personality rules live in `personalities/<id>/`.
