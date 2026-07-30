# Agent task executor — design

**Status:** approved design, not yet implemented
**Issue:** [#151](https://github.com/hikmahtech/aegis/issues/151)
**Date:** 2026-07-30

## Problem

Delegating a task to an agent does nothing. Assignee labels are written by clarify and read by
nobody that can act on them.

The framing this started from — "the user delegates work, agents should execute it" — turned out to
be wrong. Prod data, 2026-07-29:

| Assignee | `source_tag` | Count | With a due date | What they are |
|---|---|---|---|---|
| `@sebas` | `#email` | 30/30 | 0 | Inbox mail clarify called a 2-min action |
| `@pandora` | `#alert` | 40/43 | 0 | "PROLONGED: clickhouse_clickhouse degraded for over 2 hours", "Loki is down" |
| `@maou` | `#receipt` | 7/7 | 0 | "Anomaly: ? Eleven Labs", "Renewal in 19.6 days: MSEDCL" |

**There is not one user-authored delegation.** All 80 agent-assigned tasks are AEGIS's own triage
output, and the oldest has been sitting since 2026-07-01. So the real defect is that *AEGIS delegates
to itself and then drops it on the floor.*

Two structural causes:

1. **Clarify is hard-scoped to Inbox** — `find_unclassified_items` selects `WHERE t.project_id = $1`
   with `inbox_id` (`activities/clarify.py:378`).
2. **No agent holds a do-work tool.** An assignee label can only trigger `AgentChatReplyFlow` (a
   reply when the user comments) or `pandora_investigation` (content-route titles only). Reviewing
   `agents.metadata.tool_set` in prod: sebas has read-only `social_timeline`; nobody has a coding-run
   tool; pandora's infra ops are chat-only and chat sees ~8 messages a month.

Two of these groups are live breakage rather than missing features: the 30 "PROLONGED: X degraded"
tasks mean services have been degraded for weeks with nobody told, and 30 `#email` tasks that are
mostly pure notifications means clarify is misclassifying junk as actionable (issue #117).

## The insight

This pattern already exists and works — for exactly one verb. `SocialPublishFlow` scans
`todoist_tasks` **by label across all projects** (`find_due_posts`, `activities/social.py:71`, which
never mentions a project id), spawns an `InteractionFlow` approval card, and on approval posts to
Postiz and closes the task.

So this is not new machinery. It is generalising that flow, with the verb taken from `source_tag`.

## Design

Two flows, mirroring the `SentryPollFlow` → `AlertInvestigationFlow` split:

- **`AgentTaskSweepFlow`** — scheduled (`agent-task-15min`). Finds eligible tasks and spawns children
  with `ParentClosePolicy.ABANDON`. Never awaits them: a child can sit on an approval card for days,
  and awaiting it would let one unanswered card freeze every subsequent tick (the exact failure that
  caused 511 skipped Sentry polls over 41h in 2026-05-29).
- **`AgentTaskFlow`** — one run per task. Config dataclass is `(agent_id, todoist_task_id, …)`:
  `agent_id` first per convention, and `todoist_task_id` because
  `interceptors._extract_todoist_task_ref` reads exactly that attribute to populate
  `workflow_runs.todoist_task_ref` — which is what makes the cooldown query below cheap.

### Verb resolution

| Selector | Verb | Executor |
|---|---|---|
| `source_tag = '#alert'` | infra | recheck live state → investigate → card → restart |
| `source_tag = '#receipt'` | finance | gather context → decision card |
| `source_tag = '#email'` | email triage | classify → archive/trash, or `@waiting` |
| `source_tag IS NULL` + `@code` | coding | two-phase kimi run |

**`source_tag` is primary; `@code` is consulted only when `source_tag` is NULL.** This matters: one
`#email` task already carries a stray `@code` label from clarify, and treating that as "run a coding
agent on this email" would be nonsense. A NULL `source_tag` means the task is user-authored, which is
where an explicit `@code` label is a genuine instruction.

A verb with no executor is skipped with a log line, never guessed at.

### Eligibility and the backlog brake

Open task, any project, with an assignee label in the addressable set, **no due-date requirement** —
none of the 80 has one, so requiring a date would keep the executor permanently idle.

Excluded: `@someday`, `@waiting`, completed.

Brake, because the standing backlog is 80 tasks:

- **3 tasks per tick**, oldest `updated_at` first.
- **6h cooldown per task**, via
  `SELECT max(started_at) FROM workflow_runs WHERE workflow_type='AgentTaskFlow' AND todoist_task_ref = $1`.
  No migration and no new watermark column — `last_clarified_at` belongs to clarify and sharing it
  would couple the two flows' throttles.
- **At most 1 coding run per tick** within that 3. Kimi runs take minutes and the tmux window cap is
  10.

The backlog drains over roughly two days, so the first results arrive within minutes and can be
judged before the executor has touched everything.

### Terminal states

This is the load-bearing part. `@waiting` is the parking state, and because eligibility excludes
`@waiting`, reaching it removes the task from the pool — that is what stops the 6h cooldown
re-picking the same task forever.

| Verb | Outcome | End state |
|---|---|---|
| infra | service healthy now | **complete** |
| infra | restarted and recovered | **complete** |
| infra | restart declined, or still broken after restart | `@waiting` |
| finance | decision applied via card | **complete** |
| finance | needs a human | `@waiting` |
| email | notification/junk → trashed or archived | **complete** |
| email | genuinely needs a reply | `@waiting` |
| coding | PR opened | `@waiting` |
| coding | declined at the plan card | `@waiting` |

Never auto-complete a task whose work a human still has to finish. This deliberately avoids the
`SocialPublishFlow` outcome where 0 of 6743 originating tasks were ever closed: an explicit
`@waiting` is honest, whereas silently leaving a task open is how that gap went unnoticed for months.

### Safety model

Investigation is free; anything that leaves the box needs an `InteractionFlow` card, reusing the
`post_resolve_activity` hook exactly as `SocialPublishFlow` does.

| Action | Gate |
|---|---|
| Read state — service logs, repo, prior charges, email metadata | none |
| Comment findings on the task | none |
| Archive/trash an email already classified as a notification | none |
| Restart a service | card |
| Implement code / open a PR | card |
| Apply a finance decision | card |
| Post to Postiz | card |

## Per-verb detail

### `#alert` → infra (40 tasks, the largest group)

**Check current reality, do not replay alert history.** Only 12 of 42 open `#alert` tasks have an
`alert_dedup_index` row, so signature recovery covers 29% of them — and the 30 that lack one are
precisely the PROLONGED bulk. But every one of those titles names a swarm service
(`PROLONGED: clickhouse_clickhouse degraded…`, `Loki is down`), so the robust move is to ask Docker
whether the service is healthy *now*, which works for all 42 and is what a human would do.

1. Extract the service name from the title; fall back to `alert_dedup_index.signature` when present.
2. Healthy now → comment + **complete**. (Expect this to close a large fraction of the 30 immediately —
   four-week-old degradation alerts for services that have long since recovered.)
3. Still unhealthy → `get_service_logs` + `inspect_service`, comment findings, card `restart X?`.
4. Approved → `restart_service`, wait, re-check. Recovered → complete; still broken → `@waiting`.

### `#receipt` → finance (7 tasks)

These are questions ("Anomaly: ? Eleven Labs"), not work — a human decides whether a charge is
legitimate. So: gather context (prior charges for that merchant, the amount's history) and put a
decision card up. No autonomous action; the value is the assembled context, not the decision.

### `#email` → sebas (30 tasks)

**Known ceiling: the OAuth scope list is `gmail.modify`, `calendar.readonly`, `drive.readonly`
(`routes/gmail_reauth.py:32`).** `gmail.modify` permits archive, trash, and label changes but
**not sending**. Adding `gmail.send` would force a re-consent round for every account, so v1 is
triage-only:

1. Classify notification vs real action, reusing `_looks_like_notification` and the
   `_NOTIFICATION_MARKERS` list that already exists in `activities/clarify.py`.
2. Notification → archive/trash → **complete**. Most of the 30 are this.
3. Real action → comment why, `@waiting` for the user.

Drafting and sending replies is deliberately out of scope until the scope question is decided
separately.

### `@code` → coding (user-authored tasks only)

Two-phase, so a misread task or wrong repo costs nothing:

```
run 1: investigate (read-only)  → comment plan + findings
         ↓
       CARD "implement this?"   ─ skip → @waiting
         ↓ approve
run 2: implement                → branch
         ↓
       CARD "open PR?"          ─ skip → discard branch
         ↓ approve
create_github_pr                → @waiting
```

Repo resolution, most reliable first:

1. **Todoist project → repo.** The projects already mirror repos: BCP → `Stockopedia/bcp`, Aegis →
   `hikmahtech/aegis`, Home Infra → `hikmahtech/homelab-gitops`, DrWho → `hikmahtech/drwhome`. A
   stored, editable map.
2. **Title/description matching**, reusing the tiers inside `resolve_alert_resource` via a synthetic
   alert-shaped dict (`title`=content, `fingerprint`=`task:<id>`). The KG tier misses on a synthetic
   fingerprint and falls through to service-match/deterministic/LLM, which is the intent.
3. **Gate-0 repo-confirm card**, the shape that already exists (`_build_repo_confirm_prompt`),
   numbered candidate menu included.

Unresolved repo is a hard stop with a comment — never a guess.

## Three things to get right

- **Do not apply the active-work guard.** It detects "a human is already working on this repo" by
  looking for due/overdue Todoist tasks naming the repo — which is the task being executed. It would
  suppress every run. (2026-07-29: an overdue task latched that guard open and silently killed every
  homelab-gitops investigation for days.)
- **Every agent comment needs the `Workflow run:` footer.** Clarify's eligibility filter excludes
  AEGIS-authored notes by matching `[ClarifyFlow @`, `[Agent reply @`, and `%Workflow run:%`. A
  comment without one of those markers reads as fresh user input and re-spawns the flow every 15
  minutes. This loop has shipped and been fixed twice (2026-05-21, 2026-05-27).
- **`@waiting` must be applied reliably**, or the cooldown turns into an infinite slow loop over the
  same tasks. Any exit path that isn't "complete" must set it.

## Reuse inventory

Nothing here is invented:

| Need | Existing component |
|---|---|
| Approval cards | `InteractionFlow` + `post_resolve_activity` |
| Infra ops | `inspect_service`, `get_service_logs`, `restart_service` (chat tools, `services/chat.py`) |
| Alert resolution check | `AlertActivities.check_alert_resolved` (`alerts.py:1800`) |
| Coding runs | `RemoteScriptConnector.start_kimi_run`, engine routed by org |
| Reading run output | `fetch_kimi_run_output` + `_extract_kimi_transcript` (fixed in #150) |
| Notification detection | `_looks_like_notification` + `_NOTIFICATION_MARKERS` (`activities/clarify.py`) |
| Agent resolution | `AgentRegistryActivities.resolve_agents` (by capability tag, never a literal id) |
| PR creation | `create_github_pr` / `StagePendingPrInput` |
| Repo candidates | `resolve_alert_resource` tiers + `_build_repo_confirm_prompt` |
| Task comments | `AlertActivities.post_task_note` |
| Cooldown key | `workflow_runs.todoist_task_ref`, auto-populated by `interceptors._extract_todoist_task_ref` |

## Registration checklist

Per CLAUDE.md, both new flows need all of:

1. `@workflow.defn` flows + activities.
2. Registered in `worker/__main__.py` — **two separate lists**, nothing is auto-discovered. A past
   prod boot regression came from updating only one.
3. `_ACTIVITY_TYPE_MAP` entry in `schedule_sync.py`, keyed by the PascalCase class name (sweep only).
4. Seed row in `config/seed/activities.yaml` (sweep only).
5. `agent_id` as the **first** config field, so `WorkflowRunRecorderInterceptor` attributes runs.
6. New chat tools, if any, need a **DB write** to `agents.metadata.tool_set` after merge — seed yaml
   and `AGENT_TOOL_SETS` are overridden by the DB at runtime.

## Testing

- `find_actionable_tasks` against a real DB: non-Inbox project selected (the regression motivating
  the feature); `@someday`/`@waiting`/completed excluded; dateless task **selected** (the fix to the
  first draft of this design); cap of 3 respected with 10 eligible; a task run 1h ago excluded and
  one run 7h ago included; at most 1 coding task per batch.
- Verb resolution: each `source_tag` maps to its executor; `#email` + stray `@code` routes to email,
  not coding; NULL `source_tag` + `@code` routes to coding; unknown verb skipped not guessed.
- Service-name extraction from the real prod title shapes (`PROLONGED: x_y degraded for over 2
  hours`, `Loki is down`, `PostgreSQL is down`).
- Terminal states: every non-complete exit path sets `@waiting` — table-driven, one case per row
  above. This is the anti-infinite-loop test.
- Flow-level with `WorkflowEnvironment.start_time_skipping()`: healthy service short-circuits to
  complete without a card; declined restart card → `@waiting`; declined plan card stops before any
  implement run.
- One test asserting every agent comment carries the `Workflow run:` footer — the loop guard.

## Deliberately out of scope

- **Sending email replies** — needs a `gmail.send` scope and a re-consent round per account.
- Folding `@publish` / `SocialPublishFlow` into this flow. It works; merging risks the one working
  path for no gain.
- Auto-merging PRs.
- Making clarify project-aware. This flow reads all projects directly; widening clarify's Inbox scope
  would put every project's tasks through the LLM classifier — separate change, separate blast radius.
- **Fixing the upstream causes.** Two are now visible and both deserve their own work: alerts that
  never re-check after firing (so PROLONGED tasks pile up), and clarify classifying notifications as
  actionable (#117). This executor drains the symptom; it does not stop the tap.
