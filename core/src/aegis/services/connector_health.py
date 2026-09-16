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

**The Slack ping is the notification; the problem hub is the record** (#571).
The one ping is all this mechanism used to do, and ``alerted`` then silenced it
for the rest of the outage — so a dead integration produced exactly one
notification in its entire lifetime, with no problem, no task, no owner and no
timeline. Calibre was down for two days that way. So every record also runs the
connector set through :func:`hub_watch.reconcile_findings`: a connector at or
past its threshold is a ``connectordown`` finding, and one that recovers has its
problem resolved by the watchdog seam rather than by a flag. ``alerted`` goes on
suppressing repeat *pings*; it no longer decides whether the failure is visible.

The reconcile sweeps **every** ``connector_health:*`` row, not just the one being
recorded, because ``reconcile_findings`` resolves any problem of its classes that
is absent from the findings it is handed — passing one connector's state would
resolve every other connector's live problem.
"""

from __future__ import annotations

from typing import Any

import structlog

from aegis.errors import error_text
from aegis.services.hub_watch import reconcile_findings
from aegis.services.settings_store import get_setting, put_setting

logger = structlog.get_logger()

_KEY_PREFIX = "connector_health:"
# The hub identity of a dead connector. `source` is not in
# `hub_project._OWNER_BY_SOURCE`, so the task falls to the infra owner, which is
# who fixes an integration; and it is not in `hub_project._SELF_CLEARING_SOURCES`,
# so there is no settle delay — the consecutive-failure threshold already IS the
# settle window.
_SOURCE = "connector"
_SUBJECT_KIND = "integration"
_DOWN_CLASS = "connectordown"
# Only used to read a row written before `down` existed (#571).
_LEGACY_THRESHOLD = 3
# How long the system-event POST may take. Comms posts the event to Slack and
# logs the dispatch before it answers, so this uses the worker's own delivery
# client's bounds (`activities/delivery.py`): 30 s in all, 5 s to connect. The
# 10 s it used to allow can run out on a slow Slack round-trip, and the timeout
# httpx raises then carries no message (#573).
_SEND_TIMEOUT_S = 30.0
_SEND_CONNECT_TIMEOUT_S = 5.0


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
            "connector_health_record_failed", connector=connector, error=error_text(exc)
        )


async def _record(
    pool: Any, settings: Any, connector: str, *, ok: bool, error: str, threshold: int
) -> None:
    key = _KEY_PREFIX + connector
    value = await get_setting(pool, key)
    state = dict(value) if value is not None else {}
    failures = int(state.get("consecutive_failures") or 0)
    alerted = bool(state.get("alerted"))

    if ok:
        if not failures and not alerted:
            return  # steady state — no write per healthy run
        if alerted:
            await _send_system_event(settings, f"✅ Connector `{connector}` recovered.")
        state = {"consecutive_failures": 0, "alerted": False, "down": False}
    else:
        failures += 1
        logger.warning(
            "connector_health_failure", connector=connector, consecutive=failures, error=error[:300]
        )
        state = {
            "consecutive_failures": failures,
            "alerted": alerted,
            "last_error": error[:300],
            # Stored rather than re-derived: the threshold is a per-call argument
            # (a boot-time caller passes 1), so the sweep below cannot work out
            # from a bare count whether some other connector is past its own.
            "down": failures >= threshold,
        }
        if failures >= threshold and not alerted:
            # Only latch `alerted` when the event actually went out, so a
            # comms outage retries the alert on the next failure.
            state["alerted"] = await _send_system_event(
                settings,
                f"🔴 Connector `{connector}` has failed {failures} consecutive "
                f"fetch(es) and needs attention.\nLast error: {error[:300]}",
            )

    await put_setting(pool, key, state)
    # Its own guard: a hub failure must not be reported as a failure to record.
    try:
        await _reconcile(pool)
    except Exception as exc:  # noqa: BLE001 — the next record retries the sweep
        logger.warning("connector_health_reconcile_failed", error=error_text(exc))


def _is_down(state: dict) -> bool:
    """Whether a stored row says its connector is at or past its threshold."""
    if "down" in state:
        return bool(state["down"])
    # A row written before `down` existed. Its threshold is not recorded, so the
    # default is the only one available; the next record of that connector
    # replaces the guess with the real answer.
    return int(state.get("consecutive_failures") or 0) >= _LEGACY_THRESHOLD


async def _reconcile(pool: Any) -> None:
    """Turn the whole connector set into hub findings, and resolve what recovered."""
    rows = await pool.fetch(
        "SELECT key, value FROM settings WHERE key LIKE $1", _KEY_PREFIX + "%"
    )
    findings = []
    for row in rows:
        state = dict(row["value"] or {})
        if not _is_down(state):
            continue
        name = str(row["key"])[len(_KEY_PREFIX) :]
        if not name:
            continue
        findings.append(
            {
                "klass": _DOWN_CLASS,
                "subject": name,
                # No count in the title: a title is set once, when the problem
                # opens, and would then freeze at whatever the count was then.
                "title": f"Connector {name} is failing",
                "severity": "warning",
                "payload": {
                    "connector": name,
                    "consecutive_failures": int(state.get("consecutive_failures") or 0),
                    "last_error": str(state.get("last_error") or "")[:300],
                },
            }
        )
    await reconcile_findings(
        pool,
        source=_SOURCE,
        subject_kind=_SUBJECT_KIND,
        classes=[_DOWN_CLASS],
        findings=findings,
    )


async def _send_system_event(settings: Any, text: str) -> bool:
    """POST a system event to the comms delivery server. False on any failure.

    Every failure is logged with what it was: an exception (by type, since a
    timeout has no message), a reply that is not 200 (a refused API key is a
    401 and used to fail in silence), and a 200 that says `ok: false` (Slack
    refused the post)."""
    comms_url = (getattr(settings, "comms_url", "") or "").rstrip("/")
    if not comms_url:
        logger.warning("connector_health_no_comms_url", detail="system event not sent")
        return False
    import httpx

    api_key = getattr(settings, "api_key", "") or ""
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(_SEND_TIMEOUT_S, connect=_SEND_CONNECT_TIMEOUT_S)
        ) as client:
            resp = await client.post(
                f"{comms_url}/api/deliver/message",
                json={"text": text, "system_event": True},
                headers={"X-API-Key": api_key} if api_key else {},
            )
    except Exception as exc:  # noqa: BLE001 — alerting must never break the caller
        logger.warning("connector_health_event_send_failed", error=error_text(exc))
        return False
    if resp.status_code != 200:
        logger.warning(
            "connector_health_event_send_failed",
            error=f"comms answered HTTP {resp.status_code}",
            body=resp.text[:200],
        )
        return False
    try:
        ok = bool(resp.json().get("ok"))
    except Exception as exc:  # noqa: BLE001 — an unreadable reply is a failed send
        logger.warning("connector_health_event_send_failed", error=error_text(exc))
        return False
    if not ok:
        logger.warning(
            "connector_health_event_send_failed",
            error="comms answered ok=false",
            body=resp.text[:200],
        )
    return ok
