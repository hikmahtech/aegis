# AEGIS v3 Documentation

**Autonomous Executive Guild Intelligence System** — a flow-first personal AI orchestration platform.

| Document | Covers |
|----------|--------|
| [Architecture overview](architecture/overview.md) | Services, personalities, flows, activities, connectors, chat tools, primitives, schema, API |
| [SDK stubs](architecture/sdk-stubs/README.md) | **Target-state reference**: the plugin contract and provider ports for a future kernel + SDK + capability-plugin redesign (non-running stubs) |
| [Todoist sync protocol](architecture/todoist-sync-protocol.md) | Per-command status checks, outbox, comment-loop guard, watermark invariant |
| [Infrastructure registry](infrastructure.md) | Registering SSH hosts / the swarm / k8s clusters / cloud accounts / the coding host from the admin UI: encrypted credentials, kubeconfigs (incl. EKS/GKE exec-plugin auth + AWS profiles), read-only gating, chat contexts, `EXTRA_CLOUD_CLIS` image build arg |
| [Social publishing](social-publishing.md) | Todoist-scheduled social posting with approval cards; native X OAuth + Postiz transport |
| [Local development](development.md) | Docker Compose, setup, config, auth, adding flows/tools |
| [Production](production.md) | Fork-owned image build + deploy, migrations, config plane, alert routing, comms/Slack debugging |

Starter persona examples live in `personalities/<id>/` (imported into the DB on first boot).
