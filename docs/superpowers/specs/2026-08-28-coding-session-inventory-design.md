# Coding-session inventory and launch deconfliction

**Date:** 2026-08-28
**Status:** approved design, not yet implemented

## Problem

AEGIS launches headless coding runs on a coding host. It launches blind: nothing
checks whether a human is already working in the same repo on that host. The
operator can end up with an agent and a person editing the same project at the
same time — separate git worktrees keep the *files* safe, but the effort is
duplicated and the conclusions can contradict.

Three facts make this fixable cheaply, and one makes it necessary:

- Claude Code publishes a session registry per `CLAUDE_CONFIG_DIR` and exposes it
  through `claude agents --json`, documented for scripting and needing no TTY.
- `infra.coding.engines.claude.config_dirs` already maps every account label the
  deployment uses to its config directory. No new configuration is needed to know
  *where* to look.
- Headless runs register in that same registry, so AEGIS must be able to tell its
  own runs apart from a human's or it will deconflict against itself.
- There is no live run state anywhere to consult: `workflow_runs` rows are written
  only at terminal state, because writing at start segfaults asyncpg under the
  Temporal dev server (`worker/src/aegis_worker/interceptors.py:1-19`).

## Decisions taken during brainstorming

| Decision | Choice | Consequence |
|---|---|---|
| Sequencing | Three PRs, safety first | Nothing that can start or stop a run ships before the token work |
| Collision policy | Skip quietly and log | No new approval cards; starvation must be made visible another way |
| Coverage | Claude only, both accounts | Every row is authoritative; kimi and codex sessions stay invisible |
| Guard placement | Activities layer, shared helper | Testable without a workflow environment |

This document specifies **PR1** in full and outlines PR2 and PR3 well enough to
show that PR1 does not paint them into a corner.

## Non-goals

- No push channel into a live session's model. The only real one is the per-PID
  unix socket plus its `.key` file: undocumented, private, and version-fragile.
  AEGIS reaches the human; the session's model pulls from AEGIS over MCP.
- No inventory table. See "Why nothing is persisted".
- No kimi or codex session discovery. Neither publishes a registry, and under a
  skip-quietly policy a heuristic false positive silently starves a task.
- No `tmux send-keys` into an interactive session, ever.
- No change to the gated-run approval path.

## Correction to an earlier assumption

An earlier reading of this lane claimed dead tmux windows leak and that the
window cap was breached. That is wrong and no work should be spent on it.
`_plan_tmux_launch` (`core/src/aegis/connectors/remote_script.py:230-265`) prunes
lazily: at or over the cap it prunes exactly `len(windows) - cap + 1` of the
oldest dead windows and launches. Observed live state was ten agent windows, all
`pane_dead=1`, against a cap of ten — the next launch prunes one and proceeds.
The `zsh` window does not match `_AGENT_WINDOW_PREFIXES` and is not counted.

The real defect is different and narrower: when the cap is full of **live** runs,
`_plan_tmux_launch` returns `use_tmux=False` and the caller falls back to a
detached `nohup`, which is unbounded and has no window to attach to. PR1 does not
change that policy, but the inventory makes it observable for the first time.

## PR1 — inventory and deconfliction

### Configuration

All new configuration goes in the existing `infra.coding` jsonb block. That block
is already DB-owned, already validated by `services/infra.py::validate_coding`,
already editable in the admin Infra page, and already the single place a fork
describes its own coding host. No new table, no new `settings` row.

```json
"inventory": {
  "enabled": false,
  "skip_when_busy": true,
  "accounts": []
}
```

- `enabled` — master switch, **default false**. The feature changes whether runs
  start at all, so a fork must opt in deliberately. Off means not one extra SSH
  round trip.
- `skip_when_busy` — when false, collisions are recorded and reported but never
  block a launch. Lets an operator run the inventory in observe-only mode first.
- `accounts` — optional allow-list of account labels to enumerate. Empty means
  every key in `engines.claude.config_dirs`.

`validate_coding` gains rules for this block: `inventory` must be an object;
`enabled` and `skip_when_busy` must be booleans; `accounts` must be a list of
strings, and every entry must exist in `engines.claude.config_dirs` — the same
class of check the block already applies to `routing.orgs[*].account`. An unknown
account label is rejected at write time rather than silently enumerating nothing.

Nothing user-specific is hardcoded anywhere: no hostname, no path, no account
label, no repo name. The code reads what the deployment's own `infra` row says.

### Enumeration

New method on `RemoteScriptConnector`:

```python
async def list_coding_sessions(self) -> dict
```

It sits beside the launch code so it shares the SSH and config plumbing. For each
selected account it runs, over the existing SSH identity:

```
CLAUDE_CONFIG_DIR=<quoted dir> <quoted claude_binary> agents --json
```

Both values are shell-quoted from DB config. There is no new credential: this
uses the same SSH key the lane already uses to launch runs.

Return shape:

```json
{
  "status": "ok",
  "sessions": [ { "...record..." } ],
  "errors": [ {"account": "personal", "error": "…"} ]
}
```

`status` is `ok` even with partial errors; it is `unavailable` only when the
connector is unconfigured or every account failed. Callers treat anything other
than a clean result as "no collision known" — see "Failure behaviour".

### The session record

```json
{
  "account": "personal",
  "session_id": "267b3b12-…",
  "name": "aegis-b3",
  "cwd": "/home/arshad/Workspace/hikmah/aegis",
  "repo": "hikmah/aegis",
  "status": "busy",
  "kind": "interactive",
  "owner": "human"
}
```

`repo` is `cwd` made relative to `repo_base`, with two normalisations folded in
so that a worktree and its parent compare equal:

- AEGIS's own per-run worktree convention, `<repo>-aegis-wt/<run_id>` → `<repo>`
- Claude Code's own convention, `<repo>/.claude/worktrees/<name>` → `<repo>`

A `cwd` outside `repo_base` yields an empty `repo` and can never match anything.

`owner` is `aegis` when the path contained the `-aegis-wt/` segment, otherwise
`human`. This is load-bearing rather than descriptive: AEGIS's own headless runs
register in the very same registry, so without it the first run in a repo would
block every subsequent one.

**Fields deliberately dropped and never returned:** `messagingSocketPath`, `pid`,
and anything from the sibling `.key` files. The sockets are an undocumented
private interface and the key files are auth material. Nothing in AEGIS should
carry either into a log, a prompt, a chat message, or a Todoist comment. The
parser keeps an explicit allow-list of fields rather than passing the CLI's
object through, so a future CLI version cannot introduce a field that leaks by
default.

### Parsing rules

The parser is a pure function, unit-tested independently of SSH:

- Skip any leading non-JSON. One of the two accounts on the reference deployment
  currently prints a config-restore warning before its JSON array.
- Ignore unknown fields. The CLI is on 2.1.247 and moves fast.
- A missing or unparseable array yields an error entry for that account, never an
  exception and never a partial-truth inventory.
- Cap the accepted output using the connector's existing stdout cap.

### The guard

A shared helper in the worker activities layer:

```python
async def _busy_sessions_for(remote_script, repo: str) -> list[dict]
```

It returns human-owned, `status == "busy"` sessions whose normalised `repo`
equals the target. Two call sites, both activities, both **entry points**:

1. `AgentRunActivities.launch_agent_run` — returns
   `{"status": "skipped", "reason": "repo_busy", "sessions": [...]}` instead of
   launching. `AgentRunFlow` gains one branch that delivers a short message
   naming the session and returns; the interceptor records a terminal
   `workflow_runs` row whose `result_summary.reason` is `repo_busy`.

2. `AgentTaskActivities.run_task_investigation` — same `skipped` shape. This is
   the first point in the task lane at which the repo is actually known.

Continuations are deliberately **not** guarded. `run_task_implementation` runs
only after the operator explicitly approved the plan, and an explicit approval is
a stronger signal than "they have that repo open". Guarding entry points and
leaving continuations alone keeps the rule easy to state and stops a half-finished
task from being abandoned mid-flight.

**Why not filter in the sweep.** The obvious cheaper place is
`AgentTaskSweepFlow`, filtering the batch before any child workflow starts. It
does not work: the sweep does not know a task's repo. Repo resolution happens
inside `AgentTaskFlow` via `resolve_task_repo`, is three-tier, and can involve an
LLM. Pre-filtering would mean either duplicating that resolution in the sweep or
settling for the tier-1 project-name map alone, which covers only some tasks.

**The cost of guarding later, stated plainly.** Because the child workflow has
already started by then, a skip completes the workflow, and the interceptor
writes a terminal `workflow_runs` row carrying that task's `todoist_task_ref`.
`find_actionable_tasks` excludes any task with such a row inside
`cooldown_hours`, so a skipped task is reconsidered after the cooldown (six hours
by default) rather than on the next fifteen-minute tick. This is an accepted
trade, for two reasons: a repo someone is actively working in is unlikely to be
free fifteen minutes later, and `cooldown_hours` is already a configuration field
on the sweep, so a deployment that wants faster retries can lower it without a
code change.

**A skip must not park the task.** Every existing terminal path in the coding
branch calls `park_task`, which stamps `@waiting` and removes the task from
`find_actionable_tasks`' pool until something unparks it. A transient collision
must not do that or a busy afternoon would permanently retire the task. The
`skipped` path returns without parking and without commenting, leaving the task
exactly as it found it.

### Making a quiet skip visible

Skipping quietly was chosen deliberately, but a repo the operator is chronically
busy in would otherwise starve invisibly. Three existing surfaces carry it, with
no new machinery:

- A structured log line, `coding_run_skipped_repo_busy`, carrying repo, account
  and session name.
- A terminal `workflow_runs` row in **both** lanes, with
  `result_summary.reason = "repo_busy"`. `status_digest` already aggregates that
  table by type and status, and the admin Workflows page lists it — so a repo
  that is never reachable shows up as a recurring, countable outcome rather than
  as silence.
- For the dispatch lane only, a short chat message, because the operator asked
  for that specific run and should hear why it did not start.

The task lane deliberately sends no chat message and writes no Todoist comment on
a skip: it is a scheduled sweep the operator did not personally trigger, and
per-tick chatter about a repo they are sitting in would be noise.

### Read-only visibility

One new chat tool, `list_coding_sessions`, returning the same records. Schema
added to `CHAT_TOOLS` and executor to `TOOL_EXECUTORS`, both in
`core/src/aegis/services/chat.py` — there is no auto-discovery, and that file
must not be `ruff format`ed. Granted to agents through `metadata.tool_set`; no
agent gets it implicitly.

This answers "what is running on the coding host?" from chat or a terminal, which
is most of the original goal on its own, and it carries no ability to start or
stop anything.

### Failure behaviour: fail open

Any failure — connector unconfigured, SSH down, a malformed array, the feature
disabled — results in an empty collision set and the launch proceeds. That is
exactly today's behaviour, so the worst case is no worse than the status quo. A
broken enumeration must never become an outage of the coding lane. Every
fail-open path logs.

The homelab this runs on sits on a power domain with a known intermittent fault
that takes the host and every session with it. Because nothing is persisted,
there is no stale inventory to reconcile afterwards; the next call simply reads
whatever is true then.

### Why nothing is persisted

The operating system already holds this state, and it is always correct. A table
would be wrong the moment a pane dies. Worse, a live inventory table would need a
crash-consistent write path, and the one place AEGIS tried that — writing a
`workflow_runs` row at workflow start — segfaults asyncpg, which is why the
interceptor only writes terminal rows today. Composing on read avoids inventing
that path.

### Testing

Every test must be falsifiable: break the logic, confirm the test fails, revert.
This lane has a documented history of tests that passed while production was
broken, so a test that cannot fail is worse than no test.

- Parser: leading noise, unknown fields, empty array, malformed JSON, oversized
  output, and an explicit assertion that no dropped field ever appears in output.
- Normalisation: both worktree conventions, a path outside `repo_base`, and the
  `owner` classification.
- Matcher: busy-versus-idle, human-versus-aegis, matching and non-matching repo.
- Activities: `launch_agent_run` and `run_task_investigation` each skip when the
  helper reports a collision and launch when it does not, using a fake connector
  checked against the real class's signature so the fake cannot drift.
- The skip path in the task lane does **not** call `park_task`. This test exists
  because parking is the default on every other terminal path in that branch, so
  the omission is exactly the kind of thing a later refactor restores by accident.
- Config: `validate_coding` accepts a good block and rejects each bad shape.
- Fail-open: connector unconfigured and SSH failure both permit the launch.

## PR2 — per-agent short-TTL mount tokens (issue #288)

Existing, documented hole, independent of this feature: one shared API key
authenticates every `/api/mcp-server/{agent_id}` endpoint, and per-agent config
files share one directory on the coding host. Any run can read another agent's
mount file or simply change the path segment and drive that agent's tools, and
can print the key into a transcript that gets delivered to chat.

Scope: mint and verify per-agent tokens in core, have the connector write the
scoped token into the run's mount file, and set the TTL from the run timeout.
`_UNSERVED_TOOLS` stays exactly as it is on every run mount — it is an
accidental-recursion guard, not a security boundary, and PR2 does not change that
framing.

## PR3 — operator mount, dispatch and stop

Depends on PR2. A separate route serving the tools withheld from run mounts, with
a credential class that is never materialised on the coding host, so a run cannot
read its way into it. Mandatory authentication even where the deployment allows
unauthenticated MCP access. Adds `stop_agent_run`, and an optional
`todoist_task_id` on dispatch that reuses `AgentTaskFlow`'s deterministic
workflow id so the existing per-task dedup applies rather than the random id
`dispatch_agent_run` uses today.

## Out of scope, worth filing

`PROJECT_REPO_MAP` in `worker/src/aegis_worker/activities/agent_task.py:107-112`
hardcodes one operator's Todoist project names and GitHub repositories in a
public repository. It should move to DB configuration in the manner of
`content_routes` or `gtd_rules`. Unrelated to this feature; deserves its own
issue rather than silent inclusion here.
