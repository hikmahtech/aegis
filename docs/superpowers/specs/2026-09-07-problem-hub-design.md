# Problem hub: one record for every alert, investigation and session

**Date:** 2026-09-07
**Status:** approved; every PR shipped (1, 2, 3a, 3b, 4a, 4b, 5a, 5b, 6a, 6c, 7, 8). 6b was dropped and 6c (the admin page) rides the operator's UI pass — see §12. §12a records what the spec described and the code deliberately does not do.

## Problem

AEGIS runs infrastructure and coding work blind. Every producer of an
operational signal creates its own Todoist task or Slack card first and dedupes
afterwards, each with its own key. The Todoist task *is* the identity of a
problem, so nothing can ask "is this the same thing as last time?" without a
task already existing. A Claude session working a task has nowhere structured
to say what it did, and the next session cannot find out.

Measured against the tree on 2026-09-07:

1. **Three alerting substrates that cannot see each other.**
   `AlertInvestigationFlow` (a `#alert` Todoist task plus a Slack card), the
   `audit_log`-deduped watchdogs (`flow_health`, `social_stuck`,
   `comms_inbound`), and domain tables with private keys (`homelab_drift`,
   `cert_expiry`, `life.expiring_item_alerts`). The resolved-aware `NOT EXISTS`
   dedupe SQL is copy-pasted three times (`activities/alerts.py:898`,
   `activities/flow_health.py:285`, `activities/social.py:154`).
2. **Thirteen fingerprint or dedupe-key schemes**, mutually incompatible:
   vendor fingerprint, synthesised `alertmanager:{alertname}:{instance}`,
   `sentry:{issue_id}`, `aegis-heartbeat:{alertname}:{subject}`, three
   signature classes (`sentry-class:`, `infra-class:`, `{source}-class:`), three
   mute-key namespaces sharing one `alert_mutes` primary key, the
   `alert-{fingerprint}` capture id, a day-bucketed drift key and a cycle-keyed
   expiry tuple. Plus three informal links: `workflow_runs.workflow_id LIKE
   '%' || task_id || '%'` (`activities/clarify.py:556`), title substring
   matching (`activework/guard.py:37`, `extract_service_name`), and the
   `Workflow run:` comment footer as an authorship marker.
3. **Dedupe is keyed on the task, not the problem.** `alert_dedup_index.task_id`
   is `NOT NULL` and joined to `todoist_tasks`, so `InfraHeartbeatFlow` refused
   to use it and built a fourth ledger inside the `settings` row
   `infra_heartbeat_state` (`flows/infra_heartbeat.py:53-58`). `signature` is the
   primary key, so recreating a task resets `first_seen_at` and
   `occurrence_count`: recurrence history is destroyed. Only 12 of 42 open
   `#alert` tasks have a signature row at all (`flows/agent_task.py:537`).
4. **One producer bypasses capture entirely.** `alert_comms_inbound_down`
   (`activities/homelab.py:269`) builds its Todoist command by hand, writes no
   `todoist_capture_idempotency` row, and dedupes on the newest `audit_log`
   row. `close_task_for_resolved_alert` structurally cannot reach it. That is
   #341: eight open copies of the same task.
5. **There is no service state.** No maintenance window, no deploy ingress.
   The GitHub webhook claims `deployment`, `deployment_status` and
   `workflow_run` deliveries and then drops them (`flows/github_alert.py:51`).
   Suppression exists only as incidental delays: a title regex picks 0 to 600 s
   (`activities/alerts.py:1846`), drift waits 120 s, flow health floors at
   60 min. The active-work guard tried to fill the gap by substring-matching
   open tasks against the repo name and muted every swarm alert for a week
   (#355).
6. **Sessions are half-tracked.** `task_sessions` stores `session_id` and
   `host` but not the Claude account, so a change to `default_account` between
   turns makes `--resume` target a profile where the session does not exist.
   `park_task`'s reason is log-only. `STATUS:` and PR URLs are regex-scraped
   from the model's last line. Collision detection is a live `claude agents
   --json` SSH fan-out plus an LLM judge, because nothing records who is on
   what; `_own_session_owner` (`activities/agent_task.py:1007`) has to guess
   ownership from output-file liveness.
7. **The code that does all this is the code that is hardest to change.** Of
   72,499 source lines, `chat.py`, `clarify.py`, `alerts.py` and
   `alert_investigation.py` hold 11,212. CI produces coverage for every package
   and enforces none.

## Decisions taken during brainstorming

| Decision | Choice | Consequence |
|---|---|---|
| Record of truth | Postgres `problems` in AEGIS; Todoist is a rendered view | Nothing parses a task description back; user comments still arrive by webhook |
| Identity | One correlation key per problem, computed by one function | The thirteen schemes and `alert_dedup_index` go |
| Fuzzy matching | Deterministic key attaches; an LLM match only *suggests* | No silent merge of unrelated problems |
| Task creation | Deferred until a problem crosses the attention threshold | A suppressed or self-resolving problem never mints a task |
| Deploy awareness | A `service_state` table, written by webhooks, the deploy role and a chat tool | Replaces the active-work guard and the title-regex delay |
| Session memory | A `work_sessions` registry, written by AEGIS turns and by operator sessions | Collision becomes a lookup; the account is recorded |
| Migration strategy | Strangler: the hub is the seam, one spoke per PR, each PR deletes its bespoke dedupe | No big-bang rewrite; every PR is usable alone |

## Non-goals

- Replacing Todoist as the human surface. GTD stays central; the hub feeds it.
- Replacing Temporal, `interactions`, or `workflow_runs`. They are linked, not
  merged.
- Changing what `AlertInvestigationFlow` does *after* it has a problem to work
  (verification, remediation, Gate 0, Gate 2, the report).
- Pushing into a live interactive Claude session. The operator's session pulls
  context over MCP, as today.
- A general incident-management UI. One admin page, read-mostly.
- The money lane. `finance.journal_index` already has its own idempotency and is
  not an alerting substrate.

## Design

### 1. Entities

Four tables; `task_sessions` widens in place. Each lands with its first reader:
`problems`, `problem_events` and `problem_links` in `030_problem_hub.sql` (PR 1),
`service_state` in `031_service_state.sql` (PR 2), and the `work_sessions`
widening in `034_work_sessions.sql` (PR 5a; 032 and 033 were taken) — a renamed table with no code on
it yet would break the coding lane between PRs.

```sql
CREATE TABLE IF NOT EXISTS problems (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    correlation_key text NOT NULL,                 -- see §3; unique while open
    class           text NOT NULL,                 -- alertname / error class / flow type / 'manual'
    subject         text NOT NULL,                 -- service, node, flow slug, repo, or '' when unknown
    subject_kind    text NOT NULL DEFAULT '',      -- service | node | flow | repo | host | purpose
    title           text NOT NULL,
    severity        text NOT NULL DEFAULT 'warning',
    status          text NOT NULL DEFAULT 'open',  -- §4
    first_seen_at   timestamptz NOT NULL DEFAULT now(),
    last_seen_at    timestamptz NOT NULL DEFAULT now(),
    occurrences     int NOT NULL DEFAULT 1,
    muted_until     timestamptz,
    resolved_at     timestamptz,
    closed_at       timestamptz,
    todoist_task_id text,                          -- set by the projector, §6
    github_issue    text,                          -- 'owner/repo#N', §6
    metadata        jsonb NOT NULL DEFAULT '{}'
);
CREATE UNIQUE INDEX IF NOT EXISTS problems_open_key
    ON problems (correlation_key) WHERE closed_at IS NULL;

CREATE TABLE IF NOT EXISTS problem_events (
    id           bigserial PRIMARY KEY,
    problem_id   uuid NOT NULL REFERENCES problems(id),
    source       text NOT NULL,        -- alertmanager | sentry | heartbeat | flow_health | github | ansible | chat | session | investigation ...
    external_id  text NOT NULL,        -- idempotency, per source
    kind         text NOT NULL,        -- occurrence | resolved | suppressed | investigation | plan | session_note | human_note | state_change
    severity     text,
    payload      jsonb NOT NULL DEFAULT '{}',
    occurred_at  timestamptz NOT NULL DEFAULT now(),
    created_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source, external_id)
);

CREATE TABLE IF NOT EXISTS problem_links (
    problem_id  uuid NOT NULL REFERENCES problems(id),
    link_kind   text NOT NULL,          -- todoist_task | github_issue | github_pr | workflow_run | interaction | work_session | problem
    ref         text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (problem_id, link_kind, ref)
);

CREATE TABLE IF NOT EXISTS service_state (
    subject      text NOT NULL,
    subject_kind text NOT NULL DEFAULT 'service',
    state        text NOT NULL,         -- deploying | maintenance | degraded | ok
    until_at     timestamptz,           -- NULL = until cleared
    set_by       text NOT NULL,         -- 'github:deployment' | 'ansible' | 'chat:<agent>' | 'heartbeat'
    note         text NOT NULL DEFAULT '',
    updated_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (subject, subject_kind)
);

ALTER TABLE task_sessions RENAME TO work_sessions;
ALTER TABLE work_sessions DROP CONSTRAINT IF EXISTS task_sessions_pkey;
ALTER TABLE work_sessions ADD COLUMN IF NOT EXISTS id uuid DEFAULT gen_random_uuid();
ALTER TABLE work_sessions ADD COLUMN IF NOT EXISTS problem_id uuid REFERENCES problems(id);
ALTER TABLE work_sessions ADD COLUMN IF NOT EXISTS account text NOT NULL DEFAULT '';
ALTER TABLE work_sessions ADD COLUMN IF NOT EXISTS engine text NOT NULL DEFAULT 'claude';
ALTER TABLE work_sessions ADD COLUMN IF NOT EXISTS owner text NOT NULL DEFAULT 'aegis';   -- aegis | operator
ALTER TABLE work_sessions ADD COLUMN IF NOT EXISTS status text NOT NULL DEFAULT 'active'; -- active | parked | done
ALTER TABLE work_sessions ADD COLUMN IF NOT EXISTS summary text NOT NULL DEFAULT '';
ALTER TABLE work_sessions ADD COLUMN IF NOT EXISTS last_seen_at timestamptz;
ALTER TABLE work_sessions ADD PRIMARY KEY (id);
CREATE UNIQUE INDEX IF NOT EXISTS work_sessions_task_aegis
    ON work_sessions (task_id) WHERE owner = 'aegis' AND status <> 'done';
```

The rename keeps every existing row and the one-AEGIS-session-per-task rule
the coding lane relies on (`create_session`'s `ON CONFLICT DO NOTHING` moves
onto the partial index). Operator sessions are additional rows on the same
task. All DDL is idempotent, per the migration convention.

`alert_dedup_index`, `alert_mutes`, the `settings` rows `infra_heartbeat_state`
and `alert_digest_buffer` are dropped by the PR that retires their last reader
(§12), not by this migration.

### 2. Ingest

One module, `core/src/aegis/services/hub.py`, imported by the worker the way
`alert_tasks.py` is today. One entry point:

```python
@dataclass
class Event:
    source: str            # producer name, closed vocabulary in hub.py
    external_id: str       # idempotency within the source
    kind: str              # occurrence | resolved | investigation | plan | session_note | human_note
    title: str
    subject: str = ""      # service / node / flow / repo; '' when the producer does not know
    subject_kind: str = ""
    klass: str = ""        # alertname, error class, flow type; '' for free text
    severity: str = "warning"
    payload: dict = field(default_factory=dict)
    occurred_at: datetime | None = None
    problem_id: str | None = None   # a producer that already knows its problem (investigation, session)

async def ingest_event(pool, event: Event) -> IngestResult
# IngestResult: problem_id, created (bool), attached (bool), suppressed (bool), duplicate (bool)
```

`ingest_event` does, in one transaction: claim `(source, external_id)` and
return `duplicate=True` on conflict; correlate (§3); apply the transition (§4);
check `service_state` (§5); write the `problem_events` row; and enqueue a
projection (§6). It never notifies and never talks to Todoist inline. The
projector runs from the same transaction's outcome, so a crash between "problem
updated" and "task created" leaves a problem with no task, which the projector
sweep repairs, not a task with no problem.

Every current producer becomes one `ingest_event` call:

| Producer | Today | Source / class / subject |
|---|---|---|
| Alertmanager, Grafana webhook | `webhooks.py:543` starts `AlertInvestigationFlow` | `alertmanager` / alertname / service or node |
| Sentry webhook and poll | `SentryPollFlow` | `sentry` / error class / service |
| `InfraHeartbeatFlow` | `_spawn` child with a timestamped id | `heartbeat` / NodeDown, DockerServiceDown, ServiceDownProlonged, HeartbeatCollectFailed / node or service |
| `FlowHealthWatchdogFlow` | `audit_log` dedupe, Slack card | `flow_health` / failing, stale, llm_dead / flow slug or purpose |
| `DeliveryWatchdogFlow` | hand-rolled task | `delivery` / comms_inbound_down, undelivered / `comms` |
| `ServiceDriftFlow` | `homelab_drift` day key | `drift` / drift_type / service |
| `CertRadarFlow`, `ExpiryRadarFlow` | sticky column, cycle tuple | `expiry` / cert or item / domain or item id |
| `SocialMetricsFlow` | `audit_log` dedupe | `social` / stuck / post id |
| `LLMSpendGuardFlow` | edge-triggered card | `llm_governor` / breached / '' |
| GitHub webhook | drops deploy events | `github` / deployment / repo (writes `service_state`, §5) |
| Clarify pandora routes, `#chat` captures, `investigate_resource` | synthetic alert dicts | `chat` / '' / whatever the route or the LLM extracts |
| Ansible deploy role | nothing | `ansible` / deploy / service (writes `service_state`) |

Resolution is an event too (`kind='resolved'`), from the Alertmanager
`resolved` status, heartbeat recovery, flow health recovery, drift gone, and
the comms probe succeeding.

`POST /api/hub/events` accepts the same shape from outside, authenticated with
the existing alert webhook token, so a deploy job, a boot script, or a test can
report without a bespoke route.

### 3. Correlate

One pure function replaces the thirteen schemes:

```python
def correlation_key(event: Event) -> str:
    """'{class}:{subject_kind}:{subject}', lowercased and slug-safe.
    A remediable infra class keeps its service (a restart is per service);
    NodeDown keeps its node. An event with no class and no subject returns ''
    and is never auto-attached."""
```

Examples, all of which collapse today's duplicates:

| Events | Key |
|---|---|
| Alertmanager `DockerServiceDown{service=monitoring_cadvisor}` and heartbeat "cadvisor below desired 2 ticks" | `dockerservicedown:service:monitoring_cadvisor` |
| Alertmanager `NodeDown{node=wow}` and heartbeat node Ready→Down | `nodedown:node:wow` |
| Flow health "GmailIngestFlow failing" and the same flow stale an hour later | `flow_failing:flow:gmailingestflow` (stale and failing are distinct classes, deliberately: they have different fixes) |
| Comms probe failing on eight consecutive days | `comms_inbound_down:comms:polling`, one problem, eight occurrences |
| Sentry issue 4711 from the webhook and from the poll | `sentry_4711:service:koyracloud-api` |

Attach when an open problem holds the key. Otherwise create. `occurrences`
and `last_seen_at` move on every occurrence, including on a reopened problem:
history survives task recreation because the problem is the record.

**Fuzzy matching is a suggestion, not a merge.** For an event whose key is
`''` (chat text, an email-derived capture, a hand-written task routed through
clarify) the hub creates the problem and then asks the `balanced` tier, with
the same prompt shape as the collision judge, whether it matches any of the
open problems on the same `subject_kind`. A `yes` writes a
`problem_links(kind='problem')` row and a "Possibly the same as …" line in the
projection. A human merges with `merge_problems` (§7). AEGIS never merges two
problems on an LLM verdict alone: the cost of a wrong merge is a hidden outage.

### 4. State

```
open ──▶ investigating ──▶ waiting_human ──▶ fixing ──▶ verifying ──▶ resolved ──▶ closed
  │            │                 │              │           │             ▲
  └── muted ◀──┴─────────────────┴──────────────┘           └─ (recurs) ──┘
  └── suppressed (service_state says deploying/maintenance)
```

Transitions are a table in `hub.py`, not scattered `UPDATE`s:

- `occurrence` on `resolved` within `reopen_window` (default 24 h) → `open`
  again, same problem, occurrences +1. After the window → `closed` and a new
  problem with the same key; the old one is linked as `problem` for history.
- `resolved` → `resolved`, `resolved_at` set; the projector closes the task
  through today's `close_task_for_resolved_alert` logic (moved into the hub,
  same `@me` guard).
- `investigation` from `AlertInvestigationFlow` → `waiting_human` when a gate
  card is open, `fixing` when a PR was staged, `resolved` on
  `auto_remediated`. **The alert source owns whether a problem is live; an
  investigation only annotates it** (#484, added 2026-09-11). A verdict that
  lands after the alert has already resolved the problem is recorded as an
  `investigation` event, and its card still goes out, but the problem stays
  `resolved`: no `reopen`, and the task stays closed. The next real
  occurrence goes through the `occurrence` rule above — a reopen inside the
  window, a new problem after it — so the task follows and a fresh
  investigation is asked for. Any other caller of `set_status` that moves a
  problem off `resolved` still reopens it.
- A fix PR is followed (#502, added 2026-09-12; `services/hub_fix.py`). The
  flow leaves a problem whose PR opened in `fixing` — until #502 its last step
  moved it straight back to `waiting_human`. The GitHub webhook's `closed`
  event moves it on: `verifying` once a fix merged and none is still open,
  `waiting_human` when every fix PR closed unmerged. The hub sweep then
  resolves a `verifying` problem once no occurrence has come for
  `fix_verify_hours` (24) after the merge, and moves it to `open` when one
  comes later than `fix_grace_hours` (1) after it, outside a deploy window.
  Only a PR an investigation opened (`pr_urls` on its `investigation` event)
  is followed, and every move goes through `set_status` with the
  `investigation` source, so a problem the alert already resolved stays
  resolved.
- An occurrence on an existing problem (attach, reopen, promote) raises
  `severity` to the worse of the stored one and its own (`critical` >
  `error` > `warning` > `info`, the same order a group uses for its worst
  member). Nothing lowers it (#486, added 2026-09-11): a problem that was once
  critical does not read as fine because a later occurrence was milder. Other
  events — a resolution, an investigation report, a note — never change it.
- `muted_until` set → `muted`; an occurrence on a muted problem is recorded and
  not projected. This replaces `alert_mutes`; the mute key *is* the correlation
  key, so the four namespaces go.
- `closed` is set by the operator or by the `CleanupFlow` sweep 7 days after
  `resolved_at`.

Every transition writes a `state_change` event, so the timeline is complete
and the daily digest (§9) is a query.

### 5. Service state and suppression

`service_state` is the "deploying now" tracker. Writers:

- **GitHub webhook** (deferred; not in PR 2). Neither of this deployment's
  deploy paths — Ansible, or a hand-run `docker service update` — emits
  GitHub deployment events, and the image-build `workflow_run` completes
  hours before a manual rollout, so a writer here would be dead code or
  wrong. Add it when a repo actually uses the Deployments API. The design as
  it would work: `deployment` and `deployment_status` set
  `deploying` on the repo's mapped subject (via `project_repo_map` and the
  `resources` row's `service` metadata) with `until_at = now() + 15 min`;
  `deployment_status=success|failure` clears or marks `degraded`. `workflow_run`
  `completed` on the image-build workflow of a tracked repo sets `deploying`
  for the same window, because the swarm rollout follows it.
- **Ansible.** The homelab-gitops AEGIS role gains one `uri` task at the top
  and one at the bottom, posting `{"subject": "<stack>_<service>", "state":
  "deploying"|"ok", "set_by": "ansible"}` to `/api/hub/service-state`. That
  role owns every service env already; this is the natural place.
- **Chat tool** `set_service_state(subject, state, minutes, note)` for
  maintenance windows. Granted to the `infra` capability holder.
- **Sweep.** `HubSweepFlow` (every 5 min) opens every `suppressed` problem
  whose window has passed with no `resolved` event. The heartbeat only
  emits on transitions, so without the sweep a service that broke during a
  deploy and stayed broken would surface only at the 24h re-investigation.
- **Heartbeat.** When it sees a subject in `deploying` converge (desired
  replicas met for 2 ticks) it clears the state. When `until_at` passes without
  convergence, it does nothing: the next occurrence promotes.

Reader: `ingest_event` marks an occurrence `suppressed` when the subject (or
its node, for `subject_kind='service'` with a known placement) is `deploying`
or `maintenance`. A suppressed occurrence is stored, counted, and not
projected. An occurrence that arrives after `until_at`, or a `resolved` that
never comes, promotes the problem to `open` and projects with "seen during
deploy, still failing".

This retires: the active-work guard and `activework/` (#355) in PR 2, and —
each in the PR that moves its producer onto the hub, because until then the
producer still notifies directly and the incidental delay is its only
deploy tolerance — the title-regex
verification delay, `ServiceDriftFlow.recheck_delay_seconds`, and
`FlowHealthConfig.min_stale_minutes`. `alert_investigation.py` shrinks by the
guard block and step 3.

### 6. Projection to Todoist and GitHub

`hub.project(problem_id)` is the only writer to the human surfaces. It is
idempotent and re-runnable; a projector sweep (`CleanupFlow`, every 15 min)
re-projects any problem whose `last_seen_at` is newer than its last projection.

**Attention threshold.** A problem projects when it is `open` or later, not
`suppressed`, not `muted` — and, if it came from a producer that can clear its
own signal, once it has outlived its class's verification window.

Built as a window in seconds rather than a count of occurrences (#537): a
five-minute blip can occur five times, so a count does not tell a flap from an
outage while a clock does. The window is `hub.verify_seconds` — the same number
an investigation waits before spending effort, because "long enough to believe
this is real" is one question — overridable per class in the
`hub_settle_seconds` settings row, where `{"*": 0}` turns the whole thing off.

Two scopes keep it honest. It applies only to the producers that send their own
resolution (`alertmanager`, `heartbeat`, `flow_health`, `delivery`); every other
problem on the hub is a considered finding — a reconciliation, a stale feed, an
agent's question, a hand-written task — which no amount of waiting makes truer,
so those project on sight. And it holds back only the Todoist task: the
investigation still starts on the first occurrence, so the diagnosis and the
Slack card are as immediate as they ever were.

Below the window the problem lives in the hub, the digest and Slack. Nothing
has to come back for it — the sweep re-drives every open untasked problem — and
one that resolves inside its window is marked seen and never earns a task at
all.

A recurrence is not held back. `reopen` keeps the original `first_seen_at`, so
a problem that blipped, resolved untasked and came back is already past its
window and projects at once: the second episode is the evidence the first one
lacked. That reasoning is bounded by `REOPEN_WINDOW` (24 hours) and says only
what it can — a return within a day of the resolve is a pattern. Later than
that the return is a `rollover`, a fresh problem with a fresh `first_seen_at`,
and it waits like any first sighting. Measuring age from the current episode
instead would make a service that flaps every three minutes invisible forever.

The two producers in scope are the ones **outside** AEGIS that send their own
resolution — a monitoring stack and the swarm heartbeat, both re-checking on a
scale of seconds. AEGIS's own watchdogs (`flow_health`, `delivery`, `social`,
`drift`, `expiry`, `llm_governor`) also resolve what they stop finding, and
were briefly in the same set; that was a mistake of kind rather than degree.
Their sweeps run every 30 minutes or hourly, so a three-minute window cannot
observe a blip they would clear — it can only delay the task — and they have
already judged the thing worth reporting before the hub hears of it.

**Where it goes** depends on `subject_kind`, per the operator's own rule that
repo-scoped work lives in GitHub Issues:

| Subject kind | Surface |
|---|---|
| service, node, host, flow, purpose, comms | Todoist task, `#alert`, `@pandora` (or the `infra` tag holder), via `capture_to_inbox` with `external_id = 'problem-<id>'` |
| repo (a code defect from Sentry or an investigation verdict naming a repo) | GitHub issue on that repo via `gh issue create`; a Todoist task only when a gate card needs a human |

Both refs land in `problem_links` and on the problem row.

**Comments, not new tasks.** After creation every event is a comment, with one
rule against floods: occurrences are collapsed to one comment per
`collapse_window` (default 30 min) reading "N more occurrences since HH:MM
(last: <title>)". Investigation reports, plans, session notes and state changes
comment immediately. Comments keep the `Workflow run:` footer so
`is_user_note` and clarify keep excluding them.

**Plan steps become subtasks.** A `plan` event whose payload carries
`steps: [...]` with more than one entry creates Todoist subtasks
(`build_subtask_add_command` with `parent_id`), each linked as `plan_step`
with `ref = '<index>:<subtask id>'`. A session completing a step reports it
(§7, `report_progress(step_done=N)`) and the projector completes the subtask.

Shipped in 5b with two deviations. The link kind is `plan_step`, not
`todoist_task`: the parent lookup (`find_problem_for_task`) matches on a
`todoist_task` ref, and a subtask ref there would answer it with the wrong
task. And the checklist is created ONCE per problem — a re-plan comments but
does not reopen a list somebody may already have ticked off.

The producer is the coding lane: a turn is asked to write its plan under a
`PLAN:` marker, one numbered step per line, and `_plan_steps` reads the last
such block. A marker rather than a heuristic, because the whole first-turn
report is itself a numbered list.

**The status block.** The task description carries one block between markers,
replaced whole on every projection, the way `books.py` renders journal blocks:

```
<!-- aegis:problem 5f1c… -->
Status: fixing · seen 14× since 2026-09-04 09:12 · last 2026-09-07 08:40
Service: monitoring_cadvisor (deploying until 08:55, set by ansible)
Links: PR hikmahtech/homelab-gitops#212 · run alertmanager-8c2… · card gate2-…
Sessions:
  aegis  · 267b3b12 · personal@meem · turn 3 · parked 08:41 · "plan posted, waiting on approval"
  you    · 9e0a41d0 · work@meem     · active 09:02 · "checking cadvisor mounts on noon"
Take over: cd ~/Workspace/infrastructure/homelab-gitops-aegis-wt/task-6hPC… && claude --resume 267b3b12
<!-- /aegis:problem -->
```

Text above and below the block is the user's and is never touched. Nothing
reads the block back; it exists for the human and for a session that only has
the task in front of it.

### 7. Sessions

`work_sessions` is the registry of who is on what. Two writers.

**AEGIS turns.** `ensure_task_session` and `launch_task_turn` already create
and update the row. They gain `account` (the `CLAUDE_CONFIG_DIR` label
`_agent_launch_flags` resolved), `engine`, and `summary` (the `STATUS:` line
and the first 200 characters of the turn's final message). `--resume` reads
`account` from the row instead of re-resolving it, which fixes the silent
wrong-profile resume. `park_task(reason)` writes the reason to `status` and
`summary` instead of the log.

**Operator sessions.** Three tools on the operator MCP mount and in chat,
schemas in `CHAT_TOOLS`, executors in `services/tools/hub.py`, registry
entries hand-written in `chat.py` as the convention requires:

- `task_context(task_id | problem_id)` → the problem, its last 20 events,
  every `work_sessions` row with summary, links, service state, and the
  take-over command. This is what a new session reads first.
- `report_progress(task_id, summary, status='active'|'parked'|'done',
  session_id?, pr_url?)` → upserts the operator's `work_sessions` row
  (`owner='operator'`, `account` from the mount's identity, `host` from the
  connection) and writes a `session_note` event; the projector turns it into a
  comment and a line in the status block. `pr_url` adds a `github_pr` link.
- `merge_problems(keep_id, merge_id)` → moves events and links, closes the
  merged one with a `problem` link back.

`comment_on_task` stays for free-text replies. `report_progress` is withheld
from run mounts the way `comment_on_task` is (`_UNSERVED_TOOLS`): an AEGIS
turn reports through its own activity, never through the tool, or a run could
mark its own task done.

**Making the operator side happen.** A tool nobody calls is a tool that does
not exist. The operator's `~/.claude/settings.json` already carries a `Stop`
hook; this design adds a `SessionStart` hook and extends `Stop` so that a
session launched inside a `*-aegis-wt/task-<id>` worktree, or with a
`AEGIS_TASK` environment variable, calls `report_progress` through the operator
mount with a one-line summary the hook asks the model for. That lives in the
operator's dotfiles, not in this repo; the spec records it because without it
§7 is half a feature.

**Collision becomes a lookup.** `check_task_collision` reads `work_sessions`
for the task: an `operator` row with `status='active'` and `last_seen_at`
within 30 min is `you_are_in_it`; otherwise `proceed`. `list_coding_sessions`
(`claude agents --json`) stays as a liveness cross-check that flips a stale
`active` row to `parked`, run by the same 15-min sweep. The SSH git-context
fan-out, the LLM same-task judge and `_own_session_owner` are deleted. The
`hand_to_you` verdict goes with them: an operator who wants AEGIS out of a
task says so with `report_progress(status='active')` or `handoff_task`.

**Kept from 5b's delete list.** `_status_line` still reaches
`workflow_runs.result_summary`. The scrape is no longer identity-bearing — the
verdict now also feeds the session row's park reason and the plan event — but
`result_summary` is the only PER-RUN record of what a turn decided, and the
registry is per task. Deleting it would cost flow history for nothing.

### 8. Investigation as a worker

`AlertInvestigationFlow` takes `problem_id` in its input and loses ownership
of identity. Deleted from `run()`: step 1 (resolved on arrival), step 2
(`check_dedup`), step 2.5 (mute), step 2.7 (signature), step 2.8 (capture),
the active-work guard, every `accumulate_digest_item` call, and the
`log_alert` bookkeeping. Kept, unchanged in behaviour: verification wait (now a
flat `verify_seconds` from the problem's class config, not a title regex),
auto-remediation, Gate 0, Gate 2 with escalation, `stage_pending_pr`, the full
report. The report, the gate outcomes and the remediation result each become an
`ingest_event(kind='investigation', problem_id=...)`; the projector comments
them. `pending_prs` gains a `problem_id` column; `alert_fingerprint` stays for
one release and is dropped when nothing reads it.

The hub decides *whether* to investigate: `ingest_event` starts the flow as an
abandoned child (workflow id `investigate-<problem_id>-<occurrence n>`) when a
problem is created or reopened, when a `ServiceDownProlonged` problem passes
`restuck_hours` without a `resolved` (the #138 rule, now a query over
`problem_events` instead of a `settings` map), and never for `suppressed` or
`muted`. The heartbeat's `escalate` flag becomes `severity='critical'` on the
event; the flow reads it from the problem.

`AgentTaskFlow._run_infra` resolves the service from `problems.subject`
through the task's `problem-<id>` link instead of parsing the title. When the
link is missing (a hand-written task) it falls back to `extract_service_name`
and says so in its comment.

### 9. Digest and admin

`build_alert_digest` becomes one query: problems with any event in the last
24 h, grouped by status, with suppressed and muted counts. The
`settings.alert_digest_buffer` row goes.

Why it matters beyond tidiness: the buffer recorded what a flow *remembered to
append*. An item written by a branch that then failed was in the digest anyway,
and one whose branch was never reached was missing from it forever — and the
read cleared it, so a re-run of the briefing reported an empty day. The query
is over what actually happened and can be asked twice.

One admin page, **Problems**: open problems by severity and `last_seen_at`,
each expanding to its event timeline, links and sessions; `service_state` as a
strip at the top with a clear button. Routes: `GET /api/admin/problems`,
`GET /api/admin/problems/{id}`, `POST /api/admin/problems/{id}/close|mute|merge`,
`GET/PUT /api/admin/service-state`. Mutations are the same functions the tools
call.

Shipped in 6a as the routes, plus `GET /api/admin/problems/digest` (the
briefing's own query, so the page and the message cannot disagree) and a
`resolve` mutation the spec had not named — the panel needs a way to say "this
is fixed" without waiting for a producer to send a `resolved` event. The page
itself is 6c, with the wider UI pass.

The close sweep's cutoff is INCLUSIVE, which is what lets the panel's close
button retire a problem it has just resolved; a strict comparison closed
nothing at all in that case.

### 10. Error handling

- `ingest_event` is the only path that may raise to a producer, and it raises
  only on a DB failure. A producer treats that as "not recorded" and retries
  under its own activity policy; the `(source, external_id)` claim makes the
  retry safe.
- Projection failures (Todoist 5xx, `gh` failure) are logged and left to the
  15-min sweep. A problem is never rolled back because its task could not be
  created.
- `correlation_key` on malformed input returns `''`, which creates rather than
  attaches. Creating a duplicate problem is recoverable with `merge_problems`;
  attaching to the wrong one hides an outage.
- `service_state` reads fail open: an unreadable table suppresses nothing.
- `report_progress` from a session whose task has no problem (a plain `@code`
  task) creates one with `class='manual'`, `subject_kind='repo'`, so the
  session registry works for every task, not only alert-born ones.

### 11. Testing

The hub is mostly pure functions over rows, which is why it is the place to
start enforcing coverage.

- `tests/core/test_hub_correlate.py`: the key function against a fixture table
  of today's real alert payloads (Alertmanager, heartbeat, Sentry, flow health,
  comms); every pair the Problem section calls a duplicate must produce one
  key, and every pair it calls distinct must not.
- `tests/core/test_hub_transitions.py`: the state table, reopen window,
  mute, suppression promotion, close sweep. Pure.
- `tests/core/test_hub_ingest.py`: real test DB. Idempotent claim, attach vs
  create, suppressed occurrence not projected, resolved closes the task with
  the `@me` guard, a crash between problem write and projection is repaired by
  the sweep.
- `tests/core/test_hub_render.py`: the status block is replaced whole and the
  user's text around it is untouched; the collapse comment; subtask creation
  from a plan.
- `tests/worker/test_alert_investigation.py`: the deleted steps are gone;
  `problem_id` in, `investigation` events out. The 1,093-line gates test file
  is unchanged in intent and shrinks in setup.
- `tests/worker/activities/test_agent_task_sessions.py`: `account` recorded
  at launch and read at resume; collision as a lookup; park reason on the row.
- `tests/core/test_mcp_server.py`: `report_progress` withheld from run mounts
  (extend the `_UNSERVED_TOOLS` tripwire); `task_context` served.
- Registry gates: `chat_tools_golden.json` and `EXPECTED_TOOL_NAMES` gain the
  four tools, inserted surgically.
- CI: `--cov-fail-under` for `core/src/aegis/services/hub.py` at 90 in PR 1;
  the per-package gate is set to the measured value at each later PR and never
  lowered.

Each test is checked by break-and-revert before it is trusted.

### 12. Delivery order

Seven PRs. Each is usable on its own and each deletes the bespoke machinery
it replaces, so the tree never carries two ways to do one thing.

| PR | Ships | Deletes |
|---|---|---|
| 1 | Migration 030 (`problems`, `problem_events`, `problem_links`), `hub.py` (ingest, correlate, transitions, `event_from_alert`), `POST /api/hub/events`, `auth.alert_token_ok` shared with the alert webhook, tests, coverage gate. Dark: no producer calls it. | nothing |
| 2 | Migration 031 `service_state`; suppression and promotion in `hub.py`; `POST /api/hub/service-state`; the Ansible hook (homelab-gitops PR); `set_service_state` tool; `HubSweepFlow` (promotes expired suppressions); heartbeat converge-clear. | active-work guard, `activework/`, `ActiveWorkActivities`, `active_work_lookback_hours` |
| 3a | The projector (`services/hub_project.py`): task creation through the idempotent capture, collapsed occurrence comments, close on resolve / reopen on recurrence, the status block; `HubSweepFlow` projects what is pending; `load_task_context` reads the problem behind a task so the infra verb no longer parses titles. | nothing (dark until 3b: the hub still has no alert producer) |
| 3b | Alertmanager, Sentry and heartbeat producers call `ingest_alert`; clarify and `investigate_resource` keep starting the flow, whose step 0 ingests for them; `AlertInvestigationFlow` takes `problem_id`; `scripts/hub_backfill.py`. | `alert_mutes` (alert pipeline's use), `infra_heartbeat_state` re-investigation maps, steps 1–2.8, `check_dedup`, `build_alert_signature`, `close_task_for_resolved_alert`, `log_alert`, `check_alert_resolved`, `record_heartbeat_resolved`, the title-regex `get_verification_delay`. `alert_dedup_index` is unread from here and dropped in PR 6, after the backfill has read its recurrence counts. |
| 4a | `services/hub_watch.py::reconcile_findings` (findings in, fresh problems and recoveries out); flow health, stuck social posts and the LLM governor on it; migration 032 drops `alert_mutes`. | three copies of the `audit_log` dedupe SQL (two here, the third went in 3b), `alert_mutes` and its four key namespaces, the watchdogs' `dedup_hours` / `recovery_hours` knobs, the llm_dead new-evidence rule (an open problem is one problem) |
| 4b | The delivery watchdog (undelivered cards, the comms probe), service drift and cert expiry on `reconcile_findings`; the infra verb parks non-service problems. | the hand-rolled comms task, `resolve_comms_inbound_alert`, the hourly undelivered re-card. `ServiceDriftFlow.recheck_delay_seconds` and `FlowHealthConfig.min_stale_minutes` STAY: they filter unplanned restarts, which no declared window covers. The expiry radar is not a producer: its ledger claims a human ack card, not an alert. |
| 5a | Migration 034: `task_sessions` → `work_sessions` with `id`, `problem_id`, `account`, `engine`, `owner`, `status`, `summary`, `last_seen_at` and a partial unique index on the live AEGIS row. `account` recorded at launch and used at resume; `park_task` writes its reason to the row; collision is a registry lookup (`turn_still_running` / `you_are_in_it` / `proceed`) with a 15-minute liveness cross-check (`reconcile_work_sessions`); `task_context`, `report_progress` and `merge_problems` tools, granted to all four agents and withheld from run mounts; sessions on the status block. | the SSH git-context fan-out (`_enrich_sessions`, `_session_git_context`), the LLM same-task judge (`build_same_task_prompt`, `parse_same_task_verdict`, `find_session`, `human_sessions_in_repo`, `_task_identity`, and `AgentTaskActivities.llm_client`/`model_balanced`), `_own_session_owner`, the `hand_to_you` verdict and its exit |
| 5b | Plan steps as Todoist subtasks (`plan_step` links, `PLAN:` block parsed off the turn, `record_plan`), `report_progress(step_done=N)` ticks one off, step progress on the status block, `ensure_problem_for_task` shared by the tool and the activity, and the outbox description write now supersedes a pending one. | the duplicated "give a plain task a problem" block in `tools/hub.py` |
| ~~6b~~ **dropped** | GitHub-issue projection for `repo` subjects. Deferred out of 5b: nothing produces a `repo` subject without a task today (Sentry keys on the service, and a `report_progress` problem already has its task), so the surface would have shipped with no producer — dead code by the programme's own rule. It was then dropped rather than deferred again. The producer never materialised and building one would have been a product decision nobody asked for: an alert's subject is the service, not the repo, and re-keying it on the repo would change every correlation key mid-flight; a `report_progress` problem already owns its Todoist task; and an investigation that names a repo already has Gate 2's "Open PR" and a `github_pr` link for the code half. Filing alert-born defects as GitHub issues automatically is a change to how the operator's day works, not a refactor, so it stays out until asked for. Adding it later is a branch in `hub_project.project` on `subject_kind` plus a `gh issue create` over the existing SSH connector — an afternoon, on top of everything else being in place. | — |
| 6a | `hub.digest` / `close_resolved` / `list_problems` / `problem_detail`; `HubActivities.build_digest` and `close_resolved_problems`; the nightly close sweep on `CleanupFlow` (`problem_close_days`, default 7); admin routes `GET /api/admin/problems`, `/problems/digest`, `/problems/{id}` and `POST /problems/{id}/mute\|resolve\|close\|merge`, `GET/PUT /api/admin/service-state`, every mutation calling the hub's own transition. Migration 035 drops `settings.alert_digest_buffer`. | `AlertActivities.build_alert_digest`, `accumulate_digest_item`, `_read_digest_buffer` and the four investigation call sites that fed it |
| 6c | The admin Problems page (`Problems.tsx`): the live list severity-first, each row expanding to its timeline, links and sessions; the service-state strip with open and clear; mute / resolve / close / merge, each calling the hub's own function. Plus the Overview's own numbers: `open_problems` and `occurrences_24h` read from the hub, replacing an "Alerts · 24h" tile that counted `AlertInvestigationFlow` runs — which under-reported by design, since a deduped or suppressed alert starts no flow at all. | the investigation-run count behind the Overview's alert tile |
| 8 | What the end-of-programme validation found: a manual problem keyed per TASK rather than per repo (two `@code` tasks in one repo were one problem); a merge no longer replays the duplicate's timeline (a moved `resolve` completed the kept problem's live task); leaving `resolved` clears `resolved_at` and reopens the task; the admin close button closes ONE problem; the duplicate claim is read under the advisory lock; `ingest_alert` is idempotent across a Temporal retry (a retried heartbeat ingest used to cost the alert its investigation); `stale_stuck_problems` filters by class and skips `waiting_human`; the events route 400s on a malformed `problem_id`; the projection sweep gets a budget it can finish in. | `problems.github_issue` (no writer since 6b was dropped, migration 036), the heartbeat state's dead `confirmed_at` / `reinvestigated_at` clocks, `hub.find_open_problem` |
| 7 | The `SessionStart` / `Stop` hook script and its wiring, documented in `docs/infrastructure.md` (it lives in the operator's dotfiles, not this repo); the rollout runbook with its ordering and verification queries; `docs/architecture/overview.md` and `docs/how-it-works.md` brought up to date. | — |

PR 3 is the large one, so it ships as 3a (the projector, dark) and 3b (the
producers and the investigation flow, which is where the deletions happen).
Together they close #341 and the remainder of #279.

### 12a. What the spec described and the code does not do

Written at the end of the programme, from a full audit of the shipped diff
against this document. Each of these is a deliberate omission, not an
oversight found later — but none of them was written down at the time, which
is the actual failure this section fixes.

| Spec said | Shipped | Why |
|---|---|---|
| §6: a problem projects only once it passes an attention `threshold_for_class` (default 1, `flow_stale` 2) | **Built 2026-09-12 (#537), as a window in seconds.** Until then there was no threshold at all: projection gated on status and mute only, so every open problem earned a task on its first occurrence. | The original reasoning — that recovery semantics (`hub_watch.reconcile_findings`) remove the noise a threshold existed to remove, and a threshold would only delay a real outage — held for findings and was wrong for alerts. Measured in prod after a month: 15 of the 60 problems that earned a task were over inside 15 minutes, 8 inside five. Each was created, clarified and auto-completed without a human acting on it. So the gate exists, but as a clock rather than a count (a five-minute blip occurs five times), scoped to producers that clear themselves, and it holds back only the task — never the investigation. The "delays a real outage" objection is answered by that scope: the diagnosis and the Slack card still land on the first occurrence. |
| §3: a `''`-key event gets a balanced-tier "possibly the same as" suggestion, written as a `problem_links(kind='problem')` row | Not built as a suggestion. An uncorrelated event still creates; §14 shipped the LLM judgement in a narrower, acting form instead. | A suggestion is only worth writing if someone reads it. What the operator actually wanted was for the hub to ACT on the pattern — see §14, which restricts the judgement to one class and one subject kind and therefore never has to guess that two different failures are the same one. |
| Files touched: `collapse_window`, `reopen_window`, per-class `verify_seconds` and thresholds as `activities.config` on the hub sweep row | Python constants (`REOPEN_WINDOW`, `_VERIFY_SECONDS`, `COLLAPSE_WINDOW`) — except `verify_seconds`, which took a DB override in #537 (`hub_settle_seconds`, a `settings` row merged over the code defaults, not `activities.config`). | They describe what an outage is rather than what an operator prefers, so no deployment had wanted a different value. That changed when the same number started deciding whether a Todoist task exists: how long a service takes to prove itself is a fact about the operator's own homelab, and this repo is open source, so it belongs in their database. The other two are unchanged. |
| §8: `pending_prs` gains a `problem_id` column; `alert_fingerprint` stays one release | Corrected 2026-09-11 (#478): migration 038 added `pending_prs.problem_id` and dropped `alert_fingerprint` in the same release. `stage_pending_pr` writes the column; nothing reads it — the only SELECT fetches title, body and branch by id. | Kept, not dropped: it is the only record of which problem a staged fix is for until the PR is opened, and it costs one nullable column. Once opened, `record_investigation` links the PR to the problem as `problem_links(github_pr)`, which is what everything reads. An earlier version of this row said the table was unchanged; it was not. Checked again 2026-09-12 (#502), after the Pandora review found it empty "in its whole history": it is wired, not dead. A row is written only when someone picks Open PR — twice ever, 2026-07-31 and 2026-08-10 — and `CleanupFlow` prunes it after 30 days (it pruned one row on 2026-08-31; the second answer left no row to prune). Its `status` is written (`opened` / `failed`) and never read. The fix-PR follow-up does not build on it: a PR can outlive the 30 days, and the problem's link and events are the record. |
| §8: the heartbeat's `escalate` flag becomes `severity='critical'` on the event and the flow reads it from the problem | `escalate` still travels on the alert dict; every heartbeat alert is already `severity='critical'` | Severity and escalation turned out to be different questions — every heartbeat alert is critical, but only some escalate — so collapsing them would have lost the distinction the gate-2 race depends on. |
| §8: "`ingest_event` starts the flow as an abandoned child" | The producer starts it, on `IngestResult.investigate` | Only a workflow can start a child workflow; `ingest_event` is a service function called from activities and from Core. The hub still DECIDES; it just cannot be the one to start. Recorded in `CLAUDE.md` and §12's PR 3b row from the start, but §8's own text was never corrected. |
| §7: `report_progress` takes `account` from the mount's identity and `host` from the connection | Both are caller-supplied; `account` defaults to the literal `"operator"` | The MCP mount authenticates an agent, not a login: it does not know which `CLAUDE_CONFIG_DIR` the caller runs under. The session hook sends it instead (`docs/infrastructure.md`), which is the only place that actually knows. |
| §4, §6: a muted problem is "recorded and not projected"; "nothing reads the block back" | Added 2026-09-11 (#473). A muted problem that resolves still projects its resolve and closes the task; occurrences and returns stay silent until the mute ends. A task someone completes resolves its problem (`hub_project.reconcile_completed_tasks`, a `HubSweepFlow` step) — unless the completion is older than the problem's latest return, in which case it is the hub's own close and the task is reopened. A problem already resolved at its first projection gets no task. Resolves and returns waiting for one projection are told as one comment. | Skipping a muted problem skipped its recovery too: the task stayed open for the whole mute, and for good if the mute outlasted the close sweep. And with nothing reading completions back, three `waiting_human` problems in prod sat open for days behind completed tasks — one of them (2140a366) closed by the hub itself, whose reopen read a stale mirror and never reached Todoist. The status block is still never read back; only the task's completion is. |

Added 2026-09-11 from the #478 audit:

| Spec said | Shipped | Why |
|---|---|---|
| §3: a Sentry problem is keyed on its issue (`sentry_4711:service:koyracloud-api`) | Keyed on the exception type per project: `{metadata.type}:service:{project slug}`, falling back to `sentry-{issue id}` only when Sentry reports no type (`event_from_alert`) | Chosen so the stack-frame variants Sentry files as separate issues meet on one problem (`test_sentry_webhook_and_poll_share_a_key`). The cost is the other direction: two unrelated errors of one exception type in one project — two different `KeyError`s — are one problem, and the second attaches to the first with no investigation of its own. Not re-keyed here: changing the key would move every live Sentry problem onto a new one mid-flight. |
| §1: `problem_links.link_kind` is `todoist_task`, `github_issue`, `github_pr`, `workflow_run`, `interaction`, `work_session` or `problem` | Four kinds are written: `todoist_task`, `github_pr`, `problem` (rollover and merge), and `plan_step` (§6, 5b). `github_issue`, `workflow_run`, `interaction` and `work_session` have no writer. | `link_kind` is free text, so there is no vocabulary to trim; the list lives only in migration 030's comment, left as shipped. A run is on the timeline as its `investigation` event, a card is its `interactions` row, and a session is found by task — none needed a link as well. |
| §1, §7: `work_sessions.problem_id` ties a session to its problem, and a merge moves sessions to the kept problem | The column is written — at session create, by `report_progress`, and re-pointed by `merge_problems` — and read by nothing. Every session lookup is by task (`work_sessions.list_for_task`), including the Problems page's and `task_context`'s. | Kept as written: a per-problem session list would key on it, and it costs one column. But a merge moves no session that a reader can see: the merged task's sessions stay on the merged task, which the merge completes. |
| §2: sources include `prometheus`, `grafana`, `github` and `ansible`; kinds include `human_note` | Removed from `hub.SOURCES` and `hub.KINDS` | None had a producer. Grafana and Prometheus alerts reach the hub through Alertmanager's webhook as `alertmanager`; the Ansible role and a deploy job write `service_state`, not events; nothing writes a human note. `POST /api/hub/events` now answers 400 to them — a source comes back with the code that sends it. `github` came back that way in #502 (2026-09-12): the GitHub webhook writes the close of a fix PR on its problem (`hub_fix.record_pr_closed`). |
| §2: `POST /api/hub/events` lets a deploy job, a boot script or a test report | Shipped, and nothing calls it. It records the event and returns the hub's decision (`investigate`) but acts on none of it: no investigation starts and nothing is projected inline; the sweep projects within five minutes. | An event from outside carries no alert dict for an investigation to work from, and a route that is open while its secret is blank must not be able to start a billed investigation. The alert webhook is the route that acts on `investigate`. |

One correction to §12 itself: the 4a row says "migration 032 drops `alert_mutes`". It is `033_drop_alert_mutes.sql` — 032 was taken by a parallel PR. §1 and the files table already say 033.

### 13. Rollout

1. PR 1 merges; migration auto-applies; nothing changes in prod.
2. PR 2: set `service_state` from the next homelab-gitops deploy and confirm a
   heartbeat occurrence during it is stored `suppressed`.
3. PR 3: on deploy, run the one-time backfill in `scripts/hub_backfill.py`:
   open `#alert` tasks become problems with `class`/`subject` parsed from the
   capture `external_id` where it is a real fingerprint, from the title where
   it is a slug; `alert_dedup_index.occurrence_count` seeds `occurrences`.
   Then verify with the queries in `docs/infrastructure.md`: no open task
   without a problem, no open problem with two tasks, the eight #341
   duplicates merged to one.
4. PR 5a: grant `task_context`, `report_progress` and `merge_problems` to the
   four agents' `metadata.tool_set` (a DB step, as always — the seed only
   applies to an agent with no `tool_set` yet), then open one `@code` task
   from a fresh Claude session and confirm `task_context` shows the previous
   AEGIS turn. The hooks that call `report_progress` automatically are PR 7.
5. After the backfill has run, drop `alert_dedup_index`: it is the last reader
   of that table's recurrence counts. The migration is deliberately NOT in any
   PR above, because migrations apply on Core startup and would therefore run
   BEFORE the backfill on the very deploy that ships them.

The whole ordering, the grant SQL and the verification queries are in
`docs/infrastructure.md` under "Rolling the problem hub out". That is the
runbook to follow; this list is the summary.

### 14. Groups: the same failure on many entities (added 2026-09-08)

Six Postiz posts wedged in one queue produced six problems and six Todoist
tasks. The hub was behaving exactly as specified — `find_stuck_posts` sets the
subject to the individual Postiz post id, so six subjects are six correlation
keys — and the result was still wrong: one stalled worker, six chores.

A **group** is a problem whose subject is the whole class.

| | |
|---|---|
| Identity | `group_key = '{class}:{subject_kind}'` (migration 040), correlation key `'{class}:{subject_kind}:*'`. `*` is not a character `_slug` can produce, so no real subject collides. |
| Absorption | In `ingest_event`: an **occurrence** whose own key has no problem, and whose class has a live group, attaches to the group with `payload.member_subject` naming the entity. Occurrences only — one member recovering says nothing about the group. |
| Recovery | `hub_watch.reconcile_findings` resolves a group only when its watchdog finds **no** member of the class. A group's `*` is never among the findings, so without this it would resolve every tick. |
| Who decides | `HubSweepFlow`: `find_group_candidates` (≥3 live ungrouped problems of one class and kind, seen in 72h) → `judge_group` (one `think()` call, NO_RETRY, purpose `hub_group_judge`) → `apply_group`. A refusal, an unparseable answer or no model wired all mean "leave them separate". |
| Folding | `hub_group.upgrade`: the oldest member becomes the group and keeps its task, history and sessions; the rest go through `merge_problems`, and their tasks are retired with a note pointing at the survivor. One `state_change` event with `action='grouped'` records why; the projector renames the task and comments. |
| Cost control | A verdict is cached in `settings.hub_group_verdicts` for 24h, and re-asked early only if the cluster grew. Most sweeps make no model call. |
| Never | Across classes. On class `manual` — those are hand-written tasks, and folding two would move one task's sessions and PR links onto another. On the count alone. |

This is the one place the hub attaches on a judgement rather than an exact
key, which is why the judgement is fenced in on every side: same class, same
subject kind, a model that has to say yes, and a fold that is reversible
because nothing is deleted.

Shipped and exercised in production on 2026-09-08. The first sweep found both
live clusters and took them apart correctly, which is the behaviour to hold it
to: it folded five `stuck_post` problems ("all posts are stuck in the same
queue … a single queue drainage issue rather than individual post failures")
and refused three `swarmoverlayblackhole` ones ("different hosts …, different
overlay networks …, and different endpoint counts suggest independent network
partitioning issues"). Both judgements together cost $0.0009. The runbook —
reading the groups, forgetting a verdict, unpicking a fold — is
`docs/infrastructure.md` under "Groups: one problem for the same failure on
many things".

## Files touched

| Area | Files |
|---|---|
| Migrations | `migrations/030_problem_hub.sql`, `031_service_state.sql`, `033_drop_alert_mutes.sql`, `034_work_sessions.sql`, `040_problem_groups.sql` (§14) |
| Core | `services/hub.py` (new), `services/hub_group.py` (new, §14), `services/tools/hub.py` (new), `services/chat.py` (four schemas, four registry entries), `services/alert_tasks.py` (folded into hub), `services/task_sessions.py` → `work_sessions.py`, `api/routes/hub.py` (new), `api/routes/webhooks.py` (deploy events, producers), `api/routes/mcp_server.py` (`_UNSERVED_TOOLS`), `connectors/remote_script.py` (`account` returned from launch) |
| Worker | `flows/alert_investigation.py`, `flows/infra_heartbeat.py`, `flows/flow_health.py`, `flows/delivery_watchdog.py`, `flows/service_drift.py`, `flows/cert_radar.py`, `flows/expiry_radar.py`, `flows/social_metrics.py`, `flows/llm_spend_guard.py`, `flows/github_alert.py`, `flows/sentry_poll.py`, `flows/clarify.py`, `flows/agent_task.py`, `flows/cleanup.py`, `activities/alerts.py`, `activities/homelab.py`, `activities/flow_health.py`, `activities/social.py`, `activities/agent_task.py`, `activities/cleanup.py`, `activities/briefing.py`; `activework/` deleted |
| Admin | `admin-panel/frontend/src/pages/Problems.tsx` (new), `Overview.tsx` (count) |
| Seed | `config/seed/activities.yaml` (`collapse_window`, `reopen_window`, per-class `verify_seconds` and thresholds on the hub sweep row) |
| Scripts | `scripts/hub_backfill.py` |
| Docs | `docs/architecture/overview.md`, `docs/how-it-works.md`, `docs/infrastructure.md`, `CLAUDE.md` (hub conventions) |
| External | homelab-gitops `ansible/roles/*/tasks` deploy hook; operator dotfiles hooks |

## Issues this closes or absorbs

- #341 (comms-down duplicates): one problem, occurrences, one task.
- #355 (active-work guard): replaced by `service_state`.
- #279 remainder: resolve closes through the link, never through a title.
- #302 coding-lane part: `work_sessions` rows drive the worktree sweep.
- #344 in part: a `#chat` task addressed to an agent becomes a `manual`
  problem with a session registry, so "parked unworked" is visible.
- The unimplemented halves of the 2026-08-28 inventory spec (its "no inventory
  table" decision is reversed here, with the reason in §7) and the 2026-09-03
  task-sessions spec (account, park reason, collision).
- #317 gets its first real shared module; #316 loses two of the files that
  make `chat.py` hard to move.
