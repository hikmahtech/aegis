"""SocialActivities — mirror Postiz channels, find due @publish tasks, outbox
enqueue/drain, completion.

Called by SocialPublishFlow. `sync_postiz_channels` keeps the `social_accounts`
mirror honest (#182) and `retire_unpublishable_tasks` gives a task labeled for
a channel nobody connected a real ending (#183). `apply_social_approval` is
InteractionFlow's
post_resolve hook: it applies the user's card choice (approve → enqueue +
post immediately; skip → strip the publish label so the task stops
re-carding). The scheduled flow's own drain/complete steps are the retry
safety net for anything the hook attempt left behind.

Also carries the **stuck-post watchdog** (#225) that SocialMetricsFlow runs
right after the metrics refresh — see the block above `find_stuck_posts`.
"""

from __future__ import annotations

import html as _html
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
from aegis.connectors.todoist import TodoistConnector
from aegis.services import social_channels
from temporalio import activity

from aegis_worker.activities.delivery import safe_send_message

_MAX_ATTEMPTS = 5  # mirror todoist_outbox semantics

# Upstream (Todoist task authorship) sometimes writes a markdown link
# `[Title](https://…)` instead of a bare URL — either into the description
# (mapped to `link` below) or inline in the task content (`text`). LinkedIn
# comments don't render markdown, so `_post_postiz`'s first-comment branch
# needs a real URL. Normalize once here, at ingest, so every downstream
# transport sees a bare link (#114).
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://\S+?)\)")

# ---------------------------------------------------------------------------
# stuck-post watchdog (#225) — constants
# ---------------------------------------------------------------------------

#: `audit_log` actions/target — the dedup substrate, same as
#: `FlowHealthActivities` (issue #226). `alert_dedup_index` is deliberately NOT
#: used: its `task_id` is NOT NULL and joined against `todoist_tasks`, and this
#: watchdog notifies with a plain chat card, not a @pandora task.
STUCK_ALERT_ACTION = "social_stuck_alert"
STUCK_RECOVERY_ACTION = "social_stuck_recovered"
STUCK_TARGET_TYPE = "social_post"
#: `alert_mutes.mute_key` prefix — silence one known-stuck post with
#: `INSERT INTO alert_mutes (mute_key, muted_until) VALUES ('social-stuck:<postiz id>', ...)`.
STUCK_MUTE_PREFIX = "social-stuck:"
STUCK_ACTOR = "social-stuck-watchdog"

#: The only Postiz state that means "this post actually went out". Everything
#: else past its `schedule_at` — QUEUE, ERROR, DRAFT, or the `unknown` we write
#: when Postiz doesn't return the post at all — counts as stuck.
#:
#: Deliberately a denylist of one. The 2026-07-29→08-02 outage (four days, no
#: post published, nothing alerted) went unseen partly because three of the
#: seven stuck rows had aged into `unknown` rather than `QUEUE`: a QUEUE-only
#: rule stays silent on them. Only a positive publish confirmation is
#: reassuring; anything else past due is worth one card.
PUBLISHED_STATE = "PUBLISHED"

# `payload->>'schedule_at'` is free text — `enqueue_outbox` writes an ISO
# timestamp, but "" is possible and `POST /api/social/publish` copies the value
# straight out of the request body. A bare `::timestamptz` on a malformed value
# aborts the WHOLE statement, which would take out the metrics refresh AND this
# watchdog — precisely the silent-failure class this change exists to remove.
# `pg_input_is_valid` (Postgres 16) turns a bad value into NULL instead.
_SCHEDULE_AT = (
    "CASE WHEN pg_input_is_valid(o.payload->>'schedule_at', 'timestamptz') "
    "THEN (o.payload->>'schedule_at')::timestamptz END"
)

_STUCK_SQL = f"""
SELECT o.id,
       o.posted_ref,
       o.todoist_task_id,
       a.platform,
       a.label,
       s.sched,
       o.metrics->>'state' AS state,
       o.metrics_at,
       left(coalesce(o.payload->>'text', ''), 120) AS preview
FROM social_outbox o
JOIN social_accounts a ON a.id = o.account_id
CROSS JOIN LATERAL (SELECT {_SCHEDULE_AT} AS sched) s
WHERE o.status = 'posted'
  AND o.posted_ref IS NOT NULL
  AND a.meta ? 'postiz_integration_id'
  AND s.sched IS NOT NULL
  AND s.sched < now() - make_interval(hours => $1)
  AND coalesce(o.metrics->>'state', '') <> '{PUBLISHED_STATE}'
ORDER BY s.sched DESC
LIMIT $2
"""

# Resolved-aware dedup, mirroring `flow_health._SUPPRESSED_SQL`/`_OPEN_SQL`: an
# alert suppresses the next one only while no recovery row was written after it,
# so a post that gets stuck, publishes, and gets stuck again alerts twice.
_STUCK_SUPPRESSED_SQL = f"""
SELECT DISTINCT a.target_id
FROM audit_log a
WHERE a.action = '{STUCK_ALERT_ACTION}'
  AND a.target_type = '{STUCK_TARGET_TYPE}'
  AND a.target_id = ANY($1::text[])
  AND a.created_at > now() - make_interval(hours => $2)
  AND NOT EXISTS (
      SELECT 1 FROM audit_log r
      WHERE r.action = '{STUCK_RECOVERY_ACTION}'
        AND r.target_type = '{STUCK_TARGET_TYPE}'
        AND r.target_id = a.target_id
        AND r.created_at > a.created_at
  )
"""

_STUCK_OPEN_SQL = f"""
SELECT DISTINCT a.target_id
FROM audit_log a
WHERE a.action = '{STUCK_ALERT_ACTION}'
  AND a.target_type = '{STUCK_TARGET_TYPE}'
  AND a.created_at > now() - make_interval(hours => $1)
  AND NOT EXISTS (
      SELECT 1 FROM audit_log r
      WHERE r.action = '{STUCK_RECOVERY_ACTION}'
        AND r.target_type = '{STUCK_TARGET_TYPE}'
        AND r.target_id = a.target_id
        AND r.created_at > a.created_at
  )
"""


def _stuck_card(title: str, body: str) -> str:
    return f"<b>{_html.escape(title)}</b>\n{_html.escape(body)}"


def _describe_stuck(finding: dict) -> str:
    return (
        f"  {finding.get('platform', '?')}/{finding.get('label', '?')} "
        f"post {finding.get('subject', '?')}: due {finding.get('schedule_at', '?')}, "
        f"Postiz state {finding.get('state') or 'missing'} "
        f"({finding.get('overdue_hours', '?')}h overdue)\n"
        f"    {str(finding.get('preview') or '')[:120]}"
    )


def _normalize_link(text: str, link: str) -> tuple[str, str]:
    """Reduce a markdown `link` to its bare URL, or — when `link` is empty
    but `text` embeds a markdown link — lift the first URL into `link` and
    replace each inline `[title](url)` with just its title. Bare URLs
    already in `text` or `link` are left untouched."""
    link = link.strip()
    in_link = _MD_LINK_RE.search(link)
    if in_link:
        return text, in_link.group(2)
    if not link:
        first_url = ""

        def _strip(match: re.Match) -> str:
            nonlocal first_url
            if not first_url:
                first_url = match.group(2)
            return match.group(1)

        new_text = _MD_LINK_RE.sub(_strip, text)
        if first_url:
            return new_text, first_url
    return text, link


@dataclass
class SocialActivities:
    """db_pool may be None in unit tests that only exercise pure branches;
    connector is the SocialConnector (None in tests that don't post);
    delivery is DeliveryActivities, used only by the stuck-post watchdog."""

    db_pool: asyncpg.Pool | None
    connector: Any = None
    delivery: Any = None

    async def _setting(self, key: str, default):
        if self.db_pool is None:
            return default
        val = await self.db_pool.fetchval("SELECT value FROM settings WHERE key = $1", key)
        return default if val is None else val

    async def _connected_platforms(self) -> set[str]:
        """Platforms with at least one row in `social_accounts`.

        The label→platform map is operator config and lists what the user WANTS
        to publish to; this is what AEGIS actually CAN publish to. In prod they
        differ (`x` and `youtube` are mapped with no account behind them), and
        every #183 symptom comes from treating the map as if it were this set.
        """
        if self.db_pool is None:
            return set()
        rows = await self.db_pool.fetch("SELECT DISTINCT platform FROM social_accounts")
        return {r["platform"] for r in rows}

    async def _comment_missing_platforms(
        self, task_id: str, missing: list[str], *, terminal: bool
    ) -> None:
        """Tell the user, once, that a labeled platform can't be published to.

        Deterministic temp_id → idempotent across retries and across the two
        callers (`enqueue_outbox` for a partial gap, `retire_unpublishable_tasks`
        when nothing at all can publish).
        """
        if self.db_pool is None:
            return
        tail = (
            "Nothing on this task can publish, so AEGIS is removing the publish "
            "label — re-add it once the channel is connected."
            if terminal
            else "The other labeled channels still go out, and this task closes "
            "once they do."
        )
        note = TodoistConnector.build_note_add_command(
            task_id,
            "⚠️ AEGIS could not publish to: "
            + ", ".join(missing)
            + " — no connected account. Connect it on the admin Social page "
            "(or remove the label). " + tail,
        )
        await self.db_pool.execute(
            "INSERT INTO todoist_outbox (temp_id, command, status) "
            "VALUES ($1, $2, 'pending') ON CONFLICT (temp_id) DO NOTHING",
            f"social-missing-{task_id}-{'.'.join(sorted(missing))}",
            note,
        )

    async def _postiz_creds(self) -> tuple[str, str]:
        """Postiz base URL + API key.

        Deliberately delegates to `SocialConnector._postiz_creds` rather than
        re-reading the settings table here: the storage shape (plain
        `integration:postiz_url`, encrypted `integration:postiz_api_key`, boot
        snapshot as fallback) is the connector's contract, and a second copy of
        it would rot silently the day that changes.
        """
        if self.connector is None:
            return "", ""
        return await self.connector._postiz_creds()

    @activity.defn
    async def find_due_posts(
        self, lookahead_minutes: int = 10, default_post_hour: int = 9
    ) -> list[dict]:
        """Open @publish tasks ready to card, without outbox rows yet.

        Native-transport platforms post immediately on approval, so they card
        just-in-time (post_at within the lookahead). A task whose labeled
        platforms are ALL Postiz-mirrored cards as soon as it exists, however
        far out post_at is — Postiz itself holds the post until schedule_at,
        so an approved launch calendar can load days ahead (#60). Dedup is the
        outbox row + deterministic approval-child id, so the unbounded window
        can't re-card.

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
            WHERE s.post_at IS NOT NULL
            ORDER BY s.post_at, s.id
            """,
            publish_label,
            user_tz,
            default_post_hour,
        )

        # Platforms whose FIRST account (the one enqueue_outbox will pick) is
        # Postiz-mirrored — those posts are scheduled in Postiz, not published
        # on approval, so their card-eligibility ignores the lookahead.
        postiz_platforms = {
            r["platform"]
            for r in await self.db_pool.fetch(
                "SELECT DISTINCT ON (platform) platform, "
                "(meta ? 'postiz_integration_id') AS postiz "
                "FROM social_accounts ORDER BY platform, id"
            )
            if r["postiz"]
        }
        cutoff = datetime.now(UTC) + timedelta(minutes=lookahead_minutes)

        due: list[dict] = []
        for r in rows:
            labels = list(r["labels"] or [])
            platforms = [p for p, lab in platform_labels.items() if lab in labels]
            if not platforms:
                activity.logger.warning(
                    "social_find_due_no_platform_label task_id=%s labels=%s", r["id"], labels
                )
                continue
            all_postiz = all(p in postiz_platforms for p in platforms)
            if not all_postiz and r["post_at"] > cutoff:
                # At least one platform posts natively (immediately on
                # approval) — keep the just-in-time window for the whole task.
                continue
            text, link = _normalize_link(r["content"], (r["description"] or "").strip())
            due.append(
                {
                    "task_id": r["id"],
                    "text": text,
                    "link": link,
                    "platforms": platforms,
                    # The Todoist due time — Postiz-transport posts are
                    # scheduled in Postiz for exactly this moment.
                    "post_at": r["post_at"].isoformat(),
                }
            )
        activity.logger.info("social_find_due_posts found=%d", len(due))
        return due

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
        if missing:
            # Surface the gap: a labeled platform with no connected account is
            # NOT silently dropped — the user gets one comment naming it. It no
            # longer BLOCKS completion, though (#183): the connected platforms
            # publish and complete_posted_tasks closes the task, instead of
            # leaving it open forever with nothing left to happen.
            await self._comment_missing_platforms(task_id, missing, terminal=False)
        activity.logger.info(
            "social_enqueue_outbox task_id=%s queued=%d missing=%s", task_id, queued, missing
        )
        return {"queued": queued, "missing_accounts": missing}

    @activity.defn
    async def drain_social_outbox(self) -> dict:
        """Post pending rows; mark posted, or bump attempt_count (failed at cap)."""
        # ponytail: at-least-once ceiling. If the connector POSTs successfully
        # but this worker dies (or the activity times out) before the row flips
        # to 'posted', the retry re-POSTs → a possible duplicate. Postiz's public
        # CreatePostDto has NO idempotency/dedup key (body is {type, shortLink,
        # date, tags, posts:[...]}), so there is no clean way to make the POST
        # exactly-once here. Accepted for now — the window is a mid-post crash,
        # not the common path. Revisit if Postiz adds a client-supplied key.
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
        """Enqueue item_complete (via todoist_outbox) for tasks whose every
        *publishable* labeled platform has posted.

        A task completes only when (a) all its outbox rows are 'posted' AND
        (b) every labeled platform that HAS a connected account has a posted
        row. Requirement (b) exists because a platform with a connected account
        but a still-pending row would otherwise be invisible to (a)'s
        all-rows-posted check on a partially-enqueued task.

        A labeled platform with NO connected account is excluded from (b)
        (#183). It used to block completion forever "so the user notices",
        which gave that task no terminal state at all: one Todoist comment and
        then permanent silence, since nothing would ever create the outbox row
        it was waiting for. The comment (enqueue_outbox) is the notice; the task
        closes once everything that CAN publish has.

        Idempotent: the deterministic temp_id social-complete-<task_id> makes
        re-runs no-ops until the 5-min TodoistSyncFlow drains the command and
        the task flips is_completed in the projection.
        """
        if self.db_pool is None:
            return {"completed": 0}
        platform_labels: dict = await self._setting("social_platform_labels", {"x": "x"})
        connected = await self._connected_platforms()
        rows = await self.db_pool.fetch(
            """
            SELECT o.todoist_task_id AS task_id, t.labels AS labels,
                   array_agg(DISTINCT a.platform) AS posted_platforms
            FROM social_outbox o
            JOIN todoist_tasks t ON t.id = o.todoist_task_id
            JOIN social_accounts a ON a.id = o.account_id
            WHERE o.todoist_task_id IS NOT NULL AND NOT t.is_completed
            GROUP BY o.todoist_task_id, t.labels
            HAVING count(*) FILTER (WHERE o.status <> 'posted') = 0
            """
        )
        completed = 0
        for r in rows:
            labels = set(r["labels"] or [])
            intended = {p for p, lab in platform_labels.items() if lab in labels}
            missing = (intended & connected) - set(r["posted_platforms"] or [])
            if missing:
                activity.logger.warning(
                    "social_complete_skipped_missing_platform task_id=%s missing=%s",
                    r["task_id"],
                    sorted(missing),
                )
                continue
            unpublishable = sorted(intended - connected)
            if unpublishable:
                activity.logger.warning(
                    "social_complete_ignoring_unconnected_platform task_id=%s platforms=%s",
                    r["task_id"],
                    unpublishable,
                )
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
    async def sync_postiz_channels(self, min_interval_minutes: int = 60) -> dict:
        """Refresh the `social_accounts` mirror from Postiz's connected channels.

        `SocialPublishFlow` resolves a Todoist platform label against
        `social_accounts`, so a channel connected in Postiz is unpublishable
        until it is mirrored. That mirror used to be refreshed ONLY by a human
        clicking "Sync Postiz channels" on the admin Flows page (#182), which
        makes the most fragile step in the publish path a manual one.

        Throttled by the mirror's own watermark rather than a schedule of its
        own: `mirror_integrations` bumps `updated_at` on every pass, so the
        newest Postiz-mirrored row IS the last successful sync. At the flow's
        5-min cadence and a 60-min interval that is ~24 Postiz calls a day with
        a ≤1h staleness bound. (Degenerate case: an install with zero mirrored
        channels has no watermark and therefore syncs every tick — which is
        exactly the install that most needs the next connect to land fast.)

        Never raises. A Postiz outage must not stop the publish tick that
        follows from draining an already-queued post, so failures come back as
        a status and a warning line.
        """
        idle = {"synced": 0, "skipped_disabled": 0}
        # Only the pool is guarded here. A missing connector is handled once, in
        # `_postiz_creds`, which answers ("", "") — a second check for it here
        # would be a guard that can only ever be masked by that one.
        if self.db_pool is None:
            return {**idle, "status": "unconfigured"}
        last = await self.db_pool.fetchval(
            "SELECT max(updated_at) FROM social_accounts "
            "WHERE meta ? 'postiz_integration_id'"
        )
        if last is not None and last > datetime.now(UTC) - timedelta(
            minutes=min_interval_minutes
        ):
            return {**idle, "status": "throttled"}
        base_url, api_key = await self._postiz_creds()
        if not base_url or not api_key:
            return {**idle, "status": "unconfigured"}
        try:
            result = await social_channels.sync_postiz_channels(
                self.db_pool, base_url, api_key
            )
        except Exception as exc:  # noqa: BLE001 — degrade, never block publishing
            activity.logger.warning("social_sync_channels_failed err=%s", str(exc)[:200])
            return {**idle, "status": "failed"}
        activity.logger.info(
            "social_sync_channels synced=%d skipped_disabled=%d",
            result["synced"],
            result["skipped_disabled"],
        )
        return {**result, "status": "ok"}

    @activity.defn
    async def retire_unpublishable_tasks(self, limit: int = 20) -> dict:
        """Terminate open @publish tasks that no connected account can publish.

        The #183 hole: `social_platform_labels` maps platforms (prod maps `x`
        and `youtube`) that have no `social_accounts` row. A task labeled for
        only those cards, fails to enqueue, gets one "no connected account"
        Todoist comment — and then nothing else ever happens to it. It is not
        completed, not re-carded with an action, not closed: it just sits open
        while every later tick re-runs the same dead end.

        Terminal state chosen: comment once, then REVOKE the publish label
        (exactly what the card's "skip" choice does). Rejected alternatives:
        *completing* the task destroys content the user still means to post,
        and *pruning the label map at sync time* rewrites operator config —
        they'd have to re-add `x` after connecting it, and it would not help
        the partial case (some platforms connected, some not) at all.

        Tasks that already have an outbox row are excluded: something did
        publish for them, so `complete_posted_tasks` owns their ending.
        """
        if self.db_pool is None:
            return {"retired": 0}
        publish_label = str(await self._setting("social_publish_label", "publish"))
        platform_labels: dict = await self._setting("social_platform_labels", {"x": "x"})
        connected = await self._connected_platforms()
        rows = await self.db_pool.fetch(
            """
            SELECT t.id, t.labels
            FROM todoist_tasks t
            WHERE NOT t.is_completed
              AND $1 = ANY(t.labels)
              AND NOT EXISTS (
                    SELECT 1 FROM social_outbox o WHERE o.todoist_task_id = t.id
                  )
            ORDER BY t.id
            LIMIT $2
            """,
            publish_label,
            limit,
        )
        retired = 0
        for r in rows:
            labels = set(r["labels"] or [])
            intended = {p for p, lab in platform_labels.items() if lab in labels}
            # No platform label at all is a DIFFERENT problem (the user may be
            # mid-edit) — find_due_posts already warns about it. Don't strip a
            # label on a task whose intent we can't read.
            if not intended or (intended & connected):
                continue
            await self._comment_missing_platforms(r["id"], sorted(intended), terminal=True)
            await self.unpublish_task(r["id"])
            retired += 1
            activity.logger.warning(
                "social_retired_unpublishable task_id=%s platforms=%s",
                r["id"],
                sorted(intended),
            )
        activity.logger.info("social_retire_unpublishable retired=%d", retired)
        return {"retired": retired}

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
    async def refresh_post_metrics(
        self, window_days: int = 14, lookahead_days: int = 45, max_rows: int = 200
    ) -> dict:
        """Pull Postiz analytics for recently-posted rows into social_outbox.metrics.

        Only Postiz-routed accounts are touched (native X posts have no
        equivalent analytics call here). Harmless when
        social_publishing_enabled is false — this only refreshes metrics on
        rows that are ALREADY posted, it doesn't gate on the publishing kill
        switch, so SocialMetricsFlow needs no such gate either.

        `lookahead_days` bounds the forward half of the Postiz `GET /posts`
        window. It used to be a hardcoded **1 day**, which meant every post
        scheduled further out than tomorrow was absent from the response and
        fell through to `state: "unknown"` — 31 of prod's 35 unknown rows on
        2026-08-02 were simply future-scheduled, so `unknown` carried no
        information and nothing could be built on it (#225). 45 days covers the
        longest real campaign (the book launch runs Aug 2 → Aug 31, 30 days)
        with ~50% headroom for a calendar authored a couple of weeks ahead of
        its first slot. `GET /posts` is one unpaginated call (~70 posts today),
        so the wider window costs nothing.

        Eligibility is `created_at` recency **or** a `schedule_at` that is still
        ahead / recently past. The old `created_at`-only filter aged long-lead
        posts out of monitoring before they published: three prod rows created
        2026-07-15 were frozen at a 2026-07-29 snapshot and would never have
        refreshed again.

        `max_rows` bounds the per-pass Postiz analytics calls, one per row.
        Hitting it is logged loudly — silent truncation is how a stuck post
        would slip past the watchdog that reads what this writes.
        """
        if self.db_pool is None or self.connector is None:
            return {"refreshed": 0, "failed": 0}

        now = datetime.now(UTC)
        posts_by_ref: dict[str, dict] = {}
        try:
            posts = await self.connector.list_posts_window(
                (now - timedelta(days=window_days)).isoformat(),
                (now + timedelta(days=lookahead_days)).isoformat(),
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
            f"""
            SELECT o.id, o.posted_ref
            FROM social_outbox o
            JOIN social_accounts a ON a.id = o.account_id
            WHERE o.status = 'posted'
              AND o.posted_ref IS NOT NULL
              AND a.meta ? 'postiz_integration_id'
              AND (
                    o.created_at > now() - make_interval(days => $1)
                    OR {_SCHEDULE_AT} > now() - make_interval(days => $1)
                  )
            ORDER BY o.created_at DESC
            LIMIT $2
            """,
            window_days,
            max_rows,
        )
        if len(rows) >= max_rows:
            activity.logger.warning(
                "social_refresh_post_metrics_truncated max_rows=%d — eligible rows were "
                "dropped this pass; raise SocialMetricsFlow's max_rows",
                max_rows,
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

    # -- stuck-post watchdog (#225) ----------------------------------------

    @activity.defn
    async def find_stuck_posts(self, overdue_hours: int = 6, limit: int = 50) -> list[dict]:
        """Postiz-routed posts past their `schedule_at` that Postiz never published.

        Runs immediately after `refresh_post_metrics` inside SocialMetricsFlow,
        so `metrics.state` is the state Postiz reported seconds ago — checking a
        day-old snapshot would be checking nothing.

        `overdue_hours` is a false-positive margin, not a detection budget: the
        flow is daily, so latency is set by the schedule either way. Its job is
        to absorb the skew between AEGIS's intended `schedule_at` (a Todoist due
        time) and Postiz's own publish + analytics lag. 6h is comfortably longer
        than any observed skew (seconds) and comfortably shorter than the 24h
        cadence, so nothing that came due since the previous run can slip
        through the gap.

        A row with no parseable `schedule_at` is skipped rather than guessed —
        "overdue" is undefined without a due time, and guessing would either
        alert forever or never.

        Returns [] (never raises) when there is no pool.
        """
        if self.db_pool is None:
            return []
        rows = await self.db_pool.fetch(_STUCK_SQL, overdue_hours, limit)
        now = datetime.now(UTC)
        out = [
            {
                # The Postiz post id: unique per outbox row, and the thing an
                # operator pastes into Postiz to look the post up. It is the
                # audit/mute subject, so mutes read `social-stuck:<postiz id>`.
                "subject": str(r["posted_ref"]),
                "outbox_id": r["id"],
                "todoist_task_id": r["todoist_task_id"],
                "platform": r["platform"],
                "label": r["label"],
                "schedule_at": r["sched"].isoformat(),
                "overdue_hours": int((now - r["sched"]).total_seconds() // 3600),
                "state": r["state"],
                "metrics_at": r["metrics_at"].isoformat() if r["metrics_at"] else None,
                "preview": r["preview"],
            }
            for r in rows
        ]
        activity.logger.info("social_find_stuck_posts found=%d", len(out))
        return out

    @activity.defn
    async def report_stuck_posts(
        self,
        findings: list[dict],
        agent_id: str = "sebas",
        dedup_hours: int = 168,
        recovery_hours: int = 720,
    ) -> dict:
        """Notify about NEW stuck posts, and about posts that finally published.

        One chat card per run, not one per post. A subject already alerted
        within `dedup_hours` with no recovery since is silent, so a post wedged
        for a week produces one alert, not seven. `dedup_hours` defaults above
        the flow's own 24h cadence — a shorter window would not dedup at all.

        Must be called even when `findings` is empty: that is exactly when the
        recovery notice fires, and the recovery row is what re-arms dedup.
        """
        result = {"alerted": 0, "deduped": 0, "muted": 0, "recovered": 0}
        if self.db_pool is None:
            return result

        subjects = [s for s in (str(f.get("subject") or "") for f in findings) if s]

        async with self.db_pool.acquire() as conn:
            muted: set[str] = set()
            suppressed: set[str] = set()
            if subjects:
                muted = {
                    row["mute_key"][len(STUCK_MUTE_PREFIX) :]
                    for row in await conn.fetch(
                        "SELECT mute_key FROM alert_mutes "
                        "WHERE mute_key = ANY($1::text[]) AND muted_until > now()",
                        [STUCK_MUTE_PREFIX + s for s in subjects],
                    )
                }
                suppressed = {
                    row["target_id"]
                    for row in await conn.fetch(_STUCK_SUPPRESSED_SQL, subjects, dedup_hours)
                }
            open_subjects = {
                row["target_id"] for row in await conn.fetch(_STUCK_OPEN_SQL, recovery_hours)
            }

        fresh = [
            f
            for f in findings
            if f.get("subject")
            and f["subject"] not in muted
            and f["subject"] not in suppressed
        ]
        result["muted"] = sum(1 for f in findings if f.get("subject") in muted)
        result["deduped"] = sum(1 for f in findings if f.get("subject") in suppressed)

        if fresh:
            body = (
                "Postiz accepted these posts but never published them:\n"
                + "\n".join(_describe_stuck(f) for f in fresh)
                + "\n\nCheck Postiz first: a stalled orchestrator shows 0 pollers on "
                "`temporal task-queue describe --task-queue main`. "
                "`docker service update --force postiz_postiz` revives it — reschedule "
                "overdue posts BEFORE reviving or they all publish at once.\n"
                f"Silence one: INSERT INTO alert_mutes (mute_key, muted_until) "
                f"VALUES ('{STUCK_MUTE_PREFIX}<postiz post id>', now() + interval '2 days');"
            )
            await safe_send_message(
                self.delivery,
                agent_id=agent_id,
                message=_stuck_card(f"[SOCIAL] {len(fresh)} post(s) stuck in Postiz", body),
                log_event="social_stuck_notify_failed",
            )
            await self._audit_stuck(STUCK_ALERT_ACTION, fresh)
            result["alerted"] = len(fresh)

        recovered = sorted(open_subjects - set(subjects))
        if recovered:
            await safe_send_message(
                self.delivery,
                agent_id=agent_id,
                message=_stuck_card(
                    f"[SOCIAL OK] {len(recovered)} post(s) published",
                    "No longer stuck:\n" + "\n".join(f"  {s}" for s in recovered),
                ),
                log_event="social_stuck_recovery_notify_failed",
            )
            await self._audit_stuck(
                STUCK_RECOVERY_ACTION, [{"subject": s, "state": "recovered"} for s in recovered]
            )
            result["recovered"] = len(recovered)

        return result

    async def _audit_stuck(self, action: str, findings: list[dict]) -> None:
        from aegis.observability import log_audit

        for f in findings:
            await log_audit(
                self.db_pool,
                actor=STUCK_ACTOR,
                action=action,
                target_type=STUCK_TARGET_TYPE,
                target_id=str(f["subject"]),
                details={k: v for k, v in f.items() if k != "subject"},
            )
