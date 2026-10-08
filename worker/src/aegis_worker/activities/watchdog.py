"""Delivery watchdog activities: undelivered interaction cards and the comms
inbound-channel probe.

They used to live on `HomelabActivities`, which only exists while
`homelab_enabled` is on. The delivery watchdog watches AEGIS's own comms, not
the homelab, so it runs on every install. The activity names are unchanged:
running workflows call them by name.
"""

from __future__ import annotations

import html as _html
from dataclasses import dataclass
from typing import Any

import httpx
from aegis.errors import error_text
from temporalio import activity

from aegis_worker.activities.delivery import safe_send_message


@dataclass
class WatchdogActivities:
    db_pool: Any
    delivery: Any  # DeliveryActivities
    # The agent the summary card speaks as, resolved at boot in `__main__`.
    # "" sends the card to comms' default.
    agent_id: str = ""

    async def _notify_card(self, title: str, body: str, log_event: str) -> None:
        await safe_send_message(
            self.delivery,
            agent_id=self.agent_id,
            message=f"<b>{_html.escape(title)}</b>\n{_html.escape(body)}",
            log_event=log_event,
        )

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
        await self._notify_card(title, body, "watchdog_notify_undelivered_failed")

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
                "check_comms_inbound_health_request_failed error=%s", error_text(exc)
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
