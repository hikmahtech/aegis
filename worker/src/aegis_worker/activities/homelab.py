"""Shared Homelab Guardian activities: drift persistence + notify.

Note: drift detection itself is a pure function in
`aegis_worker.flows.service_drift._compute_drift_inline`, which runs inline
in the workflow to avoid a round-trip to an activity executor.
"""

from __future__ import annotations

import html as _html
from dataclasses import dataclass
from datetime import UTC
from typing import Any

import httpx
import structlog
from temporalio import activity

from aegis_worker.activities.delivery import safe_send_telegram

logger = structlog.get_logger()


def _format_card(title: str, body: str) -> str:
    """Render a homelab notification as a Telegram HTML card.

    The title is bolded and escaped; the body is escaped (callers that need
    embedded markup should escape selectively and pass HTML through, but
    none of homelab.py does that today)."""
    return f"<b>{_html.escape(title)}</b>\n{_html.escape(body)}"


@dataclass
class DriftRecord:
    service_name: str
    stack_name: str
    drift_type: str
    expected: dict
    actual: dict
    severity: str
    alert_key: str


@dataclass
class HomelabActivities:
    db_pool: Any
    homelab: Any  # HomelabConnector
    delivery: Any  # DeliveryActivities
    agent_id: str = "pandoras-actor"
    todoist_connector: Any = None  # TodoistConnector; wired in __main__ when available

    async def _notify_card(self, agent_id: str, title: str, body: str, log_event: str) -> None:
        """Fire-and-forget Telegram-card send shared by the notify_* activities.

        (notify_cert_alert deliberately bypasses this — see its body.)"""
        await safe_send_telegram(
            self.delivery,
            agent_id=agent_id,
            message=_format_card(title, body),
            log_event=log_event,
        )

    @activity.defn
    async def notify_pr_event(self, pr: dict) -> dict:
        """Notify (Slack) about a pull-request event — but only for repositories
        the user tracks in `resources` (kind='repository'), so the feed stays
        scoped to repos that involve them rather than every-repo noise.

        pr = {repo, number, title, author, action, url}. Untracked repos are
        skipped. Returns {notified: bool, reason?, repo}.
        """
        repo = (pr.get("repo") or "").strip()
        if not repo:
            return {"notified": False, "reason": "no_repo"}
        basename = repo.rsplit("/", 1)[-1]
        async with self.db_pool.acquire() as conn:
            tracked = await conn.fetchval(
                """
                SELECT 1 FROM resources
                WHERE kind = 'repository'
                  AND (
                    lower(metadata->>'github_repo') = lower($1)
                    OR lower(split_part(metadata->>'github_repo', '/', 2)) = lower($2)
                  )
                LIMIT 1
                """,
                repo,
                basename,
            )
        if not tracked:
            return {"notified": False, "reason": "untracked_repo", "repo": repo}
        action = pr.get("action", "updated")
        title = f"PR {action}: {repo} #{pr.get('number', '?')}"
        body = f"{pr.get('title', '')}\nby {pr.get('author', '?')}\n{pr.get('url', '')}".strip()
        await self._notify_card(self.agent_id, title, body, "github_pr_notify_failed")
        return {"notified": True, "repo": repo}

    @activity.defn
    async def persist_drifts(self, drifts: list[dict]) -> int:
        """Upsert drift rows keyed on alert_key (partial unique index).
        Returns number of NEW rows (not touched if already open)."""
        if not drifts:
            return 0
        new_count = 0
        async with self.db_pool.acquire() as conn:
            for d in drifts:
                row = await conn.fetchrow(
                    """
                    INSERT INTO pandoras_actor.homelab_drift
                      (service_name, stack_name, drift_type, expected, actual,
                       severity, alert_key)
                    VALUES ($1,$2,$3,$4,$5,$6,$7)
                    ON CONFLICT (alert_key) WHERE resolved_at IS NULL
                      DO NOTHING
                    RETURNING id
                    """,
                    d["service_name"],
                    d["stack_name"],
                    d["drift_type"],
                    d["expected"],
                    d["actual"],
                    d["severity"],
                    d["alert_key"],
                )
                if row is not None:
                    new_count += 1
        return new_count

    @activity.defn
    async def resolve_stale_drifts(self, alert_keys_still_open: list[str]) -> int:
        """Close any open drift rows whose alert_keys did NOT appear this run."""
        async with self.db_pool.acquire() as conn:
            status = await conn.execute(
                """
                UPDATE pandoras_actor.homelab_drift SET resolved_at = now()
                WHERE resolved_at IS NULL
                  AND alert_key <> ALL($1::text[])
                """,
                alert_keys_still_open or [],
            )
        return _parse_rowcount(status)

    @activity.defn
    async def notify_drift(self, payload: dict) -> None:
        """Send Telegram card with [DRIFT] prefix. Fire-and-forget safe."""
        title = f"[DRIFT][{payload['severity'].upper()}] {payload['service_name']}"
        body = (
            f"Type: {payload['drift_type']}\n"
            f"Expected: {payload['expected']}\n"
            f"Actual: {payload['actual']}\n"
            f"Detected: {payload.get('detected_at', 'now')}\n"
        )
        await self._notify_card(self.agent_id, title, body, "homelab_notify_drift_failed")

    @activity.defn
    async def find_undelivered_interactions(
        self, threshold_seconds: int = 120, window_hours: int = 24
    ) -> list[dict]:
        """Delivery watchdog: interaction rows are created BEFORE the card is
        dispatched, so a row whose delivery is unrecorded after a grace period
        was silently never delivered. A card counts as delivered if EITHER
        `telegram_message_id` (Telegram) OR `delivery_ref` (Slack / any
        channel-neutral adapter) is set — checking only `telegram_message_id`
        would false-alarm on every Slack card post-cutover. Returns recent
        undelivered rows so the next silent-undelivery regression is caught
        automatically instead of only by a manual query. The window bound
        excludes ancient rows from retired origins.

        Only `status = 'pending'` rows count: a resolved/archived card is no
        longer awaiting a user response, so an unrecorded delivery on it is not
        an actionable undelivery (e.g. a card force-resolved out-of-band, or
        archived on timeout, never gets a delivery ref). Without this guard such
        terminal rows re-fire the alert every tick for the whole 24h window."""
        if not self.db_pool:
            return []
        async with self.db_pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id::text, origin, status, created_at
                FROM interactions
                WHERE telegram_message_id IS NULL
                  AND delivery_ref IS NULL
                  AND status = 'pending'
                  AND created_at < now() - make_interval(secs => $1)
                  AND created_at > now() - make_interval(hours => $2)
                ORDER BY created_at DESC
                """,
                threshold_seconds,
                window_hours,
            )
        return [dict(r) for r in rows]

    @activity.defn
    async def notify_undelivered_interactions(self, rows: list[dict]) -> None:
        """Send one summary card via the active comms channel listing
        undelivered interaction cards. Fire-and-forget safe (plain text, no
        buttons — so it still gets through even when the failure was
        button-specific)."""
        if not rows:
            return
        by_origin: dict[str, int] = {}
        for r in rows:
            by_origin[r.get("origin") or "?"] = by_origin.get(r.get("origin") or "?", 0) + 1
        title = f"[DELIVERY] {len(rows)} undelivered interaction card(s)"
        breakdown = "\n".join(f"  {origin}: {n}" for origin, n in sorted(by_origin.items()))
        body = (
            "These interaction rows have no recorded delivery (neither "
            "telegram_message_id nor delivery_ref) past the grace window — "
            "cards that were never delivered:\n"
            f"{breakdown}\n\n"
            "Check aegis_comms logs for delivery errors."
        )
        await self._notify_card(self.agent_id, title, body, "homelab_notify_undelivered_failed")

    # ------------------------------------------------------------------
    # Telegram polling health check
    # ------------------------------------------------------------------

    _POLLING_ALERT_DEDUP_HOURS = 12  # do not re-alert within this window
    _POLLING_ALERT_ACTION = "telegram_polling_alert"

    @activity.defn
    async def check_telegram_polling_health(self, telegram_url: str) -> dict:
        """GET <comms_url>/api/health and inspect inbound-channel liveness.

        Prefers the channel-neutral `inbound` block (works for both Telegram and
        Slack); falls back to the legacy `telegram_api` block for old comms
        images. Under the Slack channel there is no `telegram_api` block, so its
        absence here is exactly what stops a false "down" alarm.

        Returns:
          {"status": "ok"}                       — inbound healthy, no action needed
          {"status": "down", "last_ok_seconds_ago": <int|None>, "last_error": <str|None>}
                                                 — inbound is down, caller should alert
          {"status": "unknown"}                  — endpoint unreachable or old image
                                                   (no inbound/telegram_api field); do nothing

        Never raises — all exceptions are caught and returned as "unknown".
        """
        if not telegram_url:
            return {"status": "unknown"}
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(f"{telegram_url.rstrip('/')}/api/health")
                if resp.status_code != 200:
                    return {"status": "unknown"}
                body = resp.json()
        except Exception as exc:
            activity.logger.warning(
                "check_telegram_polling_health_request_failed error=%s", str(exc)[:200]
            )
            return {"status": "unknown"}

        # Prefer the channel-neutral `inbound` block (the comms service's source
        # of truth for inbound liveness — telegram reachability or slack socket).
        inbound = body.get("inbound")
        if isinstance(inbound, dict):
            if inbound.get("healthy"):
                return {"status": "ok"}
            return {
                "status": "down",
                "last_ok_seconds_ago": inbound.get("last_ok_seconds_ago"),
                "last_error": inbound.get("last_error"),
            }

        # Fall back to the legacy telegram_api block for old comms images.
        tg_api = body.get("telegram_api")
        if not isinstance(tg_api, dict):
            # Old image — neither block present; treat as unknown (backward compatible)
            return {"status": "unknown"}

        if tg_api.get("reachable"):
            return {"status": "ok"}

        return {
            "status": "down",
            "last_ok_seconds_ago": tg_api.get("last_ok_seconds_ago"),
            "last_error": tg_api.get("last_error"),
        }

    @activity.defn
    async def alert_telegram_polling_down(
        self, last_ok_seconds_ago: int | None, last_error: str | None
    ) -> bool:
        """Create a Todoist Inbox task alerting that Telegram inbound polling is down.

        Deduplicates via audit_log: will not create more than one task per
        _POLLING_ALERT_DEDUP_HOURS window. Returns True if a task was created,
        False if deduped or if capture is unavailable.

        Uses Todoist (not Telegram) as the alert channel because Telegram itself
        is the thing that's down.
        """
        if not self.db_pool:
            return False

        from aegis.observability import log_audit

        async with self.db_pool.acquire() as conn:
            recent = await conn.fetchval(
                "SELECT id FROM audit_log WHERE action = $1 "
                "AND created_at > NOW() - INTERVAL '1 hour' * $2 LIMIT 1",
                self._POLLING_ALERT_ACTION,
                self._POLLING_ALERT_DEDUP_HOURS,
            )
        if recent is not None:
            activity.logger.info(
                "telegram_polling_alert_deduped within_%dh", self._POLLING_ALERT_DEDUP_HOURS
            )
            return False

        # Build human-readable label for last_ok
        okstr = "never" if last_ok_seconds_ago is None else f"{last_ok_seconds_ago}s ago"

        description_parts = [f"Last ok: {okstr}"]
        if last_error:
            description_parts.append(f"Error: {last_error[:200]}")

        # Use capture_to_inbox via direct Todoist path so we avoid an activity
        # dependency cycle (CaptureActivities is a separate dataclass).
        # Replicate the minimal capture logic: find inbox project + add task.
        from aegis.connectors.todoist import TodoistConnector

        async with self.db_pool.acquire() as conn:
            managed = await conn.fetchval(
                "SELECT value FROM settings WHERE key = 'todoist_managed_project_ids'"
            )
            kill = await conn.fetchval(
                "SELECT value FROM settings WHERE key = 'todoist_capture_enabled'"
            )
        inbox_id = (managed or {}).get("inbox") if isinstance(managed, dict) else None
        if kill is False or (isinstance(kill, dict) and kill.get("value") is False):
            activity.logger.warning("telegram_polling_alert_capture_disabled")
            return False
        if not inbox_id:
            activity.logger.warning("telegram_polling_alert_no_inbox_id")
            return False
        if self.todoist_connector is None:
            activity.logger.warning("telegram_polling_alert_no_todoist_connector")
            return False

        title = (
            f"\U0001f6a8 AEGIS inbound comms is DOWN (last ok {okstr})"
            " — button taps and messages are not being received"
        )
        cmd = TodoistConnector.build_create_item_command(
            project_id=inbox_id,
            content=title[:120],
            description="\n".join(description_parts),
            labels=["@pandora"],
        )
        result = await self.todoist_connector.commands([cmd])
        status = TodoistConnector.check_sync_status(result, [cmd["uuid"]])
        if not status["ok"]:
            # Don't write the dedup audit row on failure — the next watchdog
            # tick (15 min) retries instead of going silent for 12 hours.
            activity.logger.warning(
                "telegram_polling_alert_create_failed status=%s", str(status)[:200]
            )
            return False

        await log_audit(
            self.db_pool,
            actor="delivery-watchdog",
            action=self._POLLING_ALERT_ACTION,
            target_type="telegram",
            target_id="polling",
            details={"last_ok_seconds_ago": last_ok_seconds_ago, "last_error": last_error},
        )
        activity.logger.info("telegram_polling_alert_created last_ok=%s", okstr)
        return True

    @activity.defn
    async def collect_services(self) -> dict:
        """Collect service ls + ps. Returns plain dict."""
        env = await self.homelab.list_services()
        if not env["ok"]:
            raise RuntimeError(f"list_services: {env['error']}")
        services = env["data"]
        ps_map: dict[str, list[dict]] = {}
        for s in services:
            ps_env = await self.homelab.service_ps(s["name"])
            if ps_env["ok"]:
                ps_map[s["name"]] = ps_env["data"]
        return {
            "services": services,
            "ps_map": ps_map,
        }

    @activity.defn
    async def collect_schedules(self) -> dict:
        dagster = await self.homelab.list_dagster_schedules()
        return {
            "dagster": dagster["data"] if dagster["ok"] else [],
            "errors": {
                "dagster": None if dagster["ok"] else dagster["error"],
            },
        }

    @activity.defn
    async def upsert_schedule_health(self, collected: dict) -> list[dict]:
        """Upsert each schedule into pandoras_actor.schedule_health; return rows
        that represent a NEW problem.

        Fires on (a) first observation of broken state (no prev row + actual
        != expected, OR no prev row + consecutive_failures >= 2), OR
        (b) sustained >= 2 consecutive failures on a schedule that was
        previously healthy. A broken schedule on first sight IS noteworthy —
        we don't suppress it waiting for a second observation."""
        issues: list[dict] = []
        async with self.db_pool.acquire() as conn:
            for s in collected["dagster"]:
                expected = "RUNNING"
                actual = s["status"]
                prev = await conn.fetchrow(
                    "SELECT actual_status, consecutive_failures "
                    "FROM pandoras_actor.schedule_health WHERE source='dagster' AND schedule_name=$1",
                    s["name"],
                )
                failures = 0
                if s.get("last_run_ok") is False:
                    failures = (prev["consecutive_failures"] if prev else 0) + 1
                await conn.execute(
                    """
                    INSERT INTO pandoras_actor.schedule_health
                      (source, schedule_name, expected_status, actual_status,
                       last_run_at, last_run_ok, consecutive_failures, alert_key)
                    VALUES ('dagster', $1, $2, $3, to_timestamp($4), $5, $6, $7)
                    ON CONFLICT (source, schedule_name) DO UPDATE SET
                      actual_status=EXCLUDED.actual_status,
                      last_run_at=EXCLUDED.last_run_at,
                      last_run_ok=EXCLUDED.last_run_ok,
                      consecutive_failures=EXCLUDED.consecutive_failures,
                      checked_at=now()
                    """,
                    s["name"],
                    expected,
                    actual,
                    s.get("last_run_at"),
                    s.get("last_run_ok"),
                    failures,
                    f"dagster:{s['name']}:{actual}",
                )
                is_issue = (actual != expected) or failures >= 2
                was_issue = prev is not None and (
                    prev["actual_status"] != expected or prev["consecutive_failures"] >= 2
                )
                if is_issue and not was_issue:
                    issues.append(
                        {
                            "source": "dagster",
                            "name": s["name"],
                            "expected": expected,
                            "actual": actual,
                            "consecutive_failures": failures,
                        }
                    )
        return issues

    @activity.defn
    async def notify_schedule_issue(self, issue: dict) -> None:
        title = f"[SCHED][{issue['source'].upper()}] {issue['name']}"
        body = (
            f"Expected: {issue['expected']}\n"
            f"Actual: {issue['actual']}\n"
            f"Consecutive failures: {issue['consecutive_failures']}\n"
        )
        await self._notify_card(self.agent_id, title, body, "homelab_notify_schedule_issue_failed")

    _CERT_THRESHOLDS = (14, 7, 0)  # days

    @activity.defn
    async def probe_and_upsert_cert(self, domain: str) -> dict | None:
        """Probe TLS, upsert pandoras_actor.cert_expiry row, return alert payload if
        a new threshold crossed."""
        from datetime import datetime

        env = await self.homelab.probe_tls(domain)
        if not env["ok"]:
            return {"domain": domain, "error": env["error"], "unreachable": True}
        info = env["data"]
        not_after = info["not_after"]
        if not_after.tzinfo is None:
            not_after = not_after.replace(tzinfo=UTC)
        days = int((not_after - datetime.now(UTC)).total_seconds() // 86400)
        async with self.db_pool.acquire() as conn:
            prev = await conn.fetchrow(
                "SELECT last_alert_threshold FROM pandoras_actor.cert_expiry "
                "WHERE domain=$1 AND cert_serial=$2",
                domain,
                info["serial"],
            )
            crossed: int | None = None
            for threshold in self._CERT_THRESHOLDS:
                if days <= threshold and (
                    prev is None
                    or prev["last_alert_threshold"] is None
                    or prev["last_alert_threshold"] > threshold
                ):
                    crossed = threshold
                    break
            await conn.execute(
                """
                INSERT INTO pandoras_actor.cert_expiry
                  (domain, cert_serial, not_after, days_until_expiry,
                   last_alert_threshold)
                VALUES ($1,$2,$3,$4,$5)
                ON CONFLICT (domain, cert_serial) DO UPDATE SET
                  checked_at=now(),
                  not_after=EXCLUDED.not_after,
                  days_until_expiry=EXCLUDED.days_until_expiry,
                  last_alert_threshold=COALESCE(EXCLUDED.last_alert_threshold,
                                                pandoras_actor.cert_expiry.last_alert_threshold)
                """,
                domain,
                info["serial"],
                not_after,
                days,
                crossed,
            )
        if crossed is not None:
            return {
                "domain": domain,
                "days": days,
                "threshold": crossed,
                "not_after": not_after.isoformat(),
            }
        return None

    @activity.defn
    async def notify_cert_alert(self, alert: dict) -> None:
        # ----------------------------------------------------------------
        # Intentional bypass of safe_send_telegram.
        #
        # Why:
        #   probe_and_upsert_cert COMMITS `last_alert_threshold` BEFORE this
        #   activity runs. Once committed, the next probe will NOT re-fire
        #   the same threshold (it's been "alerted"). If the Telegram send
        #   silently fails (raised exception OR {"ok": false} body), the
        #   user never sees the warning and there's no retry path on the
        #   next tick.
        #
        # What this gives us instead:
        #   - ERROR-level log (NOT WARN like safe_send_telegram) carrying
        #     domain + threshold so the ops triage filter picks it up.
        #   - Inline handling of BOTH raise-paths AND {"ok": false} bodies,
        #     mirroring safe_send_telegram's two-branch shape but with the
        #     stickier ERROR level.
        #
        # If someone wants to consolidate this into safe_send_telegram:
        #   the helper would need to grow a `sticky=True` mode that escalates
        #   the log level when the caller has already committed irreversible
        #   state. Don't do it now — single-caller surface, low ROI.
        # ----------------------------------------------------------------
        if alert.get("unreachable"):
            title = f"[CERT][UNREACHABLE] {alert['domain']}"
            body = f"TLS probe failed: {alert.get('error', '')}"
        else:
            title = f"[CERT][T-{alert['threshold']}d] {alert['domain']}"
            body = f"Days until expiry: {alert['days']}\nNot after: {alert['not_after']}"
        try:
            result = await self.delivery.send_telegram(
                agent_id=self.agent_id, message=_format_card(title, body), chat_id=0
            )
        except Exception as exc:
            activity.logger.error(
                "notify_cert_alert_delivery_failed domain=%s threshold=%s err=%s",
                alert.get("domain"),
                alert.get("threshold"),
                str(exc)[:200],
            )
            return
        if isinstance(result, dict) and not result.get("ok"):
            activity.logger.error(
                "notify_cert_alert_delivery_failed domain=%s threshold=%s err=%s",
                alert.get("domain"),
                alert.get("threshold"),
                str(result.get("error", "ok=false"))[:200],
            )

    @activity.defn
    async def audit_backup_set(self, backup_set: str, nfs_base_path: str) -> list[dict]:
        """Freshness + size check for a backup set under
        `<nfs_base_path>/<backup_set>/daily/`.

        The backup tooling overwrites the latest dumps in `daily/` (no per-run
        timestamp in the filename) — postgres as flat `<db>.dump`, clickhouse
        as per-table `<db>/<table>.native`. So freshness is the newest file's
        mtime and size-drift is the set's total bytes vs the previous audit run
        (read back from `backup_health`). Returns a single-element list (one
        summary per set) — the flow treats stale/abnormal/error as an alert.
        """
        from datetime import datetime

        subpath = f"{nfs_base_path}/{backup_set}/daily"
        env = await self.homelab.list_backups(subpath)
        if not env["ok"]:
            return [
                {
                    "backup_set": backup_set,
                    "error": env["error"],
                    "stale": True,
                    "size_delta_pct": 0.0,
                }
            ]
        items = env["data"]
        if not items:
            return [
                {
                    "backup_set": backup_set,
                    "error": "no backups",
                    "stale": True,
                    "size_delta_pct": 0.0,
                }
            ]
        newest = max(items, key=lambda x: x["mtime_epoch"])
        last_backup_at = datetime.fromtimestamp(newest["mtime_epoch"], tz=UTC)
        age_h = (datetime.now(UTC) - last_backup_at).total_seconds() / 3600
        stale = age_h > 30  # daily cadence + slack
        total_bytes = sum(i["size_bytes"] for i in items)
        async with self.db_pool.acquire() as conn:
            # size-drift vs the previous audit run for this set (the overwrite
            # layout keeps no on-disk history, so backup_health IS the history).
            prev = await conn.fetchval(
                "SELECT size_bytes FROM pandoras_actor.backup_health "
                "WHERE backup_set = $1 ORDER BY checked_at DESC LIMIT 1",
                backup_set,
            )
            delta_pct = round((total_bytes - prev) / prev * 100, 2) if prev else 0.0
            abnormal_size = abs(delta_pct) > 20.0
            await conn.execute(
                """
                INSERT INTO pandoras_actor.backup_health
                  (backup_set, last_backup_at, size_bytes, size_delta_pct, notes)
                VALUES ($1, $2, $3, $4, $5)
                """,
                backup_set,
                last_backup_at,
                total_bytes,
                delta_pct,
                f"files={len(items)} stale={stale} abnormal={abnormal_size}",
            )
        return [
            {
                "backup_set": backup_set,
                "last_backup_at": last_backup_at.isoformat(),
                "size_bytes": total_bytes,
                "size_delta_pct": delta_pct,
                "stale": stale,
                "abnormal_size": abnormal_size,
            }
        ]

    @activity.defn
    async def notify_backup_issue(self, summary: dict) -> None:
        if summary.get("error"):
            title = f"[BACKUP][CRITICAL] {summary['backup_set']}"
            body = f"Error: {summary['error']}"
        elif summary.get("stale"):
            title = f"[BACKUP][STALE] {summary['backup_set']}"
            body = f"Last backup: {summary['last_backup_at']}\nSize: {summary['size_bytes']} bytes"
        elif summary.get("abnormal_size"):
            title = f"[BACKUP][SIZE] {summary['backup_set']}"
            body = (
                f"Size delta vs 6-day mean: {summary['size_delta_pct']}%\n"
                f"Latest size: {summary['size_bytes']} bytes"
            )
        else:
            return
        await self._notify_card(self.agent_id, title, body, "homelab_notify_backup_issue_failed")

    @activity.defn
    async def run_restore_drill(
        self, backup_set: str, drill_host: str, dry_run: bool = False
    ) -> dict:
        """Run monthly restore drill. When dry_run=True no SSH is performed
        and a synthetic success is returned. The real path invokes a remote
        script (deployed via Ansible) that restores into a throwaway
        container and tears it down in a trap."""
        import time as _time

        start = _time.monotonic()
        if dry_run:
            ok = True
            notes = "dry-run: no remote invocation"
            elapsed_ms = int((_time.monotonic() - start) * 1000)
        else:
            rc, out, err = await self.homelab._docker(
                "--context",
                drill_host,
                "run",
                "--rm",
                "-v",
                "/opt/aegis:/opt/aegis:ro",
                "alpine:3.19",
                "sh",
                "-c",
                f"/opt/aegis/aegis-homelab-restore-drill {backup_set}",
                timeout=900,
            )
            ok = rc == 0
            notes = (out + err)[-500:]
            elapsed_ms = int((_time.monotonic() - start) * 1000)
        async with self.db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO pandoras_actor.backup_health
                  (backup_set, restore_drill_at, restore_drill_ok,
                   restore_drill_ms, notes)
                VALUES ($1, now(), $2, $3, $4)
                """,
                backup_set,
                ok,
                elapsed_ms,
                notes,
            )
        return {"backup_set": backup_set, "ok": ok, "ms": elapsed_ms, "notes": notes}


def _extract_image_sha(image: str) -> str:
    if ":" in image:
        return image.rsplit(":", 1)[1]
    return ""


def _extract_repo(image: str) -> str:
    if ":" in image:
        return image.rsplit(":", 1)[0]
    return image


def _parse_rowcount(status: str) -> int:
    if not status:
        return 0
    try:
        return int(status.split()[-1])
    except (ValueError, IndexError):
        return 0
