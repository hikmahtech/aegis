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

from temporalio import activity

# Assignee labels this flow will act on. @me is deliberately absent: a task the
# user has claimed is theirs to handle.
ADDRESSABLE_ASSIGNEES = ["@sebas", "@raphael", "@maou", "@pandora"]

# Reaching either of these removes a task from the eligible pool. Without that,
# the cooldown becomes an infinite slow loop over the same tasks.
PARK_LABEL = "@waiting"
EXCLUDED_LABELS = ["@someday", PARK_LABEL]

# Upper bound on the eligible pool we consider per tick. Production's whole
# agent-assigned backlog is ~80 rows, so this is the pool, not a sample.
# ponytail: fixed bound; move both caps into SQL if the pool ever nears it.
_ELIGIBLE_SCAN_LIMIT = 200

# source_tag → verb. source_tag is PRIMARY; @code is consulted only when
# source_tag IS NULL (i.e. the task is user-authored). Clarify put a stray
# @code label on a real #email task in prod, and treating that as "run a
# coding agent on this email" would be nonsense.
_VERB_BY_SOURCE_TAG = {
    "#alert": "infra",
    "#receipt": "finance",
    "#email": "email",
}

# Swarm service names as they appear in real prod alert titles.
_SERVICE_PATTERNS = (
    re.compile(r"^PROLONGED:\s+(\S+)\s+degraded", re.I),
    re.compile(r"^Service\s+(\S+)\s+has\s+fewer\s+tasks", re.I),
    re.compile(r"^([A-Za-z][\w.-]*)\s+is\s+down\b", re.I),
)


def resolve_verb(task: dict) -> str:
    """Verb for a task: from source_tag, or @code when source_tag is NULL."""
    source_tag = task.get("source_tag")
    if source_tag:
        return _VERB_BY_SOURCE_TAG.get(source_tag, "unknown")
    if "@code" in (task.get("labels") or []):
        return "coding"
    return "unknown"


def extract_service_name(title: str) -> str:
    """Swarm service named by an alert title, or '' when none is."""
    text = (title or "").strip()
    for pattern in _SERVICE_PATTERNS:
        match = pattern.match(text)
        if match:
            return match.group(1).lower() if "_" not in match.group(1) else match.group(1)
    return ""


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

    @activity.defn
    async def find_actionable_tasks(
        self, max_tasks: int = 3, cooldown_hours: int = 6, max_coding: int = 1
    ) -> list[dict]:
        """Eligible agent-assigned tasks, oldest first, cooldown-filtered.

        `max_coding` caps coding tasks (source_tag IS NULL + @code) within the
        batch — a kimi run takes minutes and the coding host's tmux window cap
        is 10, so an uncapped fan-out would wedge it.
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
        """Recover the source identity a task was captured from.

        `todoist_capture_idempotency` links task → external_id with near-total
        coverage in prod (41/42 #alert, 30/30 #email). external_id is prefixed
        by source: `alert-<fingerprint>`, `gmail-<message_id>`.
        """
        empty = {"external_id": "", "fingerprint": "", "gmail_message_id": ""}
        if self.db_pool is None or not task_id:
            return empty
        external_id = await self.db_pool.fetchval(
            "SELECT external_id FROM todoist_capture_idempotency "
            "WHERE todoist_task_ref = $1 ORDER BY captured_at DESC LIMIT 1",
            task_id,
        )
        if not external_id:
            return empty
        return {
            "external_id": external_id,
            "fingerprint": external_id[len("alert-") :] if external_id.startswith("alert-") else "",
            "gmail_message_id": (
                external_id[len("gmail-") :] if external_id.startswith("gmail-") else ""
            ),
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
        cmd = TodoistConnector.build_note_add_command(task_id, content)
        try:
            result = await self.todoist_connector.commands([cmd])
            status = TodoistConnector.check_sync_status(result, [cmd["uuid"]])
        except Exception as exc:  # noqa: BLE001 — comments are best-effort
            activity.logger.warning("agent_task_comment_failed err=%s", str(exc)[:200])
            return {"ok": False, "error": str(exc)[:200]}
        if status["ok"]:
            return {"ok": True, "error": None}
        if status["envelope_error"]:
            activity.logger.warning(
                "agent_task_comment_envelope_failed task_id=%s error=%s",
                task_id,
                str(status["envelope_error"])[:200],
            )
            return {"ok": False, "error": status["envelope_error"]}
        rejected = status["rejected"].get(cmd["uuid"])
        activity.logger.warning(
            "agent_task_comment_rejected task_id=%s status=%s",
            task_id,
            str(rejected)[:200],
        )
        return {"ok": False, "error": f"command_rejected: {rejected}"}

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

        restart = await self.infra_ops.restart_service(service)
        # `restart_service` runs `docker service update --force --detach`
        # (connectors/homelab.py:175) and returns BEFORE the swarm converges, so
        # a single immediate health check would essentially never see recovery.
        # The sibling `remediate_infra_service` (alerts.py:1220-1252) polls 6x5s
        # for exactly this reason — but this runs as InteractionFlow's
        # post_resolve activity, which has a hard 30s timeout
        # (flows/interaction.py:67), so budget the poll well inside it.
        # ponytail: 5x4s=20s; if swarm convergence is routinely slower, move the
        # verification out of the hook and let the next sweep tick observe it.
        health = {"healthy": False, "detail": "not checked"}
        if restart.get("ok"):
            for _ in range(5):
                await asyncio.sleep(4)
                health = await self.infra_ops.service_health(service)
                if health.get("healthy"):
                    break
        if restart.get("ok") and health.get("healthy"):
            await self.comment(
                task_id, agent_id, f"Restarted `{service}` and it's healthy again — closing."
            )
            await self.complete_task(task_id)
        else:
            await self.comment(
                task_id,
                agent_id,
                f"Restarted `{service}` but it hadn't come back healthy within 20s "
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
