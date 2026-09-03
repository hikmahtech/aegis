# Task sessions: comment-driven coding work

**Date:** 2026-09-03
**Status:** approved design, not yet implemented

## Problem

The coding lane does not work. In the 21 days to 2026-09-03 the `coding` verb of
`AgentTaskFlow` ran zero times, `AgentRunFlow` has not run since its E2E tests on
2026-08-14, and the three open `@code` tasks are all parked under `@waiting`. Two
of them carry `#alert`, so they resolve to the infra verb and never reach the
coding lane at all.

When it does run it achieves little. Five gaps, all structural:

1. **Conversation and execution are disconnected.** A comment on a task goes
   webhook → `ClarifyFlow` → `AgentChatReplyFlow` → the core chat loop, which has
   no code access. The coding run is a headless one-shot that never sees a
   comment after launch. Nothing can steer, answer or stop it.
2. **No memory between turns.** Investigate and implement are separate fresh
   processes. Only the plan text crosses. The prompt is the title plus the
   description, with nothing from the comment thread.
3. **The busy gate checks the wrong unit.** `busy_human_sessions` reads
   `status == busy` for any of the operator's sessions in the repo at the instant
   of launch. Idle between prompts means no block; busy on unrelated work means
   block. AEGIS already runs in its own worktree, so files never collide. What
   collides is two executors on the same *task*.
4. **Slack has no task threads.** Chat threads are keyed by channel and agent. A
   Slack reply cannot be tied to a task.
5. **The operator's own Claude Code sessions cannot report in.** They hold the
   operator MCP mount but have no tool to write to a task's thread, so AEGIS
   cannot know what the operator is doing.

The Todoist webhook is live in prod (295 `note:added` events in 7 days, about 1s
latency), and Claude Code 2.1.259 accepts `--session-id <uuid>` and
`--resume <uuid>` in print mode. A spike on 2026-09-03 confirmed that a
`claude -p --session-id X -n NAME` run appears in `claude agents --json` under
that id and name while it runs, and that `claude -p --resume X` continues it
under the same id. `--bg` conflicts with `--print` and is not used.

## Decisions taken during brainstorming

| Decision | Choice | Consequence |
|---|---|---|
| Collision policy | Task-level ownership; hand to the operator only when their live session is already on that same task | The per-repo gate goes; a same-task check and an explicit override replace it |
| Engine | Claude for every repo | Sessions resume, MCP tools mount, takeover works everywhere; kimi is one-shot only, via `dispatch_agent_run` |
| Autonomy | Plan, then wait | The comment thread is the control channel; no Slack cards |
| Session primitive | `claude -p` with `--session-id` / `--resume` | Reuses the existing launch, poll and transcript code; `claude --bg` rejected |
| Comment trigger | Todoist webhook fast path, 15-min sweep fallback | Independent of `ClarifyFlow`, which only scans the Inbox project |

## Non-goals

- Text mentions such as "@pandora do this" on an `@me` task. Relabel in Todoist
  or use `handoff_task`.
- A per-task `--model` knob.
- Auto-completing a task when its PR merges. The operator completes coding tasks.
- Changing the infra, email or finance verbs of `AgentTaskFlow`.
- Kimi sessions. Kimi keeps its one-shot path through `dispatch_agent_run`.
- Pushing a comment into a session the operator has open interactively. There is
  no documented interface for that; the comment waits for them.

## Design

### 1. Unit of work

A Todoist task with an agent assignee label and `@code`, exactly today's coding
selector (`source_tag IS NULL AND '@code' = ANY(labels)`). Its comment thread is
the shared log for every executor: AEGIS turns, the operator's own sessions, and
the chat loop.

### 2. Task session

One new table, migration `025_task_sessions.sql`:

```sql
CREATE TABLE IF NOT EXISTS task_sessions (
    task_id       text PRIMARY KEY,
    agent_id      text NOT NULL,
    session_id    uuid NOT NULL,
    repo          text NOT NULL DEFAULT '',      -- workspace-relative checkout path; '' = repo not yet resolved
    github_repo   text NOT NULL DEFAULT '',
    worktree_path text NOT NULL DEFAULT '',
    branch        text NOT NULL DEFAULT '',
    host          text NOT NULL DEFAULT '',
    slack_ref     jsonb,                         -- {"channel","ts"} thread root; NULL until first delivery
    turns         int  NOT NULL DEFAULT 0,
    last_turn_at  timestamptz,
    created_at    timestamptz NOT NULL DEFAULT now()
);
```

- `session_id` is a `uuid4` minted when the row is created and stored. It is not
  derived from the task id: a task whose session was cleaned up and later
  restarted must get a fresh session, and Claude Code keys transcripts on the
  session id.
- `worktree_path` is `<repo_path>-aegis-wt/task-<task_id>`, a sibling of the
  shared checkout like today's per-run worktrees, but stable per task.
- `branch` is `aegis-task/<task_id>`, as today. The worktree is created on it:
  `git worktree add -b <branch> <path>` from the freshly pulled default branch; if
  the branch already exists, `git worktree add <path> <branch>` checks it out
  instead of resetting it.
- The row is created by a new activity `ensure_task_session(task_id, agent_id,
  task, comment)` on the first turn. It resolves the repo with the existing
  `resolve_task_repo`, then creates the worktree through a new connector method
  `RemoteScriptConnector.ensure_task_worktree(repo, worktree_path, branch)`,
  which pulls the shared checkout, adds the worktree on the branch, and copies
  the SKILL.md runbooks in with the existing `_skills_copy_fragment`. It is
  idempotent: an existing worktree is left alone.
- If `resolve_task_repo` returns candidates instead of a repo, the row is still
  created, with `repo`, `worktree_path` and `branch` empty, so later comments
  reach the flow. The flow posts a comment listing the candidates and parks. On
  the next turn `ensure_task_session` re-runs the resolver and matches
  `comment` against the candidate list (exact, case-insensitive); a match fills
  the row in and creates the worktree, no match repeats the question. No Gate-0
  card.

### 3. A turn

A turn is one `claude -p` run in the task's worktree with the owning agent's
AEGIS tools mounted over MCP, ungated (`--dangerously-skip-permissions`) as the
coding lane runs today. The worktree isolates file writes, the branch isolates
commits, and the run mount already withholds `dispatch_agent_run`,
`stop_agent_run` and the new `comment_on_task`.

Launch flags, added to `_agent_launch_flags` for the claude engine only:

- first turn: `--session-id <session_id> -n "task <task_id>: <title[:60]>"`
- later turns: `--resume <session_id>`

`start_kimi_run` gains `session_id`, `resume`, `name` and `worktree_path`
parameters and passes them through. When `worktree_path` is given, the pull and
worktree-add phases are skipped: the caller owns the worktree. A new activity
`launch_task_turn(session, prompt, agent_id, resume)` in
`activities/agent_task.py` calls it; `AgentRunActivities.launch_agent_run` and
its repo-level busy gate are not touched, so `dispatch_agent_run` behaves as
today.

**Prompt, first turn:**

```
You are working Todoist task {task_id}: {title}

{description}

Comment thread so far (oldest first; AEGIS's own notes carry a `Workflow run:` footer):
{thread}

This is your first turn on this task. Investigate only: read the code, do NOT
modify files, commit, or create branches. Report:
1. What the task is actually asking for.
2. Which files would need to change.
3. A short implementation plan.
4. Anything ambiguous or risky, as questions for the user.

You are in a per-task worktree on branch `{branch}`. Later turns implement here
when the user says so. End your final message with exactly one of:
STATUS: plan
STATUS: question: <what you need from the user>
STATUS: unactionable: <why>
```

**Prompt, later turns** (queued comments are joined with blank lines):

```
The user replied on Todoist task {task_id} ({title}):

> {comments}

Act on it. Rules for this session:
- Implement only when the user asks. Commit to branch `{branch}` in this
  worktree, never to the default branch.
- Open a pull request only when the user asks, with `gh pr create --draft`.
- Nobody can answer questions mid-turn; ask them in your final message instead.
End your final message with exactly one of:
STATUS: done
STATUS: waiting: <what you need from the user>
STATUS: pr: <url>
STATUS: unactionable: <why>
```

The `STATUS` line is recorded in `workflow_runs.result_summary` when present and
is not required: a turn ends when the process exits, not when a footer appears.

**Turn end.** The flow polls with the existing `check_agent_run` activity (output
fetch plus `fuser` liveness, first poll without the liveness probe) through
`AgentRunFlow`'s poll loop, which is extracted into a module-level helper
`poll_until_exit(...)` in `flows/agent_run.py` and called by both flows.
Per-turn deadline is 60 minutes (`activities.config` key `turn_timeout_minutes`
on the sweep row). On deadline the run is **killed** by a new activity
`kill_task_turn(output_file, host)` (`fuser -k` on the output file; the tmux
window is left for inspection) and the tail is posted. An orphan run continuing
to write the same session while the next turn starts is worse than a lost turn.

**Turn output.** The final assistant message is the `result` field of the last
`{"type": "result"}` stream-json event; fall back to the tail of
`_extract_kimi_transcript` when that event is missing. It is posted as a task
comment through the existing `comment` activity, whose `Workflow run:` footer
already marks it AEGIS-authored, with two extra footer lines:

```
Session: <session_id>  ·  turn <n>
Take over: cd <worktree_path> && claude --resume <session_id>
```

The same text goes to the task's Slack thread (section 7). Then `turns` and
`last_turn_at` are updated and the task is parked under `@waiting` by the existing
`park_task`. AEGIS never completes a coding task.

### 4. Triggers

All three land on the existing workflow id `agent-task-<task_id>` through the
same start-or-signal dance, `dispatch_task_turn(task_id, agent_id, comment)`:

1. try to start `AgentTaskFlow` with that id;
2. on `WorkflowAlreadyStartedError`, signal the running flow with `comment`;
3. if the signal fails because the flow just completed, start again.

It has two flavours with one shape: a client-side one in core for the webhook
(`temporalio.client.Client`, which the handler already opens), and an
in-workflow one in the sweep (`workflow.start_child_workflow` with
`ParentClosePolicy.ABANDON`, then `workflow.get_external_workflow_handle(id).signal`).

Inside the flow, `@workflow.signal def comment(text)` appends to a pending list.
After each turn the flow drains the list; a non-empty list is the next turn's
comments, joined; an empty list means park and exit. So a comment during a
running turn queues instead of colliding, and several comments fold into one
turn.

`AgentTaskFlowInput` gains `comment: str = ""`. When `task` is empty (the webhook
path has no task dict) the flow loads it with a new `load_task(task_id)`
activity before resolving the verb.

The triggers:

- **Sweep, first turn.** `AgentTaskSweepFlow` (every 15 minutes) keeps using
  `find_actionable_tasks`. An eligible `@code` task with no `task_sessions` row
  gets turn 1. `max_coding` default goes from 1 to 3 and counts new and resumed
  turns together.
- **Webhook, later turns.** In `routes/webhooks.py::todoist_webhook`, on
  `note:added` / `note:updated` whose `item_id` has a `task_sessions` row and
  whose content is not AEGIS-authored (the same three filters ClarifyFlow's
  `latest_user_note` uses: `CLARIFY_NOTE_PREFIX`, `AGENT_REPLY_PREFIX`,
  `Workflow run:`), call `dispatch_task_turn` through the Temporal client the
  handler already opens for ClarifyFlow. Best-effort, logged on failure.
- **Sweep, later turns (fallback).** A new activity `find_task_turns_due()`
  returns tasks with a session row whose user-authored notes were posted
  after `COALESCE(last_turn_at, created_at)`, with those notes joined
  oldest-first. The sweep
  dispatches each through `dispatch_task_turn`. This catches a missed webhook
  and also serves comments posted by `comment_on_task` when the webhook is
  down. The 6-hour cooldown does not apply to these; the watermark is
  `last_turn_at`.
- **Slack thread reply.** Section 7 turns it into a Todoist comment, which then
  takes the webhook path.

`ClarifyFlow`'s eligibility query gains `AND NOT EXISTS (SELECT 1 FROM
task_sessions ts WHERE ts.task_id = t.id)` so an Inbox task with a session does
not also spawn `AgentChatReplyFlow` on the same comment.

### 5. Collision rules

A new activity `check_task_collision(task_id, repo, session_id, override)`
runs before every turn. It returns `{"verdict", "session", "reason"}` with
verdict one of `proceed`, `you_are_in_it`, `hand_to_you`. Every failure path
returns `proceed`: a broken inventory must not stop the lane.

1. **`you_are_in_it`.** The inventory (`RemoteScriptConnector.list_coding_sessions`,
   both accounts) contains a record whose `session_id` equals ours. The operator
   resumed this session. The flow posts to the Slack thread "You are in this
   task's session (<name>); your comment is waiting for you there", does not
   comment on Todoist, and exits without changing labels.
2. **`hand_to_you`.** Otherwise, for every human-owned record in the same repo
   (any status, not only busy), one SSH round trip collects, per session:
   `git -C <cwd> branch --show-current`, `git log -3 --oneline`,
   `git status --short | head -20`. One `balanced`-tier LLM call receives the
   task title and description plus those records and answers JSON
   `{"same_task": bool, "session_name": str, "reason": str}`. `same_task`
   true means hand to you: the flow comments "You look to be on this already in
   session '<name>' on branch <branch>. I'll stay out. Reply `take over` when you
   want me to proceed." and parks.
3. **`proceed`.** Anything else. When human sessions exist in the repo but are
   judged unrelated, the turn's comment is prefixed with "FYI: you have a live
   session in this repo ('<name>'); I'm working in my own worktree at
   <worktree_path>."
4. **Override.** A comment whose text starts with `take over` (case-insensitive)
   sets `override=True`, which skips rule 2 for that turn only. Rule 1 still
   applies.
5. **Explicit claim.** `handoff_task(task_id, "@me")` from any session. AEGIS
   never touches `@me` tasks (unchanged). Hand back by relabelling and commenting.

Every verdict bumps `last_turn_at`, including `you_are_in_it` and
`hand_to_you`: the comment has been handed to the operator, and the sweep
fallback must not re-dispatch it every 15 minutes.

The old repo-level `busy_human_sessions` call in `run_task_investigation` is
deleted with that activity. `launch_agent_run` keeps its own call for
`dispatch_agent_run` runs, unchanged.

The LLM call goes through `LLMClient.think` on the `balanced` tier from the
tier map (never `settings.model_*`), asking for a JSON object; the reasoning
floor applies automatically for kimi-class models.

### 6. The operator's side

One new chat tool, `comment_on_task(task_id, text)`, in
`services/tools/gtd.py` with its schema in `CHAT_TOOLS` and its entry in
`TOOL_EXECUTORS` (both still hand-written in `services/chat.py`). It posts `text`
as a plain Todoist note through `TodoistConnector.note_add`, with no AEGIS prefix
or footer, so it counts as user-authored and triggers a turn. It is added to
`_UNSERVED_TOOLS` in `routes/mcp_server.py`: served on the operator mount and in
chat, never inside a run, otherwise a run commenting on its own task would loop.
Granting it to agents is a DB step at rollout (`metadata.tool_set`).

Everything else the operator needs exists: `handoff_task`, `list_coding_sessions`,
`whats_next`, and `claude --resume <session_id>` inside the worktree to take a
task over. While that interactive session is open, rule 1 keeps AEGIS out.

### 7. Slack

Task messages go to the owning agent's channel as one thread per task.

**Outbound.** New activity `deliver_task_message(task_id, agent_id, text)`:
read `task_sessions.slack_ref`; POST comms `/api/deliver/message` with a new
optional `thread_ref` field; comms posts with `thread_ts` when given (the adapter
already has `post_thread`; this path also returns the sent message's ref) and
without it otherwise; the response carries `ref`. When there was no root, the
first message is the root and its ref is saved on the session row. The root
message text is "Task <task_id>: <title>" followed by the first turn's comment.

**Inbound.** `adapters/slack.py::_on_message` passes `thread_ts` through to
`SlackInbound.on_message`. When `thread_ts` is present and differs from `ts`,
inbound calls a new core route `GET /api/admin/task-sessions/by-thread?channel=&ts=`.
A hit posts the text to `POST /api/admin/tasks/{task_id}/comment`, which writes a
plain Todoist note through `TodoistConnector.note_add`, and stops. The webhook
then starts the turn and the reply lands back in the thread. A miss falls through
to today's routing. Bot messages are already ignored, so AEGIS's own thread posts
never loop.

### 8. Configuration, not code

- Coding block on the `meem` infra row: `routing.default_engine: "claude"`,
  `engines.claude.default_account: "personal"`. Org routes stay as they are.
- `activities.config` on `agent-task-15min`: `max_coding: 3`,
  `turn_timeout_minutes: 60`.
- Grant `comment_on_task` to the four active agents' `metadata.tool_set`.

### 9. Deleted

- `_investigate_coding_task`, `_run_implementation`, both `InteractionFlow`
  cards, `_confirm_repo_gate0` and `stage_pending_pr` in `flows/agent_task.py`.
- `run_task_investigation`, `run_task_implementation` and `collect_coding_run`
  in `activities/agent_task.py`, and their STATUS-footer polling.
- The repo-level busy gate in the coding lane.

Worktree cleanup for finished tasks joins the existing `CleanupFlow`: rows in
`task_sessions` whose task is completed or gone from `todoist_tasks`, with
`last_turn_at` older than 7 days, get `remove_worktree` and the row deleted. The
branch stays; it may back a PR. This settles #302 for this lane.

### 10. Error handling

- `launch_agent_run` is `NO_RETRY`, as everywhere: a retry is a second CLI
  session.
- Poll failures are treated as "still running"; the deadline bounds the loop.
- Any activity failure in the coding path posts "turn failed: <reason>" as a
  comment and parks. The existing crashed-child best-effort park stays.
- `deliver_task_message` and `comment` failures are logged, never fatal.
- `check_task_collision` fails open to `proceed`.
- A `hand_to_you` verdict on turn 1 still creates the session row, so the
  operator's later `take over` resumes into the same worktree.

### 11. Testing

- `tests/worker/test_agent_task_flow.py`: turn 1 comments and parks; a `comment`
  signal during a turn runs a second turn with the queued text; `you_are_in_it`
  exits without a comment or label change; `hand_to_you` comments and parks;
  `take over` bypasses rule 2; deadline kills and reports.
- `tests/core/test_coding_sessions.py`: same-task classification parsing and the
  `session_id` match are pure functions.
- `tests/core/test_remote_script_launch.py`: `_agent_launch_flags` emits
  `--session-id`/`-n` on turn 1 and `--resume` later, never both.
- `tests/worker/activities/test_agent_task_turns.py`: `find_task_turns_due`
  against the real test DB, including the AEGIS-author exclusions.
- `tests/core/test_webhooks_todoist.py`: `note:added` on a session task
  dispatches; an AEGIS-authored note does not.
- `tests/comms/test_slack_inbound.py`: a threaded reply on a task thread posts a
  comment and does not route to chat; an unknown thread routes as today.
- `tests/core/test_mcp_server.py`: `comment_on_task` is withheld from run mounts
  (extend the existing `_UNSERVED_TOOLS` tripwire).
- `tests/worker/test_cleanup.py`: the worktree sweep removes only finished,
  aged sessions.
- Registry gates: `tests/core/fixtures/chat_tools_golden.json` and
  `EXPECTED_TOOL_NAMES` both need the new tool, inserted surgically.

Each test is checked by break-and-revert before it is trusted.

### 12. Delivery order

Three PRs, each usable on its own:

1. **Sessions and turns.** Migration, connector flags, `ensure_task_session`,
   `launch_task_turn`, `kill_task_turn`, `check_task_collision`, the rewritten
   coding path in `AgentTaskFlow` with the `comment` signal, `poll_until_exit`
   extraction, webhook and sweep triggers, the ClarifyFlow exclusion, and the
   deletions in section 9. After this PR the lane works from Todoist alone.
2. **Slack threads.** `deliver_task_message`, the `thread_ref` deliver field,
   inbound `thread_ts` routing, the two admin routes.
3. **Operator tool and housekeeping.** `comment_on_task`, the CleanupFlow
   worktree sweep, docs.

### 13. Rollout

1. Merge; the migration auto-applies on core start.
2. Apply the three configuration changes in section 8.
3. Comment on one of the parked `@code` tasks to start its first turn and walk
   the full loop: plan comment and Slack thread root, a reply in the thread, an
   implement turn, a draft PR, `claude --resume` takeover, rule 1 firing.
4. Verify `workflow_runs` shows `verb=coding` rows with `turn` and `session_id`.

## Files touched

| Area | Files |
|---|---|
| Migration | `migrations/025_task_sessions.sql` |
| Connector | `core/src/aegis/connectors/remote_script.py` (`_agent_launch_flags`, `start_kimi_run`, `ensure_task_worktree`, `kill_run`), `core/src/aegis/connectors/coding_sessions.py` (same-task helpers) |
| Core routes | `core/src/aegis/api/routes/webhooks.py`, a new `routes/task_sessions.py` for the two admin endpoints |
| Core tools | `core/src/aegis/services/tools/gtd.py`, `services/chat.py` (schema + registry entry), `api/routes/mcp_server.py` (`_UNSERVED_TOOLS`) |
| Worker | `flows/agent_task.py`, `flows/agent_run.py` (extract `poll_until_exit`), `activities/agent_task.py`, `activities/delivery.py`, `activities/clarify.py` (one `NOT EXISTS`), `flows/cleanup.py` + `activities/cleanup.py` |
| Comms | `adapters/slack.py`, `slack_inbound.py`, `__main__.py` (deliver route) |
| Seed | `config/seed/activities.yaml` (new config keys with defaults) |
| Docs | `docs/how-it-works.md` section 5, `docs/infrastructure.md` coding block |
