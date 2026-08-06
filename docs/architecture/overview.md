# AEGIS v3 Architecture

AEGIS v3 is a flow-first personal AI orchestration platform. It coordinates 4 named personalities over 38 Temporal flows (32 of them schedule-driven), a chat surface with 51 tools gated per-personality, native ingest connectors, a `life` schema for personal context (people, observations, expiring items, assets), and a native Postgres+pgvector knowledge store for semantic search and query-time RAG.

This document is the canonical reference for what the running system does today. For commands and setup, see [`development.md`](../development.md). For deployment, see [`production.md`](../production.md). For where the architecture is **going** — a kernel + SDK + capability-plugin redesign for productization — see the reference stubs in [`sdk-stubs/`](sdk-stubs/README.md).

## Services

| Service | Package | Port | Purpose |
|---------|---------|------|---------|
| Core API | `aegis-core` | 8080 | REST API, personalities, chat, connectors, admin panel SPA |
| Worker | `aegis-worker` | — | Temporal workflows (38 flows), activities, schedule sync |
| Comms | `aegis-comms` | 8081 | Channel adapter — **Slack** (Socket Mode); FastAPI delivery server + interaction cards. Core reaches it via `AEGIS_COMMS_URL`; idles as a no-op until Slack is configured |
| Postgres | pgvector/pg16 | 25432 | Primary database (migrations 001 → 021) |
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
| **Sebas** | Executive assistant | `smart` | `GmailIngestFlow`, `CalendarIngestFlow`, `TodoistSyncFlow`, `ClarifyFlow`, `DailyReviewFlow` + `WeeklyReviewFlow`, `SocialPublishFlow`, `SocialMetricsFlow`, `MemoryReflectionFlow`, `CuriosityCardFlow`, `ProfileReflectionFlow`, `ExpiryRadarFlow`, `WearableIngestFlow` |
| **Raphael** | Research + knowledge | `smart` | `DailyBriefingFlow`, `DayLogFlow` (×3 rows: nightly / weekly / monthly), `IntelligenceScanFlow` (×3 sources), `RaindropIngestFlow`, `RssIngestFlow`, `DriveSyncFlow` |
| **Maou** | Finance | `smart` | `MoneyProcessFlow`, `MoneyHygieneDailyFlow`, `ReceiptIngestFlow`, `SubscriptionAuditFlow` |
| **Pandora's Actor** | Infrastructure | `smart` | `ServiceDriftFlow`, `CertRadarFlow`, `SentryPollFlow`, `DeliveryWatchdogFlow`, `InfraHeartbeatFlow` (2-min swarm node/service poll, transition-only alerts), `FlowHealthWatchdogFlow` (30-min watchdog over AEGIS's own flows), `LLMSpendGuardFlow`, `AgentTaskSweepFlow`, `CleanupFlow`, `WorkspaceRepoSyncFlow`, `GitHubAlertFlow` (PR notifier, webhook-driven) |

**Utility flows (driven by their callers, not owner-scheduled):**
- `InteractionFlow` — man-in-the-middle handoff; any flow spawns this as a child to wait for a human response.
- `AlertInvestigationFlow` — reusable alert classifier + investigator; called by `SentryPollFlow`, the Grafana/Alertmanager webhook, and the Pandora APP-<n>: clarify branch.
- `GitHubAlertFlow` — webhook-driven PR notifier (`pandoras-actor`). On `pull_request` `opened`/`reopened`/`ready_for_review` for a repo tracked in `resources`, posts a Slack card via `HomelabActivities.notify_pr_event`. No longer investigates issues (repurposed 2026-06-27).
- `AgentChatReplyFlow` — synthesizes a personality reply for a Todoist task comment; spawned by `ClarifyFlow` for `@sebas`/`@raphael`/`@maou`/`@pandora` followups.
- `AgentTaskFlow` — one run per agent-assigned Todoist task; abandoned child of the scheduled `AgentTaskSweepFlow`. Resolves a verb from the task's `source_tag` and executes it behind interaction cards — see [`how-it-works.md`](../how-it-works.md#5-the-agent-task-executor).

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

Todoist owns the GTD layer. **GTD state is labels; containers are yours.**
Multi-step work uses Todoist **subtasks** (not sub-projects).

- **The only managed container is the native Inbox.** AEGIS adopts Todoist's
  built-in Inbox and creates no projects of its own —
  `settings.todoist_managed_project_ids` maps just `inbox`, and
  `config/seed/todoist.yaml` ships `managed_projects: []`. Your work-area
  projects (and the work-stream projects nested under them) are user-owned;
  AEGIS reads them but never reorganises them.
- **State:** `@next` (actionable) / `@someday` (not yet) / `@waiting`
  (blocked or parked) / `@reference` (information, ingested into the knowledge
  store). `@next` and `@someday` are labels — they replaced the old
  `Next` / `Someday / Later` managed projects.
- **Delegation** is an assignee label — `@me` or an agent alias (`@sebas`,
  `@raphael`, `@maou`, `@pandora` for the shipped set, derived from each
  agent's `mention_aliases`) — combined with `@waiting`.
- **Contexts** (`@5min`, `@deep`, `@code`, …) and the pre-seeded filter views
  come from `config/seed/todoist.yaml` at bootstrap.
- **Scheduled** is the native *Upcoming* view (any task with a due date), not a
  container.

`ClarifyFlow` classifies Inbox tasks into `trash | reference | someday | 2_min
| next_action`; both `someday` and `next_action` are label updates (multi-step →
add subtasks), with no `item_move`. There is no `project_seed` classification.
`list_projects` (chat tool) enumerates the user's leaf work-stream projects
(`todoist_projects` rows with a non-null `parent_id`) with open-task counts —
the old `project/*` label convention is retired.

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

Per-agent pre-fetch hooks are gated on the target's **behavior tag**, not its id: a `research` agent gets KS context (`KnowledgeConnector.search`); a `finance` agent gets recent receipts from the `finance.receipt_email` schema; agents without those tags have no hook (their context IS the task; their tool sets fetch the rest).

Self-loop guarded by the `AGENT_REPLY_PREFIX = "[Agent reply @ "` constant — webhooks.py recognises it just like `CLARIFY_NOTE_PREFIX`.

Cross-agent handoff via agent-written `@<other>` labels is **out of scope for v1** — user-initiated only.

## Capture surfaces

Everything the owner tells AEGIS on purpose lands in one of two lanes: a **task**
(Todoist Inbox) or a **`life_fact`** (a knowledge-store fact about your life).
`POST /api/admin/capture` is the single entry point — `kind` is `task` (default),
`life_fact`, or `auto`, which hands the text to the intent classifier in
`core/src/aegis/services/capture_classify.py` and takes whichever lane it names.
The response echoes the `lane` actually written, so the caller can confirm where a
classified note went. Every classifier failure — no LLM, kill switch, timeout,
truncation, unparseable JSON, low confidence — degrades to the task lane, the
recoverable one.

The front doors onto it:

| Surface | Lane | Notes |
|---|---|---|
| Chat / admin UI | `task` | the original `/capture` command |
| Slack `/remember <text>` | `life_fact` | explicit filing, not a to-do |
| Slack message opening `remember …` / `note to self …` / `capture …` / `make a note …` / `add to inbox …` | `auto` | whole-word openers only — "remembering the milk" still reaches chat |
| Slack reaction (`slack_saveit_emoji`, default `:brain:`) | `life_fact` | files **your own** message; both the reactor and the author must be `slack_owner_member_id`, checked before any Slack API call. Deduped on `slack://{channel}/{ts}` |
| Voice note (Slack, or `POST /api/ingest/voice` on comms) | `auto` | raw audio as the request body, `X-Voice-Secret` header; needs `AEGIS_VOICE_INGEST_SECRET` on comms, else 503 |

Machine-pushed personal data is a different lane entirely: signed and structured,
via `POST /api/webhooks/life/{source}` into `life.observations` — see
[`production.md`](../production.md#life-data-push-post-apiwebhookslifesource).

## Flows

38 Temporal workflows on task queue `aegis-main` — 32 schedule-driven (36 seed rows, since `DayLogFlow` and `IntelligenceScanFlow` each back three) and 6 event-driven or child-only. Flow code lives in `worker/src/aegis_worker/flows/`, and every flow is declared exactly once as a `FlowSpec` in `worker/src/aegis_worker/registry.py` — the `Worker(...)` workflow list, `schedule_sync._ACTIVITY_TYPE_MAP` and `_FEATURE_FLAGGED_TYPES` are all derived from it, and `registry.check_registration()` refuses to boot on a half-wired flow. Activities are not registered anywhere: `collect_activities` serves every `@activity.defn` method on the instances `main()` constructs.

Most scheduled flows carry `agent_id` in their config dataclass so `WorkflowRunRecorderInterceptor` can populate `workflow_runs.agent_id`; utility flows (`InteractionFlow`, `AlertInvestigationFlow`, `AgentChatReplyFlow`, `AgentTaskFlow`) take it from their caller.

Owner-scheduled flows are listed in the Personalities table above. The remaining named flows:

- `TodoistSyncFlow` — 5-min Sync API tick: incremental sync from Todoist, drains the outbox.
- `DailyBriefingFlow` (Raphael, daily) — gathers interactions/activity/knowledge → synthesizes → the active comms channel (Slack).
- `DailyReviewFlow` / `WeeklyReviewFlow` (Sebas) — daily + weekly digests; logs to `review_digest_log`, spawns acknowledgement InteractionFlow.
- `MemoryReflectionFlow` (Sebas, nightly) — per-agent memory consolidation. Optional first step (`consolidate: true`): an LLM *proposes* ADD/UPDATE/DELETE/NOOP ops over the agent's rows, and every proposed op is logged to `agent_memory_ops_log`. **Applying needs two independent keys** — `dry_run=False` on the `activities` row (the intent) *and* `AEGIS_MEMORY_CONSOLIDATION_APPLY_ENABLED=true` in the worker environment (the deployment-level switch). Both default to off, so the flow ships inert. Even when armed, a DELETE is a **soft retire**, never a row removal, and a plan whose destructive ops exceed `max_ops_pct` of the agent's live rows is refused. Then the cap: prunes oldest/lowest-importance beyond `keep` — the only step that hard-deletes, and it skips soft-retired rows so step 1's retirements survive.
- `ProfileReflectionFlow` (Sebas, weekly) — proposes one revision of an agent's `user`-kind persona doc from the week's evidence and sends it as a `draft_review` card. **Nothing is written until a human approves**; applied patches land an `agent_profile_revisions` row and are revertible. A resolve whose acknowledged base fingerprint no longer matches the live doc is refused with **409** and must be resubmitted with a `base_ack` matching the current fingerprint (`services/personalities.py`).
- `CuriosityCardFlow` (Sebas, daily) — at most one `input` card a day asking about a gap in what AEGIS knows; the answer is banked as `agent_memory`. Notification-budget gated, so a quiet day is the normal outcome.
- `ExpiryRadarFlow` (Sebas, daily) — warns on `life.expiring_items` crossing a `lead_days` threshold, deduped through `life.expiring_item_alerts`. Empty registry = silent.
- `WearableIngestFlow` (Sebas, 6-hourly) — wearable vendor poll (Oura today) into `life.observations`; needs both an API token and an active `wearable` channel row, else it reports `token_missing` / `no_channel`.
- `DayLogFlow` (Raphael) — three schedule rows off one class (`mode: daily | weekly | monthly`): files the day as one dated knowledge entry (`aegis://daylog/<date>`), then rolls weeks and months up over those entries.
- `DriveSyncFlow` (Raphael) — incremental ingest of a tracked Google Drive folder into the knowledge store; no-ops until a folder is configured.
- `DeliveryWatchdogFlow` (Pandora's Actor, hourly) — catches interaction cards that were never delivered and checks comms `/api/health` inbound liveness; on outage captures a Todoist Inbox task (the chat channel is the thing that's down).
- `FlowHealthWatchdogFlow` (Pandora's Actor, every 30 min) — the watchdog over AEGIS's own flows (#226). Alerts when the most recent N runs of one `workflow_type` are all `failed` (N=2, recency-ordered — a later success breaks the chain, so a resolved incident never re-alerts), and when an active `activities` row has had no *successful* run in `3x` its own cadence (derived from `schedule_cron`). Deduped in `audit_log` (`flow_health_alert`/`flow_health_recovered`), so a flow wedged all day is one card; recovery sends a `[FLOW OK]` card and re-arms the dedup. Silence one flow with an `alert_mutes` row keyed `flow-health:<workflow_type|slug>`.
- `WorkspaceRepoSyncFlow` (Pandora's Actor, daily) — scans the coding host's workspace for git checkouts and makes the `resources` table mirror it (one `kind='repository'` row per checkout); also reports tracked GitHub repos whose AEGIS webhook is missing/dead (`check_github_webhooks`, detection only — it never creates a hook). It reports the **change, not the level** (#142): the standing set is ~14 of 33 and never reaches zero because most tracked checkouts are client repos that shouldn't carry a homelab webhook, so only `result_summary.webhooks_newly_missing` / `webhooks_recovered` warn, diffed against the previous run's own `workflow_runs` row. The full standing list stays in `result_summary.missing_webhooks` (and the flow is chat-triggerable) so it's still readable on demand. `webhook_check_status` (`ok`/`skipped`/`failed`) marks which runs are valid diff baselines — an inconclusive run must not reset the baseline to empty.
- `MoneyProcessFlow` (Maou, child) — single-email money hygiene: `store_receipt_email` → `load_receipts` → `classify_and_extract` → `upsert_charges`. Spawned by `GmailIngestFlow` on `financial`/`payments` tags and by the weekly `ReceiptIngestFlow` safety-net. `ParentClosePolicy.ABANDON`; idempotent on `message_id` at the `store_receipt_email` step.

### Email Triage

`GmailIngestFlow` runs hourly over every `kind='email'` channel. `classify_email` resolves a category through a cheapest-signal-first cascade — a user `sender_overrides` rule, then the per-sender reputation cache (`triage_state`, short-circuits the LLM at n≥3 and confidence ≥0.75), then Gmail's promotions/social marker for unseen senders, then the LLM — and every *learned* verdict passes through `cap_notification_category`, which can only ever demote `important_action` to `important_read` when the subject matches a courtesy-notification marker.

What each category does to the mail:

| Category | Gmail | Elsewhere |
|---|---|---|
| `important_action` | `IMPORTANT`, left unread | Todoist Inbox task tagged `#email`; Slack ping when confidence > 0.9 and tagged `security`/`payments` |
| `important_read` | `IMPORTANT`, **marked read** | ingested into the knowledge store |
| `informational` / `useless` | `UNREAD` and `IMPORTANT` both removed | — |

`important_action` is the only tier that interrupts, so it is guarded twice: the notification cap above, and a live `is_message_unread` re-read immediately before the task and the ping — mail you have already read on your phone gets labelled but never interrupts. Both guards fail open.

Two knobs are yours, edited on the admin **Email triage** page (`GET/PUT /api/admin/email/triage-rules`) and stored in the `settings` row `email_triage_rules`. The repo ships both empty so a fork carries nobody's mailbox — see `services/email_rules.py`:

- `sender_overrides` — `{"@substack.com": "informational"}`. Exact address beats domain, decides outright with no LLM call, and deliberately writes no `triage_state`, so deleting a rule stops it applying instead of leaving learned state behind. It returns no content tags, so don't override bank/receipt senders — the `MoneyProcessFlow` fan-out keys on the `financial`/`payments` tags and would stop firing for them.
- `extra_notification_markers` — extra subject substrings for the cap, for phrasing specific to your bank or tooling.

The page leads with **stuck senders**: those at n≥3 and confidence ≥0.75 short-circuit the classifier entirely, so a wrong verdict there cannot self-correct (a cache hit never reaches the LLM, and only the LLM path re-teaches the cache) — an override is the only thing that changes them. Categories are a dropdown, not free text, because the read path drops an unknown category rather than crashing; a typed one would vanish silently. The write path 400s on anything invalid for the same reason.

Accuracy is scored in `triage_accuracy` from two independent human signals, both zero-effort: what you do to the mail's Gmail labels, and how you close the `#email` task AEGIS created (`#trash` or `@reference` ⇒ "this needed nothing from me"). Both feed `triage_state` through the same disagreement arithmetic, so a correction demotes a sender rather than overriding it.

**Scope — AEGIS only owns mail it actually fetched.** The window is `is:unread newer_than:7d` *and* a forward-only cursor, so anything that ages out before a run sees it is never triaged, and never will be: widening the query cannot reach backwards, because each run advances the cursor to the newest message it saw. A backfill would need purpose-built oldest-first iteration. **Your unread count is therefore not a measure of triage health** — on the author's own accounts it was ~102,000, of which AEGIS had ever judged 2,366. Related: `is:unread is:important` is *not* "AEGIS labelled it" either; Gmail applies IMPORTANT at delivery, so 26,000 messages carried it while only 1,467 came from an AEGIS verdict. Any query about what triage did must intersect against `triage_accuracy.email_id`, never against a Gmail label.

Two known gaps, both documented rather than fixed: a sender above the cache threshold [cannot self-heal from a wrong verdict](https://github.com/hikmahtech/aegis/issues/262), and an override [returns no tags, so overriding a biller silently stops receipt extraction](https://github.com/hikmahtech/aegis/issues/263). Useful checks:

```sql
-- who can still interrupt you, and can't correct itself
SELECT email_addr, state, metadata FROM triage_state
WHERE state = 'important_action'
  AND (metadata->>'n')::int >= 3 AND (metadata->>'confidence')::float >= 0.75;

-- is the feedback loop learning in both directions? (pre-2026-08 it could not)
SELECT predicted, actual, corrected_by, count(*) FROM triage_accuracy
WHERE actual IS NOT NULL GROUP BY 1,2,3 ORDER BY 4 DESC;
```

For each new message `classify_email` also returns a list of `tags` from a closed vocabulary (`financial`, `payments`, `receipt`, `subscription`, `security`, `calendar_invite`, `shipping`, `travel`, `health`, `work`, `personal`, `newsletter`, `technology`, `support`). Tags are additive and orthogonal to the routing category.

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
11. **Gate 2** (non-Jira, non-self-resolved verdicts) — Open PR(s) / Run fix (infra alerts whose investigation ends with a `PROPOSED_COMMANDS:` footer — human-approved SSH execution, read_only-gated, note overrides the commands) / Mute 24h / Acknowledge / Discard via Slack. Jira-source runs (`source=='todoist-jira'`) bypass Gate 2 by contract.
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
| **FinanceConnector** (`finance.py`) | Provider-agnostic web market data (keyless `yahoo` / `stooq` quote providers, selected via the Finance integration config: `finance_provider` / `finance_indices`). Powers Maou's `get_quote` / `get_market_overview` tools and the `/api/market/summary` briefing section; `get_finance_news` rides SearchConnector. |
| **SocialConnector** (`social.py`) | Social posting for `SocialPublishFlow` — native X/Twitter OAuth or a self-hosted Postiz transport — see [`social-publishing.md`](../social-publishing.md). |
| **VercelConnector** (`vercel.py`) | Vercel REST client — backs Pandora's read-only deployment chat tools (`vercel_get_project` et al). |

## Chat with Tool Calling

`POST /api/chat` (non-streaming) and `POST /api/chat/stream` (SSE). 51 tools in `CHAT_TOOLS`, gated per-agent by `agents.metadata.tool_set` (the runtime source of truth, edited on the admin **Behavior** tab); `AGENT_TOOL_SETS` in `core/src/aegis/services/chat.py` is only the seed-time default for the four example agents, and an agent with no configured tool set falls back to the minimal read-only `_FALLBACK_TOOL_SET`.

`CHAT_TOOLS` and `TOOL_EXECUTORS` stay in `chat.py` as the single registry, but the executor *bodies* for extracted domains live in `core/src/aegis/services/tools/<domain>.py` (today `infra.py` and `vercel.py`); `ToolContext` lives in `services/tools/base.py` and is re-exported from `chat.py`.

Tool loop: max iterations bounded by the service config; per-tool timeout via `asyncio.wait_for` (default `tool_timeout_seconds`, with per-tool overrides in `_TOOL_TIMEOUT_OVERRIDES` for long-running tools like `aegis_self_diagnose`); result truncation per `max_bytes`. Every tool call recorded to `chat_tool_calls`.

Tool counts a fresh install actually gets (from `config/seed/agents.yaml`, which writes `metadata.tool_set`), next to the Python fallback:

| Personality | Seeded (`agents.yaml`) | Fallback (`AGENT_TOOL_SETS`) |
|-------------|------:|------:|
| Sebas | 20 | 22 |
| Raphael | 13 | 13 |
| Maou | 13 | 13 |
| Pandora's Actor | 34 | 34 |

`AGENT_TOOL_SETS` is only consulted when the agent's DB row carries no `tool_set` — so for the four seeded agents it is effectively dead once the seed has run. The two-tool gap matters: `query_observations` and `last_contact_with_person` read the `life` schema and are **not** seeded to anyone, so they need a manual grant on the Behavior tab before an agent can ask about your people or observations.

Startup validator: Core refuses to boot if `AGENT_TOOL_SETS` references a tool that isn't in `CHAT_TOOLS` (`_validate_agent_tool_sets`), and warns on any DB `metadata.tool_set` entry naming a missing executor.

`call_mcp_tool` is the single passthrough to external [MCP](https://modelcontextprotocol.io) servers. It is default-deny on three independent gates — `settings.mcp_enabled`, the tool being in the agent's `tool_set`, and a per-server/per-tool grant in `agents.metadata.mcp_servers`. Remote tool names are never spliced into `CHAT_TOOLS`. Setup and the threat model are in [`development.md`](../development.md#mcp-servers-external-tool-servers).

### Proactive knowledge context

Before every LLM call, `_gather_knowledge_context()` runs a semantic chunk search via the native `KnowledgeService.search`. Results are boosted per-personality domain affinity, capped at 2000 chars injected into the system prompt. 5s timeout (`knowledge_context_timeout_seconds`) — never blocks chat. Each result that survives the threshold is logged to `knowledge_injection_log`.


## API

33 route modules in `core/src/aegis/api/routes/`. All `/api/*` routes require Basic auth or `X-API-Key` (API keys are generated from the admin **Integrations** page and stored encrypted in the DB; `AEGIS_API_KEY` is the env fallback). Auth can be switched off entirely with `AEGIS_AUTH_DISABLED=true` — for deployments fronted by an authenticating proxy only. Exceptions: `GET /health` and webhook paths under `/api/webhooks/*` (HMAC-verified).

Route modules: `activities`, `agents`, `api_key`, `assets_admin`, `audit`, `capture`, `channels`, `chat`, `expiring_items_admin`, `gmail_reauth`, `health`, `homelab`, `infra`, `infra_admin` (infrastructure registry CRUD + provisioning + k8s/cloud ops — see [`infrastructure.md`](../infrastructure.md)), `integrations`, `interactions`, `knowledge`, `llm_backend`, `market`, `mcp`, `money`, `observability`, `overview`, `people_admin`, `references`, `resources`, `settings`, `slack`, `social_auth`, `system_status`, `temporal`, `todoist`, `webhooks`.

Inbound webhooks are `/api/webhooks/{todoist,github,sentry,alert}` plus `POST /api/webhooks/life/{source}` — the signed personal-data push lane (location / health / observation), off until a secret is configured. Signing, replay window, body cap and per-source behaviour are documented in [`production.md`](../production.md#life-data-push-post-apiwebhookslifesource).

## Admin UI

React SPA served by Core at `/`. Top-level pages include: Overview, Interactions, Workflows, Agents + Agent detail (incl. the personality editor), Chat, Knowledge / Content / Content detail, Channels, Flows & Integrations, Models & Providers, Todoist, Resources, AuditLog, Money, Market, References, People / Expiring Items / Assets (the `life` registries), System Monitoring, Settings, Slack config, and Infra (the infrastructure registry — register SSH hosts / the swarm / k8s clusters / cloud accounts with encrypted pasted credentials; see [`infrastructure.md`](../infrastructure.md)). The admin UI is the primary configuration surface: agents, personalities, channels, schedules, integration secrets, the LLM backend, and infrastructure are all DB-owned and edited here — seed YAML and env vars are first-boot/bootstrap inputs, not the ongoing source of truth.

## Comms (Slack)

Slack Socket Mode (`slack_sdk`) + FastAPI delivery server (port 8081). One Slack channel per personality. Slack is optional: tokens are configured from the admin **Slack** page (stored encrypted in the DB; `AEGIS_SLACK_*` env is the dev fallback) and comms idles as a no-op until they exist — interaction cards always land in the admin UI's **Interactions** inbox (web) regardless. Core reaches the delivery server via `AEGIS_COMMS_URL`.

- Agent→channel mapping populated from `agents.slack_channel_id` (falls back to resolving `#aegis-<short>` by name).
- Message bodies are authored in a light HTML dialect and converted to Slack mrkdwn (`html_to_mrkdwn`); all user-controlled strings pass through `_safe()` (`html.escape()`).
- Interaction cards render as Block Kit with the uniform callback identity `interaction:{id}:{value}` — resolved by `/api/interactions/{id}/resolve`. Comment-channel reply callbacks use a separate `agent-chat-reply-…` workflow id namespace.
- Approval/choice/ack cards also carry an optional free-text note input (`correction_note`). Slack includes the message's input state with every button tap, so a typed note rides along as `response.note` — which core records as a durable `agent_memory` lesson (the learning loop).
- The delivery server exposes `/api/deliver/message`, `/api/deliver/document`, `/api/deliver/voice`, `/api/deliver/card`, `/api/comms/delete` and `/api/health` (inbound Socket Mode liveness).

## Database

PostgreSQL 16 + pgvector. Migrations 001 → 021 in `migrations/` (001 is the squashed baseline); auto-apply on Core startup, tracked in `schema_migrations`. Migration files are iterated on disk and already-recorded ones skipped, so the numbering is a sort key, not a version.

**Core primitives** — `agents`, `agent_personalities`, `agent_profile_revisions` (persona edit log, revertible), `agent_memory` (+ `agent_memory_ops_log`, the consolidation audit ledger), `activities`, `interactions`, `workflow_runs`, `resources`, `channels`, `settings`, `infra`.

**Chat** — `chat_history`, `chat_tool_calls`.

**Todoist GTD layer** — `todoist_projects`, `todoist_tasks`, `todoist_notes`, `todoist_labels`, `todoist_outbox`, `todoist_sync_state`, `todoist_webhook_events`, `todoist_capture_idempotency`, `gtd_clarify_log`.

**Triage feedback** — `triage_state`, `triage_accuracy`.

**Knowledge (native RAG)** — `knowledge_content`, `knowledge_chunks` (pgvector embeddings), `knowledge_injection_log`.

**Maou (finance)** — `finance.recurring_charge`, `finance.receipt_email`, `finance.renewal_alert`, `finance.subscription_digest`.

**Pandora's Actor (infra)** — `pandoras_actor.homelab_drift`, `pandoras_actor.cert_expiry`. (`pandoras_actor.backup_health` and `pandoras_actor.schedule_health` were created by the baseline and dropped again by migration 022: their producers, `BackupAuditFlow` and `ScheduleHealthFlow`, were removed when the owner-specific homelab probes were stripped, leaving the tables and their `CleanupFlow` retention entries behind as dead weight.)

**Life context (`life` schema)** — `life.people` (who matters, aliases, `last_contact`), `life.observations` (time-series personal signals from wearables and the life webhook; 365-day retention by `observed_at`), `life.expiring_items` + `life.expiring_item_alerts` (the expiry radar's registry and its per-threshold dedup ledger), `life.assets` (owned things; an asset with a service interval mirrors itself in as an `asset_service` expiring item).

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

`llm_calls` rows come from **one choke point**, `LLMClient._record_call` (issue #106):
`think()`/`chat()` record every terminal outcome — success, `clipped`, `LLMTruncationError`,
upstream failure — for any call that names a `purpose`. `status` therefore has three values,
not two: `clipped` marks a call that returned usable content but hit `finish_reason=length`
mid-write, which previously counted as a clean success and hid steady truncation on the
intelligence-scoring path (#255). Filter on `status <> 'success'` rather than `= 'error'` when
asking "what is degrading". The pool is the client's own unless the caller
passes `db_pool=`, so a client constructed with a pool records by construction while a client
built without one (`routes/llm_backend.py::test_backend`) stays deliberately ungoverned. The
kill switch produces no row on purpose: it raises before any HTTP request, so nothing was
spent. **A call site must never call `record_llm_call` itself after `think()`/`chat()`** — the
second row inflates reported spend and nothing errors anywhere. Passing `db_pool=` without a
`purpose` logs `llm_call_unrecorded` at WARNING rather than silently skipping the write. The
one remaining explicit recorder is `services/chat.py`'s tool loop, which passes neither
argument and so is not double-counted.

Distributed tracing: OTel SDK + JSON-formatted logs with `trace_id`/`span_id` injected from the active span. Per-package `telemetry.py` + `logging_config.py` modules. Gated on `OTEL_ENABLED=true`. Auto-instrumentation covers FastAPI, asyncpg, httpx, requests. The Worker registers `temporalio.contrib.opentelemetry.TracingInterceptor` so trace context flows through workflow headers automatically.

## Knowledge (native RAG)

Knowledge is a native Core service (`core/src/aegis/services/knowledge.py`, replacing the old external knowledge-service): ingested content is chunked and embedded into Postgres + pgvector (`knowledge_content` / `knowledge_chunks`), with embeddings produced by the configured `embedding_model` (default `nomic-embed-text`) through the LLM backend. It captures ephemeral content streams (RSS, Raindrop, HN/news/finance scans, Drive folders, GTD references, URL/upload/folder seeds) via ingest flows. `search` does semantic chunk retrieval; `ask` synthesizes an answer across retrieved chunks with the local LLM. No knowledge graph.

The `source_type` vocabulary — and each type's retrieval decay window — is declared in one place, `core/src/aegis/services/source_types.py`; an unknown value is warned on rather than silently ranked with the default decay. Two of its members come from the day log: `daylog` (one dated entry per night at `aegis://daylog/<date>`) and `daylog_rollup` (the weekly / monthly condensations), which is what gives retrieval a timeline instead of only a topic index. Free-form owner notes filed through the capture surfaces land as `life_fact`.

References-as-knowledge: a Todoist task classified `@reference` is captured via `ingest_reference_to_ks` (raises on transient failures, returns a verdict on permanent ones); the verdict is dispatched by `_dispatch_reference_verdict` in `ClarifyFlow` to either complete the task (success) or demote to `@to-read` (permanent failure). The `/api/references` route surfaces the live library from the knowledge store; `/api/references/failures` is the `@to-read` lane from the Todoist projection.

## Config

- `config/.env` — bootstrap secrets and endpoints (copy from `.env.example`); pydantic-settings adds the `AEGIS_` prefix. Integration secrets (Slack, Todoist, GitHub, API keys, infra credentials, …) are entered in the admin UI and stored **encrypted in the DB** — env vars are the fallback, not the primary store.
- `config/seed/{agents,activities,channels,resources,todoist}.yaml` — seed data loaded via FastAPI lifespan (`channels.yaml` is first-boot starter examples only — channels are DB-owned and managed from the admin panel's Channels page / `/api/admin/channels` afterwards)
- `config/models.yaml` — model tier mapping resolved against the configured LLM backend
- `personalities/<agent>/{SOUL,AGENTS,USER,MEMORY}.md` — starter persona examples, imported into the `agent_personalities` table on first boot (DB/admin-UI-managed afterwards)
- `runbooks/<AlertName>.md` — per-alert runbooks (baked into worker image); stubs containing `TODO: fill in` are treated as absent
