"""Consecutive-failure tracking for connector fetches (issue #76).

A connector that fails once is noise; one that fails every run is an outage
nobody sees — the Miniflux failure mode (#70): a warning-level boot log,
integration dead for months. ``record_connector_health()`` keeps a
per-connector consecutive-failure counter in the ``settings`` table (key
``connector_health:<name>``) and posts a Slack system event through comms
when the counter crosses the threshold, plus a recovery event when the
connector comes back.

Boot-time callers should pass ``threshold=1`` — the next retry is a whole
restart away. The persisted ``alerted`` flag keeps repeated boots (or runs)
from re-alerting until the connector recovers.
"""

from __future__ import annotations

from typing import Any

import structlog

logger = structlog.get_logger()

_KEY_PREFIX = "connector_health:"


async def record_connector_health(
    pool: Any,
    settings: Any,
    connector: str,
    *,
    ok: bool,
    error: str = "",
    threshold: int = 3,
) -> None:
    """Record one connector fetch outcome. Never raises."""
    try:
        await _record(pool, settings, connector, ok=ok, error=error, threshold=threshold)
    except Exception as exc:  # noqa: BLE001 — health tracking must never break the caller
        logger.warning(
            "connector_health_record_failed", connector=connector, error=str(exc)[:200]
        )


async def _record(
    pool: Any, settings: Any, connector: str, *, ok: bool, error: str, threshold: int
) -> None:
    key = _KEY_PREFIX + connector
    row = await pool.fetchrow("SELECT value FROM settings WHERE key = $1", key)
    state = dict(row["value"]) if row else {}
    failures = int(state.get("consecutive_failures") or 0)
    alerted = bool(state.get("alerted"))

    if ok:
        if not failures and not alerted:
            return  # steady state — no write per healthy run
        if alerted:
            await _send_system_event(settings, f"✅ Connector `{connector}` recovered.")
        state = {"consecutive_failures": 0, "alerted": False}
    else:
        failures += 1
        logger.warning(
            "connector_health_failure", connector=connector, consecutive=failures, error=error[:300]
        )
        state = {"consecutive_failures": failures, "alerted": alerted, "last_error": error[:300]}
        if failures >= threshold and not alerted:
            # Only latch `alerted` when the event actually went out, so a
            # comms outage retries the alert on the next failure.
            state["alerted"] = await _send_system_event(
                settings,
                f"🔴 Connector `{connector}` has failed {failures} consecutive "
                f"fetch(es) and needs attention.\nLast error: {error[:300]}",
            )

    await pool.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES ($1, $2, NOW()) "
        "ON CONFLICT (key) DO UPDATE SET value = $2, updated_at = NOW()",
        key,
        state,
    )


async def _send_system_event(settings: Any, text: str) -> bool:
    """POST a system event to the comms delivery server. False on any failure."""
    comms_url = (getattr(settings, "comms_url", "") or "").rstrip("/")
    if not comms_url:
        logger.warning("connector_health_no_comms_url", detail="system event not sent")
        return False
    import httpx

    api_key = getattr(settings, "api_key", "") or ""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{comms_url}/api/deliver/message",
                json={"text": text, "system_event": True},
                headers={"X-API-Key": api_key} if api_key else {},
            )
            return resp.status_code == 200 and bool(resp.json().get("ok"))
    except Exception as exc:  # noqa: BLE001 — alerting must never break the caller
        logger.warning("connector_health_event_send_failed", error=str(exc)[:200])
        return False
