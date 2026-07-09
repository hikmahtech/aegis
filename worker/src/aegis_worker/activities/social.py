"""SocialActivities — find due @publish tasks, outbox enqueue/drain, completion.

Called by SocialPublishFlow. `apply_social_approval` is InteractionFlow's
post_resolve hook: it applies the user's card choice (approve → enqueue +
post immediately; skip → strip the publish label so the task stops
re-carding). The scheduled flow's own drain/complete steps are the retry
safety net for anything the hook attempt left behind.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
from aegis.connectors.todoist import TodoistConnector
from temporalio import activity

_MAX_ATTEMPTS = 5  # mirror todoist_outbox semantics


@dataclass
class SocialActivities:
    """db_pool may be None in unit tests that only exercise pure branches;
    connector is the SocialConnector (None in tests that don't post)."""

    db_pool: asyncpg.Pool | None
    connector: Any = None

    async def _setting(self, key: str, default):
        if self.db_pool is None:
            return default
        val = await self.db_pool.fetchval("SELECT value FROM settings WHERE key = $1", key)
        return default if val is None else val

    @activity.defn
    async def find_due_posts(
        self, lookahead_minutes: int = 10, default_post_hour: int = 9
    ) -> list[dict]:
        """Open @publish tasks due within the lookahead, without outbox rows yet.

        Post time = raw->'due'->>'datetime' when present (naive values are the
        user's local time; 'Z' values are UTC), else due_date at
        default_post_hour local. Returns [] when social_publishing_enabled is
        false — the kill switch that lets the seed ship active but inert.
        """
        if self.db_pool is None:
            return []
        if not await self._setting("social_publishing_enabled", False):
            return []
        publish_label = str(await self._setting("social_publish_label", "publish"))
        platform_labels: dict = await self._setting(
            "social_platform_labels", {"x": "x"}
        )
        user_tz = await self._setting("user_timezone", "UTC")
        if not isinstance(user_tz, str) or not user_tz:
            user_tz = "UTC"

        rows = await self.db_pool.fetch(
            """
            SELECT id, content, description, labels, post_at FROM (
              SELECT t.id, t.content, t.description, t.labels,
                CASE
                  WHEN d.due_dt IS NOT NULL THEN
                    CASE WHEN d.due_dt LIKE '%Z'
                         THEN d.due_dt::timestamptz
                         ELSE (d.due_dt::timestamp AT TIME ZONE $2)
                    END
                  WHEN t.due_date IS NOT NULL THEN
                    ((t.due_date::timestamp + make_interval(hours => $3))
                      AT TIME ZONE $2)
                END AS post_at
              FROM todoist_tasks t
              -- Todoist's SYNC API carries a timed due inside due.date
              -- ("YYYY-MM-DDTHH:MM:SS", floating local; "...Z" when the due
              -- has a fixed timezone). due.datetime only exists in REST-API
              -- payloads — kept as a fallback for rows mirrored that way.
              CROSS JOIN LATERAL (
                SELECT COALESCE(
                         t.raw->'due'->>'datetime',
                         CASE WHEN t.raw->'due'->>'date' LIKE '%T%'
                              THEN t.raw->'due'->>'date' END
                       ) AS due_dt
              ) d
              WHERE NOT t.is_completed
                AND $1 = ANY(t.labels)
                AND NOT EXISTS (
                      SELECT 1 FROM social_outbox o WHERE o.todoist_task_id = t.id
                    )
            ) s
            WHERE s.post_at <= now() + make_interval(mins => $4)
            ORDER BY s.post_at, s.id
            """,
            publish_label,
            user_tz,
            default_post_hour,
            lookahead_minutes,
        )

        due: list[dict] = []
        for r in rows:
            labels = list(r["labels"] or [])
            platforms = [p for p, lab in platform_labels.items() if lab in labels]
            if not platforms:
                activity.logger.warning(
                    "social_find_due_no_platform_label task_id=%s labels=%s", r["id"], labels
                )
                continue
            due.append(
                {
                    "task_id": r["id"],
                    "text": r["content"],
                    "link": (r["description"] or "").strip(),
                    "platforms": platforms,
                    # The Todoist due time — Postiz-transport posts are
                    # scheduled in Postiz for exactly this moment.
                    "post_at": r["post_at"].isoformat(),
                }
            )
        activity.logger.info("social_find_due_posts found=%d", len(due))
        return due

    @activity.defn
    async def enqueue_outbox(
        self, task_id: str, platforms: list[str], text: str, link: str, post_at: str = ""
    ) -> dict:
        """One social_outbox row per platform; idempotent per (task, account).

        post_at (ISO, from the Todoist due time) rides in the payload as
        schedule_at — the Postiz transport schedules the post for that moment
        instead of publishing immediately.
        """
        if self.db_pool is None:
            return {"queued": 0, "missing_accounts": []}
        queued, missing = 0, []
        for platform in platforms:
            # ponytail: first account per platform; a per-task account label
            # (e.g. @x:work) is the upgrade path when multi-account matters.
            account_id = await self.db_pool.fetchval(
                "SELECT id FROM social_accounts WHERE platform = $1 ORDER BY id LIMIT 1",
                platform,
            )
            if account_id is None:
                missing.append(platform)
                activity.logger.warning(
                    "social_enqueue_no_account task_id=%s platform=%s", task_id, platform
                )
                continue
            result = await self.db_pool.execute(
                "INSERT INTO social_outbox (todoist_task_id, account_id, payload) "
                "VALUES ($1, $2, $3) "
                "ON CONFLICT (todoist_task_id, account_id) WHERE todoist_task_id IS NOT NULL "
                "DO NOTHING",
                task_id,
                account_id,
                {"text": text, "link": link, "schedule_at": post_at},
            )
            queued += int(result.endswith("1"))
        activity.logger.info(
            "social_enqueue_outbox task_id=%s queued=%d missing=%s", task_id, queued, missing
        )
        return {"queued": queued, "missing_accounts": missing}

    @activity.defn
    async def drain_social_outbox(self) -> dict:
        """Post pending rows; mark posted, or bump attempt_count (failed at cap)."""
        if self.db_pool is None or self.connector is None:
            return {"posted": 0, "failed": 0}
        rows = await self.db_pool.fetch(
            "SELECT id, account_id, payload, attempt_count FROM social_outbox "
            "WHERE status = 'pending' ORDER BY created_at, id LIMIT 20"
        )
        posted = failed = 0
        for r in rows:
            try:
                ref = await self.connector.post(r["account_id"], r["payload"])
                await self.db_pool.execute(
                    "UPDATE social_outbox SET status = 'posted', posted_ref = $1, "
                    "last_attempt_at = now(), attempt_count = attempt_count + 1 WHERE id = $2",
                    ref,
                    r["id"],
                )
                posted += 1
            except Exception as exc:  # noqa: BLE001 — one bad row must not block the rest
                next_attempts = r["attempt_count"] + 1
                new_status = "failed" if next_attempts >= _MAX_ATTEMPTS else "pending"
                await self.db_pool.execute(
                    "UPDATE social_outbox SET status = $1, attempt_count = $2, "
                    "last_attempt_at = now() WHERE id = $3",
                    new_status,
                    next_attempts,
                    r["id"],
                )
                if new_status == "failed":
                    failed += 1
                activity.logger.warning(
                    "social_outbox_post_failed id=%s attempts=%d status=%s err=%s",
                    r["id"],
                    next_attempts,
                    new_status,
                    str(exc)[:200],
                )
        activity.logger.info("social_drain_outbox posted=%d failed=%d", posted, failed)
        return {"posted": posted, "failed": failed}

    @activity.defn
    async def complete_posted_tasks(self) -> dict:
        """Enqueue item_complete (via todoist_outbox) for tasks fully posted.

        Idempotent: the deterministic temp_id social-complete-<task_id> makes
        re-runs no-ops until the 5-min TodoistSyncFlow drains the command and
        the task flips is_completed in the projection.
        """
        if self.db_pool is None:
            return {"completed": 0}
        rows = await self.db_pool.fetch(
            """
            SELECT o.todoist_task_id AS task_id
            FROM social_outbox o
            JOIN todoist_tasks t ON t.id = o.todoist_task_id
            WHERE o.todoist_task_id IS NOT NULL AND NOT t.is_completed
            GROUP BY o.todoist_task_id
            HAVING count(*) FILTER (WHERE o.status <> 'posted') = 0
            """
        )
        completed = 0
        for r in rows:
            cmd = TodoistConnector.build_item_complete_command(r["task_id"])
            result = await self.db_pool.execute(
                "INSERT INTO todoist_outbox (temp_id, command, status) "
                "VALUES ($1, $2, 'pending') ON CONFLICT (temp_id) DO NOTHING",
                f"social-complete-{r['task_id']}",
                cmd,
            )
            completed += int(result.endswith("1"))
        if completed:
            activity.logger.info("social_complete_posted_tasks enqueued=%d", completed)
        return {"completed": completed}

    @activity.defn
    async def unpublish_task(self, task_id: str) -> dict:
        """Skip = revoke publish intent: strip the publish label off the task.

        Writes the label change through todoist_outbox AND optimistically
        updates the local projection so the next 5-min tick doesn't re-card
        before the sync round-trips.
        """
        if self.db_pool is None:
            return {"unpublished": False}
        publish_label = str(await self._setting("social_publish_label", "publish"))
        labels = await self.db_pool.fetchval(
            "SELECT labels FROM todoist_tasks WHERE id = $1", task_id
        )
        if labels is None:
            return {"unpublished": False}
        new_labels = [lab for lab in labels if lab != publish_label]
        cmd = TodoistConnector.build_item_update_command(task_id, labels=new_labels)
        await self.db_pool.execute(
            "INSERT INTO todoist_outbox (temp_id, command, status) "
            "VALUES ($1, $2, 'pending') ON CONFLICT (temp_id) DO NOTHING",
            f"social-skip-{task_id}",
            cmd,
        )
        await self.db_pool.execute(
            "UPDATE todoist_tasks SET labels = $1, updated_at = now() WHERE id = $2",
            new_labels,
            task_id,
        )
        activity.logger.info("social_unpublish_task task_id=%s", task_id)
        return {"unpublished": True}

    async def _current_post_at(self, task_id: str) -> str:
        """The task's CURRENT mirrored due time, at approval time.

        The card's metadata snapshots post_at at card-creation; the due can
        move afterwards (user reschedule, or the sync flow landing the update
        a beat after the same-instant social tick). Approval is the moment
        that matters — re-read the mirror and prefer it over the snapshot.
        """
        if self.db_pool is None:
            return ""
        user_tz = await self._setting("user_timezone", "UTC")
        if not isinstance(user_tz, str) or not user_tz:
            user_tz = "UTC"
        val = await self.db_pool.fetchval(
            """
            SELECT CASE
              WHEN d.due_dt IS NOT NULL THEN
                CASE WHEN d.due_dt LIKE '%Z' THEN d.due_dt::timestamptz
                     ELSE (d.due_dt::timestamp AT TIME ZONE $2) END
            END
            FROM todoist_tasks t
            CROSS JOIN LATERAL (
              SELECT COALESCE(
                       t.raw->'due'->>'datetime',
                       CASE WHEN t.raw->'due'->>'date' LIKE '%T%'
                            THEN t.raw->'due'->>'date' END
                     ) AS due_dt
            ) d
            WHERE t.id = $1
            """,
            task_id,
            user_tz,
        )
        return val.isoformat() if val else ""

    @activity.defn
    async def apply_social_approval(
        self, interaction_id: str, response: dict, metadata: dict
    ) -> dict:
        """InteractionFlow post_resolve hook — apply the card choice.

        Approve posts immediately (enqueue → drain → complete as plain method
        calls); any failure here is retried by the scheduled flow's own
        drain/complete steps on the next tick.
        """
        choice = (response.get("value") or "").strip()
        task_id = metadata.get("task_id") or ""
        if not task_id:
            return {"applied": "none"}
        if choice == "approve":
            await self.enqueue_outbox(
                task_id,
                list(metadata.get("platforms") or []),
                str(metadata.get("text") or ""),
                str(metadata.get("link") or ""),
                await self._current_post_at(task_id) or str(metadata.get("post_at") or ""),
            )
            await self.drain_social_outbox()
            await self.complete_posted_tasks()
            return {"applied": "approved"}
        if choice == "skip":
            await self.unpublish_task(task_id)
            return {"applied": "skipped"}
        activity.logger.info(
            "social_approval_no_action interaction_id=%s choice=%s", interaction_id, choice
        )
        return {"applied": "none"}

    @activity.defn
    async def refresh_post_metrics(self, window_days: int = 14) -> dict:
        """Pull Postiz analytics for recently-posted rows into social_outbox.metrics.

        Only Postiz-routed accounts are touched (native X posts have no
        equivalent analytics call here). Harmless when
        social_publishing_enabled is false — this only refreshes metrics on
        rows that are ALREADY posted, it doesn't gate on the publishing kill
        switch, so SocialMetricsFlow needs no such gate either.
        """
        if self.db_pool is None or self.connector is None:
            return {"refreshed": 0, "failed": 0}

        now = datetime.now(UTC)
        posts_by_ref: dict[str, dict] = {}
        try:
            posts = await self.connector.list_posts_window(
                (now - timedelta(days=window_days)).isoformat(),
                (now + timedelta(days=1)).isoformat(),
            )
            for p in posts:
                pid = str(p.get("id") or "")
                if not pid:
                    continue
                posts_by_ref[pid] = {
                    "state": p.get("state"),
                    "release_url": p.get("releaseURL"),
                    "publish_date": p.get("publishDate"),
                }
        except Exception as exc:  # noqa: BLE001 — per-post analytics is the core
            # value; a failed list call just means state/release_url stay
            # unknown for this pass, not that we skip the pass entirely.
            activity.logger.warning("social_list_posts_window_failed err=%s", str(exc)[:200])

        rows = await self.db_pool.fetch(
            """
            SELECT o.id, o.posted_ref
            FROM social_outbox o
            JOIN social_accounts a ON a.id = o.account_id
            WHERE o.status = 'posted'
              AND o.posted_ref IS NOT NULL
              AND o.created_at > now() - make_interval(days => $1)
              AND a.meta ? 'postiz_integration_id'
            ORDER BY o.created_at DESC
            LIMIT 50
            """,
            window_days,
        )

        refreshed = failed = 0
        for r in rows:
            post_ref = r["posted_ref"]
            try:
                analytics = await self.connector.get_post_metrics(post_ref)
                info = posts_by_ref.get(str(post_ref), {})
                metrics = {
                    "state": info.get("state") or "unknown",
                    "release_url": info.get("release_url"),
                    "publish_date": info.get("publish_date"),
                    "series": analytics.get("series", {}),
                }
                await self.db_pool.execute(
                    "UPDATE social_outbox SET metrics = $1, metrics_at = now() WHERE id = $2",
                    metrics,
                    r["id"],
                )
                refreshed += 1
            except Exception as exc:  # noqa: BLE001 — one bad row must not block the rest
                failed += 1
                activity.logger.warning(
                    "social_refresh_post_metrics_failed id=%s posted_ref=%s err=%s",
                    r["id"],
                    post_ref,
                    str(exc)[:200],
                )
        activity.logger.info(
            "social_refresh_post_metrics refreshed=%d failed=%d", refreshed, failed
        )
        return {"refreshed": refreshed, "failed": failed}
