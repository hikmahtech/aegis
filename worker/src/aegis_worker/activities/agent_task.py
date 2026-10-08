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
from dataclasses import dataclass, field
from typing import Any

from aegis.errors import error_text
from aegis.services.agent_task_verbs import (
    DEFAULT_VERBS,
    UNTAGGED,
    VERBS,  # noqa: F401 — re-export: tests import it here
)
from aegis.services.agent_task_verbs import SETTINGS_KEY as VERBS_SETTING
from aegis.services.agent_task_verbs import merge as merge_verbs
from aegis.services.settings_store import get_setting
from temporalio import activity

# Assignee labels this flow will act on. @me is deliberately absent: a task the
# user has claimed is theirs to handle.
ADDRESSABLE_ASSIGNEES = ["@sebas", "@raphael", "@maou"]

# Reaching either of these removes a task from the eligible pool. Without that,
# the cooldown becomes an infinite slow loop over the same tasks.
PARK_LABEL = "@waiting"
# `#money`: Maou raises these and the user acts on them; no verb could act on one
# without guessing about the user's money. `#feeds` (#513): a feed that stopped
# fetching or publishing is the user's to fix or drop, and the research verb on
# one would research the feed's URL.
EXCLUDED_LABELS = ["@someday", PARK_LABEL, "#money", "#feeds"]

# How much of a task's comment thread `_load_task` reads (oldest first).
_TASK_NOTE_LIMIT = 30

# comment() retries in-activity rather than via a Temporal retry_policy, so the
# command uuid stays stable and the Sync API dedups. A parked task's comment is
# its ONLY user-visible explanation, and a transient Todoist http_503 silently
# lost both comments on this flow's first production tick (issue #159).
# ponytail: 3 attempts / 2s; worst case 4s of sleep inside comment's 60s
# TIMEOUT_STANDARD budget.
_COMMENT_ATTEMPTS = 3
_COMMENT_RETRY_SECONDS = 2

# source_tag → verb. source_tag is PRIMARY; @code is consulted only when
# source_tag IS NULL (i.e. the task is user-authored). Clarify put a stray
# @code label on a real #email task in prod, and treating that as a coding
# task would be nonsense. The coding verb itself only parks now: the
# development lane moved to the Development vertical (a2-development).
#
# The table itself — every tag AEGIS captures under, with a verb or an explicit
# None — is `DEFAULT_VERBS` in `aegis.services.agent_task_verbs`, with the
# lenient `merge` this lane reads the `agent_task_verbs` settings row through
# and the strict `validate` the admin Todoist page writes it through (#558).
# The names are re-exported here because this lane and its tests use them.


async def load_verbs(pool: Any) -> dict[str, str | None]:
    """The effective verb table. A failed read is the defaults, never an
    outage of the lane."""
    if pool is None:
        return dict(DEFAULT_VERBS)
    try:
        value = await get_setting(pool, VERBS_SETTING)
    except Exception as exc:  # noqa: BLE001 — routing must never break on a config read
        activity.logger.warning("agent_task_verbs_read_failed err=%s", error_text(exc))
        return dict(DEFAULT_VERBS)
    return merge_verbs(value)


def resolve_verb(task: dict, verbs: dict[str, str | None] | None = None) -> str:
    """Verb for a task: its source tag's, or `coding` for an untagged `@code` task
    (which the flow parks with a note: the coding lane left v1).

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


# --- the `ask` verb (#344) --------------------------------------------------

# How much of a thread or a description a message quotes. Per field, so one pasted stack trace cannot crowd out the rest.
_ASK_NOTE_LIMIT = 15
_ASK_NOTE_CAP = 800
_FIELD_CAP = 2000


def _cut(text: str, cap: int) -> str:
    value = (text or "").strip()
    return value if len(value) <= cap else value[:cap].rstrip() + " […]"


def _ask_message(task: dict) -> str:
    """The turn the sweep sends an agent when it hands over a task.

    Read-only is a product rule:
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


@dataclass
class AgentTaskActivities:
    db_pool: Any = None
    todoist_connector: Any = None
    gmail_accounts: list[str] = field(default_factory=list)
    # GmailActivities instance (for triage_email's apply_label calls). A plain
    # field, late-wired in __main__.py after GmailActivities is constructed.
    gmail_activities: Any = None

    @activity.defn
    async def find_actionable_tasks(
        self, max_tasks: int = 3, cooldown_hours: int = 6, max_coding: int = 0
    ) -> list[dict]:
        """Eligible agent-assigned tasks, oldest first, cooldown-filtered.

        `max_coding` is ignored. It was the coding lane's cap; a sweep recorded
        before the lane left v1 still passes it, so the parameter stays one
        release (`PATCH_DROP_CODING_SWEEP`).
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
            ORDER BY t.updated_at ASC
            LIMIT $4
            """,
            ADDRESSABLE_ASSIGNEES,
            EXCLUDED_LABELS,
            cooldown_hours,
            max_tasks,
        )
        out: list[dict] = []
        for row in rows:
            task = dict(row)
            task["labels"] = list(task["labels"] or [])
            out.append(task)
        return out

    @activity.defn
    async def load_task_context(self, task_id: str) -> dict:
        """What the flow needs to know about where a task came from.

        `todoist_capture_idempotency` links task → external_id with near-total
        coverage in prod (41/42 #alert, 30/30 #email). external_id is prefixed
        by source: `alert-<fingerprint>`, `gmail-<message_id>`, which is why
        the mail lane can read a message id back out of it.

        `subject` / `subject_kind` come from the problem behind the task.

        Every key here has a reader. The alert `fingerprint` this used to
        return was the pre-hub identity — nothing has looked one up since the
        problem hub replaced that lookup, and the problem id it also returned
        was never read either.

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
        # (`problems.subject`).
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
        flight. A re-arm restarts `created_at`, the clock drain_outbox's
        retry window runs on, or an old row would fail on its first retry."""
        await self.db_pool.execute(
            "INSERT INTO todoist_outbox (temp_id, command, status) "
            "VALUES ($1, $2, 'pending') "
            "ON CONFLICT (temp_id) DO UPDATE "
            "SET command = EXCLUDED.command, status = 'pending', attempt_count = 0, created_at = now() "
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
                last_error = error_text(exc)
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
        task = await self._load_task(task_id)
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

    # --- the retired infra verb ------------------------------------------------

    @activity.defn
    async def plan_infra_task(self, task_id: str, title: str) -> dict:
        """Stub for one release: the infra verb is gone (the lane moved to the
        DevOps vertical, a2-devops). Only an `AgentTaskFlow` run recorded
        before the change can schedule this (`PATCH_DROP_INFRA_VERB`); it gets a
        plan that parks the task with a note. Remove it with that branch."""
        return {
            "action": "report",
            "handler": "retired",
            "kind": "",
            "comment": (
                "AEGIS v1 no longer works infra tasks: the infra lane moved to the "
                "DevOps vertical. Handle this one yourself and complete the task."
            ),
            "reason": "infra lane moved to DevOps",
        }

    async def _load_task(self, task_id: str) -> dict:
        """The task plus the tail of its comment thread, oldest first — the
        order a person reads them in and the order `_ask_message` renders them.
        An unknown task is `{}`."""
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
        # with the same second, and an unstable sort would reorder the thread.
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
                "posted_at": note["posted_at"].isoformat() if note["posted_at"] else "",
            }
            for note in notes
        ]
        return task

    # --- the retired coding lane -----------------------------------------------

    @activity.defn
    async def reconcile_work_sessions(self) -> dict:
        """Stub for one release: the coding lane moved to the Development
        vertical (a2-development). Only an `AgentTaskSweepFlow` recorded before
        the change schedules this (`PATCH_DROP_CODING_SWEEP`). Remove it with
        that branch."""
        return {"refreshed": 0, "parked": 0, "inventory": "retired"}

    @activity.defn
    async def find_task_turns_due(self, limit: int = 20) -> list[dict]:
        """Stub for one release (`PATCH_DROP_CODING_SWEEP`): no turn is ever
        due now. Remove it with that branch."""
        return []
