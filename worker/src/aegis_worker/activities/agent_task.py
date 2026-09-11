"""AgentTaskActivities — execute AEGIS's own agent-assigned Todoist tasks.

Every one of the 80 agent-assigned tasks in prod is AEGIS's own triage output
(source_tag #alert/#email/#receipt), not a user delegation, and NONE has a due
date. So eligibility deliberately does not require one — requiring a date would
keep this flow permanently idle. The brake is instead: a small cap per tick,
oldest first, and a per-task cooldown.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
from aegis.services import hub, work_sessions
from aegis.services.project_repo_map import get_project_repo_map, lookup
from temporalio import activity

# Assignee labels this flow will act on. @me is deliberately absent: a task the
# user has claimed is theirs to handle.
ADDRESSABLE_ASSIGNEES = ["@sebas", "@raphael", "@maou", "@pandora"]

# Reaching either of these removes a task from the eligible pool. Without that,
# the cooldown becomes an infinite slow loop over the same tasks.
PARK_LABEL = "@waiting"
# `#money`: Maou raises these and the user acts on them; no verb could act on one
# without guessing about the user's money.
EXCLUDED_LABELS = ["@someday", PARK_LABEL, "#money"]

# Upper bound on the eligible pool we consider per tick. Production's whole
# agent-assigned backlog is ~80 rows, so this is the pool, not a sample.
# ponytail: fixed bound; move both caps into SQL if the pool ever nears it.
_ELIGIBLE_SCAN_LIMIT = 200

# The comment thread is a coding session's memory, and the whole tail is re-read
# on every turn. 30 notes is a long day of back-and-forth; the flow, not this
# activity, caps the RENDERED thread at 12,000 characters (newest kept).
_TASK_NOTE_LIMIT = 30

# A turn's MCP mount token outlives its deadline by an hour, so a run that is
# being killed or inspected past the deadline still has its tools.
_TURN_TOKEN_GRACE_SECONDS = 3600

# comment() retries in-activity rather than via a Temporal retry_policy, so the
# command uuid stays stable and the Sync API dedups. A parked task's comment is
# its ONLY user-visible explanation, and a transient Todoist http_503 silently
# lost both comments on this flow's first production tick (issue #159).
# ponytail: 3 attempts / 2s; worst case 4s of sleep inside comment's 60s
# TIMEOUT_STANDARD budget. `apply_restart_approval` calls comment() directly
# inside InteractionFlow's hard 30s post_resolve deadline, so this must stay
# small — see the budget note there before raising it.
_COMMENT_ATTEMPTS = 3
_COMMENT_RETRY_SECONDS = 2

# source_tag → verb. source_tag is PRIMARY; @code is consulted only when
# source_tag IS NULL (i.e. the task is user-authored). Clarify put a stray
# @code label on a real #email task in prod, and treating that as "run a
# coding agent on this email" would be nonsense.
#
# EVERY tag AEGIS captures under has an entry: a verb, or an explicit None
# meaning "decided: nothing here works these". That is the `_GTD_STATE_FOR`
# contract from clarify (#139), and test_agent_task_verbs.py derives the tag
# vocabulary from `gtd_rules.SOURCE_TAGS` and the hub's tags, so a new tag
# added without a decision fails CI instead of silently parking (#344).
#
# These are generic defaults. A deployment changes any of them with the
# `agent_task_verbs` settings row, merged over this table by `merge_verbs`.
#
# `ask` hands the task to the agent it is assigned to, through that agent's
# own chat path — `AgentChatReplyFlow`, the executor clarify already uses when
# you comment on an agent's task. A `#chat`, `#research`, `#calendar` or
# `#manual` task given to an agent is a request to that agent; before #344 all
# four resolved to no verb, got "No executor for this task type" and parked
# with nothing done (prod: an outage question given to the infra agent, an
# article given to the research agent).
UNTAGGED = "untagged"  # the settings key for a task with no source tag
DEFAULT_VERBS: dict[str, str | None] = {
    "#alert": "infra",
    "#receipt": "finance",
    "#email": "email",
    "#chat": "ask",
    "#research": "ask",
    "#calendar": "ask",
    "#manual": "ask",
    # A hand-written task carrying an agent's label and no `@code`: somebody
    # gave it to that agent, which is the same request a `#manual` task is.
    UNTAGGED: "ask",
    # Maou raises these and the user acts on them. `EXCLUDED_LABELS` keeps the
    # sweep off them before a verb is ever resolved; this says why.
    "#money": None,
}
# The verbs a tag may be routed to. `coding` is not one: it is chosen by the
# `@code` label on an untagged task, never by a tag.
VERBS = frozenset({"infra", "email", "finance", "ask"})
VERBS_SETTING = "agent_task_verbs"


def merge_verbs(value: Any) -> dict[str, str | None]:
    """`DEFAULT_VERBS` with the `agent_task_verbs` settings row merged over it.

    Lenient on read, like every settings merge in AEGIS: an entry that names a
    verb this lane does not have is ignored, so a typo in the row cannot turn
    a tag that works into one that parks. None is honoured — it is how a
    deployment says "leave these tasks to me".
    """
    merged = dict(DEFAULT_VERBS)
    if not isinstance(value, dict):
        return merged
    for tag, verb in value.items():
        if verb is None or verb in VERBS:
            merged[str(tag)] = verb
    return merged


async def load_verbs(pool: Any) -> dict[str, str | None]:
    """The effective verb table. A failed read is the defaults, never an
    outage of the lane."""
    if pool is None:
        return dict(DEFAULT_VERBS)
    try:
        value = await pool.fetchval("SELECT value FROM settings WHERE key = $1", VERBS_SETTING)
    except Exception as exc:  # noqa: BLE001 — routing must never break on a config read
        activity.logger.warning("agent_task_verbs_read_failed err=%s", str(exc)[:200])
        return dict(DEFAULT_VERBS)
    return merge_verbs(value)


# Swarm service names as they appear in real prod alert titles, and in the
# heartbeat's own (flows/infra_heartbeat.py). A task the hub projected never
# needs these — its problem names the subject — but one that predates the hub
# has only its title.
_SERVICE_PATTERNS = (
    re.compile(r"^PROLONGED:\s+(\S+)\s+(?:degraded|still\s+down)", re.I),
    re.compile(r"^Service\s+(\S+)\s+has\s+fewer\s+tasks", re.I),
    re.compile(r"^Service\s+(\S+)\s+down\b", re.I),
    re.compile(r"^([A-Za-z][\w.-]*)\s+is\s+down\b", re.I),
)
_NODE_PATTERN = re.compile(r"^Swarm\s+node\s+(\S+)\s+down\b", re.I)


def resolve_verb(task: dict, verbs: dict[str, str | None] | None = None) -> str:
    """Verb for a task: its source tag's, or `coding` for an untagged `@code` task.

    `verbs` is the effective table (`load_verbs`); None is the shipped one. A
    tag the table maps to None resolves to `none` (decided: nothing works it)
    and a tag it does not know to `unknown` (nobody decided). Both park, and
    the run's summary says which.
    """
    table = DEFAULT_VERBS if verbs is None else verbs
    source_tag = task.get("source_tag")
    if not source_tag:
        if "@code" in (task.get("labels") or []):
            return "coding"
        source_tag = UNTAGGED
    if source_tag not in table:
        return "unknown"
    return table[source_tag] or "none"


def extract_service_name(title: str) -> str:
    """Swarm service named by an alert title, or '' when none is."""
    text = (title or "").strip()
    for pattern in _SERVICE_PATTERNS:
        match = pattern.match(text)
        if match:
            # Compose-style names (`redis_redis`) already come out of the
            # PROLONGED/fewer-tasks patterns lowercase and underscore-joined —
            # don't touch them. Only the free-text "X is down" pattern needs
            # normalising, since that title can carry whatever casing a human
            # or another system used.
            return match.group(1).lower() if "_" not in match.group(1) else match.group(1)
    return ""


def extract_node_name(title: str) -> str:
    """Swarm node named by the heartbeat's node-down title, or ''."""
    match = _NODE_PATTERN.match((title or "").strip())
    return match.group(1) if match else ""


# #receipt task title shapes. LEGACY: the v1 subscription tracker's renewal
# and cancellation sweeps were the only things that ever wrote these titles,
# and they are gone (2026-09). No new task carries one, so these patterns exist
# only to still parse the `#receipt` tasks already sitting in Todoist. A title
# that matches none of them yields "", which routes the finance verb to its
# "I couldn't tell which merchant this is about" park — the honest outcome.
_MERCHANT_PATTERNS = (
    re.compile(r"^Anomaly:\s*\?\s*(.+?)\s*$", re.I),
    re.compile(r"^Anomaly:\s*[\d.,]+\s+\w+\s+(.+?)\s*$", re.I),
    re.compile(r"^Renewal in [\d.]+ days:\s*(.+?)\s*\([^)]*\)\s*$", re.I),
)


# Stamped on every `merchant_history` summary — the two tables it reads have
# had no writer since the v1 subscription tracker was deleted. Both the Todoist
# comment and the decision card render that summary verbatim, so this is what
# stops a frozen answer being read as a current one.
_RETIRED_SOURCE = (
    "source retired 2026-09: finance.recurring_charge is frozen and no receipt "
    "stored since then is linked to it, so this covers nothing recent — the "
    "hledger books are the record now"
)


def extract_merchant(title: str) -> str:
    """Merchant named by a #receipt task title, or '' when none is."""
    text = (title or "").strip()
    for pattern in _MERCHANT_PATTERNS:
        match = pattern.match(text)
        if match:
            return match.group(1).strip()
    return ""


# Todoist project name → GitHub repo now lives in the `project_repo_map`
# settings row (`core/src/aegis/services/project_repo_map.py`), edited at
# GET/PUT /api/admin/todoist/project-repo-map. It used to be a constant here,
# which shipped one operator's Todoist layout in a public repo and could not be
# changed without editing code (issue #345). Ships EMPTY: a deployment with no
# mapping simply falls through to the resolver's later tiers.

# Tier 2 (title/description match via AlertActivities.resolve_alert_resource)
# auto-accepts only at or above this bar. resolve_alert_resource's OWN "llm"
# vs "llm_unconfirmed" split sits at 0.5 — calibrated for the alert-investigation
# flow, which re-scores the pick against the issue content at its own Gate-0
# (score_resource_relevance) and further guards with an active-work check
# before ever touching a repo. resolve_task_repo has neither of those extra
# checks downstream — the flow proceeds straight to a real kimi run — so a
# bare 0.5 here would let a coin-flip LLM guess kick one off unsupervised,
# which is the one thing this resolver must never do (issue #158). 0.8 still
# passes genuinely confident picks (free-text token overlap is always 1.0;
# a clear LLM match commonly scores >= 0.85 — see
# tests/worker/test_alert_resource_resolution.py) while anything softer is
# surfaced as `candidates` for the flow's tier 3 Gate-0 confirm card instead
# of guessed.
_TIER2_CONFIDENCE_THRESHOLD = 0.8


def match_repo_candidate(candidates: list[dict], comment: str) -> dict | None:
    """The candidate the operator named in a comment, or None.

    An EXACT (case-insensitive) match on one of the three names a candidate is
    known by. Deliberately not a substring or fuzzy match: this is the answer to
    "which repo?", and the cost of a wrong pick is an unattended coding session
    in someone else's checkout. Anything unrecognised repeats the question.
    """
    text = (comment or "").strip().lower()
    if not text:
        return None
    for candidate in candidates:
        for key in ("github_repo", "resource_title", "resource_path"):
            value = str(candidate.get(key) or "").strip().lower()
            if value and value == text:
                return candidate
    return None


# --- the `ask` verb and the infra verb's plan (#344) ------------------------

# How much of a thread, a description, a verdict or a runbook a message
# quotes. Per field, so one pasted stack trace cannot crowd out the rest.
_ASK_NOTE_LIMIT = 15
_ASK_NOTE_CAP = 800
_FIELD_CAP = 2000
_QUOTE_CAP = 400
_RUNBOOK_CAP = 1200
_GROUP_MEMBER_CAP = 12
# One GET, bounded well inside the plan activity's 60s start-to-close.
_PROBE_TIMEOUT_S = 10.0


def _cut(text: str, cap: int) -> str:
    value = (text or "").strip()
    return value if len(value) <= cap else value[:cap].rstrip() + " […]"


def _at(value: Any) -> str:
    return f"{value:%Y-%m-%d %H:%M} UTC" if hasattr(value, "strftime") else str(value or "")


def _ask_message(task: dict) -> str:
    """The turn the sweep sends an agent when it hands over a task.

    Read-only is a product rule, the same one the coding lane's turn 1 keeps:
    nobody is in this conversation when the sweep asks, so the turn may look
    and answer but not change anything. A change waits for the user's
    go-ahead — a reply on the task, which clarify's comment channel carries to
    the agent while the task is in the Inbox, or a message in its channel —
    where the person asking is the approval.
    """
    notes = list(task.get("notes") or [])[-_ASK_NOTE_LIMIT:]
    thread = "\n".join(
        f"[{n.get('posted_at') or ''}] {str(n.get('content') or '')[:_ASK_NOTE_CAP]}"
        for n in notes
    )
    description = _cut(str(task.get("description") or ""), _FIELD_CAP)
    return (
        f"Todoist task {task.get('id')} was given to you: {task.get('content') or ''}\n\n"
        + (f"{description}\n\n" if description else "")
        + "Comment thread so far (oldest first; AEGIS's own notes carry a "
        "`Workflow run:` footer or an `[Agent reply @` header):\n"
        + (thread or "(no comments yet)")
        + "\n\nNobody is waiting in a chat for this: the task queue is handing you "
        "the task. Work it with read-only steps: look things up, check, investigate, "
        "and answer. Do not restart, deploy, delete, merge, send, complete or change "
        "anything. If it needs a change, say exactly what you would do and ask the "
        "user to reply on the task to go ahead. Your answer is posted on the task."
    )


# A task with no problem on the hub has no timeline to read.
_NO_FACTS: dict = {
    "sources": frozenset(),
    "alertname": "",
    "url": "",
    "description": "",
    "verdict": None,
}


def _report(handler: str, kind: str, parts: list[str], reason: str) -> dict:
    """A plan the flow posts as one comment before parking the task once."""
    return {
        "action": "report",
        "handler": handler,
        "kind": kind,
        "comment": "\n\n".join(p for p in parts if p),
        "reason": reason,
    }


def _verdict_line(problem: dict | None, facts: dict) -> str:
    """The hub's latest investigation finding, quoted, or that there is none.

    Read from the problem's timeline, never re-run: the investigation is the
    hub's, started once when the problem appeared (`IngestResult.investigate`).
    """
    if problem is None:
        return ""
    row = facts.get("verdict")
    if row is None:
        return "No investigation has reported on it yet."
    payload = row["payload"] if isinstance(row["payload"], dict) else {}
    text = _cut(str(payload.get("text") or payload.get("status") or ""), _QUOTE_CAP)
    resource = str(payload.get("resource") or "")
    line = f"The investigation said ({_at(row['occurred_at'])}): {text}"
    return line + (f" (looked at {resource})" if resource else "")


def _timeline_line(problem: dict | None) -> str:
    if problem is None:
        return ""
    status = str(problem.get("status") or "")
    head = f"The hub has this problem as {status}. " if status in ("resolved", "closed") else ""
    return (
        f"{head}Full timeline: problem {problem['id']} on the admin Problems page, "
        "or `task_context` from a session."
    )


def _alert_says(facts: dict) -> str:
    description = str(facts.get("description") or "")
    return f"The alert says: {_cut(description, _QUOTE_CAP)}" if description else ""


# What a person does about a problem of one of AEGIS's own kinds. Generic
# AEGIS vocabulary (the producers are AEGIS's flow-health, comms and social
# watchdogs), not anything a deployment names.
_TODO_BY_KIND = {
    "flow": (
        "What to do: open the flow's recent failed runs on the admin Workflows page "
        "(a failing LLM purpose also shows on the Models page) and fix what they show. "
        "The watchdog resolves this problem when the failures stop."
    ),
    "comms": (
        "What to do: check that the comms service is running and that its chat tokens "
        "are valid (admin Slack page). The watchdog resolves this problem when messages "
        "arrive again."
    ),
    "post": (
        "What to do: check the social scheduler's worker and its queue. The watchdog "
        "resolves this problem when the posts publish."
    ),
}


async def _probe(url: str) -> dict:
    """One GET against `url`: its status and time, or why it did not answer."""
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT_S, follow_redirects=True) as client:
            response = await client.get(url)
    except Exception as exc:  # noqa: BLE001 — a failed probe IS the finding
        return {"ok": False, "status": 0, "ms": 0,
                "error": f"{type(exc).__name__}: {str(exc)[:160]}"}
    return {
        "ok": response.status_code < 400,
        "status": response.status_code,
        "ms": int((time.monotonic() - started) * 1000),
        "error": "",
    }


@dataclass
class AgentTaskActivities:
    db_pool: Any = None
    todoist_connector: Any = None
    remote_script: Any = None
    homelab_connector: Any = None
    gmail_accounts: list[str] = field(default_factory=list)
    # InfraOpsActivities instance. A plain field, not a private seam, so tests
    # pass a fake and production passes the real thing.
    infra_ops: Any = None
    # GmailActivities instance (for triage_email's apply_label calls). A plain
    # field like infra_ops above, late-wired in __main__.py after GmailActivities
    # is constructed.
    gmail_activities: Any = None
    # AlertActivities instance — resolve_task_repo's tier 2 reuses its
    # resolve_alert_resource directly (same plain-field, direct-call pattern as
    # gmail_activities.apply_label above), late-wired in __main__.py after
    # AlertActivities is constructed. None ⇒ tier 2/3 are skipped and
    # resolve_task_repo behaves exactly as tier-1-only (never guesses).
    alert_act: Any = None

    @activity.defn
    async def find_actionable_tasks(
        self, max_tasks: int = 3, cooldown_hours: int = 6, max_coding: int = 1
    ) -> list[dict]:
        """Eligible agent-assigned tasks, oldest first, cooldown-filtered.

        `max_coding` caps coding tasks (source_tag IS NULL + @code) within the
        batch — a kimi run takes minutes and the coding host's tmux window cap
        is 10, so an uncapped fan-out would wedge it.

        A task that already has a `work_sessions` row is excluded outright: the
        sweep only ever starts TURN ONE. Later turns come from
        `find_task_turns_due`, keyed on the session's own `last_turn_at`
        watermark, so without this exclusion every tick would start a second
        first turn on a conversation that is already going.
        """
        if self.db_pool is None:
            return []
        rows = await self.db_pool.fetch(
            """
            SELECT t.id, t.content, t.description, t.labels, t.source_tag,
                   t.project_id, t.assignee_label, t.updated_at
            FROM todoist_tasks t
            WHERE NOT t.is_completed
              AND t.assignee_label = ANY($1::text[])
              AND NOT (t.labels && $2::text[])
              AND NOT EXISTS (
                  SELECT 1 FROM workflow_runs wr
                  WHERE wr.workflow_type = 'AgentTaskFlow'
                    AND wr.todoist_task_ref = t.id
                    AND wr.started_at > now() - make_interval(hours => $3)
              )
              AND NOT EXISTS (
                  SELECT 1 FROM work_sessions ts WHERE ts.task_id = t.id AND ts.owner = 'aegis'
              )
            ORDER BY t.updated_at ASC
            LIMIT $4
            """,
            ADDRESSABLE_ASSIGNEES,
            EXCLUDED_LABELS,
            cooldown_hours,
            _ELIGIBLE_SCAN_LIMIT,
        )

        out: list[dict] = []
        coding_seen = 0
        for row in rows:
            task = dict(row)
            task["labels"] = list(task["labels"] or [])
            is_coding = task["source_tag"] is None and "@code" in task["labels"]
            if is_coding:
                if coding_seen >= max_coding:
                    continue
                coding_seen += 1
            out.append(task)
            if len(out) >= max_tasks:
                break
        return out

    @activity.defn
    async def load_task_context(self, task_id: str) -> dict:
        """What the flow needs to know about where a task came from.

        `todoist_capture_idempotency` links task → external_id with near-total
        coverage in prod (41/42 #alert, 30/30 #email). external_id is prefixed
        by source: `alert-<fingerprint>`, `gmail-<message_id>`, which is why
        the mail lane can read a message id back out of it.

        `subject` / `subject_kind` come from the problem behind the task, so
        the infra verb resolves a service from `problems.subject` instead of
        parsing it out of a title.

        Every key here has a reader. The alert `fingerprint` this used to
        return was the pre-hub identity — nothing has looked one up since the
        problem hub replaced that lookup, and the problem id it also returned
        was never read either: `subject` is what the verb actually needs.

        `verb` is the task's verb under the effective table (`load_verbs`): a
        settings row can change it, and the flow cannot read the database, so
        it is decided here (#344). `unknown` for a task not in the mirror.
        """
        empty = {
            "external_id": "",
            "gmail_message_id": "",
            "subject": "",
            "subject_kind": "",
            "verb": "unknown",
        }
        if self.db_pool is None or not task_id:
            return empty
        row = await self.db_pool.fetchrow(
            "SELECT source_tag, labels FROM todoist_tasks WHERE id = $1", task_id
        )
        verb = (
            resolve_verb(
                {"source_tag": row["source_tag"], "labels": list(row["labels"] or [])},
                await load_verbs(self.db_pool),
            )
            if row is not None
            else "unknown"
        )
        empty["verb"] = verb
        # A task the problem hub projected knows its subject exactly
        # (`problems.subject`), so the infra verb need not parse the title.
        problem = await self.db_pool.fetchrow(
            "SELECT id::text AS id, subject, subject_kind FROM problems "
            "WHERE todoist_task_id = $1 "
            "ORDER BY first_seen_at DESC LIMIT 1",
            task_id,
        )
        external_id = await self.db_pool.fetchval(
            "SELECT external_id FROM todoist_capture_idempotency "
            "WHERE todoist_task_ref = $1 ORDER BY captured_at DESC LIMIT 1",
            task_id,
        )
        if not external_id and problem is None:
            return empty
        external_id = external_id or ""
        return {
            "external_id": external_id,
            "gmail_message_id": (
                external_id[len("gmail-") :] if external_id.startswith("gmail-") else ""
            ),
            "subject": problem["subject"] if problem else "",
            "subject_kind": problem["subject_kind"] if problem else "",
            "verb": verb,
        }

    # --- terminal states ---

    async def _queue_command(self, temp_id: str, command: dict) -> None:
        """Enqueue a Todoist Sync command. The temp_id is deterministic and
        permanent per task (e.g. `agent-task-park-{task_id}`), so a plain
        `DO NOTHING` would only cover the FIRST park/complete ever — once
        TodoistSyncFlow drains that row to a terminal status ('committed' or
        'failed', per activities/todoist.py), a LATER re-park of the same
        task (label removed, task re-enters the pool, cooldown re-fires)
        would insert nothing, leaving only the local optimistic projection
        updated — which the next TodoistSyncFlow pull overwrites via its
        `labels = EXCLUDED.labels` upsert, silently dropping the park
        forever. Re-queue whenever the existing row is terminal; leave an
        undrained 'pending' row untouched so we don't clobber work in
        flight."""
        await self.db_pool.execute(
            "INSERT INTO todoist_outbox (temp_id, command, status) "
            "VALUES ($1, $2, 'pending') "
            "ON CONFLICT (temp_id) DO UPDATE "
            "SET command = EXCLUDED.command, status = 'pending', attempt_count = 0 "
            "WHERE todoist_outbox.status <> 'pending'",
            temp_id,
            command,
        )

    @activity.defn
    async def park_task(self, task_id: str, reason: str) -> dict:
        """Add @waiting — the parking state. Eligibility excludes @waiting, so
        this is what removes a task from the pool and stops the cooldown
        re-picking it forever."""
        from aegis.connectors.todoist import TodoistConnector

        if self.db_pool is None or not task_id:
            return {"parked": False}
        labels = await self.db_pool.fetchval(
            "SELECT labels FROM todoist_tasks WHERE id = $1", task_id
        )
        if labels is None:
            return {"parked": False}
        # The registry says why the task is parked, not only the worker log:
        # a session opened on the task later reads it from `task_context`.
        # No-op for a task with no coding session.
        try:
            await work_sessions.set_state(self.db_pool, task_id, status="parked", summary=reason)
        except Exception as exc:  # noqa: BLE001 — the park itself must still land
            activity.logger.warning(
                "task_park_state_not_recorded task_id=%s err=%s", task_id, str(exc)[:200]
            )
        if PARK_LABEL in labels:
            return {"parked": True}
        new_labels = [*labels, PARK_LABEL]
        await self._queue_command(
            f"agent-task-park-{task_id}",
            TodoistConnector.build_item_update_command(task_id, labels=new_labels),
        )
        # Optimistic local update so the next tick doesn't re-select the task
        # before the 5-min sync round-trips.
        await self.db_pool.execute(
            "UPDATE todoist_tasks SET labels = $1, updated_at = now() WHERE id = $2",
            new_labels,
            task_id,
        )
        activity.logger.info("agent_task_parked task_id=%s reason=%s", task_id, reason[:120])
        return {"parked": True}

    @activity.defn
    async def complete_task(self, task_id: str) -> dict:
        """Close the task — only when no human work remains."""
        from aegis.connectors.todoist import TodoistConnector

        if self.db_pool is None or not task_id:
            return {"completed": False}
        exists = await self.db_pool.fetchval(
            "SELECT 1 FROM todoist_tasks WHERE id = $1", task_id
        )
        if not exists:
            return {"completed": False}
        await self._queue_command(
            f"agent-task-complete-{task_id}",
            TodoistConnector.build_item_complete_command(task_id),
        )
        await self.db_pool.execute(
            "UPDATE todoist_tasks SET is_completed = true, updated_at = now() WHERE id = $1",
            task_id,
        )
        return {"completed": True}

    @activity.defn
    async def comment(self, task_id: str, agent_id: str, body: str) -> dict:
        """Post a task comment. The `Workflow run:` footer is REQUIRED: clarify
        excludes AEGIS-authored notes by matching it, and without it this
        comment re-eligibles the task and the flow re-spawns every 15 min.

        Delivery mirrors `activities/alerts.py::post_task_note`'s
        build_note_add_command + commands() + check_sync_status() shape —
        the Sync API envelope can report ok=True while the per-command
        note_add was rejected, so the envelope alone is not proof the comment
        landed. Exceptions from the connector call are caught (comments are
        best-effort) so a delivery failure never blocks the park_task step
        that always follows this one.
        """
        from aegis.connectors.todoist import TodoistConnector

        if self.todoist_connector is None or not task_id:
            return {"ok": False}
        info = activity.info() if activity.in_activity() else None
        run_ref = info.workflow_id if info else "local"
        content = f"[{agent_id}] {body}\n\nWorkflow run: {run_ref}"
        # Build the command ONCE and reuse it across attempts. The Sync API keys
        # idempotency on the command uuid, so a retry with the same uuid cannot
        # double-post; rebuilding it (which a Temporal activity retry would do,
        # since the whole activity re-runs) mints a new uuid and could.
        cmd = TodoistConnector.build_note_add_command(task_id, content)
        last_error: Any = None
        for attempt in range(1, _COMMENT_ATTEMPTS + 1):
            try:
                result = await self.todoist_connector.commands([cmd])
                status = TodoistConnector.check_sync_status(result, [cmd["uuid"]])
            except Exception as exc:  # noqa: BLE001 — comments are best-effort
                last_error = str(exc)[:200]
                activity.logger.warning(
                    "agent_task_comment_failed task_id=%s attempt=%s err=%s",
                    task_id,
                    attempt,
                    last_error,
                )
            else:
                if status["ok"]:
                    return {"ok": True, "error": None}
                if status["envelope_error"]:
                    # Transient in practice — a live Todoist http_503 lost both
                    # comments on this flow's first production tick (issue #159).
                    last_error = status["envelope_error"]
                    activity.logger.warning(
                        "agent_task_comment_envelope_failed task_id=%s attempt=%s error=%s",
                        task_id,
                        attempt,
                        str(last_error)[:200],
                    )
                else:
                    # A per-command rejection is a permanent 4xx-class verdict
                    # (bad item id, malformed content) — retrying poison-loops.
                    rejected = status["rejected"].get(cmd["uuid"])
                    activity.logger.warning(
                        "agent_task_comment_rejected task_id=%s status=%s",
                        task_id,
                        str(rejected)[:200],
                    )
                    return {"ok": False, "error": f"command_rejected: {rejected}"}
            if attempt < _COMMENT_ATTEMPTS:
                await asyncio.sleep(_COMMENT_RETRY_SECONDS)
        return {"ok": False, "error": last_error}

    @activity.defn
    async def apply_restart_approval(
        self, interaction_id: str, response: dict, metadata: dict
    ) -> dict:
        """InteractionFlow post_resolve hook for the restart card.

        Approve: restart, re-check health, and complete the task only if the
        service actually recovered — a restart that didn't fix it must stay
        visible, so it parks instead.
        """
        choice = (response.get("value") or "").strip()
        task_id = str(metadata.get("task_id") or "")
        service = str(metadata.get("service") or "")
        agent_id = str(metadata.get("agent_id") or "")
        if not task_id or not service:
            return {"applied": "none"}

        if choice == "skip":
            await self.comment(task_id, agent_id, f"Leaving `{service}` alone as you asked.")
            await self.park_task(task_id, "restart declined")
            return {"applied": "skipped"}

        if choice != "approve":
            activity.logger.info(
                "agent_task_restart_no_action interaction_id=%s choice=%s",
                interaction_id,
                choice,
            )
            return {"applied": "none"}

        # This activity has maximum_attempts=2 (flows/interaction.py's
        # _BEST_EFFORT_RETRY) and `restart_service` is a real write —
        # `docker service update --force` — so a retried attempt must NOT
        # re-issue it: that would reschedule the tasks the first call just
        # scheduled and actively delay the convergence we're polling for.
        # Treat a second attempt as having already issued the restart.
        if activity.in_activity() and activity.info().attempt > 1:
            restart = {"ok": True, "detail": "restart already issued on a previous attempt"}
        else:
            restart = await self.infra_ops.restart_service(service)

        if not restart.get("ok"):
            # The restart call itself failed (no connector, connector
            # exception, or `docker service update --force` exiting non-zero —
            # covers a renamed/missing service, a read_only infra entry, or an
            # unreachable daemon). Nothing was restarted, so say so — do NOT
            # fall into the "still converging" message below, which would
            # falsely claim a restart happened.
            await self.comment(
                task_id,
                agent_id,
                f"Tried to restart `{service}` but the restart itself failed "
                f"({restart.get('detail', 'unknown error')}) — nothing was restarted.",
            )
            await self.park_task(task_id, "restart_service failed")
            return {"applied": "approved"}

        # `restart_service` runs `docker service update --force --detach`
        # (connectors/homelab.py:175) and returns BEFORE the swarm converges, so
        # a single immediate health check would essentially never see recovery.
        # The sibling `remediate_infra_service` (alerts.py:1220-1252) polls 6x5s
        # for exactly this reason — but this runs as InteractionFlow's
        # post_resolve activity, which has a hard 30s timeout
        # (flows/interaction.py:67) and only ONE retry-safe attempt (above), so
        # budget the poll with real headroom inside it, not right up against it.
        # ponytail: 3x3s=9s; if swarm convergence is routinely slower, move the
        # verification out of the hook and let the next sweep tick observe it.
        health = {"healthy": False, "detail": "not checked"}
        for _ in range(3):
            await asyncio.sleep(3)
            health = await self.infra_ops.service_health(service)
            if health.get("healthy"):
                break

        if health.get("healthy"):
            await self.comment(
                task_id, agent_id, f"Restarted `{service}` and it's healthy again — closing."
            )
            await self.complete_task(task_id)
        else:
            await self.comment(
                task_id,
                agent_id,
                f"Restarted `{service}` but it hadn't come back healthy within 9s "
                f"({health.get('detail', 'unknown')}) — it may still be converging; "
                "leaving this open for you.",
            )
            await self.park_task(task_id, "restart did not restore health")
        return {"applied": "approved"}

    @activity.defn
    async def triage_email(self, task_id: str, title: str, gmail_message_id: str) -> dict:
        """Archive notification mail; leave anything needing a reply.

        Sending is impossible under the current `gmail.modify` scope, so a real
        action is parked for the user rather than answered.

        Reuses clarify's notification detection so this flow and the classifier
        agree on what counts as junk.
        """
        from aegis_worker.activities.clarify import ClarifyActivities

        if not gmail_message_id:
            return {"action": "needs_human", "account": ""}
        if not ClarifyActivities._looks_like_notification(title):
            return {"action": "needs_human", "account": ""}
        if self.gmail_activities is None:
            return {"action": "not_found", "account": ""}

        # The task doesn't record which of the three accounts the message came
        # from, so probe: a wrong account 404s, which is a clean discriminator.
        for account in self.gmail_accounts:
            result = await self.gmail_activities.apply_label(
                account, gmail_message_id, "ARCHIVE"
            )
            if result.get("ok"):
                return {"action": "archived", "account": account}
        return {"action": "not_found", "account": ""}

    @activity.defn
    async def merchant_history(self, title: str, limit: int = 6) -> dict:
        """Prior charges for the merchant this task names, from a RETIRED source.

        The value of this verb is assembled context, not an autonomous
        decision — whether a charge is legitimate is the user's call.

        Every answer carries `_RETIRED_SOURCE` because this reads two frozen
        tables. Nothing has written `finance.recurring_charge` or stamped
        `finance.receipt_email.charge_id` since the v1 subscription tracker was
        deleted (2026-09), so the join below can only ever match receipts
        stored before that date. A bare "no prior charges on record" would
        therefore mean "we stopped looking", while the operator — or an agent
        summarising this for them — would read it as "this merchant is new".
        Both are decision-grade claims about money, so neither is made silently.
        """
        merchant = extract_merchant(title)
        if not merchant or self.db_pool is None:
            return {"merchant": "", "charges": [], "summary": ""}
        # `recurring_charge` is UPSERT-keyed on
        # (account, sender_label, amount_cents, currency) — one row per charge
        # SIGNATURE with last_seen_at bumped in place — so a merchant billing a
        # steady amount has exactly ONE row and no history. `receipt_email` IS
        # append-only (one row per receipt, unique on message_id), so join
        # through its charge_id FK for the canonical vendor and read each
        # receipt's own amount from the `parsed` extraction.
        rows = await self.db_pool.fetch(
            """
            SELECT re.received_at,
                   COALESCE((re.parsed->>'amount')::numeric,
                            rc.amount_cents / 100.0) AS amount,
                   COALESCE(re.parsed->>'currency', rc.currency) AS currency
            FROM finance.receipt_email re
            JOIN finance.recurring_charge rc ON rc.id = re.charge_id
            WHERE rc.vendor_name = $1
            ORDER BY re.received_at DESC
            LIMIT $2
            """,
            merchant,
            limit,
        )
        charges = [
            {
                "amount": float(r["amount"] or 0),
                "currency": r["currency"] or "",
                "last_seen_at": r["received_at"].isoformat() if r["received_at"] else "",
            }
            for r in rows
        ]
        found = "; ".join(
            f"{c['amount']:g} {c['currency']} on {c['last_seen_at'][:10]}" for c in charges
        )
        summary = f"{found} ({_RETIRED_SOURCE})" if found else f"nothing found — {_RETIRED_SOURCE}"
        return {"merchant": merchant, "charges": charges, "summary": summary}

    @activity.defn
    async def apply_finance_decision(
        self, interaction_id: str, response: dict, metadata: dict
    ) -> dict:
        """InteractionFlow post_resolve hook for the anomaly decision card."""
        choice = (response.get("value") or "").strip()
        task_id = str(metadata.get("task_id") or "")
        agent_id = str(metadata.get("agent_id") or "")
        merchant = str(metadata.get("merchant") or "this charge")
        if not task_id:
            return {"applied": "none"}

        if choice == "expected":
            await self.comment(task_id, agent_id, f"You confirmed {merchant} is expected — closing.")
            await self.complete_task(task_id)
            return {"applied": "expected"}
        if choice == "investigate":
            await self.comment(
                task_id, agent_id, f"Flagged {merchant} for you to investigate."
            )
            await self.park_task(task_id, "finance anomaly needs investigation")
            return {"applied": "investigate"}

        activity.logger.info(
            "agent_task_finance_no_action interaction_id=%s choice=%s", interaction_id, choice
        )
        return {"applied": "none"}

    # --- the `ask` verb (#344) -------------------------------------------------

    @activity.defn
    async def prepare_agent_ask(self, task_id: str) -> dict:
        """The `ask` verb's input: which agent the task goes to, and what to say.

        The agent is the one the task is assigned to, found in the agent
        registry by its `mention_aliases` (`agents.metadata`, defaulting to the
        agent's id) — the lookup clarify's comment channel makes — so nothing
        here names an agent. The thread id is that channel's too, so a later
        reply on the task continues this conversation.

        An empty `agent_id` comes with `comment` (what a person does next) and
        `reason` (why the task parks).
        """
        from aegis_worker.activities.clarify import get_agent_registry

        nobody = {"agent_id": "", "message": "", "thread_id": "", "comment": "", "reason": ""}
        task = await self.load_task(task_id)
        if not task:
            return {**nobody, "reason": "the task is not in the Todoist mirror"}
        label = str(task.get("assignee_label") or "")
        registry = await get_agent_registry(self.db_pool)
        agent_id = next(
            (aid for aid in sorted(registry) if label and label in registry[aid].get("aliases", [])),
            "",
        )
        if not agent_id:
            named = label or "this task's label"
            return {
                **nobody,
                "comment": (
                    f"No active agent answers to {named}, so nobody here can pick this up. "
                    "Give the task to an agent that exists, or take it yourself (@me) and "
                    "complete it when it is done."
                ),
                "reason": f"no active agent answers to {named}",
            }
        return {
            "agent_id": agent_id,
            "message": _ask_message(task),
            "thread_id": f"todoist-task-{task_id}",
            "comment": "",
            "reason": "",
        }

    # --- the infra verb's plan (#344) ------------------------------------------

    @activity.defn
    async def plan_infra_task(self, task_id: str, title: str) -> dict:
        """What the infra verb can usefully do for this task.

        Decided from the problem behind the task (`hub.find_problem_for_task`);
        the title is read only when the hub has no problem for it. Before, the
        verb understood swarm services and nothing else: a Dagster failure ran
        `docker service ps dagster` and got a restart card for a service that
        does not exist, and every other kind parked with an apology — 85 of 110
        runs in the fortnight to 2026-09-11 did nothing.

        Returns one of two plans:

        * `{"action": "service", "service", "health"}` — a service the swarm
          runs. `health` is `service_health`'s answer; the flow completes a
          healthy one and cards a restart for an unhealthy one, as before.
        * `{"action": "report", "handler", "kind", "comment", "reason"}` —
          anything else. Every handler is read-only: it reads the hub, reads
          the heartbeat's last sample, or probes a URL once, and says what a
          person does next. The flow posts `comment` and parks with `reason`.

        Money problems keep the answer they had: they are Maou's (#497).
        """
        problem = (
            await hub.find_problem_for_task(self.db_pool, task_id)
            if self.db_pool is not None and task_id
            else None
        )
        if problem is None:
            return await self._plan_from_title(title, None, _NO_FACTS) or self._plan_manual(
                None, _NO_FACTS
            )
        facts = await self._problem_facts(problem["id"])
        kind = str(problem.get("subject_kind") or "")
        subject = str(problem.get("subject") or "")
        if "money" in facts["sources"]:
            return _report(
                "money",
                kind,
                [
                    f"This is a {kind} problem ({subject}); there is no service to check or "
                    "restart, so I have no automatic action for it."
                ],
                f"no automatic action for a {kind} problem",
            )
        if subject == hub.GROUP_SUBJECT or problem.get("group_key"):
            return await self._plan_group(problem, facts)
        if kind == "node" and subject:
            return await self._plan_node(problem, facts, subject)
        if facts["url"]:
            return await self._plan_endpoint(problem, facts, facts["url"])
        if facts["sources"] and facts["sources"] <= {"sentry"}:
            return self._plan_exception(problem, facts)
        if kind == "service" and subject:
            return await self._plan_service(subject, problem, facts)
        if kind in ("", hub.TASK_SUBJECT_KIND, "repo") or problem.get("class") == "manual":
            # The subject is the task itself, or nothing: the title may still
            # name a service or a node.
            return await self._plan_from_title(title, problem, facts) or self._plan_manual(
                problem, facts
            )
        return self._plan_kind(problem, facts)

    async def _problem_facts(self, problem_id: str) -> dict:
        """What the timeline says about a problem: who reported it, what the
        latest alert carried, and the investigation's latest finding."""
        sources = {
            r["source"]
            for r in await self.db_pool.fetch(
                "SELECT DISTINCT source FROM problem_events "
                "WHERE problem_id = $1::uuid AND kind = 'occurrence'",
                problem_id,
            )
        }
        payload = await self.db_pool.fetchval(
            "SELECT payload FROM problem_events WHERE problem_id = $1::uuid "
            "AND kind = 'occurrence' ORDER BY occurred_at DESC, id DESC LIMIT 1",
            problem_id,
        )
        # The closing verdict wins over a later "investigation started" line:
        # it is the finding a person acts on.
        verdict = await self.db_pool.fetchrow(
            "SELECT payload, occurred_at FROM problem_events WHERE problem_id = $1::uuid "
            "AND kind = 'investigation' "
            "ORDER BY (payload ? 'verdict') DESC, occurred_at DESC, id DESC LIMIT 1",
            problem_id,
        )
        payload = payload if isinstance(payload, dict) else {}
        labels = payload.get("labels") if isinstance(payload.get("labels"), dict) else {}
        instance = str(labels.get("instance") or "")
        return {
            "sources": sources,
            "alertname": str(labels.get("alertname") or ""),
            "url": instance if instance.startswith(("http://", "https://")) else "",
            "description": str(payload.get("description") or ""),
            "verdict": verdict,
        }

    async def _plan_service(self, service: str, problem: dict | None, facts: dict) -> dict:
        """A service the swarm runs goes to the flow's check; anything else is
        reported. `restart_service` would refuse a name the swarm does not run
        anyway, so the card it used to get could never have worked."""
        if self.infra_ops is None:
            health = {"found": False, "healthy": False, "detail": "no swarm connection wired"}
        else:
            health = await self.infra_ops.service_health(service)
        if health.get("found"):
            return {"action": "service", "service": service, "health": health,
                    "handler": "service", "kind": "service"}
        detail = str(health.get("detail") or "not found")
        return _report(
            "no_swarm_service",
            str((problem or {}).get("subject_kind") or "service"),
            [
                f"I can't check `{service}` as a swarm service ({detail}), so there is "
                "nothing here to restart and I have not offered a restart.",
                _alert_says(facts),
                _verdict_line(problem, facts),
                "What to do: act on what the investigation found, in the system that "
                "raised the alert, then complete this task.",
                _timeline_line(problem),
            ],
            f"{service} is not a swarm service I can check ({detail[:80]})",
        )

    async def _plan_node(self, problem: dict | None, facts: dict, node: str) -> dict:
        """The heartbeat's last sample of the node, from its own settings row.

        Never a Docker or SSH command: a node that is down cannot answer
        either, and the heartbeat already asks the swarm's managers every tick.
        """
        from aegis_worker.activities.homelab import HomelabActivities

        row = (
            await self.db_pool.fetchrow(
                "SELECT value, updated_at FROM settings WHERE key = $1",
                HomelabActivities._HEARTBEAT_STATE_KEY,
            )
            if self.db_pool is not None
            else None
        )
        value = row["value"] if row is not None and isinstance(row["value"], dict) else {}
        nodes = value.get("nodes") if isinstance(value.get("nodes"), dict) else {}
        status = str(nodes.get(node) or "")
        if row is None:
            seen = f"The heartbeat has no sample yet, so I can't say whether `{node}` is up."
        elif not status:
            seen = f"The heartbeat's last sample ({_at(row['updated_at'])}) does not list `{node}`."
        else:
            seen = f"Node `{node}`: the heartbeat's last sample ({_at(row['updated_at'])}) has it {status}."
        fails = int(value.get("fail_count") or 0)
        if fails:
            seen += (
                f" The heartbeat has failed to reach the swarm {fails} time(s) in a row "
                "since then, so that sample may be old."
            )
        if status == "Ready":
            todo = (
                "What to do: nothing, unless it drops again. The heartbeat resolves this "
                "problem, and that closes this task."
            )
            reason = f"node {node} is Ready again"
        else:
            todo = (
                "What to do: check the machine itself, power and network first, then "
                "Docker once it answers. The heartbeat resolves this problem when it sees "
                "the node Ready."
            )
            reason = f"node {node} is {status or 'not in the heartbeat'}; a person has to look at it"
        return _report(
            "node",
            "node",
            [
                seen,
                "I ran nothing against the node: a machine that is down cannot answer "
                "Docker or SSH.",
                _verdict_line(problem, facts),
                todo,
                await self._runbook(facts.get("alertname") or ""),
                _timeline_line(problem),
            ],
            reason,
        )

    async def _plan_endpoint(self, problem: dict, facts: dict, url: str) -> dict:
        """One GET against the URL the alert probes, and what that says."""
        probe = await _probe(url)
        if probe["ok"]:
            seen = f"{url} answered {probe['status']} in {probe['ms']} ms just now."
            todo = (
                "What to do: nothing, unless it fails again. The alert resolves on its "
                "own, and that closes this task."
            )
            reason = f"{url} answers again"
        else:
            seen = (
                f"{url} answered {probe['status']} just now."
                if probe["status"]
                else f"{url} did not answer just now: {probe['error']}."
            )
            todo = (
                "What to do: check the route to it (proxy, DNS, TLS certificate) and "
                "the service behind it."
            )
            reason = f"{url} still fails ({probe['status'] or probe['error'][:60]})"
        return _report(
            "endpoint",
            str(problem.get("subject_kind") or ""),
            [seen, _verdict_line(problem, facts), todo, _timeline_line(problem)],
            reason,
        )

    def _plan_exception(self, problem: dict, facts: dict) -> dict:
        """An error Sentry reported. A restart does not fix code or data, and a
        project that happens to share a swarm service's name would otherwise be
        found "healthy" and have its task closed."""
        subject = str(problem.get("subject") or "")
        return _report(
            "exception",
            str(problem.get("subject_kind") or ""),
            [
                f"This is an application error Sentry reported for `{subject}`, not a "
                "service that is down. A restart would not fix it, so I have not checked "
                "or offered one.",
                _alert_says(facts),
                _verdict_line(problem, facts),
                "What to do: fix the code or data the investigation points at, then "
                "complete this task.",
                _timeline_line(problem),
            ],
            "an application error; the fix is in the code or data",
        )

    async def _plan_group(self, problem: dict, facts: dict) -> dict:
        """One problem standing for many members: who they are, from the hub."""
        rows = await self.db_pool.fetch(
            "SELECT DISTINCT m FROM ("
            "  SELECT jsonb_array_elements_text(payload->'members') AS m FROM problem_events"
            "   WHERE problem_id = $1::uuid AND kind = 'state_change'"
            "     AND payload->>'action' = 'grouped' AND jsonb_typeof(payload->'members') = 'array'"
            "  UNION ALL"
            "  SELECT payload->>'member_subject' FROM problem_events"
            "   WHERE problem_id = $1::uuid AND kind = 'occurrence'"
            ") s WHERE m IS NOT NULL AND m <> '' ORDER BY m",
            problem["id"],
        )
        members = [r["m"] for r in rows]
        kind = str(problem.get("subject_kind") or "")
        listed = ", ".join(f"`{m}`" for m in members[:_GROUP_MEMBER_CAP])
        if len(members) > _GROUP_MEMBER_CAP:
            listed += f" and {len(members) - _GROUP_MEMBER_CAP} more"
        times = int(problem.get("occurrences") or 0)
        return _report(
            "group",
            kind,
            [
                f"This one problem stands for {len(members)} {kind or 'subject'}s with the "
                f"same failure (`{problem.get('class')}`): {listed or 'none recorded'}. "
                f"Seen {times} time(s) in all, last at {_at(problem.get('last_seen_at'))}.",
                _verdict_line(problem, facts),
                "What to do: one fix should clear all of them, so work it here. The "
                "members' own tasks were closed into this one, and a new member joins it "
                "instead of opening another.",
                _timeline_line(problem),
            ],
            f"a group of {len(members)} {kind}s; the shared fix is a person's call",
        )

    def _plan_kind(self, problem: dict, facts: dict) -> dict:
        """A kind of AEGIS's own (a flow, the comms probe, a post) or one this
        lane has no check for: the finding and what a person does."""
        kind = str(problem.get("subject_kind") or "")
        subject = str(problem.get("subject") or "")
        todo = _TODO_BY_KIND.get(kind) or (
            "What to do: act on what the investigation found, then complete this task. "
            f"This lane has no check of its own for a {kind or 'problem of this'} kind."
        )
        return _report(
            kind or "other",
            kind,
            [
                f"This is a {kind} problem (`{subject}`), not a swarm service, so there is "
                "nothing here to check or restart.",
                _alert_says(facts),
                _verdict_line(problem, facts),
                todo,
                _timeline_line(problem),
            ],
            f"a {kind} problem; the fix is a person's",
        )

    async def _plan_from_title(self, title: str, problem: dict | None, facts: dict) -> dict | None:
        """A service or node the title names, when the problem names none."""
        service = extract_service_name(title)
        if service:
            return await self._plan_service(service, problem, facts)
        node = extract_node_name(title)
        if node:
            return await self._plan_node(problem, facts, node)
        return None

    def _plan_manual(self, problem: dict | None, facts: dict) -> dict:
        return _report(
            "manual",
            str((problem or {}).get("subject_kind") or ""),
            [
                "Nothing on this task names a service, node or URL I can check.",
                _verdict_line(problem, facts),
                "What to do: reply on this task with what you want done, or do it "
                "yourself and complete the task.",
                _timeline_line(problem),
            ],
            "nothing on the task names something to check",
        )

    async def _runbook(self, alertname: str) -> str:
        """The runbook for this alert, cut short, when the deployment has one.

        Read through AlertActivities, which owns where runbooks live (the
        infra coding block's `runbooks_dir`, else the image's) and skips a
        stub. Best-effort: a missing runbook is a shorter comment, not a
        failure.
        """
        if not alertname or self.alert_act is None:
            return ""
        try:
            folder = await self.alert_act._effective_runbooks_dir()
            text = self.alert_act._read_runbook(alertname, folder)
        except Exception as exc:  # noqa: BLE001
            activity.logger.warning("agent_task_runbook_read_failed err=%s", str(exc)[:200])
            return ""
        return f"Runbook ({alertname}):\n{_cut(text, _RUNBOOK_CAP)}" if text else ""

    @activity.defn
    async def resolve_task_repo(self, task: dict) -> dict:
        """Resolve a coding task to a repo. Never guesses.

        Tier 1: Todoist project name -> the `project_repo_map` setting — the strongest
        signal, since a project already mirrors a repo.

        Tier 2 (only when tier 1 misses): reuse
        AlertActivities.resolve_alert_resource's title/description-matching
        tiers by synthesising an alert-shaped dict. `service` is deliberately
        left blank — a Todoist task has no alertmanager service label, so the
        (otherwise deterministic) sentry_project/service_match tiers correctly
        sit out rather than false-matching on an empty string, and `fingerprint`
        is a synthetic `task:<id>` that no knowledge-graph claim was ever
        written against, so the KG tier misses by construction too. Both fall
        through to the free-text/LLM tiers, which is the intent. A confident
        pick (>= _TIER2_CONFIDENCE_THRESHOLD) resolves exactly like tier 1.

        Tier 3: anything less confident is surfaced as `candidates` (never
        auto-applied) for the flow's Gate-0 confirm card — running a coding
        agent against the wrong checkout is worse than not running it.
        """
        empty = {"github_repo": "", "repo_path": "", "source": "none", "candidates": []}
        project_id = task.get("project_id")
        name = None
        if self.db_pool is not None and project_id:
            name = await self.db_pool.fetchval(
                "SELECT name FROM todoist_projects WHERE id = $1", project_id
            )
        mapping = await get_project_repo_map(self.db_pool) if self.db_pool is not None else {}
        github_repo = lookup(name, mapping)
        if github_repo:
            # repo_path is the workspace-relative checkout path start_kimi_run needs.
            # The JSONB key is `path`, NOT `resource_path`. `resource_path` is only an
            # application-level rename applied AFTER reading (alerts.py:521);
            # inventory.py:386-397 writes {"path", "github_repo", "origin_url"}.
            # Querying 'resource_path' always yields NULL, silently flattening a
            # nested checkout (stockopedia/bcp -> bcp) so start_kimi_run then hard-
            # fails with a false "Repo checkout missing".
            row = await self.db_pool.fetchrow(
                "SELECT metadata->>'path' AS rpath FROM resources "
                "WHERE kind = 'repository' AND metadata->>'github_repo' = $1 LIMIT 1",
                github_repo,
            )
            return {
                "github_repo": github_repo,
                "repo_path": (row["rpath"] if row and row["rpath"] else github_repo.split("/")[-1]),
                "source": "project_map",
                "candidates": [],
            }
        if name:
            activity.logger.info("agent_task_repo_unmapped project=%s", name)

        if self.alert_act is None:
            return empty
        synthetic_alert = {
            "title": str(task.get("content") or ""),
            "description": str(task.get("description") or ""),
            "fingerprint": f"task:{task.get('id') or ''}",
            "service": "",
        }
        try:
            resolved = await self.alert_act.resolve_alert_resource(synthetic_alert)
        except Exception as exc:  # noqa: BLE001 — tier 2 is best-effort; never guess on error
            activity.logger.warning("agent_task_repo_tier2_failed err=%s", str(exc)[:200])
            return empty

        # Candidate shape matches what _build_repo_confirm_prompt expects
        # (resource_title/github_repo/resource_path/score) so the flow's Gate-0
        # card can consume it unchanged. Drop any candidate with no github_repo —
        # nothing to check out, so it isn't a pickable option.
        candidates = [
            {
                "resource_title": r.get("resource_title") or "",
                "github_repo": r.get("github_repo") or "",
                "resource_path": r.get("resource_path") or "",
                "score": float(r.get("confidence") or 0.0),
            }
            for r in (resolved.get("resources") or [])
            if r.get("github_repo")
        ]
        tier2_repo = resolved.get("github_repo") or ""
        tier2_confidence = float(resolved.get("confidence") or 0.0)
        if tier2_repo and tier2_confidence >= _TIER2_CONFIDENCE_THRESHOLD:
            repo_path = resolved.get("resource_path") or tier2_repo.split("/")[-1]
            return {
                "github_repo": tier2_repo,
                "repo_path": repo_path,
                "source": "title_match",
                "candidates": [],
            }
        return {**empty, "candidates": candidates}

    # --- task sessions: one persistent coding session per @code task ---------

    @activity.defn
    async def load_task(self, task_id: str) -> dict:
        """The task plus the tail of its comment thread.

        The webhook path carries a task id and nothing else, so the flow loads
        the task here rather than trusting whatever a payload claimed. The
        thread rides along because it IS the session's context: notes come back
        oldest-first, the order a person reads them in and the order the turn
        prompt renders them.

        An unknown task is `{}`, not an error — it may have been deleted between
        the comment that woke us and this activity, and that reads as "nothing
        to do".
        """
        if self.db_pool is None or not task_id:
            return {}
        row = await self.db_pool.fetchrow(
            "SELECT id, content, description, labels, source_tag, project_id, assignee_label "
            "FROM todoist_tasks WHERE id = $1",
            task_id,
        )
        if row is None:
            return {}
        # `id` breaks the tie on `posted_at`: Todoist stamps a burst of notes
        # with the same second, and an unstable sort would reorder the
        # conversation between turns.
        notes = await self.db_pool.fetch(
            "SELECT content, posted_at FROM ("
            "  SELECT id, content, posted_at FROM todoist_notes WHERE item_id = $1"
            "  ORDER BY posted_at DESC, id DESC LIMIT $2"
            ") recent ORDER BY posted_at ASC, id ASC",
            task_id,
            _TASK_NOTE_LIMIT,
        )
        task = dict(row)
        task["labels"] = list(task["labels"] or [])
        task["notes"] = [
            {
                "content": note["content"] or "",
                # ISO strings rather than datetimes: the thread is rendered
                # straight into a prompt and echoed in the flow's result
                # summary, and both want one stable textual shape.
                "posted_at": note["posted_at"].isoformat() if note["posted_at"] else "",
            }
            for note in notes
        ]
        return task

    @activity.defn
    async def ensure_task_session(
        self, task_id: str, agent_id: str, task: dict, comment: str
    ) -> dict:
        """The task's session row, with a repo and a live worktree when known.

        Called before every turn. A row that already carries a repo skips the
        RESOLVER — re-resolving each turn would let a later LLM guess move a
        task to a different checkout mid-conversation — but still verifies its
        worktree, which is one idempotent SSH round trip. Without that check a
        tree removed out of band (a manual `git worktree remove`, a disk clean)
        would leave the row `ready` for ever while every turn launched into a
        directory that is not there.

        An unresolved task still gets its row. That row is what makes the NEXT
        comment reach the flow at all (the webhook keys on its existence), and
        it is how the operator answers: they name one of the returned
        `candidates` in a comment and the following turn matches it. There is
        no card and no guess — running an unattended coding session in the
        wrong checkout is worse than not running one.

        `set_repo` is deliberately the LAST step, after the worktree exists. A
        row carrying a repo short-circuits to `ready` for ever, so recording one
        whose worktree failed to build would wedge the task on a directory that
        is not there; leaving it empty makes the next turn retry.
        """
        empty: dict = {"status": "unresolved", "session": None, "candidates": [], "error": ""}
        if self.db_pool is None or not task_id:
            return {**empty, "error": "no database pool"}
        session = await work_sessions.create_session(
            self.db_pool, task_id=task_id, agent_id=agent_id
        )
        if self.remote_script is None:
            return {
                **empty,
                "session": session,
                "error": "remote_script connector is not configured",
            }
        if session.get("repo"):
            error = await self._build_task_worktree(
                repo=str(session["repo"]),
                worktree_path=str(session.get("worktree_path") or ""),
                branch=str(session.get("branch") or ""),
                host=str(session.get("host") or ""),
            )
            if error:
                return {**empty, "session": session, "error": error}
            return {"status": "ready", "session": session, "candidates": [], "error": ""}

        resolved = await self.resolve_task_repo(task or {})
        github_repo = str(resolved.get("github_repo") or "")
        repo_path = str(resolved.get("repo_path") or "")
        candidates = list(resolved.get("candidates") or [])
        if not repo_path:
            picked = match_repo_candidate(candidates, comment)
            if picked is None:
                return {
                    "status": "candidates" if candidates else "unresolved",
                    "session": session,
                    "candidates": candidates,
                    "error": "",
                }
            github_repo = str(picked.get("github_repo") or "")
            repo_path = str(picked.get("resource_path") or "") or github_repo.split("/")[-1]

        settings = await self.remote_script.coding_settings()
        host = str(settings.get("host") or "")
        # Sibling of the shared checkout, like the per-run worktrees, but keyed
        # on the TASK: turn 2 has to find turn 1's uncommitted work.
        worktree_path = (
            f"{str(settings.get('repo_base') or '').rstrip('/')}/{repo_path}"
            f"-aegis-wt/task-{task_id}"
        )
        branch = f"aegis-task/{task_id}"
        error = await self._build_task_worktree(
            repo=repo_path, worktree_path=worktree_path, branch=branch, host=host
        )
        if error:
            return {
                **empty,
                "session": session,
                "candidates": candidates,
                "error": error,
            }
        await work_sessions.set_repo(
            self.db_pool,
            task_id,
            repo=repo_path,
            github_repo=github_repo,
            worktree_path=worktree_path,
            branch=branch,
            host=host,
        )
        fresh = await work_sessions.get_session(self.db_pool, task_id)
        return {"status": "ready", "session": fresh or session, "candidates": [], "error": ""}

    async def _build_task_worktree(
        self, *, repo: str, worktree_path: str, branch: str, host: str
    ) -> str:
        """Create-or-verify the task's worktree. `""` on success, else the error.

        Idempotent by design on the connector side, so calling it before every
        turn costs one cheap SSH round trip and buys the self-heal: a worktree
        that disappeared is rebuilt on the same branch, with the task's
        committed work still on it.
        """
        built = await self.remote_script.ensure_task_worktree(
            repo=repo, worktree_path=worktree_path, branch=branch, host=host
        )
        if built.get("status") == "ready":
            return ""
        return str(built.get("error") or "the task worktree could not be created")

    @activity.defn
    async def check_task_collision(self, task_id: str, override: bool = False) -> dict:
        """Who is on this task right now: `proceed`, `you_are_in_it` or
        `turn_still_running`. A registry lookup, not an investigation.

        1. `turn_still_running` — the last turn AEGIS launched is still holding
           its output file open: an orphan the deadline kill did not reach.
           Launching `--resume` beside it would have two runs writing one
           session, so the comment waits for the sweep to re-dispatch it.
        2. `you_are_in_it` — an operator session reported itself `active` on
           the task inside the last `OPERATOR_LIVE_WINDOW` (via
           `report_progress`). The comment is already in front of them.
        3. `proceed` — everything else.

        `override` (the operator's `take over`) skips step 2 only: a person
        who is in the task and says "go" means it, but a comment cannot
        authorise driving over a turn of ours that is still running.

        EVERY failure path returns `proceed`. An unreadable registry or an
        unreachable host must not become an outage of the coding lane; the
        launch that follows fails on its own terms if the host is down.
        """
        proceed: dict = {"verdict": "proceed", "session": None, "reason": ""}
        if self.db_pool is None or not task_id:
            return {**proceed, "reason": "no database pool"}
        try:
            row = await work_sessions.get_session(self.db_pool, task_id) or {}
            output_file = str(row.get("last_output_file") or "")
            if output_file and self.remote_script is not None:
                try:
                    alive = await self.remote_script.kimi_run_alive(
                        output_file, host=str(row.get("last_host") or "")
                    )
                except Exception as exc:  # noqa: BLE001 — unknown is "not running"
                    activity.logger.warning(
                        "task_turn_probe_failed task_id=%s err=%s", task_id, str(exc)[:200]
                    )
                    alive = False
                if alive:
                    return {
                        "verdict": "turn_still_running",
                        "session": {
                            "owner": "aegis",
                            "session_id": str(row.get("session_id") or ""),
                            "name": f"task {task_id}",
                            "output_file": output_file,
                        },
                        "reason": f"the last turn is still writing {output_file}",
                    }
            if override:
                return {**proceed, "reason": "override"}
            live = await work_sessions.live_operator_sessions(self.db_pool, task_id)
            if live:
                sess = live[0]
                account = str(sess.get("account") or "operator")
                return {
                    "verdict": "you_are_in_it",
                    "session": {
                        "owner": "operator",
                        "session_id": str(sess.get("session_id") or ""),
                        "account": account,
                        "name": str(sess.get("summary") or "")[:80] or f"{account} session",
                        "summary": str(sess.get("summary") or ""),
                    },
                    "reason": f"operator session ({account}) active on the task",
                }
            return proceed
        except Exception as exc:  # noqa: BLE001 — see the docstring: fails open
            activity.logger.warning(
                "task_collision_check_failed task_id=%s err=%s", task_id, str(exc)[:200]
            )
            return {**proceed, "reason": f"check failed: {str(exc)[:200]}"}

    @activity.defn
    async def reconcile_work_sessions(self) -> dict:
        """The registry's liveness cross-check, run by the sweep.

        `report_progress` says a session is active; `claude agents --json` says
        whether it still exists. An active operator row whose session the
        host lists is touched, and one the host does not list that has gone
        quiet past `OPERATOR_LIVE_WINDOW` is parked, so the collision check
        and `task_context` stop reporting a session that ended without a
        final report. Fails open: with no inventory nothing is parked, and the
        window in `live_operator_sessions` still bounds the collision check.
        """
        if self.db_pool is None:
            return {"refreshed": 0, "parked": 0, "inventory": "no database pool"}
        live: list[str] = []
        status = "unavailable"
        if self.remote_script is not None:
            try:
                inventory = await self.remote_script.list_coding_sessions() or {}
                status = str(inventory.get("status") or "unavailable")
                if status == "ok":
                    live = [
                        str(s.get("session_id") or "")
                        for s in (inventory.get("sessions") or [])
                        if s.get("session_id")
                    ]
            except Exception as exc:  # noqa: BLE001
                activity.logger.warning("work_sessions_inventory_failed err=%s", str(exc)[:200])
        if status != "ok":
            return {"refreshed": 0, "parked": 0, "inventory": status}
        result = await work_sessions.reconcile_operator_sessions(self.db_pool, live)
        return {**result, "inventory": status}

    @activity.defn
    async def launch_task_turn(
        self,
        session: dict,
        prompt: str,
        agent_id: str,
        resume: bool,
        name: str,
        turn_timeout_minutes: int,
    ) -> dict:
        """Start one turn of the task's session. NOT idempotent — a retry is a
        second billed CLI session, so the flow launches this exactly once.

        Three arguments are what make this a TURN rather than a fresh run:
        `session_id` (the same conversation), `resume` (continue it instead of
        creating it) and `worktree_path` (the task's own tree, which the
        connector then neither creates nor removes). Drop any one and the result
        is a healthy-looking run with no memory of the last turn.

        The engine is forced to claude for every repo: only claude resumes a
        session, mounts the agent's AEGIS tools and can be taken over
        interactively with `claude --resume`.
        """
        failed: dict = {
            "status": "failed",
            "run_id": "",
            "output_file": "",
            "host": "",
            "engine": "",
            "tmux_window": "",
            "worktree_path": "",
            "error": "",
        }
        if self.remote_script is None:
            return {**failed, "error": "remote_script connector is not configured"}
        repo = str(session.get("repo") or "")
        if not repo:
            return {**failed, "error": "the task session has no repo yet"}

        settings = await self.remote_script.coding_settings()
        started = await self.remote_script.start_kimi_run(
            repo=repo,
            prompt=prompt,
            kimi_binary=settings.get("kimi_binary", ""),
            github_repo=str(session.get("github_repo") or ""),
            engine_override="claude",
            # The account the session was created under, once known. A resume
            # on another profile is a fresh, amnesiac session (the spec's
            # "silent wrong-profile resume"); an empty label lets the
            # connector resolve it from routing, and the answer is recorded.
            claude_account=str(session.get("account") or ""),
            agent_id=agent_id,
            session_id=str(session.get("session_id") or ""),
            resume=bool(resume),
            name=name,
            worktree_path=str(session.get("worktree_path") or ""),
            token_ttl_seconds=int(turn_timeout_minutes) * 60 + _TURN_TOKEN_GRACE_SECONDS,
        )
        if started.get("status") != "running":
            return {
                **failed,
                "run_id": started.get("run_id", ""),
                "engine": started.get("engine", ""),
                "error": str(started.get("error") or "")[:500],
            }

        from aegis_worker.activities.agent_run import _tmux_window_name

        engine = started.get("engine", "")
        run_id = started.get("run_id", "")
        # Remember WHERE this turn is writing and under WHICH account.
        # `check_task_collision` probes the file to tell an orphan of ours from
        # a finished turn; the next turn's `--resume` runs under the account,
        # and a resume on another profile is a fresh, amnesiac session.
        # Best-effort: the session is already running and this activity is
        # NO_RETRY, so raising here would strand a live turn nobody polls.
        task_id = str(session.get("task_id") or "")
        if self.db_pool is not None and task_id:
            try:
                await work_sessions.set_last_run(
                    self.db_pool,
                    task_id,
                    output_file=str(started.get("output_file") or ""),
                    host=str(started.get("host") or ""),
                    account=str(started.get("claude_account") or ""),
                    engine=str(engine or ""),
                )
            except Exception as exc:  # noqa: BLE001
                activity.logger.warning(
                    "task_last_run_not_recorded task_id=%s err=%s", task_id, str(exc)[:200]
                )
        return {
            "status": "running",
            "run_id": run_id,
            "output_file": started.get("output_file", ""),
            "host": started.get("host", ""),
            "engine": engine,
            # Same composition the connector's tmux launch uses, so the name we
            # hand the operator is the name they can attach to.
            "tmux_window": (
                _tmux_window_name(engine, repo, run_id) if started.get("in_tmux") else ""
            ),
            "worktree_path": started.get("worktree_path", ""),
            "error": "",
        }

    @activity.defn
    async def kill_task_turn(self, output_file: str, host: str) -> dict:
        """Kill whatever process still holds this turn's output file open.

        `killed: True` means the SSH round trip RAN — not that a process died.
        The remote `fuser` may be absent, or may have found nothing to kill
        because the turn had already exited. Treat it as "the kill was
        attempted" and keep polling for the exit; never as proof the run is
        gone.

        The tmux window is deliberately left for inspection. An orphan run still
        writing the same session while the next turn starts is worse than a lost
        turn, which is why this exists at all.
        """
        if self.remote_script is None or not output_file:
            return {"killed": False}
        return {"killed": bool(await self.remote_script.kill_run(output_file, host=host))}

    @activity.defn
    async def record_task_turn(self, task_id: str, launched: bool) -> dict:
        """Move the session's watermark past the comment this turn consumed.

        EVERY verdict bumps it, including the two that hand the task back to the
        operator: the comment has been dealt with, and the 15-minute fallback
        sweep would otherwise re-dispatch it for ever. Only a turn that actually
        launched a session counts towards `turns`.

        `recorded: False` means no session row matched — it was cleaned up (or
        never created) while the turn ran, so the watermark this claims to have
        moved does not exist.
        """
        if self.db_pool is None or not task_id:
            return {"recorded": False}
        moved = await work_sessions.record_turn(self.db_pool, task_id, launched=bool(launched))
        if not moved:
            activity.logger.warning("task_turn_not_recorded task_id=%s", task_id)
        return {"recorded": bool(moved)}

    @activity.defn
    async def set_task_slack_ref(self, task_id: str, ref: dict) -> dict:
        """Remember the root of the task's Slack thread.

        Written once, by the first task message that lands — every later
        message threads under it, and inbound replies are routed back to the
        task by matching on it. An empty ref is refused rather than stored: it
        would overwrite a working root with one that matches no thread, which
        is worse than having none.
        """
        if self.db_pool is None or not task_id or not ref:
            return {"stored": False}
        await work_sessions.set_slack_ref(self.db_pool, task_id, dict(ref))
        return {"stored": True}

    @activity.defn
    async def find_task_turns_due(self, limit: int = 20) -> list[dict]:
        """Sessions whose newest USER comment is newer than their last turn.

        The Todoist webhook is the fast path; this is the sweep's fallback for a
        missed one, so it keys on the session's own `last_turn_at` watermark and
        NOT on the flow cooldown — a comment must not wait six hours because the
        task ran recently.
        """
        if self.db_pool is None:
            return []
        return await work_sessions.find_turns_due(self.db_pool, limit)
