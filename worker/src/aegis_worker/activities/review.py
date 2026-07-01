"""ReviewActivities — Phase 5 GTD daily + weekly review digests.

DailyReviewFlow + WeeklyReviewFlow gather projection counts, format a
Telegram-safe digest, send via DeliveryActivities, optionally spawn an
InteractionFlow child for acknowledgement, and log to
review_digest_log (migration 014).

See docs/superpowers/specs/2026-05-20-gtd-todoist-phase5-reviews-design.md.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

import asyncpg
from aegis.llm import parse_llm_json
from temporalio import activity

# review-copilot thresholds; override live via settings key 'review_config'.
# ponytail: code default, no migration — tune with PUT /api/settings/review_config.
_REVIEW_DEFAULTS = {
    "waiting_days": 7,
    "next_actions_days": 14,
    "someday_resurface_days": 90,
    "top_n": 5,
}
# State labels mark a task non-actionable (parked/delegated/reference).
_STATE_LABELS = ["@waiting", "@reference", "@to-read"]


@dataclass
class ReviewActivities:
    db_pool: asyncpg.Pool | None
    # Optional Temporal host (e.g. 'aegis_temporal:7233'). When set,
    # apply_review_acknowledgement uses it to schedule a delayed
    # DailyReviewFlow re-fire on the 'need_time' / 'snooze' user choice.
    temporal_host: str | None = None
    task_queue: str = "aegis-main"
    llm_client: object | None = None
    frame_model: str = "gpt-oss:20b"
    todoist_connector: object | None = None

    @activity.defn
    async def gather_daily_digest(self) -> dict:
        """Pull daily-review counts from the Todoist projection.

        Returns a dict ready for `_format_daily_preview` + persistence.
        All counts default to 0 when settings/projection is empty.
        """
        empty = {
            "inbox_count": 0,
            "inbox_top3": [],
            "due_today_count": 0,
            "due_today_top3": [],
            "waiting_stale_count": 0,
            "pending_clarify_count": 0,
            "applied_24h_count": 0,
            "outbox_failed_7d_count": 0,
        }
        if self.db_pool is None:
            return empty
        async with self.db_pool.acquire() as conn:
            managed = await conn.fetchval(
                "SELECT value FROM settings WHERE key='todoist_managed_project_ids'"
            )
            if not isinstance(managed, dict):
                return empty
            inbox_id = managed.get("inbox")
            if not inbox_id:
                return empty
            inbox_count = await conn.fetchval(
                "SELECT count(*) FROM todoist_tasks "
                "WHERE project_id=$1 AND NOT is_completed",
                inbox_id,
            )
            inbox_top3 = [
                r["content"] for r in await conn.fetch(
                    "SELECT content FROM todoist_tasks "
                    "WHERE project_id=$1 AND NOT is_completed "
                    "ORDER BY updated_at DESC LIMIT 3",
                    inbox_id,
                )
            ]
            due_today_count = await conn.fetchval(
                "SELECT count(*) FROM todoist_tasks WHERE NOT is_completed "
                "AND (assignee_label='@me' OR assignee_label IS NULL) "
                "AND due_date IS NOT NULL AND due_date <= CURRENT_DATE"
            )
            due_today_top3 = [
                (r["content"], r["due_date"]) for r in await conn.fetch(
                    "SELECT content, due_date FROM todoist_tasks "
                    "WHERE NOT is_completed "
                    "AND (assignee_label='@me' OR assignee_label IS NULL) "
                    "AND due_date IS NOT NULL AND due_date <= CURRENT_DATE "
                    "ORDER BY due_date, updated_at LIMIT 3"
                )
            ]
            waiting_stale_count = await conn.fetchval(
                "SELECT count(*) FROM todoist_tasks "
                "WHERE '@waiting' = ANY(labels) AND NOT is_completed "
                "AND updated_at < now() - interval '3 days'"
            )
            pending_clarify_count = await conn.fetchval(
                "SELECT count(*) FROM todoist_tasks "
                "WHERE project_id=$1 AND NOT is_completed "
                "AND source_tag IS NOT NULL AND last_clarified_at IS NULL",
                inbox_id,
            )
            applied_24h_count = await conn.fetchval(
                "SELECT count(*) FROM gtd_clarify_log "
                "WHERE applied=true AND created_at > now() - interval '24 hours'"
            )
            # Permanently failed Todoist writes — each one is a captured task
            # or clarify outcome that never reached Todoist. 7-day window so
            # ancient failures don't nag forever.
            outbox_failed_7d = await conn.fetchval(
                "SELECT count(*) FROM todoist_outbox "
                "WHERE status='failed' AND created_at > now() - interval '7 days'"
            )
        return {
            "inbox_count": int(inbox_count or 0),
            "inbox_top3": list(inbox_top3 or []),
            "due_today_count": int(due_today_count or 0),
            "due_today_top3": [
                {"content": c, "due_date": d.isoformat() if d else None}
                for (c, d) in (due_today_top3 or [])
            ],
            "waiting_stale_count": int(waiting_stale_count or 0),
            "pending_clarify_count": int(pending_clarify_count or 0),
            "applied_24h_count": int(applied_24h_count or 0),
            "outbox_failed_7d_count": int(outbox_failed_7d or 0),
        }

    @activity.defn
    async def gather_weekly_digest(self) -> dict:
        """Pull weekly-review counts: stale next actions, inactive projects,
        old waiting-for, never-clarified inbox, last-7d completion stats.
        """
        empty = {
            "stale_next_actions_count": 0,
            "stale_next_actions_top3": [],
            "someday_count": 0,
            "waiting_stale_7d_count": 0,
            "waiting_stale_top": [],
            "inbox_unclarified_7d_count": 0,
            "completed_7d_count": 0,
            "never_clarified_count": 0,
            "never_clarified_oldest5": [],
        }
        if self.db_pool is None:
            return empty
        async with self.db_pool.acquire() as conn:
            managed = await conn.fetchval(
                "SELECT value FROM settings WHERE key='todoist_managed_project_ids'"
            )
            if not isinstance(managed, dict):
                return empty
            inbox_id = managed.get("inbox")
            someday_id = managed.get("someday")
            # Stale next-actions: open tasks carrying a project/* work-stream
            # label, untouched for 14 days.
            stale_next_count = await conn.fetchval(
                "SELECT count(*) FROM todoist_tasks t "
                "WHERE EXISTS (SELECT 1 FROM unnest(t.labels) lab "
                "             WHERE lab LIKE 'project/%') "
                "AND NOT t.is_completed "
                "AND t.updated_at < now() - interval '14 days'"
            )
            stale_top3 = [
                r["content"] for r in await conn.fetch(
                    "SELECT content FROM todoist_tasks t "
                    "WHERE EXISTS (SELECT 1 FROM unnest(t.labels) lab "
                    "             WHERE lab LIKE 'project/%') "
                    "AND NOT t.is_completed "
                    "AND t.updated_at < now() - interval '14 days' "
                    "ORDER BY t.updated_at ASC LIMIT 3"
                )
            ]
            # Someday/Later is a project now; count its open tasks (the weekly
            # review's primary resurface surface).
            someday_count = 0
            if someday_id:
                someday_count = await conn.fetchval(
                    "SELECT count(*) FROM todoist_tasks "
                    "WHERE project_id=$1 AND NOT is_completed",
                    someday_id,
                )
            waiting_stale = await conn.fetchval(
                "SELECT count(*) FROM todoist_tasks "
                "WHERE '@waiting' = ANY(labels) AND NOT is_completed "
                "AND updated_at < now() - interval '7 days'"
            )
            # Per-item nudge list: the actual stale waiting-for tasks (oldest
            # first) so the weekly review names who/what to chase, not just a
            # count. delegate/<person> labels are included for context.
            waiting_stale_top = [
                {
                    "content": r["content"],
                    "days": int(r["days"]),
                    "delegates": [
                        lab for lab in (r["labels"] or []) if lab.startswith("delegate/")
                    ],
                }
                for r in await conn.fetch(
                    "SELECT content, labels, "
                    "EXTRACT(day FROM now() - updated_at)::int AS days "
                    "FROM todoist_tasks "
                    "WHERE '@waiting' = ANY(labels) AND NOT is_completed "
                    "AND updated_at < now() - interval '7 days' "
                    "ORDER BY updated_at ASC LIMIT 5"
                )
            ]
            inbox_unclarified_7d = 0
            if inbox_id:
                inbox_unclarified_7d = await conn.fetchval(
                    "SELECT count(*) FROM todoist_tasks "
                    "WHERE project_id=$1 AND NOT is_completed "
                    "AND source_tag IS NOT NULL "
                    "AND last_clarified_at IS NULL "
                    "AND updated_at < now() - interval '7 days'",
                    inbox_id,
                )
            completed_7d = await conn.fetchval(
                "SELECT count(*) FROM todoist_tasks "
                "WHERE is_completed AND completed_at > now() - interval '7 days'"
            )
            # Never-clarified backlog across ALL managed projects
            # (inbox + next + someday). ClarifyFlow only scans Inbox, so
            # tasks that migrated to Next/Someday before clarification are
            # silently missed. Age is measured from raw->>'added_at' (the
            # Todoist creation timestamp) rather than updated_at, which the
            # 5-min sync bumps on every projection upsert.
            all_managed_ids = [
                v for v in managed.values() if isinstance(v, str) and v
            ]
            never_clarified_count = 0
            never_clarified_oldest5: list[dict] = []
            if all_managed_ids:
                never_clarified_count = await conn.fetchval(
                    "SELECT count(*) FROM todoist_tasks "
                    "WHERE project_id = ANY($1::text[]) AND NOT is_completed "
                    "AND last_clarified_at IS NULL",
                    all_managed_ids,
                )
                rows = await conn.fetch(
                    "SELECT content, project_id, "
                    "(raw->>'added_at') AS added_at "
                    "FROM todoist_tasks "
                    "WHERE project_id = ANY($1::text[]) AND NOT is_completed "
                    "AND last_clarified_at IS NULL "
                    "AND raw->>'added_at' IS NOT NULL "
                    "ORDER BY raw->>'added_at' ASC LIMIT 5",
                    all_managed_ids,
                )
                for r in rows:
                    try:
                        added = dt.date.fromisoformat(str(r["added_at"])[:10])
                        age_days = (dt.date.today() - added).days
                    except (ValueError, TypeError):
                        age_days = 0
                    never_clarified_oldest5.append({
                        "content": r["content"],
                        "project_id": r["project_id"],
                        "age_days": age_days,
                    })
        return {
            "stale_next_actions_count": int(stale_next_count or 0),
            "stale_next_actions_top3": list(stale_top3),
            "someday_count": int(someday_count or 0),
            "waiting_stale_7d_count": int(waiting_stale or 0),
            "waiting_stale_top": waiting_stale_top,
            "inbox_unclarified_7d_count": int(inbox_unclarified_7d or 0),
            "completed_7d_count": int(completed_7d or 0),
            "never_clarified_count": int(never_clarified_count or 0),
            "never_clarified_oldest5": never_clarified_oldest5,
        }

    @activity.defn
    async def gather_weekly_state(self) -> dict:
        """Superset of gather_weekly_digest + the four review-copilot
        detectors. Deterministic SQL only — no LLM. frame_review ranks +
        phrases this; format_weekly_preview can render it as a fallback."""
        base = await self.gather_weekly_digest()
        base.update({
            "stalled_projects": [],
            "aging_waiting_items": [],
            "slipping_items": [],
            "to_read_count": 0,
            "someday_resurface_items": [],
            "_top_n": _REVIEW_DEFAULTS["top_n"],
        })
        if self.db_pool is None:
            return base
        async with self.db_pool.acquire() as conn:
            cfg_raw = await conn.fetchval(
                "SELECT value FROM settings WHERE key='review_config'"
            )
            cfg = {**_REVIEW_DEFAULTS,
                   **(cfg_raw if isinstance(cfg_raw, dict) else {})}
            base["_top_n"] = int(cfg["top_n"])
            managed = await conn.fetchval(
                "SELECT value FROM settings WHERE key='todoist_managed_project_ids'"
            )
            someday_id = managed.get("someday") if isinstance(managed, dict) else None

            # Stalled: non-managed, non-archived project with open tasks but
            # NO actionable task (every open task carries a state label).
            base["stalled_projects"] = [
                {"project_id": r["id"], "name": r["name"], "url": r["url"]}
                for r in await conn.fetch(
                    "SELECT p.id, p.name, p.raw->>'url' AS url "
                    "FROM todoist_projects p "
                    "WHERE NOT p.is_managed AND NOT p.is_archived "
                    "AND EXISTS (SELECT 1 FROM todoist_tasks t "
                    "  WHERE t.project_id=p.id AND NOT t.is_completed) "
                    "AND NOT EXISTS (SELECT 1 FROM todoist_tasks t "
                    "  WHERE t.project_id=p.id AND NOT t.is_completed "
                    "  AND NOT (t.labels && $1::text[])) "
                    "ORDER BY p.name LIMIT 5",
                    _STATE_LABELS,
                )
            ]
            # Aging @waiting (with task id + url for decision cards).
            base["aging_waiting_items"] = [
                {"task_id": r["id"], "content": r["content"],
                 "days": int(r["days"]), "url": r["url"]}
                for r in await conn.fetch(
                    "SELECT id, content, raw->>'url' AS url, "
                    "EXTRACT(day FROM now()-updated_at)::int AS days "
                    "FROM todoist_tasks "
                    "WHERE '@waiting' = ANY(labels) AND NOT is_completed "
                    "AND updated_at < now() - make_interval(days => $1) "
                    "ORDER BY updated_at ASC LIMIT 5",
                    int(cfg["waiting_days"]),
                )
            ]
            # Slipping: overdue OR a project/* next-action stale past threshold.
            base["slipping_items"] = [
                {"task_id": r["id"], "content": r["content"],
                 "due_date": r["due_date"].isoformat() if r["due_date"] else None,
                 "url": r["url"]}
                for r in await conn.fetch(
                    "SELECT id, content, due_date, raw->>'url' AS url "
                    "FROM todoist_tasks t WHERE NOT is_completed AND ( "
                    "  (due_date IS NOT NULL AND due_date < CURRENT_DATE) "
                    "  OR (EXISTS (SELECT 1 FROM unnest(t.labels) lab "
                    "        WHERE lab LIKE 'project/%') "
                    "      AND updated_at < now() - make_interval(days => $1)) "
                    ") ORDER BY due_date ASC NULLS LAST, updated_at ASC LIMIT 5",
                    int(cfg["next_actions_days"]),
                )
            ]
            base["to_read_count"] = int(await conn.fetchval(
                "SELECT count(*) FROM todoist_tasks "
                "WHERE '@to-read' = ANY(labels) AND NOT is_completed"
            ) or 0)
            # Someday resurface: oldest few, untouched past threshold.
            if someday_id:
                rows = await conn.fetch(
                    "SELECT id, content, raw->>'added_at' AS added_at "
                    "FROM todoist_tasks WHERE project_id=$1 AND NOT is_completed "
                    "AND updated_at < now() - make_interval(days => $2) "
                    "ORDER BY raw->>'added_at' ASC NULLS LAST LIMIT 3",
                    someday_id, int(cfg["someday_resurface_days"]),
                )
                for r in rows:
                    try:
                        added = dt.date.fromisoformat(str(r["added_at"])[:10])
                        age = (dt.date.today() - added).days
                    except (ValueError, TypeError):
                        age = 0
                    base["someday_resurface_items"].append(
                        {"task_id": r["id"], "content": r["content"], "age_days": age}
                    )
        return base

    def _build_decisions(self, snapshot: dict) -> list[dict]:
        """Deterministic decision list from detector items. Only the
        one-tap-safe signals become cards; stalled/to_read stay in the
        narrative (deep-link, human re-enters)."""
        decs: list[dict] = []
        for it in snapshot.get("aging_waiting_items") or []:
            decs.append({
                "id": f"aging_waiting:{it['task_id']}",
                "signal": "aging_waiting", "task_id": it["task_id"],
                "prompt": f"⏳ Waiting {it.get('days', '?')}d: {it['content']}",
                "options": {"nudge": "Nudge", "done": "Mark done",
                            "drop": "Drop", "keep": "Keep"},
            })
        for it in snapshot.get("slipping_items") or []:
            due = it.get("due_date")
            label = f"📌 Slipping{f' (due {due})' if due else ''}: {it['content']}"
            decs.append({
                "id": f"slipping:{it['task_id']}",
                "signal": "slipping", "task_id": it["task_id"],
                "prompt": label,
                "options": {"tomorrow": "Do tomorrow", "next_week": "Next week",
                            "letgo": "Let it go", "keep": "Keep"},
            })
        for it in snapshot.get("someday_resurface_items") or []:
            decs.append({
                "id": f"someday_resurface:{it['task_id']}",
                "signal": "someday_resurface", "task_id": it["task_id"],
                "prompt": f"💭 Someday {it.get('age_days', '?')}d: {it['content']}",
                "options": {"activate": "Activate", "drop": "Drop",
                            "keep": "Keep someday"},
            })
        return decs

    def _build_frame_prompt(self, snapshot: dict, decisions: list[dict]) -> str:
        lines = [
            "You are sebas, an executive assistant writing a weekly GTD review.",
            "Below is the user's current state and a list of decision ids.",
            "Return STRICT JSON: {\"narrative\": <2-4 sentence plain-text "
            "summary, no markdown headers>, \"order\": [decision ids, most "
            "important first]}. Do not invent decisions; only reorder the ids.",
            "",
            f"Stalled projects: {len(snapshot.get('stalled_projects') or [])}",
            f"To-read backlog: {snapshot.get('to_read_count') or 0}",
            f"Completed this week: {snapshot.get('completed_7d_count') or 0}",
            "Decisions:",
        ]
        for d in decisions:
            lines.append(f"- {d['id']}: {d['prompt']}")
        return "\n".join(lines)

    @activity.defn
    async def frame_review(self, snapshot: dict) -> dict:
        """One LLM call ranks + phrases the snapshot. On ANY failure, fall
        back to format_weekly_preview + detector order so the review always
        ships."""
        decisions = self._build_decisions(snapshot)
        top_n = int(snapshot.get("_top_n") or _REVIEW_DEFAULTS["top_n"])
        fallback = format_weekly_preview(snapshot)
        if not self.llm_client or not decisions:
            return {"narrative": fallback, "decisions": decisions[:top_n]}
        try:
            result = await self.llm_client.think(
                self._build_frame_prompt(snapshot, decisions),
                model=self.frame_model,
            )
            raw = result.get("response", "") if isinstance(result, dict) else (result or "")
            parsed = parse_llm_json(raw) or {}
            narrative = (parsed.get("narrative") or "").strip() or fallback
            order = parsed.get("order")
            if isinstance(order, list):
                by_id = {d["id"]: d for d in decisions}
                seen = set()
                ranked = []
                for i in order:
                    if i in by_id and i not in seen:
                        ranked.append(by_id[i])
                        seen.add(i)
                ranked += [d for d in decisions if d["id"] not in seen]
                decisions = ranked
            return {"narrative": narrative, "decisions": decisions[:top_n]}
        except Exception as exc:  # noqa: BLE001
            activity.logger.warning("frame_review_llm_failed err=%s", str(exc)[:200])
            return {"narrative": fallback, "decisions": decisions[:top_n]}

    @activity.defn
    async def apply_review_decision(
        self, interaction_id: str, response: dict, metadata: dict
    ) -> dict:
        """Post-resolve hook: turn a tapped weekly-review choice into a
        Todoist command. 'keep'/unknown are no-ops. Mirrors the per-command
        status check from apply_clarify_resolution."""
        from aegis.connectors.todoist import TodoistConnector

        signal = metadata.get("signal")
        task_id = metadata.get("task_id")
        choice = (response.get("value") or "").strip()
        if not task_id or not signal:
            return {"applied": False, "reason": "missing_metadata"}
        if choice in ("", "keep"):
            return {"applied": True, "choice": choice or "keep", "noop": True}

        cmds: list[dict] = []
        if signal == "aging_waiting":
            if choice == "nudge":
                cmds = [TodoistConnector.build_note_add_command(
                    task_id, "⏳ Following up (weekly review)")]
            elif choice in ("done", "drop"):
                cmds = [TodoistConnector.build_item_complete_command(task_id)]
        elif signal == "slipping":
            if choice == "tomorrow":
                cmds = [TodoistConnector.build_item_update_command(
                    task_id, due={"string": "tomorrow"})]
            elif choice == "next_week":
                cmds = [TodoistConnector.build_item_update_command(
                    task_id, due={"string": "next monday"})]
            elif choice == "letgo":
                cmds = [TodoistConnector.build_item_complete_command(task_id)]
        elif signal == "someday_resurface":
            if choice == "drop":
                cmds = [TodoistConnector.build_item_complete_command(task_id)]
            elif choice == "activate":
                next_id = None
                if self.db_pool is not None:
                    async with self.db_pool.acquire() as conn:
                        managed = await conn.fetchval(
                            "SELECT value FROM settings "
                            "WHERE key='todoist_managed_project_ids'"
                        )
                    if isinstance(managed, dict):
                        next_id = managed.get("next")
                if next_id:
                    cmds = [TodoistConnector.build_item_move_command(task_id, next_id)]

        if not cmds:
            return {"applied": False, "reason": f"unhandled:{signal}/{choice}"}
        if self.todoist_connector is None:
            return {"applied": False, "reason": "no_connector"}

        result = await self.todoist_connector.commands(cmds)
        status = TodoistConnector.check_sync_status(result, [c["uuid"] for c in cmds])
        if not status["ok"]:
            activity.logger.warning(
                "review_decision_rejected signal=%s choice=%s task=%s err=%s rejected=%s",
                signal, choice, task_id, status["envelope_error"],
                str(status["rejected"])[:200],
            )
            # Transient (5xx-class) → raise so InteractionFlow's best-effort
            # retry re-attempts; permanent → return applied=False (logged).
            if status.get("retryable") or status.get("rejected_retryable"):
                raise RuntimeError(f"todoist transient on review decision {task_id}")
            return {"applied": False, "choice": choice, "reason": "todoist_rejected"}
        activity.logger.info(
            "review_decision_applied signal=%s choice=%s task=%s interaction=%s",
            signal, choice, task_id, interaction_id,
        )
        return {"applied": True, "signal": signal, "choice": choice}

    @activity.defn
    async def gather_today_focus(self) -> list[dict]:
        """Ranked 'my next actions' for today: open, @me (or unassigned),
        not parked (state labels), not in inbox/someday. Overdue/due first,
        then priority. ponytail: no calendar free-time sizing in v1 —
        ranks by due+priority; add calendar sizing if this falls short."""
        if self.db_pool is None:
            return []
        async with self.db_pool.acquire() as conn:
            managed = await conn.fetchval(
                "SELECT value FROM settings WHERE key='todoist_managed_project_ids'"
            )
            exclude = []
            if isinstance(managed, dict):
                exclude = [managed.get("inbox"), managed.get("someday")]
                exclude = [e for e in exclude if e]
            where = [
                "NOT t.is_completed",
                "(t.assignee_label='@me' OR t.assignee_label IS NULL)",
                "NOT (t.labels && $1::text[])",
            ]
            params: list = [_STATE_LABELS]
            if exclude:
                params.append(exclude)
                where.append(
                    f"(t.project_id IS NULL OR t.project_id <> ALL(${len(params)}::text[]))"
                )
            sql = (
                "SELECT t.id, t.content, t.due_date FROM todoist_tasks t "
                f"WHERE {' AND '.join(where)} "
                "ORDER BY (t.due_date IS NULL), t.due_date ASC, "
                "t.priority DESC NULLS LAST, t.updated_at DESC LIMIT 5"
            )
            rows = await conn.fetch(sql, *params)
        return [
            {"task_id": r["id"], "content": r["content"],
             "due_date": r["due_date"].isoformat() if r["due_date"] else None}
            for r in rows
        ]

    @activity.defn
    async def log_review_digest(
        self,
        kind: str,
        counts: dict,
        preview: str,
        interaction_id: str | None,
    ) -> int:
        """Insert a review_digest_log row; returns the new id."""
        if self.db_pool is None:
            return 0
        async with self.db_pool.acquire() as conn:
            return await conn.fetchval(
                "INSERT INTO review_digest_log "
                "(review_kind, counts, preview, interaction_id) "
                "VALUES ($1, $2, $3, $4) RETURNING id",
                kind, counts, preview, interaction_id,
            )

    @activity.defn
    async def apply_review_acknowledgement(
        self,
        interaction_id: str,
        response: dict,
        metadata: dict,
    ) -> dict:
        """Called by InteractionFlow's post_resolve hook when the user
        taps a button on the review card. Writes acknowledgement back
        onto the matching review_digest_log row.
        """
        if self.db_pool is None:
            return {"acknowledged": False, "reason": "no_pool"}
        kind = metadata.get("kind") or "daily"
        choice = (response.get("value") or "").strip() or "unknown"
        # review_digest_log.interaction_id stores the InteractionFlow's
        # workflow_id (from _spawn_review_interaction). The caller passes
        # the interactions.id UUID. Look up flow_run_id to bridge them.
        # Cast $2::uuid explicitly so asyncpg's parameter inference doesn't
        # treat it as text and break the WHERE clause.
        async with self.db_pool.acquire() as conn:
            tag = await conn.execute(
                "UPDATE review_digest_log SET acknowledged=true, "
                "user_choice=$1, acknowledged_at=now() "
                "WHERE interaction_id = ("
                "SELECT flow_run_id FROM interactions WHERE id=$2::uuid"
                ") AND NOT acknowledged",
                choice,
                interaction_id,
            )
        activity.logger.info(
            "review_acknowledged kind=%s choice=%s interaction=%s pgtag=%s",
            kind, choice, interaction_id, tag,
        )

        # Phase 5 polish: 'need_time' snoozes the DAILY review for 1h.
        # We schedule a delayed re-fire via the Temporal client. Best-effort:
        # failure logs but does not unfresh the acknowledgement.
        snoozed = False
        if kind == "daily" and choice == "need_time" and self.temporal_host:
            try:
                import uuid as _uuid
                from datetime import timedelta as _td

                from temporalio.client import Client as _Client

                client = await _Client.connect(self.temporal_host)
                # Use a different workflow_id so it doesn't collide with the
                # scheduled one. Tagged 'snooze' so prod logs make sense.
                await client.start_workflow(
                    "DailyReviewFlow",
                    {"agent_id": "sebas", "activity_name": "gtd-daily-review-snoozed"},
                    id=f"daily-review-snooze-{_uuid.uuid4()}",
                    task_queue=self.task_queue,
                    start_delay=_td(hours=1),
                )
                snoozed = True
                activity.logger.info(
                    "daily_review_snoozed_for_1h interaction=%s", interaction_id,
                )
            except Exception as exc:  # noqa: BLE001
                activity.logger.warning(
                    "daily_review_snooze_failed interaction=%s err=%s",
                    interaction_id, str(exc)[:200],
                )
        return {
            "acknowledged": True,
            "kind": kind,
            "choice": choice,
            "snoozed": snoozed,
        }


# --- Telegram preview formatting (workflow-side, not an activity) ---


def format_daily_preview(digest: dict, today: dt.date | None = None) -> str:
    """Build the daily Telegram message body. Plain text + light HTML."""
    today = today or dt.date.today()
    weekday = today.strftime("%a %d %b")
    lines: list[str] = [f"☀ <b>Daily review</b> — {weekday}", ""]
    inbox_n = digest.get("inbox_count") or 0
    if inbox_n:
        lines.append(f"📥 <b>Inbox</b>: {inbox_n} open")
        for title in (digest.get("inbox_top3") or [])[:3]:
            lines.append(f"  • {_clip(title, 80)}")
    else:
        lines.append("📥 Inbox: ✨ clear")
    pending = digest.get("pending_clarify_count") or 0
    if pending:
        lines.append(f"🔍 Needs clarify: {pending}")
    lines.append("")
    today_n = digest.get("due_today_count") or 0
    if today_n:
        lines.append(f"📅 <b>Today / overdue</b>: {today_n}")
        for item in (digest.get("due_today_top3") or [])[:3]:
            content = item.get("content") if isinstance(item, dict) else item
            due = item.get("due_date") if isinstance(item, dict) else None
            suffix = f" — due {due}" if due else ""
            lines.append(f"  • {_clip(content, 70)}{suffix}")
    else:
        lines.append("📅 Today: nothing due")
    lines.append("")
    waiting = digest.get("waiting_stale_count") or 0
    applied = digest.get("applied_24h_count") or 0
    lines.append(f"⏳ Waiting For (stale >3d): {waiting}")
    lines.append(f"✅ Applied last 24h: {applied}")
    outbox_failed = digest.get("outbox_failed_7d_count") or 0
    if outbox_failed:
        lines.append(
            f"🚨 <b>Todoist write failures</b> (7d): {outbox_failed} — "
            "captured work may be lost, check /admin/todoist"
        )
    body = "\n".join(lines)
    if len(body) > 3500:
        body = body[:3500] + "\n…(truncated)"
    return body


def format_today_focus(items: list[dict]) -> str:
    """Build the 'today's focus' message — a do-these-in-order shortlist."""
    if not items:
        return "🎯 <b>Today's focus</b>: nothing queued — you're clear."
    lines = ["🎯 <b>Today's focus</b>", ""]
    for it in items:
        due = it.get("due_date")
        suffix = f" — due {due}" if due else ""
        lines.append(f"  • {_clip(it.get('content'), 70)}{suffix}")
    return "\n".join(lines)


def format_weekly_preview(digest: dict, today: dt.date | None = None) -> str:
    today = today or dt.date.today()
    week_ending = today.strftime("%d %b")
    lines: list[str] = [f"📆 <b>Weekly review</b> — week ending {week_ending}", ""]
    stale_n = digest.get("stale_next_actions_count") or 0
    lines.append(f"🐌 <b>Stale next actions</b> (>14d): {stale_n}")
    for t in (digest.get("stale_next_actions_top3") or [])[:3]:
        lines.append(f"  • {_clip(t, 80)}")
    lines.append("")
    lines.append(f"💭 Someday / Later: {digest.get('someday_count') or 0}")
    lines.append(f"⏳ Waiting For (stale >7d): {digest.get('waiting_stale_7d_count') or 0}")
    for item in (digest.get("waiting_stale_top") or [])[:5]:
        who = f" ({', '.join(item['delegates'])})" if item.get("delegates") else ""
        lines.append(f"  • {_clip(item.get('content'), 70)}{who} — {item.get('days')}d")
    lines.append(f"📭 Inbox unclarified (>7d): {digest.get('inbox_unclarified_7d_count') or 0}")
    never_n = digest.get("never_clarified_count") or 0
    if never_n:
        lines.append(f"🗂️ <b>Never-clarified (all projects)</b>: {never_n}")
        for item in (digest.get("never_clarified_oldest5") or [])[:5]:
            age = item.get("age_days") or 0
            lines.append(f"  • {_clip(item.get('content'), 70)} — {age}d old")
    lines.append(f"✅ Completed this week: {digest.get('completed_7d_count') or 0}")
    body = "\n".join(lines)
    if len(body) > 3500:
        body = body[:3500] + "\n…(truncated)"
    return body


def _clip(value: Any, n: int) -> str:
    s = str(value or "")
    return s if len(s) <= n else s[: n - 1] + "…"
