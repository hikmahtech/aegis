# Meet AEGIS: My Weird Little Operating Layer

Every week there's another agent demo, another workflow canvas, another pitch about software running software for us. OpenClaw, browser agents, Zapier, n8n, Claude Code, MCP servers, LLM cron jobs — nobody needs convincing that software can glue tools together anymore.

So AEGIS isn't interesting because it automates Gmail, Calendar, GitHub, Sentry, RSS, bookmarks, finance, or infra. That's table stakes.

The question I actually care about is smaller:

> Can software learn the shape of one person's life well enough to interrupt less, act more carefully, and make an ordinary day feel quieter?

AEGIS stands for *Autonomous Executive Guild Intelligence System* — a deliberately serious name for a deeply personal system held together by FastAPI, Temporal, Postgres, Slack, and stubbornness. It sits between me and the systems that keep asking for attention. Instead of me checking ten places, it watches them and decides what's worth surfacing.

*(This started life on Slack; the chat channel is Slack now. I've updated the references below, but this post is otherwise the original 2026-04 origin story.)*

When something is worth surfacing, the rule is simple: don't just notify me. Bring a proposed next step.

## Four Personalities, Not One Agent

"One agent that can do everything" is a bad interface. AEGIS has four, each with its own tools, model tier, memory, tone, and Slack channel:

- **Sebas** — executive assistant. Owns Gmail, GitHub, Calendar, daily brief.
- **Raphael** — research. Owns RSS, Raindrop, knowledge ingestion, topic tracking.
- **Maou** — finance. Owns receipts, renewals, subscriptions, market data.
- **Pandora's Actor** — infrastructure. Owns Sentry, backups, certs, drift, homelab ops.

They're permission boundaries with names. The anime flavouring is the point, not the bug.

## The Spine

Three Python services and a knowledge layer:

- `aegis-core` — FastAPI. API, Postgres, connectors, admin UI, chat.
- `aegis-worker` — Temporal. Anything that retries, schedules, waits for signals, or gates on approval becomes a workflow.
- `aegis-comms` — Slack bot (Socket Mode) + delivery server. One channel per personality.
- knowledge layer — the semantic memory: a native Postgres + pgvector RAG store (originally a separate `knowledge-service`, since folded into core). Fact storage, contradiction checks.

Around that: LiteLLM for model routing, local Ollama (qwen3:14b, gemma4) where it fits, Claude Haiku/Sonnet/Opus when it doesn't, SearXNG for search, pgvector for embeddings. Production is a small Docker Swarm cluster at home — three nodes, not a VPC.

## The Core Primitive: `interactions`

Every older half-pattern — decisions, pending claims, ad-hoc callbacks, state hiding in the wrong place — collapsed into one idea.

An `interaction` is:

- a row in Postgres,
- a card in Slack or the admin UI,
- a Temporal workflow waiting on a signal.

Five kinds cover everything: `approval`, `choice`, `input`, `draft_review`, `ack`. Callbacks are uniform (`interaction:{id}:{value}`). Timeouts are declarative — archive, auto-reject, auto-approve, or hold.

If AEGIS needs me, it creates an interaction. If it doesn't, it stays quiet. That one idea changed how the system feels. It's not a notification machine anymore. It's a queue of interruptions that had to earn their way in.

## What The Flows Actually Do

23 Temporal workflows on one task queue. A sample of what runs on a given day:

- **GmailIngestFlow** — hourly per account. Classifies each email as noise, transactional, or needs-me. Most vanish into the record. The few that matter become Slack cards I can approve, dismiss, or turn into a reply.
- **GitHubAlertFlow** — webhook. Routes through `AlertInvestigationFlow` which knows what kind of repo it came from and when to ask before spending tokens on code.
- **SentryPollFlow** — hourly. Waits long enough to avoid chasing blips, deduplicates against prior investigations, pulls the relevant runbook, asks before acting.
- **ReceiptIngestFlow / RenewalRadarFlow** — extract structured data from receipts, flag subscriptions about to renew.
- **IntelligenceScanFlow / RssIngestFlow / RaindropIngestFlow** — poll, dedupe, score, summarise, push into the knowledge graph.
- **ServiceDriftFlow / BackupAuditFlow / CertRadarFlow** — quiet infra watchdogs. They only make noise when something actually drifts.
- **DailyBriefingFlow** — once a day. Answers the question most dashboards avoid: *what changed that I should actually care about?*

Two reusable flows do a lot of the heavy lifting:

- **AlertInvestigationFlow** — verification delay, dedup, LLM verdict (`resolved` / `actionable` / `auto_fixable` / `not_actionable`), optional human gate, resource resolution.
- **InteractionFlow** — the man-in-the-middle child any flow spawns when it needs me.

## Chat With Tool Calling

`POST /api/chat` is the other half of the system. Each personality gets a curated tool set — 29 tools total, gated per-agent:

- Sebas can trigger workflows and list interactions.
- Raphael can research topics, query facts, check contradictions.
- Maou can pull market regimes, forecasts, and trade decisions from ClickHouse.
- Pandora can restart services, list deployments, update runbooks.

Before every reply, a parallel semantic search + entity KG lookup runs in the background, gets boosted against the personality's domain affinity, and injected into the system prompt. 5-second timeout; never blocks the chat.

## Why This Instead Of An Agent Platform

The automation race asks: *how much can we let the machine do?*

AEGIS asks: *where should the human re-enter?*

I don't want a giant autonomous employee running around my accounts. I want a careful backstage system that knows when to ignore things, when to prepare context, and when to ask.

Because AEGIS is built for exactly one person, it can be strange in ways a product can't. No onboarding. No generic roles. No clean abstractions for a hypothetical second customer. If the infra personality has a theatrical name and a specific set of SSH scripts, that's not a branding problem. It's the point.

The value isn't that AEGIS can click buttons for me. The value is that it holds context across days — workflows, alerts, notes, decisions — and turns that context into a small number of useful interruptions.

Less command center. More trusted household system.

## Why It's Not Open Source Yet

I do want to open-source it eventually. Not so everyone runs my exact life stack, but because the patterns travel: durable workflows, human approval gates, personality-scoped tools, local-first inference, personal knowledge as infrastructure, one primitive for all human-in-the-loop handoffs.

Right now it's wired too tightly into real systems — secrets, webhook assumptions, homelab paths, personal shortcuts. Before it ships, it needs a risk pass and a cleaner line between the reusable system and my own weird environment.

## The Real Reason I Keep Building It

AEGIS isn't trying to win the automation race. It's trying to make my day quieter.

That's less impressive, but it's the part I care about. A good personal system doesn't need to feel like magic. It needs to notice the boring thing, prepare the next step, and ask for attention only when attention is actually useful.

Two rewrites in, that still feels worth building.

Not because it's unique.

Because it's mine, and because it's slowly becoming useful in exactly the places where generic automation usually gives up.
