# AEGIS v3 Architecture

AEGIS v3 is a flow-first personal AI orchestration platform. It coordinates 4 named personalities over 28 Temporal flows, a chat surface with 42 tools gated per-personality, native ingest connectors, and a native Postgres+pgvector knowledge store for semantic search and query-time RAG.

This document is the canonical reference for what the running system does today. For commands and setup, see [`development.md`](../development.md). For deployment, see [`production.md`](../production.md). For where the architecture is **going** — a kernel + SDK + capability-plugin redesign for productization — see the reference stubs in [`sdk-stubs/`](sdk-stubs/README.md).

## Services

| Service | Package | Port | Purpose |
|---------|---------|------|---------|
| Core API | `aegis-core` | 8080 | REST API, personalities, chat, connectors, admin panel SPA |
| Worker | `aegis-worker` | — | Temporal workflows (28 flows), activities, schedule sync |
| Comms | `aegis-comms` | 8081 | Channel adapter — **Slack** (Socket Mode); FastAPI delivery server + interaction cards. Core reaches it via `AEGIS_COMMS_URL`; idles as a no-op until Slack is configured |
| Postgres | pgvector/pg16 | 25432 | Primary database (migrations 001 → 008) |
| Temporal | auto-setup | 7233 | Workflow orchestration (task queue `aegis-main`) |
| Temporal UI | temporalio/ui | 8233 | Workflow debugging |
| Ollama | `--profile local-llm` | 11434 | Optional bundled local model server (point the LLM backend at it for fully-local) |
| ElevenLabs | hosted vendor | api.elevenlabs.io | Media transcription (Scribe STT) + per-persona voice notes (TTS) |

Knowledge is **native to Core** (Postgres + pgvector, `services/knowledge.py`) — there is no separate knowledge service. Deployment is a fork-owned concern: this repo's CI is test-only, and images are built/deployed by your own infrastructure — see [`production.md`](../production.md). ElevenLabs is a hosted vendor (no in-cluster GPU service).

## Personalities

4 named AI personalities. Loaded from the `agents` table; persona content (kinds `soul`/`agents`/`user`/`memory`) lives in the `agent_personalities` table and is edited from the admin UI. The files at `personalities/<id>/{SOUL,AGENTS,USER,MEMORY}.md` are import-on-first-boot starter examples only.

**Behavior is keyed on capability tags, not identity** (issue #36). Nothing in the code branches on a literal agent id anymore; behavior is driven by what each agent self-declares in the `agents` table:

- **`capabilities`** (JSONB) doubles as the behavior-tag store. Closed vocabulary in `core/src/aegis/agent_tags.py`: `gtd` (task/clarify/review flows), `finance` (money/receipts/market), `research` (knowledge/RSS), `infra` (homelab/k8s/alerts, slow async ops). Resolve a tag → its owning agent via `services/agents.py::resolve_tag`/`agents_by_tag` (core) or the `AgentRegistryActivities.resolve_agents` Temporal activity (worker, since workflows can't hit the DB). Zero holders ⇒ the feature skips with a logged warning; never a crash.
- **`metadata`** (JSONB) carries the per-agent routing knobs: `intent_keywords` + `intent_description` (chat routing), `mention_aliases` (chat/clarify/Slack @-addressing, default `[id]`), `async_dispatch` (Slack sync vs. async), `tool_set` (per-agent chat tools), `knowledge_domains` (entity-boost / RAG), `voice_lines`.
- **Ownership** of scheduled flows is `activities.agent_id` (seeded in `config/seed/activities.yaml`, reconciled by `schedule_sync`); a flow addresses cross-agent targets by resolving a tag, not a literal id.
- **Editable in the UI:** the Agents → **Behavior** tab (`AgentDetail.tsx`, `PATCH /api/agents/{id}` + `GET /api/agents/meta/options`) sets tags, tool set, aliases, intent description, and async-dispatch. `seed.py` treats `capabilities`/`metadata` as **DB-owned once set** — yaml only seeds first boot and adds *new* metadata keys on upgrade, so UI edits survive restarts. (Consequence: a new capability tag added to the yaml does **not** auto-apply to an existing deployment — tick it once in the Behavior tab.)

To add or repurpose an agent, create it and check the capability tags that describe its role — no code changes. The four shipped agents map `Sebas→gtd`, `Raphael→research`, `Maou→finance`, `Pandora's Actor→infra`; each also owns the scheduled flows listed below.

| Personality | Role | Model tier | Workflows owned (per `config/seed/activities.yaml`) |
|-------------|------|------------|-----------------|
| **Sebas** | Executive assistant | `smart` | `GmailIngestFlow`, `CalendarIngestFlow`, `TodoistSyncFlow`, `ClarifyFlow`, `DailyReviewFlow` + `WeeklyReviewFlow`, `SocialPublishFlow`, `MemoryReflectionFlow` |
| **Raphael** | Research + knowledge | `smart` | `DailyBriefingFlow`, `IntelligenceScanFlow` (×3 sources), `RaindropIngestFlow`, `RssIngestFlow`, `DriveSyncFlow` |
| **Maou** | Finance | `smart` | `MoneyProcessFlow`, `MoneyHygieneDailyFlow`, `ReceiptIngestFlow`, `SubscriptionAuditFlow` |
| **Pandora's Actor** | Infrastructure | `smart` | `ServiceDriftFlow`, `CertRadarFlow`, `SentryPollFlow`, `DeliveryWatchdogFlow`, `CleanupFlow`, `WorkspaceRepoSyncFlow`, `VercelProjectSyncFlow`, `GitHubAlertFlow` (PR notifier, webhook-driven) |

**Utility flows (driven by their callers, not owner-scheduled):**
- `InteractionFlow` — man-in-the-middle handoff; any flow spawns this as a child to wait for a human response.
- `AlertInvestigationFlow` — reusable alert classifier + investigator; called by `SentryPollFlow`, the Grafana/Alertmanager webhook, and the Pandora APP-<n>: clarify branch.
- `GitHubAlertFlow` — webhook-driven PR notifier (`pandoras-actor`). On `pull_request` `opened`/`reopened`/`ready_for_review` for a repo tracked in `resources`, posts a Slack card via `HomelabActivities.notify_pr_event`. No longer investigates issues (repurposed 2026-06-27).
- `AgentChatReplyFlow` — synthesizes a personality reply for a Todoist task comment; spawned by `ClarifyFlow` for `@sebas`/`@raphael`/`@maou`/`@pandora` followups.

**Model tiers**: `agents.model_tier` is `fast` | `balanced` | `smart`, resolved from `config/models.yaml` against whatever LLM backend you configure on the admin **Models & Providers** page — a LiteLLM proxy, a hosted key (Claude / OpenAI / OpenRouter), or a local Ollama. Point the tiers at any models you like; no `ollama/` prefix — proxies serve bare names.

## Interactions Primitive

Any open handoff from a workflow to a human is an `interactions` row.

**Kinds:** `approval` (binary), `choice` (pick one of N), `input` (free-text; Admin UI primary), `draft_review` (edit-and-submit; Admin UI primary), `ack` (single acknowledge button).

**Callback format** (uniform across all interactions): `interaction:{interaction_id}:{response_value}`.

**End-to-end:**

```
parent flow
  └─ await InteractionFlow child (creates row, posts a card via the active channel)
      ├─ Slack button tap (Socket Mode) → /api/interactions/{id}/resolve → signal
      ├─ Admin click → /api/interactions/{id}/resolve → signal
      └─ Timeout → apply timeout_policy (archive | hold)
  resume parent with response
```

## Todoist GTD structure

Todoist owns the GTD layer. **Work-streams are labels, not projects.** A small,
fixed set of containers holds tasks; every other dimension is a label, and
multi-step work uses Todoist **subtasks** (not sub-projects).

- **Containers (projects):** `Inbox` (native, capture point), `Next` (active
  actionable tasks), `Someday / Later` (holding list — the single source of
  truth for "someday"; the weekly review's primary resurface surface). These
  are the only managed projects; `settings.todoist_managed_project_ids` keys
  are `inbox` / `next` / `someday`. Bootstrap adopts existing projects by name
  before creating, so it never duplicates them.
- **Work-streams:** `project/<name>` labels (e.g. `project/bcp`,
  `project/screener-p`), mirroring the `area/<name>` convention.
- **State / delegation:** `@reference` (label); delegation is `@waiting` plus a
  `delegate/<person>` label (the "Delegated" view is the `@@waiting` filter).
  There is **no `@someday` label** — someday is the project above.
- **Scheduled** is the native *Upcoming* view (any task with a due date), not a
  container.

`ClarifyFlow` classifies Inbox tasks into `trash | reference | someday | 2_min
| next_action`. `someday` moves the task into the `Someday / Later` project;
`next_action` is a label update (multi-step → add subtasks). There is no
`project_seed` classification. `list_projects` (chat tool) enumerates the
`project/*` labels with open-task counts.

## Todoist Comment Channel

Parallel inbound surface for conversational replies from a specific personality. Any Inbox task labelled with an addressable agent (any active agent's `mention_aliases` — `@sebas`, `@raphael`, `@maou`, `@pandora` for the shipped set) that receives a user comment routes through `ClarifyFlow` → `AgentChatReplyFlow`, which produces a personality-voiced reply in the agent's Slack channel AND as a mirrored Todoist comment on the same task. The addressable-agent list, the classifier's assignee vocabulary, and the `@alias → id` mapping are all derived from `mention_aliases` (cached in `clarify.py`), not hardcoded.

```
user comment on @<agent>-labelled Inbox task
  └─ Todoist webhook → todoist_notes row, last_note_at bumped
      └─ ClarifyFlow (15-min scheduled tick)
          ├─ find_unclassified_items — eligibility: source_tag OR APP-<n>: OR `@<agent>` label,
          │   AND MAX(posted_at) over USER notes > last_clarified_at
          ├─ classify_one → `<agent>_followup` (rules engine, conf 1.0)
          └─ apply_outcome → spawn_kind="agent_chat_reply"
              └─ AgentChatReplyFlow (abandoned child)
                  ├─ ChatActivities.synthesize_reply → POST /api/chat/agent-reply (core)
                  ├─ DeliveryActivities.send_message → agent's channel
                  └─ ClarifyActivities.post_agent_reply_comment → "[Agent reply @ HH:MM UTC agent=<id>]"
```

**Invariants:**

1. **Agent id mapping** in the spawn payload: `target_agent = classification.replace("_followup", "")` for sebas/raphael/maou; **`pandoras-actor`** (NOT `pandora`) for `pandora_chat_followup` — the `@pandora` label is just a prefix.
2. **Jira route is sacred** — `@pandora` `APP-<n>:` tasks always route to `pandora_followup` (re-runs `AlertInvestigationFlow`). Non-APP `@pandora` tasks route to the new `pandora_chat_followup`.
3. **User-only eligibility** — `find_unclassified_items` filters notes via `MAX(posted_at)` over notes whose content does NOT match `[ClarifyFlow @`, `[Agent reply @`, or `%Workflow run:%`. Without this filter, every agent reply re-eligibles the task into a self-perpetuating 15-min loop.
4. **Activity timeout** for `synthesize_reply` is `TIMEOUT_CHAT_REPLY=600s` + `RETRY_ONCE`; CoreClient httpx ceiling for `ChatActivities` is `550s`. Smart-tier agents with heavy tools (kimi SSH, deep KS search) legitimately run 3-6 min.
5. **Recent-thread transcript** in the synthetic input — `_build_agent_synthetic_input` includes the last 15 notes on the task (user + prior agent replies, oldest first) so the agent sees its own past turns and doesn't repeat itself.

Per-agent pre-fetch hooks are gated on the target's **behavior tag**, not its id: a `research` agent gets KS context (`KnowledgeConnector.search`); a `finance` agent gets recent receipts from the `maou.receipt_email` schema (see #43 for the pending schema rename); agents without those tags have no hook (their context IS the task; their tool sets fetch the rest).

Self-loop guarded by the `AGENT_REPLY_PREFIX = "[Agent reply @ "` constant — webhooks.py recognises it just like `CLARIFY_NOTE_PREFIX`.

Cross-agent handoff via agent-written `@<other>` labels is **out of scope for v1** — user-initiated only.

## Flows

28 Temporal workflows on task queue `aegis-main`. Flow code lives in `worker/src/aegis_worker/flows/`. Most scheduled flows carry `agent_id` in their config dataclass so `WorkflowRunRecorderInterceptor` can populate `workflow_runs.agent_id`; utility flows (`InteractionFlow`, `AlertInvestigationFlow`, `AgentChatReplyFlow`) take it from their caller.

Owner-scheduled flows are listed in the Personalities table above. The remaining named flows:

- `TodoistSyncFlow` — 5-min Sync API tick: incremental sync from Todoist, drains the outbox.
- `DailyBriefingFlow` (Raphael, daily) — gathers interactions/activity/knowledge → synthesizes → the active comms channel (Slack).
- `DailyReviewFlow` / `WeeklyReviewFlow` (Sebas) — daily + weekly digests; logs to `review_digest_log`, spawns acknowledgement InteractionFlow.
- `MemoryReflectionFlow` (Sebas, nightly) — per-agent memory consolidation: caps `agent_memory` (prunes oldest/lowest-importance beyond `keep`).
- `DriveSyncFlow` (Raphael) — incremental ingest of a tracked Google Drive folder into the knowledge store; no-ops until a folder is configured.
- `DeliveryWatchdogFlow` (Pandora's Actor, hourly) — catches interaction cards that were never delivered and checks comms `/api/health` inbound liveness; on outage captures a Todoist Inbox task (the chat channel is the thing that's down).
- `WorkspaceRepoSyncFlow` (Pandora's Actor, daily) — scans the coding host's workspace for git checkouts and makes the `resources` table mirror it (one `kind='repository'` row per checkout).
- `VercelProjectSyncFlow` (Pandora's Actor, daily) — mirrors Vercel personal + team projects into `resources` (`kind='vercel_project'`), linking GitHub repos for alert investigations.
- `MoneyProcessFlow` (Maou, child) — single-email money hygiene: `store_receipt_email` → `load_receipts` → `classify_and_extract` → `upsert_charges`. Spawned by `GmailIngestFlow` on `financial`/`payments` tags and by the weekly `ReceiptIngestFlow` safety-net. `ParentClosePolicy.ABANDON`; idempotent on `message_id` at the `store_receipt_email` step.

### Email Triage Tag Fan-out

`GmailIngestFlow` runs hourly. For each new message it calls `classify_email`, which returns a `category` AND a list of `tags` from a closed vocabulary (`financial`, `payments`, `receipt`, `subscription`, `security`, `calendar_invite`, `shipping`, `travel`, `health`, `work`, `personal`, `newsletter`, `technology`, `support`). Tags are additive and orthogonal to the routing category.

Specialist flows subscribe to tag subsets and run as abandoned children:

| Tag subset | Child flow | Owner |
|------------|------------|-------|
| `{financial, payments}` | `MoneyProcessFlow` | Maou |

### Alert Investigation

`AlertInvestigationFlow` is the unified investigation pipeline. Steps:

1. Skip if alert already resolved on arrival.
2. **Signature dedup** — `build_alert_signature` collapses Sentry stack-frame variations to `sentry-class:<service>:<metadata.type>`; if an open `@pandora` task with the same signature exists, post "another occurrence" comment and exit early.
3. **Fingerprint dedup** — filters `audit_log` on `action='alert_investigated'`.
4. **Mute short-circuit** — `check_alert_mute` against `alert_mutes`.
5. **Gate 1** (`requires_approval` only) — `InteractionFlow` child: Investigate / Skip / Mute 24h.
6. **Verification delay** — per-severity sleep + `check_alert_resolved` recheck.
7. **Resource resolution** — deterministic service-match then LLM picks the owning repo from the `resources` table. `metadata.path` is the repo's workspace-relative checkout path (e.g. `acme/bcp`), maintained by `WorkspaceRepoSyncFlow` — there is no per-run JIT clone; a missing checkout fails the kimi path and falls back to the LLM-only investigation.
8. **Knowledge context** — `gather_alert_knowledge` prepends `runbooks/<AlertName>.md` (if present and non-stub), then appends prior-incident context from KS.
9. **Investigation** — coding-CLI (kimi/claude) via `run_investigation` when a `resource_path` is available; LLM fallback otherwise. The run executes on the effective host (the configured kimi host when reachable, else the base coding host — see [`infrastructure.md`](../infrastructure.md)); that host is threaded back through the read-back poll, worktree cleanup, and PR push so they all happen where the branch was made.
10. **Haiku assessment** → structured verdict: `resolved` / `not_actionable` / `actionable` / `inconclusive`.
11. **Gate 2** (non-Jira, non-self-resolved verdicts) — Open PR(s) / Mute 24h / Acknowledge / Discard via Slack. Jira-source runs (`source=='todoist-jira'`) bypass Gate 2 by contract.
12. Comms notification (Slack) + Todoist task comment + audit log write.

When a `todoist_task_id` is on the alert (pandora APP-<n>: clarify path), the flow attaches to the existing task; otherwise `capture_to_inbox(extra_labels=["@pandora"])` creates one upfront. Start + final comments are posted via `AlertActivities.post_task_note`.

## Connectors

7 public connectors in `core/src/aegis/connectors/` (plus `_base.py`, `_ssh.py` and `_subprocess.py` private helpers). Knowledge is not a connector anymore — it is the native Core service `services/knowledge.py` (see [Knowledge](#knowledge-native-rag) below).

| Connector | Role |
|-----------|------|
| **TodoistConnector** (`todoist.py`) | Todoist Sync API client + outbox + per-command status checks (`check_sync_status`) — see [`todoist-sync-protocol.md`](todoist-sync-protocol.md). |
| **RemoteScriptConnector** (`remote_script.py`) | SSH to the designated coding host. Runs predefined infra scripts and coding-CLI (kimi/claude) runs for alert investigations against fixed checkouts under the configured repo base (workspace-relative `metadata.path`, no JIT cloning). The host, SSH key, engines, accounts, org routing, and optional separate kimi host are configured on an infra registry entry's **Coding agent** block (env `AEGIS_REMOTE_SCRIPT_*` is the fallback) — see [`infrastructure.md`](../infrastructure.md). |
| **HomelabConnector** (`homelab.py`) | Docker Swarm ops over SSH (`list_services`, `service_ps`, `restart_service`) + `probe_tls` cert checks. Gated by the `homelab_enabled` setting. Kubernetes ops go through the infrastructure registry instead (`services/infra.py` — see [`infrastructure.md`](../infrastructure.md)). |
| **SearchConnector** (`search.py`) | SearxNG HTTP client. Used by the `research_topic` chat tool. |
| **FinanceConnector** (`finance.py`) | Provider-agnostic web market data (keyless `yahoo` / `stooq` quote providers, selected via the Finance integration config: `finance_provider` / `finance_api_key` / `finance_indices`). Powers Maou's `get_quote` / `get_market_overview` tools and the `/api/market/summary` briefing section; `get_finance_news` rides SearchConnector. |
| **SocialConnector** (`social.py`) | Social posting for `SocialPublishFlow` — native X/Twitter OAuth or a self-hosted Postiz transport — see [`social-publishing.md`](../social-publishing.md). |
| **VercelConnector** (`vercel.py`) | Vercel REST client — project inventory (`VercelProjectSyncFlow`) and Pandora's deployment chat tools. |

## Chat with Tool Calling

`POST /api/chat` (non-streaming) and `POST /api/chat/stream` (SSE). 42 tools in `CHAT_TOOLS`, gated per-personality via `AGENT_TOOL_SETS` in `core/src/aegis/services/chat.py`.

Tool loop: max iterations bounded by the service config; per-tool timeout via `asyncio.wait_for` (default `tool_timeout_seconds`, with per-tool overrides in `_TOOL_TIMEOUT_OVERRIDES` for long-running tools like `aegis_self_diagnose`); result truncation per `max_bytes`. Every tool call recorded to `chat_tool_calls`.

Per-personality tool counts (authoritative — counted from `AGENT_TOOL_SETS`):

| Personality | Tools |
|-------------|------:|
| Sebas | 15 |
| Raphael | 11 |
| Maou | 13 |
| Pandora's Actor | 32 |

Worker startup validator: refuses to boot if a personality references a tool that isn't in `CHAT_TOOLS`. (Validation runs at Core boot via `_validate_agent_tool_sets`.)

### Proactive knowledge context

Before every LLM call, `_gather_knowledge_context()` runs a semantic chunk search via the native `KnowledgeService.search`. Results are boosted per-personality domain affinity, capped at 2000 chars injected into the system prompt. 5s timeout (`knowledge_context_timeout_seconds`) — never blocks chat. Each result that survives the threshold is logged to `knowledge_injection_log`.


## API

30 route modules in `core/src/aegis/api/routes/`. All `/api/*` routes require Basic auth or `X-API-Key` (API keys are generated from the admin **Integrations** page and stored encrypted in the DB; `AEGIS_API_KEY` is the env fallback). Auth can be switched off entirely with `AEGIS_AUTH_DISABLED=true` — for deployments fronted by an authenticating proxy only. Exceptions: `GET /health` and webhook paths under `/api/webhooks/*` (HMAC-verified).

Route modules: `activities`, `agents`, `api_key`, `audit`, `capture`, `channels`, `chat`, `gmail_reauth`, `health`, `homelab`, `infra`, `infra_admin` (infrastructure registry CRUD + provisioning + k8s/cloud ops — see [`infrastructure.md`](../infrastructure.md)), `integrations`, `interactions`, `knowledge`, `llm_backend`, `market`, `mcp`, `money`, `observability`, `overview`, `references`, `resources`, `settings`, `slack`, `social_auth`, `system_status`, `temporal`, `todoist`, `webhooks`.

## Admin UI

React SPA served by Core at `/`. Top-level pages include: Overview, Interactions, Workflows, Agents + Agent detail (incl. the personality editor), Chat, Knowledge / Content / Content detail, Channels, Flows & Integrations, Models & Providers, Todoist, Resources, AuditLog, Money, Market, References, System Monitoring, Settings, Slack config, and Infra (the infrastructure registry — register SSH hosts / the swarm / k8s clusters / cloud accounts with encrypted pasted credentials; see [`infrastructure.md`](../infrastructure.md)). The admin UI is the primary configuration surface: agents, personalities, channels, schedules, integration secrets, the LLM backend, and infrastructure are all DB-owned and edited here — seed YAML and env vars are first-boot/bootstrap inputs, not the ongoing source of truth.

## Comms (Slack)

Slack Socket Mode (`slack_sdk`) + FastAPI delivery server (port 8081). One Slack channel per personality. Slack is optional: tokens are configured from the admin **Slack** page (stored encrypted in the DB; `AEGIS_SLACK_*` env is the dev fallback) and comms idles as a no-op until they exist — interaction cards always land in the admin UI's **Interactions** inbox (web) regardless. Core reaches the delivery server via `AEGIS_COMMS_URL`.

- Agent→channel mapping populated from `agents.slack_channel_id` (falls back to resolving `#aegis-<short>` by name).
- Message bodies are authored in a light HTML dialect and converted to Slack mrkdwn (`html_to_mrkdwn`); all user-controlled strings pass through `_safe()` (`html.escape()`).
- Interaction cards render as Block Kit with the uniform callback identity `interaction:{id}:{value}` — resolved by `/api/interactions/{id}/resolve`. Comment-channel reply callbacks use a separate `agent-chat-reply-…` workflow id namespace.
- Approval/choice/ack cards also carry an optional free-text note input (`correction_note`). Slack includes the message's input state with every button tap, so a typed note rides along as `response.note` — which core records as a durable `agent_memory` lesson (the learning loop).
- The delivery server exposes `/api/deliver/message`, `/api/deliver/document`, `/api/deliver/voice`, `/api/deliver/card`, `/api/comms/delete` and `/api/health` (inbound Socket Mode liveness).

## Database

PostgreSQL 16 + pgvector. Migrations 001 → 008 in `migrations/` (001 is the squashed baseline); auto-apply on Core startup, tracked in `schema_migrations`.

**Core primitives** — `agents`, `agent_personalities`, `agent_memory`, `activities`, `interactions`, `workflow_runs`, `resources`, `channels`, `settings`, `infra`.

**Chat** — `chat_history`, `chat_tool_calls`.

**Todoist GTD layer** — `todoist_projects`, `todoist_tasks`, `todoist_notes`, `todoist_labels`, `todoist_outbox`, `todoist_sync_state`, `todoist_webhook_events`, `todoist_capture_idempotency`, `gtd_clarify_log`.

**Triage feedback** — `triage_state`, `triage_accuracy`.

**Knowledge (native RAG)** — `knowledge_content`, `knowledge_chunks` (pgvector embeddings), `knowledge_source_quality`, `knowledge_injection_log`.

**Maou (finance)** — `maou.recurring_charge`, `maou.receipt_email`, `maou.renewal_alert`, `maou.subscription_digest`.

**Pandora's Actor (infra)** — `pandoras_actor.homelab_drift`, `pandoras_actor.cert_expiry`.

**Social publishing** — `social_accounts`, `social_outbox`.

**Alert governance** — `alert_mutes`, `pending_prs`, `alert_dedup_index` (Sentry signature dedup).

**Reviews / notifications** — `review_digest_log`, `notification_log`.

**Observability** — `llm_calls`, `connector_calls`, `audit_log`.

**Idempotency** — `ingest_idempotency`.

## Activity-Driven Schedules

The `activities` table drives Temporal schedules. Worker on startup queries active rows with cron, registers each as a Temporal schedule (create-or-update), deletes orphans. Schedule names match `activities.slug`. Seed in `config/seed/activities.yaml`.

## Observability

| Table | What it records |
|-------|-----------------|
| `llm_calls` | Every `LLMClient.think()` / `chat()` call — model, input_tokens, output_tokens, latency_ms, ttft_ms |
| `connector_calls` | Every connector call — name, action, status, ms, external_ref |
| `audit_log` | Interactions, settings changes, webhooks — columns `target_type` / `target_id` |
| `chat_tool_calls` | Every tool call during a chat turn |
| `workflow_runs` | Temporal workflow start / complete / fail via `WorkflowRunRecorderInterceptor` |

Distributed tracing: OTel SDK + JSON-formatted logs with `trace_id`/`span_id` injected from the active span. Per-package `telemetry.py` + `logging_config.py` modules. Gated on `OTEL_ENABLED=true`. Auto-instrumentation covers FastAPI, asyncpg, httpx, requests. The Worker registers `temporalio.contrib.opentelemetry.TracingInterceptor` so trace context flows through workflow headers automatically.

## Knowledge (native RAG)

Knowledge is a native Core service (`core/src/aegis/services/knowledge.py`, replacing the old external knowledge-service): ingested content is chunked and embedded into Postgres + pgvector (`knowledge_content` / `knowledge_chunks`), with embeddings produced by the configured `embedding_model` (default `nomic-embed-text`) through the LLM backend. It captures ephemeral content streams (RSS, Raindrop, HN/news/finance scans, Drive folders, GTD references, URL/upload/folder seeds) via ingest flows. `search` does semantic chunk retrieval; `ask` synthesizes an answer across retrieved chunks with the local LLM. No knowledge graph.

References-as-knowledge: a Todoist task classified `@reference` is captured via `ingest_reference_to_ks` (raises on transient failures, returns a verdict on permanent ones); the verdict is dispatched by `_dispatch_reference_verdict` in `ClarifyFlow` to either complete the task (success) or demote to `@to-read` (permanent failure). The `/api/references` route surfaces the live library from the knowledge store; `/api/references/failures` is the `@to-read` lane from the Todoist projection.

## Config

- `config/.env` — bootstrap secrets and endpoints (copy from `.env.example`); pydantic-settings adds the `AEGIS_` prefix. Integration secrets (Slack, Todoist, GitHub, API keys, infra credentials, …) are entered in the admin UI and stored **encrypted in the DB** — env vars are the fallback, not the primary store.
- `config/seed/{agents,activities,channels,resources,todoist}.yaml` — seed data loaded via FastAPI lifespan (`channels.yaml` is first-boot starter examples only — channels are DB-owned and managed from the admin panel's Channels page / `/api/admin/channels` afterwards)
- `config/models.yaml` — model tier mapping resolved against the configured LLM backend
- `personalities/<agent>/{SOUL,AGENTS,USER,MEMORY}.md` — starter persona examples, imported into the `agent_personalities` table on first boot (DB/admin-UI-managed afterwards)
- `runbooks/<AlertName>.md` — per-alert runbooks (baked into worker image); stubs containing `TODO: fill in` are treated as absent
