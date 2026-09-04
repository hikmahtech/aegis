"""CaptureActivities — shared Todoist capture helper.

Every ingest flow reaches Todoist through `_capture`: `capture_to_inbox` for
the managed Inbox, `capture_task` for any project with an optional due date,
`capture_due` for a books bill or failed payment (spec §7.1). The helper owns:

- kill switch read (settings.todoist_capture_enabled)
- inbox project lookup (settings.todoist_managed_project_ids['inbox']) when
  the caller named no project
- per-source dedup (todoist_capture_idempotency)
- Sync API command build via TodoistConnector
- outbox fallback on retryable failure

Returns the Todoist task ref (real id, or temp_id while outbox is draining),
or None if the capture was skipped (kill switch off, no inbox project,
empty title).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import asyncpg
from temporalio import activity

from aegis_worker.shared.todoist_write import submit_or_queue


@dataclass
class CaptureActivities:
    db_pool: asyncpg.Pool | None
    connector: Any  # TodoistConnector at runtime; Any for unit tests
    # entity -> Todoist project id for books dues (spec §10). Empty = the Inbox.
    todoist_projects: dict[str, str] = field(default_factory=dict)

    async def _capture(
        self,
        source_tag: str,
        external_id: str,
        title: str,
        description: str | None,
        labels: list[str] | None,
        project_id: str | None,
        due_date: str | None,
    ) -> str | None:
        """The capture body shared by every public entry point.

        `labels`: extra labels beyond the source tag. AlertInvestigationFlow
        passes ["@pandora"] so the task is born already-clarified —
        ClarifyFlow's find_unclassified_items skips it (last_clarified_at is
        bumped after the item_add) and even if the row predates that bump, the
        explicit @pandora ownership marker tells the clarify short-circuit to
        leave it alone.

        `project_id`: None means the managed Inbox; an explicit id (a books
        entity project, spec §7.1) skips the Inbox lookup entirely, so a
        deployment that captures dues into real projects is not held hostage
        by an unconfigured Inbox id.
        """
        if self.db_pool is None or self.connector is None:
            return None
        if not title:
            activity.logger.warning(
                "capture_skipped_empty_title source=%s ext=%s", source_tag, external_id
            )
            return None

        async with self.db_pool.acquire() as conn:
            # Kill switch
            kill = await conn.fetchval(
                "SELECT value FROM settings WHERE key = 'todoist_capture_enabled'"
            )
            if kill is False or (isinstance(kill, dict) and kill.get("value") is False):
                return None
            # When the seed inserted 'true' as a bare boolean JSONB scalar,
            # asyncpg returns True. Any other shape we treat as enabled
            # unless explicitly false above.

            # Inbox project id — only when the caller named no project.
            if project_id is None:
                managed = await conn.fetchval(
                    "SELECT value FROM settings WHERE key = 'todoist_managed_project_ids'"
                )
                inbox_id = (managed or {}).get("inbox") if isinstance(managed, dict) else None
                if not inbox_id:
                    activity.logger.warning(
                        "capture_skipped_no_inbox_id source=%s ext=%s", source_tag, external_id
                    )
                    return None
                project_id = inbox_id

            # Dedup insert. On conflict, fetch the existing ref.
            inserted = await conn.fetchval(
                """
                INSERT INTO todoist_capture_idempotency (source_tag, external_id)
                VALUES ($1, $2)
                ON CONFLICT DO NOTHING
                RETURNING captured_at
                """,
                source_tag,
                external_id,
            )
            if inserted is None:
                existing = await conn.fetchval(
                    "SELECT todoist_task_ref FROM todoist_capture_idempotency "
                    "WHERE source_tag = $1 AND external_id = $2",
                    source_tag,
                    external_id,
                )
                activity.logger.info(
                    "capture_dedup_hit source=%s ext=%s existing_ref=%s",
                    source_tag,
                    external_id,
                    existing,
                )
                return existing

        # Build the item_add command
        from aegis.connectors.todoist import TodoistConnector

        item_labels = [source_tag]
        if labels:
            # Dedup-preserving merge — Todoist tolerates dupes but
            # downstream label-set comparisons get noisy.
            for lbl in labels:
                if lbl and lbl not in item_labels:
                    item_labels.append(lbl)

        cmd = TodoistConnector.build_create_item_command(
            project_id=project_id,
            content=title[:120],
            description=description,
            labels=item_labels,
            due_date=due_date,
        )

        # Submit
        result = await self.connector.commands([cmd])
        status = TodoistConnector.check_sync_status(result, [cmd["uuid"]])
        ref: str | None = None
        if status["ok"]:
            mapping = (result.get("data") or {}).get("temp_id_mapping", {}) or {}
            ref = mapping.get(cmd["temp_id"])
        elif status["retryable"] or status["rejected_retryable"]:
            # Transient failure (5xx / timeout / rate-limit) — stage in outbox
            # so drain_outbox can retry. Permanent rejections (ITEM_NOT_FOUND,
            # INVALID_ARGUMENT, etc.) skip the outbox: replaying would just
            # fail again and burn five wasted attempts per call.
            async with self.db_pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO todoist_outbox (temp_id, command, status) "
                    "VALUES ($1, $2, 'pending') ON CONFLICT (temp_id) DO NOTHING",
                    cmd["temp_id"],
                    cmd,
                )
            ref = cmd["temp_id"]
            activity.logger.warning(
                "capture_outbox_staged source=%s ext=%s temp_id=%s error=%s",
                source_tag,
                external_id,
                ref,
                status["envelope_error"] or str(status["rejected"])[:200],
            )
        else:
            # Permanent rejection — leave ref=None so the idempotency row
            # records the attempt but no Todoist task ref. Caller decides
            # how to surface the failure.
            activity.logger.warning(
                "capture_rejected_nonretryable source=%s ext=%s envelope_err=%s rejected=%s",
                source_tag,
                external_id,
                status["envelope_error"],
                str(status["rejected"])[:200],
            )

        # Backfill the idempotency row with whatever ref we have.
        async with self.db_pool.acquire() as conn:
            await conn.execute(
                "UPDATE todoist_capture_idempotency SET todoist_task_ref = $1 "
                "WHERE source_tag = $2 AND external_id = $3",
                ref,
                source_tag,
                external_id,
            )

        activity.logger.info(
            "capture_emitted source=%s ext=%s ref=%s", source_tag, external_id, ref
        )
        return ref

    @activity.defn
    async def capture_to_inbox(
        self,
        source_tag: str,
        external_id: str,
        title: str,
        description: str | None = None,
        extra_labels: list[str] | None = None,
    ) -> str | None:
        """Idempotent Inbox capture. See module docstring."""
        return await self._capture(
            source_tag, external_id, title, description, extra_labels, None, None
        )

    @activity.defn
    async def capture_task(
        self,
        source_tag: str,
        external_id: str,
        title: str,
        description: str | None = None,
        labels: list[str] | None = None,
        project_id: str | None = None,
        due_date: str | None = None,
    ) -> str | None:
        """Idempotent capture into any project with an optional due date (spec §7.1)."""
        return await self._capture(
            source_tag, external_id, title, description, labels, project_id, due_date
        )

    @activity.defn
    async def capture_due(self, event: dict, mailbox: str, message_id: str) -> str | None:
        """A bill, statement, autopay reminder or failed payment → one dated task (spec §7.1).

        Dedupe key is `(payee_key, due_on)`, not the Gmail message id: a biller
        that re-sends the same statement must not mint a second task.

        No GTD state label. The due date is what surfaces the task, and
        stacking `@next` on a dated item is exactly the drift the Next Actions
        filter exists to catch.
        """
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo

        from aegis.api.models.money import MoneyEvent
        from aegis.services.money_format import fmt_money

        ev = MoneyEvent(**{k: v for k, v in event.items() if not k.startswith("_")})
        if ev.kind not in ("due", "failed") or ev.due_on is None or ev.amount is None:
            return None
        today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
        # A day of warning, but never a task that is born overdue.
        due = max(ev.due_on - timedelta(days=1), today)
        prefix = "Fix payment:" if ev.kind == "failed" else "Pay"
        title = f"{prefix} {ev.payee} {fmt_money(ev.amount, ev.currency or '')}"
        description = (
            f"Due {ev.due_on.isoformat()}\n{ev.channel} · {ev.instrument or '-'}\n"
            f"{mailbox} · gmail {message_id}"
        )
        return await self._capture(
            "#bill",
            f"{ev.payee_key}:{ev.due_on.isoformat()}",
            title,
            description,
            None,
            self.todoist_projects.get(ev.entity),
            due.isoformat(),
        )

    @activity.defn
    async def complete_captured_task(self, task_ref: str) -> bool:
        """Close a captured task (a due that got paid). False for an unresolved temp ref.

        An `item-…` ref is a temp_id whose item_add is still sitting in the
        outbox — Todoist has never seen that id, so completing it would be
        rejected. The caller keeps the due open and retries after the drain.
        """
        if not task_ref or task_ref.startswith("item-") or self.connector is None:
            return False
        from aegis.connectors.todoist import TodoistConnector

        cmd = TodoistConnector.build_item_complete_command(task_ref)
        result = await self.connector.commands([cmd])
        status = TodoistConnector.check_sync_status(result, [cmd["uuid"]])
        if status["ok"]:
            return True
        if (status["retryable"] or status["rejected_retryable"]) and self.db_pool is not None:
            # item_complete carries no temp_id, so the command uuid is the
            # outbox key. drain_outbox replays `command` verbatim and only
            # reads temp_id to look up a created id, which this has none of.
            async with self.db_pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO todoist_outbox (temp_id, command, status) VALUES ($1, $2, 'pending') "
                    "ON CONFLICT (temp_id) DO NOTHING",
                    cmd["uuid"],
                    cmd,
                )
            return True
        activity.logger.warning(
            "complete_captured_task_rejected ref=%s err=%s", task_ref, status["envelope_error"]
        )
        return False

    @activity.defn
    async def link_email_to_task(self, msg: dict, body: str = "") -> dict:
        """Apply the first matching ``email_task_links`` rule to an existing task.

        The mirror of ``capture_to_inbox``: instead of creating a task from an
        email, this changes the state of one AEGIS already tracks — closing the
        Todoist row for a Jira ticket the mail says was resolved, or unparking a
        ``@waiting`` task whose reply just arrived.

        Best-effort. It runs on every triaged email, so every failure returns
        ``{"applied": False, "reason": ...}`` rather than raising.
        """
        from aegis.connectors.todoist import TodoistConnector
        from aegis.services.email_task_links import (
            UNBLOCK_ADD,
            UNBLOCK_REMOVE,
            blocks_unblock,
            get_email_task_links,
            match_link,
            task_key_pattern,
        )

        if self.db_pool is None or self.connector is None:
            return {"applied": False, "reason": "no_pool"}

        links = await get_email_task_links(self.db_pool)
        if not links:
            return {"applied": False, "reason": "no_rules"}

        hit = match_link(links, msg.get("subject") or "", body)
        if hit is None:
            return {"applied": False, "reason": "no_match"}

        # Only open tasks are candidates, which is also what makes a re-run safe:
        # a second pass over the same email finds nothing left to close.
        async with self.db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, content, labels FROM todoist_tasks "
                "WHERE NOT is_completed AND content ~ $1 "
                "ORDER BY updated_at DESC LIMIT 1",
                task_key_pattern(hit["task_key"]),
            )
        if row is None:
            activity.logger.info(
                "email_task_link_no_open_task rule=%s key=%s", hit["key"], hit["task_key"]
            )
            return {"applied": False, "reason": "no_open_task", "task_key": hit["task_key"]}

        task_id = row["id"]
        action = hit["action"]

        # `@waiting` on an agent-assigned task is agent_task.PARK_LABEL — "this
        # pass is done", not "blocked on a human". Stripping it re-enters the
        # task into find_actionable_tasks and recreates the cooldown loop that
        # parking exists to prevent. Refuse rather than guess; the audit note is
        # skipped too, because there is nothing to report to the human.
        if action == "unblock" and (agent_label := blocks_unblock(list(row["labels"] or []))):
            activity.logger.info(
                "email_task_link_unblock_skipped_agent_parked rule=%s task=%s label=%s",
                hit["key"],
                task_id,
                agent_label,
            )
            return {
                "applied": False,
                "reason": "agent_parked",
                **hit,
                "task_id": task_id,
                "agent_label": agent_label,
            }

        subject = (msg.get("subject") or "(no subject)")[:150]
        permalink = msg.get("permalink") or ""
        marker = {"complete": "✅ Closed by email", "unblock": "▶️ Unblocked by email"}.get(
            action, "📬 Email"
        )
        note = f"{marker}: {subject}"
        if permalink:
            note += f"\n{permalink}"

        cmds = [TodoistConnector.build_note_add_command(task_id, note)]
        if action == "complete":
            cmds.append(TodoistConnector.build_item_complete_command(task_id))
        elif action == "unblock":
            labels = [lab for lab in (row["labels"] or []) if lab != UNBLOCK_REMOVE]
            if UNBLOCK_ADD not in labels:
                labels.append(UNBLOCK_ADD)
            cmds.append(TodoistConnector.build_item_update_command(task_id, labels=labels))

        res = await submit_or_queue(
            self.db_pool, self.connector, cmds, f"email_task_link:{hit['key']}"
        )
        if not res["ok"]:
            if res["queued"]:
                return {"applied": True, "queued": True, **hit, "task_id": task_id}
            return {"applied": False, "reason": "rejected", **hit, "task_id": task_id}

        activity.logger.info(
            "email_task_link_applied rule=%s action=%s key=%s task=%s title=%s",
            hit["key"],
            action,
            hit["task_key"],
            task_id,
            (row["content"] or "")[:80],
        )
        return {"applied": True, "queued": False, **hit, "task_id": task_id}
