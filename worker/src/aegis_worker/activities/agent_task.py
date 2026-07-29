"""AgentTaskActivities — execute AEGIS's own agent-assigned Todoist tasks.

Every one of the 80 agent-assigned tasks in prod is AEGIS's own triage output
(source_tag #alert/#email/#receipt), not a user delegation, and NONE has a due
date. So eligibility deliberately does not require one — requiring a date would
keep this flow permanently idle. The brake is instead: a small cap per tick,
oldest first, and a per-task cooldown.
"""

from __future__ import annotations

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
