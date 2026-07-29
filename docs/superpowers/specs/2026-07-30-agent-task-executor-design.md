# Agent task executor — design

**Status:** approved design, not yet implemented
**Issue:** [#151](https://github.com/hikmahtech/aegis/issues/151)
**Date:** 2026-07-30

## Problem

Delegating a task to an agent does nothing. Assignee labels are written by clarify and read by
nobody that can act on them.

Measured in prod on 2026-07-29:

| Signal | Count |
|---|---|
| Open `@pandora` tasks | 43 |
| Open `@sebas` tasks | 30 |
| Open `@maou` tasks | 7 |
| …of those, living outside Inbox | **1** |
| Open `@code` tasks | 53 |
| `resources` rows carrying a `github_repo` | 55 |

Two causes:

1. **Clarify is hard-scoped to Inbox.** `find_unclassified_items` selects
   `WHERE t.project_id = $1` with `inbox_id` (`activities/clarify.py:378`). A task assigned to an
   agent inside a real project — BCP, Aegis, Home Infra — is invisible to the entire system.

2. **No agent holds a do-work tool.** An assignee label can trigger exactly two things:
   `AgentChatReplyFlow` (a reply when the user comments) or `pandora_investigation` (only when the
   title matches a content route). Neither *performs* the task. Reviewing `agents.metadata.tool_set`
   in prod: sebas has `social_timeline`, which is read-only; nobody has a coding-run tool;
   pandora's infra ops are chat-only, and chat sees ~8 messages a month.

Net effect: no agent-assigned task has ever been completed by an agent.

## The insight

This pattern already exists and works — for exactly one verb. `SocialPublishFlow` scans
`todoist_tasks` **by label across all projects** (`find_due_posts`, `activities/social.py:71` — note
it never mentions a project id), spawns an `InteractionFlow` approval card, and on approval posts to
Postiz and closes the task.

So this is not new machinery. It is generalising that flow from `@publish` to `@code`.

## Design

A new scheduled flow, `AgentTaskFlow` (`agent-task-15min`), a structural sibling of
`SocialPublishFlow`.

```
find_actionable_tasks   → open tasks, ANY project:
                            assignee ∈ {@sebas,@raphael,@maou,@pandora}
                          + verb label (v1: @code)
                          + due_date <= today
                          + not @someday / @waiting
                          + no in-flight or completed run for this task
        ↓ per task (cap 2 per tick)
resolve agent            → AgentRegistryActivities.resolve_agents
resolve repo             → project → repo, then title match, then Gate-0 card
        ↓
run 1: investigate       → start_kimi_run, read-only prompt
        ↓
comment plan on task     → "[Pandora] …" + "Workflow run:" footer
        ↓
InteractionFlow card     → "implement this?"    ─ skip → @waiting, stop
        ↓ approve
run 2: implement         → start_kimi_run, fix prompt → branch
        ↓
InteractionFlow card     → "open PR?"           ─ skip → discard branch
        ↓ approve
create_github_pr         → task → @waiting (never auto-completed)
```

### Eligibility

Assignee label **and** verb label **and** `due_date <= today`. The date is the go signal: the 53
existing `@code` tasks are all dateless, so the system ships silent and tasks are opted in one at a
time by giving one a date. `@someday` and `@waiting` are hard exclusions.

Deduplication follows the pandora cooldown precedent: a deterministic workflow id
(`agent-task-<task_id>`) plus a check for any `AgentTaskFlow` run against that task id in
`workflow_runs`. No new watermark column — `last_clarified_at` belongs to clarify and sharing it
would couple the two flows' throttles.

### Verb → executor

v1 handles `@code` only. **`@publish` stays with `SocialPublishFlow`, untouched.** It works;
merging the two flows buys nothing today and risks the one working path.

The mapping is a dict in the activity, not a config table — one entry does not justify a schema.

### Repo resolution

Three tiers, most reliable first:

1. **Todoist project → repo.** The projects already mirror repos: BCP → `Stockopedia/bcp`, Aegis →
   `hikmahtech/aegis`, Home Infra → `hikmahtech/homelab-gitops`, DrWho → `hikmahtech/drwhome`. A
   stored map, seeded from the obvious pairs and editable.
2. **Title/description matching**, reusing the existing tiers inside `resolve_alert_resource` by
   synthesising an alert-shaped dict (`title`=content, `description`=description,
   `fingerprint`=`task:<id>`). The KG tier will miss on a synthetic fingerprint and fall through to
   the service-match, deterministic, and LLM tiers, which is the intent.
3. **Gate-0 repo-confirm card**, the shape that already exists for alerts
   (`_build_repo_confirm_prompt`), including its numbered candidate menu.

A repo that resolves to nothing is a hard stop with a comment on the task — never a guess.

### Safety model

Investigation is free; anything that leaves the box needs a card.

| Action | Gate |
|---|---|
| Read repo, run the coding CLI in investigate mode | none |
| Comment findings/plan on the task | none |
| Implement (write code, commit to a branch) | card |
| Open a PR | card |
| Post to Postiz, restart a service | card |

This mirrors `AlertInvestigationFlow`'s Gate-2 and reuses `InteractionFlow`'s
`post_resolve_activity` hook exactly as `SocialPublishFlow` does.

### What "done" means

A coding task terminates at **`@waiting`**, never auto-completed — the PR still needs human review,
and only the user closes the task. This deliberately avoids the `SocialPublishFlow` outcome where 0
of 6743 originating tasks were ever closed: `@waiting` is an honest terminal state, whereas silently
leaving a task open is how that gap went unnoticed for months.

## Three things to get right

- **Do not apply the active-work guard.** It exists to detect "a human is already working on this
  repo", and it keys on due/overdue Todoist tasks naming the repo — which is precisely the task being
  executed. It would suppress every run. (See the 2026-07-29 fix round: an overdue task latched that
  guard open and silently killed every homelab-gitops investigation.)
- **Every agent comment needs the `Workflow run:` footer.** Clarify's eligibility filter excludes
  AEGIS-authored notes by matching `[ClarifyFlow @`, `[Agent reply @`, and `%Workflow run:%`. A
  comment without one of those markers reads as fresh user input and re-spawns the flow every 15
  minutes. This loop has been shipped and fixed twice in this codebase (2026-05-21, 2026-05-27).
- **Cap concurrency at 2 coding runs per tick.** The tmux window cap is 10 and kimi runs take
  minutes; an uncapped fan-out over even 10 due tasks would wedge the coding host.

## Reuse inventory

Nothing here is invented:

| Need | Existing component |
|---|---|
| Approval cards | `InteractionFlow` + `post_resolve_activity` |
| Coding runs | `RemoteScriptConnector.start_kimi_run`, engine routed by org |
| Reading run output | `fetch_kimi_run_output` + `_extract_kimi_transcript` (fixed in #150) |
| Agent resolution | `AgentRegistryActivities.resolve_agents` (by capability tag) |
| PR creation | `create_github_pr` / `StagePendingPrInput` |
| Repo candidates | `resolve_alert_resource` tiers + `_build_repo_confirm_prompt` |
| Task comments | `AlertActivities.post_task_note` |
| Scheduling | `schedule_sync` + `_ACTIVITY_TYPE_MAP` + `config/seed/activities.yaml` |

## Registration checklist

Per the conventions in CLAUDE.md, a new scheduled flow needs all of:

1. `@workflow.defn` flow + activities.
2. Both registered in `worker/__main__.py` — **two separate lists**, nothing is auto-discovered
   (a past regression came from updating only one).
3. `_ACTIVITY_TYPE_MAP` entry in `schedule_sync.py`, keyed by the PascalCase class name.
4. Seed row in `config/seed/activities.yaml`.
5. `agent_id` as the **first** field of the config dataclass, so
   `WorkflowRunRecorderInterceptor` can attribute runs.

## Testing

- `find_actionable_tasks` against a real DB: dateless task ignored; `@someday` ignored; `@waiting`
  ignored; due + assignee + verb selected; task in a non-Inbox project selected (the regression that
  motivates the whole feature); already-run task not re-selected.
- Repo resolution: project map hit; fallback to title match; unresolved → card, never a guess.
- Flow-level with `WorkflowEnvironment.start_time_skipping()`: skip at the plan card stops before any
  implement run; approve → implement → PR card → `@waiting`.
- Concurrency cap: 10 due tasks yield 2 runs.
- One test asserting every agent comment carries the `Workflow run:` footer — the loop guard.

## Deliberately out of scope

- Folding `@publish` into this flow.
- Infra-op verbs (`restart service`, etc.) — pandora has those as chat tools; wire them here only
  once `@code` is proven.
- Auto-merging PRs.
- Making clarify itself project-aware. This flow reads all projects directly; widening clarify's
  Inbox scope is a separate change with its own blast radius (it would put every project's tasks
  through the LLM classifier).
