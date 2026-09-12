"""Shared Homelab Guardian activities: drift persistence + notify.

Note: drift detection itself is a pure function in
`aegis_worker.flows.service_drift._compute_drift_inline`, which runs inline
in the workflow to avoid a round-trip to an activity executor.
"""

from __future__ import annotations

import html as _html
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog
from temporalio import activity

from aegis_worker.activities.delivery import safe_send_message

logger = structlog.get_logger()

# The ingress canary's patience. Long enough to clear a slow TLS handshake
# through a tunnel, short enough that a hung proxy is a failure inside one
# 2-minute heartbeat tick rather than a timeout on the activity itself.
_INGRESS_TIMEOUT_S = 10.0


def _format_card(title: str, body: str) -> str:
    """Render a homelab notification as a light-HTML chat card.

    The title is bolded and escaped; the body is escaped (callers that need
    embedded markup should escape selectively and pass HTML through, but
    none of homelab.py does that today)."""
    return f"<b>{_html.escape(title)}</b>\n{_html.escape(body)}"


@dataclass
class HomelabActivities:
    db_pool: Any
    homelab: Any  # HomelabConnector
    delivery: Any  # DeliveryActivities
    agent_id: str = "pandoras-actor"
    heartbeat_ping_url: str = ""  # healthchecks.io dead-man URL; "" = disabled
    infra_cluster: str = ""       # Prometheus cluster label for synthetic alerts

    async def _notify_card(self, agent_id: str, title: str, body: str, log_event: str) -> None:
        """Fire-and-forget chat-card send shared by the notify_* activities.

        (notify_cert_alert deliberately bypasses this — see its body.)"""
        await safe_send_message(
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
        """Send chat card with [DRIFT] prefix. Fire-and-forget safe."""
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
        `telegram_message_id` (legacy column) OR `delivery_ref` (Slack / any
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
    # Comms inbound-channel health check (the alert itself is the delivery
    # watchdog's `comms_inbound_down` problem on the hub)
    # ------------------------------------------------------------------

    @activity.defn
    async def check_comms_inbound_health(self, comms_url: str) -> dict:
        """GET <comms_url>/api/health and inspect inbound-channel liveness.

        Reads the channel-neutral `inbound` block (the comms service's source
        of truth for inbound liveness — the Slack Socket Mode probe).

        Returns:
          {"status": "ok"}                       — inbound healthy, no action needed
          {"status": "down", "last_ok_seconds_ago": <int|None>, "last_error": <str|None>}
                                                 — inbound is down, caller should alert
          {"status": "unknown"}                  — endpoint unreachable or old image
                                                   (no inbound field); do nothing

        Never raises — all exceptions are caught and returned as "unknown".
        """
        if not comms_url:
            return {"status": "unknown"}
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(f"{comms_url.rstrip('/')}/api/health")
                if resp.status_code != 200:
                    return {"status": "unknown"}
                body = resp.json()
        except Exception as exc:
            activity.logger.warning(
                "check_comms_inbound_health_request_failed error=%s", str(exc)[:200]
            )
            return {"status": "unknown"}

        inbound = body.get("inbound")
        if not isinstance(inbound, dict):
            # No inbound block; treat as unknown (backward compatible).
            return {"status": "unknown"}
        if inbound.get("healthy"):
            return {"status": "ok"}
        return {
            "status": "down",
            "last_ok_seconds_ago": inbound.get("last_ok_seconds_ago"),
            "last_error": inbound.get("last_error"),
        }

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
    async def probe_ingress(self, url: str, expect_status: int = 0) -> dict:
        """One GET at `url`, to test the way IN to AEGIS from outside.

        Core's healthcheck runs inside core's own container, so it stays green
        while the proxy in front of it is a black hole. That is how a 3.5-hour
        outage went entirely unnoticed on 2026-09-11 (#492): Traefik could not
        reach `aegis_core`, so no GitHub and no Todoist webhook arrived, and
        nothing said so. An outside monitor cannot tell AEGIS when the way in
        is the broken thing — the only party who can notice is AEGIS reaching
        out, which is this.

        **By default any HTTP answer from the right host means the path
        works**, 401/404/405 included: the question is whether bytes reach
        core, not what core makes of them, and the useful probe targets are the
        ones an identity proxy does not challenge. A transport error, a
        timeout, or a 5xx is a fault — a proxy with no healthy backend answers
        exactly 502 or 504, which is the signature of the outage this watches
        for.

        Two things stop that rule passing an answer core never saw:

        * **A redirect off the host is a fault.** An identity proxy in front of
          the URL sends the probe to its own login page, which cheerfully
          answers 200 forever whether or not the origin is alive — a green
          canary watching nothing. Redirects are still followed (a bare `/` to
          `/docs` is fine); landing on a different host is not.
        * **`expect_status` asserts which answer**, for the cases where the
          status alone says who replied. A proxy that lost the route to core
          serves its own 404 with a 200-shaped conscience; pin 405 (what a
          webhook path gives a bare GET) and that 404 is a fault.
        """
        target = (url or "").strip()
        if not target:
            return {"url": "", "ok": True, "status": 0, "ms": 0, "error": "", "configured": False}
        started = time.monotonic()

        def _result(ok: bool, status: int, error: str) -> dict:
            return {
                "url": target,
                "ok": ok,
                "status": status,
                "ms": int((time.monotonic() - started) * 1000),
                "error": error,
                "configured": True,
            }

        try:
            async with httpx.AsyncClient(
                timeout=_INGRESS_TIMEOUT_S, follow_redirects=True
            ) as client:
                response = await client.get(target)
        except Exception as exc:  # noqa: BLE001 — a failed probe IS the finding
            return _result(False, 0, f"{type(exc).__name__}: {str(exc)[:160]}")

        status = response.status_code
        asked = httpx.URL(target).host
        answered = response.url.host
        if answered and asked and answered != asked:
            return _result(False, status, f"redirected to {answered}, not {asked}")
        if expect_status and status != int(expect_status):
            return _result(False, status, f"HTTP {status}, expected {int(expect_status)}")
        if status >= 500:
            return _result(False, status, f"HTTP {status}")
        return _result(True, status, "")

    _CERT_THRESHOLDS = (14, 7, 0)  # days

    @activity.defn
    async def probe_and_upsert_cert(self, domain: str) -> dict:
        """Probe TLS, upsert the pandoras_actor.cert_expiry row, and report the cert.

        Returns ``{"domain", "days", "not_after", "threshold"}`` for every cert
        it can read. ``threshold`` is the 14/7/0-day mark this probe crossed
        for the first time on this cert, else ``None``. The flow sends its
        Slack card on ``threshold`` (once per mark) and builds its hub finding
        from ``days`` (every day inside 14 days, #475). A renewed cert has a
        new serial, so it gets a new row and its thresholds start again.

        An unreachable domain returns ``{"domain", "error", "unreachable":
        True}``, plus the ``days`` and ``not_after`` of the last cert seen for
        it, when there is one. A probe that could not connect has not seen a
        renewal, so the flow keeps an expiring problem open rather than
        resolving it on a network blip.

        Until #475 this returned ``None`` on any day nothing was crossed. A
        history recorded then still replays through CertRadarFlow, which
        treats ``None`` as "nothing to report".
        """
        env = await self.homelab.probe_tls(domain)
        if not env["ok"]:
            report: dict = {"domain": domain, "error": env["error"], "unreachable": True}
            async with self.db_pool.acquire() as conn:
                last_seen = await conn.fetchval(
                    "SELECT not_after FROM pandoras_actor.cert_expiry "
                    "WHERE domain=$1 ORDER BY checked_at DESC LIMIT 1",
                    domain,
                )
            if last_seen is not None:
                report.update(days=_days_until(last_seen), not_after=last_seen.isoformat())
            return report
        info = env["data"]
        not_after = info["not_after"]
        if not_after.tzinfo is None:
            not_after = not_after.replace(tzinfo=UTC)
        days = _days_until(not_after)
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
        return {
            "domain": domain,
            "days": days,
            "threshold": crossed,
            "not_after": not_after.isoformat(),
        }

    @activity.defn
    async def notify_cert_alert(self, alert: dict) -> None:
        # ----------------------------------------------------------------
        # Intentional bypass of safe_send_message.
        #
        # Why:
        #   probe_and_upsert_cert COMMITS `last_alert_threshold` BEFORE this
        #   activity runs. Once committed, the next probe will NOT re-fire
        #   the same threshold (it's been "alerted"). If the chat send
        #   silently fails (raised exception OR {"ok": false} body), the
        #   user never sees the warning and there's no retry path on the
        #   next tick.
        #
        # What this gives us instead:
        #   - ERROR-level log (NOT WARN like safe_send_message) carrying
        #     domain + threshold so the ops triage filter picks it up.
        #   - Inline handling of BOTH raise-paths AND {"ok": false} bodies,
        #     mirroring safe_send_message's two-branch shape but with the
        #     stickier ERROR level.
        #
        # If someone wants to consolidate this into safe_send_message:
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
            result = await self.delivery.send_message(
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

    # ------------------------------------------------------------------
    # Infra heartbeat (dead-man's switch for the swarm + AEGIS itself)
    # ------------------------------------------------------------------

    _HEARTBEAT_STATE_KEY = "infra_heartbeat_state"

    @staticmethod
    def _default_heartbeat_state() -> dict:
        # The per-service `confirmed_at` / `reinvestigated_at` clocks behind
        # the #138 re-investigate path are GONE: the problem hub knows when a
        # problem was first seen and when it was last investigated, so
        # `HubActivities.stale_stuck_problems` answers that question instead.
        # A state row still carrying them reads back with the extra keys and
        # nothing looks at them.
        return {
            "nodes": {},
            "stuck": [],
            "confirmed": [],
            "fail_count": 0,
        }

    @activity.defn
    async def collect_infra_state(self) -> dict:
        """One heartbeat sample: node statuses + services stuck below desired."""
        if not self.homelab:
            return {"ok": False, "nodes": {}, "stuck": [], "error": "no_homelab_connector"}
        nodes_env = await self.homelab.list_nodes()
        if not nodes_env.get("ok"):
            return {"ok": False, "nodes": {}, "stuck": [], "error": str(nodes_env.get("error"))[:200]}
        svc_env = await self.homelab.list_services()
        if not svc_env.get("ok"):
            return {"ok": False, "nodes": {}, "stuck": [], "error": str(svc_env.get("error"))[:200]}
        nodes = {n["hostname"]: n["status"] for n in nodes_env.get("data") or [] if n.get("hostname")}
        stuck = sorted(
            s["name"]
            for s in svc_env.get("data") or []
            if (s.get("replicas_desired") or 0) > 0
            and (s.get("replicas_actual") or 0) < (s.get("replicas_desired") or 0)
        )
        return {"ok": True, "nodes": nodes, "stuck": stuck, "error": ""}

    @activity.defn
    async def read_heartbeat_state(self) -> dict:
        if not self.db_pool:
            return self._default_heartbeat_state()
        row = await self.db_pool.fetchrow(
            "SELECT value FROM settings WHERE key = $1", self._HEARTBEAT_STATE_KEY
        )
        if not row or not row["value"]:
            return self._default_heartbeat_state()
        value = row["value"]
        return {**self._default_heartbeat_state(), **value} if isinstance(value, dict) else self._default_heartbeat_state()

    @activity.defn
    async def write_heartbeat_state(self, state: dict) -> None:
        if not self.db_pool:
            return
        await self.db_pool.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES ($1, $2, NOW()) "
            "ON CONFLICT (key) DO UPDATE SET value = $2, updated_at = NOW()",
            self._HEARTBEAT_STATE_KEY,
            state,
        )

    @activity.defn
    async def ping_deadman(self) -> dict:
        """Fire-and-forget healthchecks.io ping. Only called on a SUCCESSFUL
        collect, so a silent heartbeat (AEGIS/node death) stops the pings."""
        if not self.heartbeat_ping_url:
            return {"pinged": False}
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                await client.get(self.heartbeat_ping_url)
            return {"pinged": True}
        except Exception as exc:  # noqa: BLE001 — dead-man ping is never fatal
            activity.logger.warning("heartbeat_deadman_ping_failed err=%s", str(exc)[:200])
            return {"pinged": False}

    @activity.defn
    async def get_heartbeat_routing(self) -> dict:
        """Settings-derived knobs for the flow (workflows can't read Settings)."""
        return {"infra_cluster": self.infra_cluster}

    @activity.defn
    async def notify_node_transition(self, node: str, status: str) -> None:
        """Plain FYI ping for a quiet node's Down/Ready transition — no
        investigation, no task, no escalation (e.g. a dual-boot box that is
        expected to leave and rejoin the swarm). Fire-and-forget safe."""
        if status == "up":
            title = f"[NODE] {node} is back up"
            body = f"Quiet node {node} rejoined the swarm (Ready)."
        else:
            title = f"[NODE] {node} is down"
            body = (
                f"Quiet node {node} left the swarm. No investigation started — "
                f"it is on the quiet_nodes list (expected downtime)."
            )
        await self._notify_card(self.agent_id, title, body, "homelab_notify_node_transition_failed")


def _days_until(not_after: datetime) -> int:
    """Whole days until a cert's notAfter; negative once it has expired."""
    return int((not_after - datetime.now(UTC)).total_seconds() // 86400)


def _parse_rowcount(status: str) -> int:
    if not status:
        return 0
    try:
        return int(status.split()[-1])
    except (ValueError, IndexError):
        return 0
