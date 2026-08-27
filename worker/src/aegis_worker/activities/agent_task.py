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

from aegis.connectors.coding_sessions import busy_human_sessions
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
            # Compose-style names (`redis_redis`) already come out of the
            # PROLONGED/fewer-tasks patterns lowercase and underscore-joined —
            # don't touch them. Only the free-text "X is down" pattern needs
            # normalising, since that title can carry whatever casing a human
            # or another system used.
            return match.group(1).lower() if "_" not in match.group(1) else match.group(1)
    return ""


# #receipt task title shapes, as clarify actually produces them in prod.
_MERCHANT_PATTERNS = (
    re.compile(r"^Anomaly:\s*\?\s*(.+?)\s*$", re.I),
    re.compile(r"^Anomaly:\s*[\d.,]+\s+\w+\s+(.+?)\s*$", re.I),
    re.compile(r"^Renewal in [\d.]+ days:\s*(.+?)\s*\([^)]*\)\s*$", re.I),
)


def extract_merchant(title: str) -> str:
    """Merchant named by a #receipt task title, or '' when none is."""
    text = (title or "").strip()
    for pattern in _MERCHANT_PATTERNS:
        match = pattern.match(text)
        if match:
            return match.group(1).strip()
    return ""


# Todoist project name → GitHub repo. The projects already mirror repos, which
# is a far stronger signal than guessing from a task title.
PROJECT_REPO_MAP = {
    "bcp": "Stockopedia/bcp",
    "aegis": "hikmahtech/aegis",
    "home infra": "hikmahtech/homelab-gitops",
    "drwho": "hikmahtech/drwhome",
}

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
        """Prior charges for the merchant this task names.

        The value of this verb is assembled context, not an autonomous
        decision — whether a charge is legitimate is the user's call.
        """
        merchant = extract_merchant(title)
        if not merchant or self.db_pool is None:
            return {"merchant": "", "charges": [], "summary": ""}
        # `recurring_charge` is UPSERT-keyed on
        # (account, sender_label, amount_cents, currency) — one row per charge
        # SIGNATURE with last_seen_at bumped in place (money.py:250-252) — so a
        # merchant billing a steady amount has exactly ONE row and no history.
        # `receipt_email` IS append-only (one row per receipt, unique on
        # message_id), so join through its charge_id FK for the canonical vendor
        # and read each receipt's own amount from the `parsed` extraction.
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
        summary = (
            "; ".join(f"{c['amount']:g} {c['currency']} on {c['last_seen_at'][:10]}" for c in charges)
            or "no prior charges on record"
        )
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

    @activity.defn
    async def resolve_task_repo(self, task: dict) -> dict:
        """Resolve a coding task to a repo. Never guesses.

        Tier 1: Todoist project name -> PROJECT_REPO_MAP — the strongest
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
        github_repo = PROJECT_REPO_MAP.get(str(name).strip().lower(), "") if name else ""
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

    @activity.defn
    async def run_task_investigation(
        self,
        task_id: str,
        title: str,
        description: str,
        repo_path: str,
        github_repo: str,
    ) -> dict:
        """Read-only coding-CLI run: understand the task and propose a plan.

        Phase 1 of two. Investigating first means a misread task or wrong repo
        costs nothing. MUST NOT write code — the prompt says so and the plan
        card gates phase 2.
        """
        if self.remote_script is None or not repo_path:
            return {"status": "failed", "transcript": "", "run_id": ""}

        # Entry point of the coding lane: defer to a human already busy in this
        # repo. The FLOW must not park on this path — parking stamps @waiting,
        # which would retire the task over a transient collision.
        busy = await busy_human_sessions(self.remote_script, repo_path)
        if busy:
            activity.logger.warning(
                "coding_run_skipped_repo_busy repo=%s sessions=%s",
                repo_path,
                [s.get("name") for s in busy],
            )
            return {
                "status": "skipped",
                "reason": "repo_busy",
                "transcript": "",
                "run_id": "",
            }

        prompt = (
            "You are investigating a task. Do NOT modify any files, do NOT commit, "
            "do NOT create branches.\n\n"
            f"Task: {title}\n\n{description}\n\n"
            "Read the code and report:\n"
            "1. What the task is actually asking for.\n"
            "2. Which files would need to change.\n"
            "3. A short implementation plan.\n"
            "4. Anything that makes this ambiguous or risky.\n\n"
            "End your final message with exactly one of:\n"
            "     STATUS: scoped\n"
            "     STATUS: unactionable: <why>\n"
        )
        settings = await self.remote_script.coding_settings()
        started = await self.remote_script.start_kimi_run(
            repo=repo_path,
            prompt=prompt,
            kimi_binary=settings.get("kimi_binary", ""),
            github_repo=github_repo,
        )
        if started.get("status") != "running":
            return {
                "status": "failed",
                "transcript": started.get("error", "")[:500],
                "run_id": started.get("run_id", ""),
            }
        return {
            "status": "running",
            "transcript": "",
            "run_id": started.get("run_id", ""),
            "output_file": started.get("output_file", ""),
            "host": started.get("host", ""),
            "worktree_path": started.get("worktree_path", ""),
        }

    @activity.defn
    async def collect_coding_run(
        self, output_file: str, host: str, max_polls: int = 40
    ) -> dict:
        """Poll a coding run until its STATUS footer appears, then extract text.

        Returns the ASSISTANT TRANSCRIPT, never the raw stream-json: handing raw
        jsonl to an LLM is what made every prod verdict confidence=0.0 with
        `{"role":"tool"` in its root_cause (fixed in #150).
        """
        from aegis_worker.activities.alerts import (
            _INVESTIGATION_OUTPUT_CAP,
            _extract_kimi_transcript,
            _kimi_output_complete,
        )

        if self.remote_script is None or not output_file:
            return {"status": "failed", "transcript": ""}

        latest = ""
        for _ in range(max_polls):
            raw = await self.remote_script.fetch_kimi_run_output(output_file, host=host)
            if raw:
                latest = raw
                if _kimi_output_complete(raw):
                    return {
                        "status": "succeeded",
                        "transcript": _extract_kimi_transcript(raw)[-_INVESTIGATION_OUTPUT_CAP:],
                    }
            # heartbeat() RAISES RuntimeError("Not in activity context") outside a
            # Worker, unlike activity.logger which degrades silently. The unit
            # tests call this method directly, so guard it the way comment()
            # already does in this same file.
            if activity.in_activity():
                activity.heartbeat()
            await asyncio.sleep(30)

        return {
            "status": "timed_out",
            "transcript": (
                _extract_kimi_transcript(latest)[-_INVESTIGATION_OUTPUT_CAP:] if latest else ""
            ),
        }

    @activity.defn
    async def run_task_implementation(
        self,
        task_id: str,
        title: str,
        description: str,
        plan: str,
        repo_path: str,
        github_repo: str,
    ) -> dict:
        """Phase 2: implement the approved plan on a branch. Does NOT open a PR.

        Opening the PR is a separate gated step, so a bad implementation stays
        local and reviewable rather than becoming a PR nobody asked for.
        """
        if self.remote_script is None or not repo_path:
            return {"status": "failed", "transcript": "", "branch": "", "run_id": ""}

        branch = f"aegis-task/{task_id}"
        prompt = (
            "Implement the approved plan below. Commit your work to a new branch "
            f"named exactly `{branch}`. Do NOT open a pull request.\n\n"
            f"Task: {title}\n\n{description}\n\n"
            f"Approved plan:\n{plan}\n\n"
            "End your final message with exactly one of:\n"
            f"     BRANCH: {github_repo.split('/')[-1]}:{branch}\n"
            "     STATUS: implemented\n"
            "   or STATUS: unactionable: <why>\n"
        )
        settings = await self.remote_script.coding_settings()
        started = await self.remote_script.start_kimi_run(
            repo=repo_path,
            prompt=prompt,
            kimi_binary=settings.get("kimi_binary", ""),
            github_repo=github_repo,
        )
        if started.get("status") != "running":
            return {
                "status": "failed",
                "transcript": started.get("error", "")[:500],
                "branch": "",
                "run_id": started.get("run_id", ""),
            }
        return {
            "status": "running",
            "transcript": "",
            "branch": branch,
            "run_id": started.get("run_id", ""),
            "output_file": started.get("output_file", ""),
            "host": started.get("host", ""),
        }
