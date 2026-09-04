# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

AEGIS — a flow-first, self-hosted personal AI orchestration platform. A small fleet of
named agents run scheduled and event-driven [Temporal](https://temporal.io) workflows over
your own data (GTD/tasks, money, knowledge, homelab alerts) and ask you for a decision only
when they actually need one. Local-LLM-first via a LiteLLM proxy; can reach for
Claude/OpenAI when you want more horsepower.

Three Python packages in one repo:

| Package | Role |
|---|---|
| `aegis-core` (`core/`) | FastAPI API (port 8080) + admin SPA, chat, knowledge (native Postgres+pgvector RAG), connectors |
| `aegis-worker` (`worker/`) | Temporal worker — all flows and activities (task queue `aegis-main`) |
| `aegis-comms` (`comms/`) | Chat-channel bot + delivery server (Slack Socket Mode; Telegram removed 2026-07) |

Backed by **Postgres 16 + pgvector** (migrations auto-apply on core startup), **Temporal**
for durable workflows, and a **LiteLLM proxy** that resolves `fast` / `balanced` / `smart`
model tiers to whatever models you point it at.

- **Architecture:** [`docs/architecture/overview.md`](docs/architecture/overview.md) — flows, agents, connectors, primitives, schema.
- **Local development:** [`docs/development.md`](docs/development.md) — setup, ports, adding flows/connectors/chat-tools.

## Commands

```bash
# Setup
python -m venv .venv && source .venv/bin/activate
pip install -e "core[dev]" -e "worker[dev]" -e "comms[dev]"

# Infrastructure (local dev)
docker compose up -d postgres redis temporal temporal-ui

# Run services (each in its own shell)
python -m aegis              # Core API (:8080) — runs migrations + serves admin panel
python -m aegis_worker       # Temporal worker (task queue: aegis-main)
python -m aegis_comms        # channel bot + delivery server (Slack tokens DB-configured via admin UI; env fallback)

# Tests / lint — run ONE package at a time, in parallel, exactly as CI does
pytest tests/core/ -n auto --dist loadfile --timeout=300     # what CI runs (also worker/, comms/)
pytest tests/worker/test_cleanup.py::test_name               # single test
pytest tests/core/ -x                                        # stop on first failure
ruff check core/src/ tests/core/                             # lint — CI lints per package, scoped
```

**Do not run the whole suite in one process** — `pytest` with no arguments (and
`pytest tests/worker/` without `-n`) deadlocks; it has always deadlocked, including on
pristine `main`. `-n auto --dist loadfile` is what makes it terminate, and `tests/conftest.py`
gives every xdist worker its own `aegis_test_<gwN>` database so parallel runs don't collide.
Lint the same way: CI runs `ruff check` **scoped per package** (`core/src/ tests/core/`, and
the worker/comms equivalents), which is the gate your PR must pass. A bare `ruff check .` is
also clean and equivalent — `docs/` is in ruff's `extend-exclude` (#236) because the Python
under it is non-running illustration, so the root sweep no longer reports a nit CI never sees.
`ruff format` is deliberately absent from this block — see the "must NOT `ruff format`"
convention below.

Tests need a real Postgres (`docker compose up -d postgres`, port 25432) — no DB mocks.
The full stack (built images, all services) is `docker compose up -d`; add `--profile slack`
for comms and `--profile local-llm` for a bundled Ollama.

## Key paths

- **Migrations:** `migrations/NNN_*.sql` — auto-applied in filename-sorted order on Core startup (`db/pool.py::run_migrations`, advisory-locked), tracked in `schema_migrations` keyed on the **filename**. Two consequences: renaming or renumbering an already-applied migration makes it run **again** (so write idempotent DDL — `IF NOT EXISTS`, `ADD COLUMN IF NOT EXISTS`), and when two parallel PRs both claim the next `NNN`, renumber the loser *before* it merges, never after.
- **Core API:** `core/src/aegis/` — routes in `api/routes/`, services in `services/`, connectors in `connectors/`, LLM tiers in `llm/`.
- **Worker:** `worker/src/aegis_worker/` — flows in `flows/`, activities in `activities/`, schedule reconcile in `schedule_sync.py`, DI in `bootstrap.py`.
- **Comms:** `comms/src/aegis_comms/` — channel-neutral types in `adapters/base.py`.
- **Config (seed, edit these not code):** `config/seed/{agents,activities,channels,resources,todoist}.yaml`, `config/models.yaml` (tier→model map — **fallback only**, the DB `settings.llm_backend` row wins; see Model tiers). Personas are DB-managed in `agent_personalities` (kinds soul/agents/user/memory), edited via the admin Agents page or GET/PUT `/api/admin/agents/{id}/personality` (`services/personalities.py`); `personalities/<agent>/*.md` are import-on-first-boot starter examples only.
- **Secrets:** `config/.env` (gitignored; copy from `config/.env.example`).
- **Infrastructure registry:** `infra` table + `services/infra.py` + `routes/infra_admin.py` + admin Infra page — SSH hosts / swarm / k8s clusters / **cloud accounts** (kind=`cloud`: AWS multi-profile ini or GCP SA JSON, provision = identity check; k8s entries reference one via `cloud.cloud_slug`, inline creds are fallback) with per-entry encrypted credentials (SSH key, kubeconfig, auth env, AWS credentials file, GCP SA JSON), per-entry `read_only` mutation gate, kubectl ops for `kind=k8s`, and the **coding-host block** (`coding` jsonb: claude/kimi engines, named CLAUDE_CONFIG_DIR accounts, org→engine routing, tmux — RemoteScriptConnector reads it DB-first, env fallback). Exec-plugin kubeconfigs need the image built with `--build-arg EXTRA_CLOUD_CLIS="aws gcloud"`. Full how-to: `docs/infrastructure.md`.
- **The books (Maou's ledger):** `core/src/aegis/services/books.py` (block render/parse, rules, repo sync, the allowlisted read-only `hledger` runner) + `services/bank_parsers.py` (deterministic parsers, tried before the LLM) + `services/journal_index.py` (`finance.journal_index` — idempotency on the Gmail message id, receipt↔bank matching, dues dedupe). **The journal is the record and Postgres is only the index**, so never treat an amount in `journal_index` as authoritative — run hledger. Core and worker share ONE checkout at `books_path`, serialised by an flock on `.aegis.lock`, so every write goes through `books.py`; a hand-rolled file write skips the `hledger check --strict` guard and its revert. An empty `books_repo_url` means events are indexed and never posted — and such a row is deliberately **not** stamped `parsed.version = 2`, so the weekly sweep replays the whole backlog once a repo is configured; stamping it would strand those payments outside the journal and let a later counterpart post a duplicate block. The whole money lane is gated on `money_hygiene_enabled`. Spec: `docs/superpowers/specs/2026-09-05-maou-books-design.md`; setup and backfill: `docs/infrastructure.md`.

## Domain model

- **GTD is central; Todoist is the canonical task store.** The only managed container is the native Todoist **Inbox** (`settings.todoist_managed_project_ids` maps just `inbox`; clarify pulls exclusively from it). GTD state lives in **labels** — `@next` / `@someday` (which replaced the old Next / Someday-Later projects), `@waiting`, `@reference`; work-areas are real user-owned Todoist projects that AEGIS deliberately does not manage (see `config/seed/todoist.yaml`). Delegation is an assignee label (`@sebas`/`@raphael`/`@maou`/`@pandora`) + `@waiting`. Todoist is mirrored by a 5-min `TodoistSyncFlow` (incremental `sync_token` + `todoist_outbox` write queue).
- **Knowledge** is a native Postgres+pgvector RAG store (`routes/knowledge.py`, seeded from URLs/uploads/folders/watched Drive).
- **`life` is a real Postgres schema**, not a table prefix — `life.people` / `life.observations` / `life.expiring_items` / `life.assets`, always written through their owning service (`services/{observations,people,expiring_items,assets}.py`), never by a hand-rolled INSERT. Two funnels, and a new capture surface calls one of them rather than opening a third door: free text goes to `POST /api/admin/capture` (which is the only place that picks a lane), and numeric/categorical readings go to `observations.record_external_observation` — where **a `None` return means "already ingested", not "failed"**. Dedupe is the partial unique index on `(source, metric, external_id)`, so never SELECT-then-INSERT; poll flows re-read overlapping windows by design. `metric`/`source` are stored stripped+lowercased, so reads must normalise too or the series silently splits.
- **`interactions`** is the universal human-in-the-loop primitive: `InteractionFlow` child workflow → creates an `interactions` row → dispatches a card to the active comms channel → awaits the `submit_response` signal. It replaces any per-domain decisions/tasks/projects tables — don't reintroduce those.
- **Meeting notes ride the email triage.** A sender-override rule on the Email triage page with tag `meeting` (e.g. `gemini-notes@google.com` → `important_read` + `["meeting"]`) makes `GmailIngestFlow` spawn `MeetingNotesFlow` per email, which follows the Google Docs link with that mailbox's Drive token (`drive.readonly` — re-authorise once via Admin → Gmail re-auth), files the notes as `source_type=meeting` (never the transcript), computes the user's talk share in code, files an LLM self-review as `meeting_review`, and writes `talk_share_pct`/`words_per_turn`/`turns` to `life.observations` (source `meeting`, external id = doc id). Who "you" are is `settings.meeting_rules.self_names` (`GET/PUT /api/admin/email/meeting-rules`); empty ⇒ notes only, review skipped with `analysis=no_self_names`. The Sunday `WeeklyReviewFlow` appends a meetings block from `ReviewActivities.gather_meeting_week`. Every doc failure is a `doc_status` (`no_link`/`inaccessible`/`no_drive_scope`/`fetch_failed`) on the stored row, and the weekly block names the mailbox to re-authorise when the token lacks the Drive scope, or lists the other doc statuses by name.

## Conventions

- **Config fields:** snake_case without `aegis_` prefix in Python; env vars get the `AEGIS_` prefix from pydantic-settings.
- **DB access:** `request.app.state.db_pool` in route handlers.
- **Route test auth:** `app.dependency_overrides[get_settings] = lambda: settings`; use httpx `ASGITransport` + a real `db_pool`.
- **Activity/flow tests:** `ActivityEnvironment` + `respx` for activities; `WorkflowEnvironment.start_time_skipping()` + `Worker` for workflows.
- **Flows belong to one personality:** every flow config dataclass has `agent_id: str` as its **first** field. `WorkflowRunRecorderInterceptor` uses it to populate `workflow_runs.agent_id` and catches top-level failures into `result_summary->>'reason'` + `exception_type`. For step context, raise `ApplicationError(f"<flow>_failed at step=X: {exc!r}", non_retryable=True) from exc`.
- **New scheduled flow:** create the flow (`@workflow.defn`) + activities, add **one**
  `FlowSpec` to `FLOWS` in `worker/src/aegis_worker/registry.py` (flow class, a
  `schedule_config` builder from an `activities` row, an optional `feature_flag`), and
  insert a seed row in `config/seed/activities.yaml`. `__main__.WORKFLOWS`, the
  `Worker(...)` list, `schedule_sync._ACTIVITY_TYPE_MAP` and `_FEATURE_FLAGGED_TYPES`
  are all derived from that entry. **Activities need no registration** —
  `registry.collect_activities` serves every `@activity.defn` method on the instances
  `main()` constructs; only a brand-new Activities *class* needs its constructor call in
  `main()` plus its name in that `collect_activities(...)` argument list.
  `registry.check_registration()` runs before the Worker is built and raises on any
  half-wired state. `schedule_sync` reconciles Temporal schedules on startup and every
  ~300s, so a DB `activities.config` change propagates without a redeploy.
- **Agent behavior is keyed on capability tags, not id (issue #36, PRs #38–#42):** `agents.capabilities` (JSONB) holds behavior tags — closed vocab `gtd`/`finance`/`research`/`infra` in `core/src/aegis/agent_tags.py`. Resolve a tag → owning agent via `services/agents.py::resolve_tag`/`agents_by_tag` (core) or the `AgentRegistryActivities.resolve_agents` activity (worker; workflows can't hit the DB). Never branch on a literal agent id — resolve by tag; zero holders ⇒ skip + warn, never crash. Per-agent routing knobs live in `agents.metadata`: `intent_keywords`/`intent_description` (chat routing), `mention_aliases` (chat/clarify/Slack @-addressing, default `[id]`), `async_dispatch`, `tool_set`, `knowledge_domains`, `voice_lines`. All editable on the admin **Behavior** tab (`AgentDetail.tsx`, `PATCH /api/agents/{id}`, vocab from `GET /api/agents/meta/options`). `seed.py` treats `capabilities`/`metadata` as DB-owned once non-empty (yaml seeds first boot + merges new keys), so UI edits survive restarts — a new yaml capability tag needs a manual tick on an existing deploy. (#43 maou→finance rename and #44 agent-lifecycle tooling both shipped 2026-07-16.)
- **New chat tool:** add the schema to `CHAT_TOOLS` and the entry to `TOOL_EXECUTORS` — **both still live in `core/src/aegis/services/chat.py`**, and there is no auto-discovery. Only the executor *bodies* were extracted: an already-extracted domain (`services/tools/infra.py`, `services/tools/vercel.py`) takes the function, everything else stays in `chat.py`, and either way you hand-write the import plus the `TOOL_EXECUTORS` entry. A schema or registry entry parked in `services/tools/` is never advertised or never dispatched. Inside a `tools/` module import `ToolContext` from `services/tools/base.py`, never from `chat` (`chat.py` imports these modules — the reverse is a cycle), and leave the `# noqa: F401` re-exports in `chat.py` alone: `routes/infra.py` and the tests import `_exec_*` **from `aegis.services.chat`** and rely on object identity. Then grant the tool to agents via `metadata.tool_set` (admin Behavior tab is the runtime source of truth; `config/seed/agents.yaml` seeds it). `AGENT_TOOL_SETS` is now only a seed-time default for the 4 example agents — a DB `metadata.tool_set` overrides it, and an unconfigured agent falls back to the minimal read-only `_FALLBACK_TOOL_SET` (NOT Sebas's full surface). `_validate_agent_tool_sets` refuses to boot on a tool with no executor; Core also warns at startup on any DB `tool_set` entry referencing a missing executor. **Gotcha:** if you edit both, `metadata.tool_set` (DB) wins over the Python dict at runtime.
- **Two files you must NOT `ruff format`:** `core/src/aegis/services/chat.py` and
  `core/src/aegis/services/tools/infra.py`. Both carry hand-laid-out data tables
  (`CHAT_TOOLS`/`TOOL_EXECUTORS`, `_INFRA_SPECS`) with local-ruff-version format drift —
  `ruff format --check` rewrites them wholesale even on pristine main, while CI's ruff
  version considers them clean. Running it produces whole-file churn that buries your
  change and conflicts with any parallel edit. Run `ruff check` (must pass), write
  already-formatted edits, and trust CI for format. Verify a minimal diff with
  `git diff main -- <file> | grep -c '^@@'`.
- **Model tiers:** `agents.model_tier` is `fast` | `balanced` | `smart`. The tier→model map is resolved **DB-first** by `services/llm_backend.py` from the `settings.llm_backend` row (provider/base_url/api_key/tiers, edited in the admin UI), falling back to `config/models.yaml` and then the `model_fast/balanced/smart` settings; the result is installed via `set_model_tiers` (`core/src/aegis/llm/tier.py`) at boot from `api/app.py` and `worker/bootstrap.py`. **Gotcha:** saving in the admin UI rebuilds Core's map immediately but the worker only picks it up on restart. `resolve_model_for_agent` silently falls back to `balanced` on a NULL or unknown tier, so a typo never errors. No `ollama/` prefix — the proxy serves bare names.
- **Reasoning-model token budget:** a reasoning model bills its hidden `reasoning_content` against `max_tokens` *before* visible content, so tight caps return `finish_reason=length` with empty `content`. `LLMClient.think()` detects this and raises `LLMTruncationError` instead of returning `""` (which would crash downstream `json.loads`). Structured-extraction calls need ≥3× the expected JSON size as headroom; callers catch `LLMTruncationError` where a degraded result is acceptable. `_reasoning_floor` raises any budget below `_REASONING_MIN_TOKENS` (4096) for models matching `_REASONING_MODELS` (`kimi`, `qwen`) — **a reasoning model missing from that tuple gets no floor at all and fails silently**, which is exactly how `qwen3.5:9b` came to run `briefing_frame` at a raw 2000 and return empty content on 100% of calls for six days (#255). Extend the tuple when the tier map moves.
- **Truncation has two classes and only one raises** (#255). Empty content + `finish_reason=length` → `LLMTruncationError`. **Non-empty** content + `finish_reason=length` is a response cut mid-write — a short JSON array or an unparseable tail — and it used to be recorded as a plain success, which hid 6/42 `intel_score_significance` calls clipping at exactly the floor. It is now recorded as `llm_calls.status='clipped'` and deliberately **not** raised, because partial content is usually still usable. So `status` has three values (`success` / `clipped` / `error`): a query that assumes two is under-counting degradation.
- **Resolve models through the tier map, never `settings.model_*` directly.** `worker/__main__.py` builds one tier-resolved `model_balanced` local and every consumer takes it. Two sites (`BriefingActivities`, `ReviewActivities`) reached past it for the raw settings field, so decommissioning a model and repointing the env var degraded exactly those two while the rest of the fleet was unaffected — invisible because both fall back to non-LLM output. A new activity that needs a model takes the resolved local.
- **MCP is default-deny through three independent gates**, all of which must be open: `settings.mcp_enabled`, `call_mcp_tool` in the agent's `tool_set`, and a per-server *and per-tool* grant object in `agents.metadata.mcp_servers` (`{"docs": ["search_docs"]}`; `["*"]` is the only "all tools", a bare list is rejected, and there is no admin UI field — set it over `PATCH /api/agents/{id}`). Remote tool names are **never** spliced into `CHAT_TOOLS`/`TOOL_EXECUTORS`; there is exactly one registry entry, `call_mcp_tool`, so a third party's config can never decide what AEGIS considers a valid tool. Grants are re-read from the DB inside the executor, not taken from `ToolContext` — do not "optimize" an authorization decision into a caller-populated field. Server-supplied names/descriptions enter the prompt as an explicitly-labelled untrusted catalog. Servers are configured **env-only** (`AEGIS_MCP_SERVERS` JSON, restart required); the `kind: mcp_server` rows in `config/seed/resources.yaml` are documentation the client never reads.
- **`agent_memory` is soft-retired, so every new read needs `AND superseded_at IS NULL`** (migration 020 — the column is `superseded_at`, not `retired_at`; `superseded_by` is an optional provenance link, usually NULL). Consolidation retires rather than deletes, so a retire-blind SELECT silently resurrects a withdrawn belief into a system prompt or a persona proposal. The one legitimate exception is a dedupe/existence check, where a retired row must still block a re-insert. `services/memory.py::apply_consolidation` is the only sanctioned writer.
- **Automated persona edits go through `personalities.py::apply_profile_patch`, never `set_personality`** — only the patch path writes the `agent_profile_revisions` before/after row in the same transaction, and it takes a mandatory `source` plus the authorising `interaction_id`. `set_personality` is the human `PUT` path. Only the `user` kind may be written by an automated writer; `soul`/`agents`/`memory` are human-only. Approvals carry a `revision_of` fingerprint and 409 on base drift — `doc_fingerprint` lives in core so worker and core hash identically; don't duplicate it.
- **Todoist Sync API:** callers of `TodoistConnector.commands()` must inspect per-command `sync_status[uuid]`, not just the envelope — `check_sync_status(envelope, uuids)` distinguishes `rejected_retryable` (5xx → outbox-queue) from permanent (4xx → log and drop; queuing poison-loops).
- **Clarify watermark** (`activities/clarify.py`): only bump `todoist_tasks.last_clarified_at` on a real terminal state (`applied`, `outbox_queued>0`, or `interaction_spawned`); a bare `applied=False` keeps it NULL so the task re-enters `find_unclassified_items`.
- **Every terminal clarify outcome must leave a GTD state label** (issue #139). GTD state lives entirely in labels, so a task exiting `apply_outcome` with none of `@next`/`@someday`/`@waiting`/`@reference` is in no GTD state and is invisible to every "what's next" / "what am I blocked on" view. `_GTD_STATE_FOR` in `activities/clarify.py` is the contract: one entry per classification, either a state label or an explicit `None` with the reason (only `trash` — it completes the item — `pandora_gate` and `skipped`, where a card decides). `test_clarify_gtd_state.py` derives the outcome vocabulary from the module's own AST, so a new classification added without a decision fails CI. **Clarify must never stamp `@waiting`**: that is `agent_task.PARK_LABEL`, and reaching it removes an agent-assigned task from `find_actionable_tasks`' pool. An assignee label means "an AEGIS agent should work this", not "blocked" — `@waiting` is applied by `agent_task.park_task` at the END of a run.
- **Email triage's user-facing knobs are DB-configured too** — `settings.email_triage_rules` (`core/src/aegis/services/email_rules.py`, defaults deliberately EMPTY so a fork ships nobody's senders), merged and read by `classify_email` via `_load_email_rules`, edited on the admin **Email triage** page via `GET/PUT /api/admin/email/triage-rules` (`routes/email_admin.py`), mirroring the Todoist gtd-rules pair. **`merge` (read) is lenient and `validate` (write) is strict, deliberately** — a malformed row must never stop mail being classified, but that same leniency at the write boundary would let a typo'd category save with a 200 and then do nothing forever, so the PUT 400s on it. The generic `/api/settings` editor can also reach the row but has no create control and validates nothing; don't route users there. `sender_overrides` short-circuits the whole cascade and must NEVER write `triage_state` (deleting a rule has to stop it applying). A rule value is **either** a bare category string **or** `{"category": ..., "tags": [...]}` — `merge` normalises both to the tagged shape, so every consumer sees `{"category", "tags"}` and `match_sender_override` returns a dict, not a string. The tags exist because an override skips the LLM and the money fan-out keys on `financial`/`payments`: without them, silencing a biller silently disabled its receipt extraction (#263). The cache-hit (LLM-skip) branch, by contrast, **does** write `triage_state` — but only when `cap_notification_category` demoted the cached verdict, so a repeatedly-capped sender decays out of `important_action` instead of being stuck forever (#262); reinforcing on agreement would ratchet every sender to n=∞/conf=1.0. `important_action` is the only category that interrupts the user, so it is capped twice before it can: `cap_notification_category` (shares `_NOTIFICATION_MARKERS` with clarify — gmail imports clarify, never the reverse) demotes courtesy notifications, and the flow re-reads live unread state via `is_message_unread` before creating a task or pinging chat. Both guards fail OPEN. `important_read` marks mail READ (the `IMPORTANT_READ` verdict in `apply_label`) — it is 68% of all mail and keeping it unread is what made the inbox grow without bound; do not restore keep-unread.
- **GTD clarify rules are DB-configured, not Python constants.** `_RuleSet` in `activities/clarify.py` is a merge shell: the effective ruleset is `services/gtd_rules.py::get_gtd_ruleset(pool)` — the `settings` row keyed `gtd_rules` merged over the `DEFAULT_*` constants in that module, 30s-cached, edited at `GET/PUT /api/admin/todoist/gtd-rules`. A second admin-owned table, `content_routes` (`services/content_routes.py`, `GET/PUT /api/admin/todoist/content-routes`), is an ordered first-match-wins list over the task title. **So editing the Python defaults does not change a running deployment** — the DB override wins. A third member of the same family is `project_repo_map` (`services/project_repo_map.py`, `GET/PUT /api/admin/todoist/project-repo-map`): Todoist project name → `owner/repo`, the coding lane's tier-1 repo resolver. It ships **EMPTY** and a fresh deployment must populate it or that tier simply misses and the resolver falls through to its title-matching tiers (#345 — it used to be a Python constant carrying one operator's projects). Only the `@sebas`/`@raphael`/`@maou`/`@pandora` addressable routing is still hardcoded.

## Observability

All three packages emit OpenTelemetry traces + structured JSON logs with `trace_id`. Shared
init is `telemetry.py::setup_telemetry()` (OTel + auto-instrumentation, called once per
process at startup — in the worker's `main()`, but in Core at `__main__` import time, before
any app import, which is what the `# noqa: E402` ordering there protects) and
`logging_config.py::JsonFormatter`. Core owns the canonical copies; the worker
imports them from `aegis.*`; comms keeps its own (no `aegis-core` dep). No-op unless
`OTEL_ENABLED=true`. FastAPI/asyncpg/redis/httpx are auto-instrumented — only
`LLMClient.{think,chat,embed}` are manually spanned (all three as `llm.call`). Use stdlib
`logging.getLogger(__name__)`; the JSON formatter is wired into the root logger.

**`chat_tool_calls` covers BOTH tool surfaces, and `status` is not a summary of
the HTTP result.** The chat loop and the MCP server (`routes/mcp_server.py`) both
write rows via `observability.py::record_tool_call`; `surface` says which
(`chat`, `mcp`, `mcp_gated`, `mcp_operator`) and defaults to `chat`. Two traps,
both of which have already hidden a real outage: an executor reports failure by
**returning** an error envelope (`{"error": ..., "exit_code": ...}`) rather than
raising, so both surfaces downgrade a `success` whose payload carries a truthy
`error` — without that the table called a failed call successful and every
"which tools are failing?" query answered "none" (that is how infra tools broken
2026-07-16→08-28 went unnoticed). And a tool that returns a *prose* apology is
indistinguishable from an answer at this layer, so it is still recorded
`success`; that limit is deliberate and pinned by tests. `approve_tool_use` is
never recorded — it is the permission gate, not a tool call, and its outcome is
an `interactions` row.

## Deployment

This is a personal project built to be **forked and configured for your own life**. The
GitHub Actions workflows in this repo are **test-only** — there is no build or deploy job
here at all. Image build and rollout live in a separate private infrastructure repo
(Ansible), deliberately, so a fork never builds or deploys and no registry or host details
land in the open-source tree. Keep it that way: a deploy step added here would leak
infrastructure detail into a public repo. Wire your own fork up to your own
registry/runner if you want CD.

Two CI facts worth knowing before you open a PR. First, the three test workflows are
`paths:`-filtered, and the filter must list every input the job's tests *read*, not just its
package — `config/**`, `migrations/**`, `personalities/**`, `tests/conftest.py` and the root
`pyproject.toml` are all in scope for core and worker, because `tests/conftest.py` builds each
test database by running `migrations/` and `load_seeds(config/seed)` and several tests parse
`config/seed/*.yaml` directly. Add the path when you add the dependency: a missing entry does
not merely skip a job, it silently disarms the tests that exist to validate that path (#170).
A `docs/`-only change still runs no test job. Second, `ci-grep-guard.yml` runs on every PR,
failing the build if deleted n8n-era files or symbols reappear.

## Issue Tracking — GitHub Issues

Defects, bugs, tasks, improvements, and deferred fixes for this repo are managed via **GitHub
Issues** (`gh issue list` / `gh issue create`) — NOT Todoist or Notion. When work surfaces
mid-session that we aren't fixing immediately, create an issue proactively (context, repro,
file paths in the body). Check `gh issue list` when picking up work here. Close issues via the
fixing PR/commit (`Fixes #N`). Never put secret values in an issue.
