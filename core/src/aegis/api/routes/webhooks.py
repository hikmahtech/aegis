"""Source-specific signed webhooks.

Phase 2 ships the GitHub handler with HMAC verification; the sentry
handler is scaffolded but returns 503 until its secret is configured.
Each secret is a separate env var (see config.py):

- AEGIS_GITHUB_WEBHOOK_SECRET — validates X-Hub-Signature-256
- AEGIS_SENTRY_WEBHOOK_SECRET — validates Sentry-Hook-Signature

Phase 3 wires GitHub and Sentry to Temporal flows (GitHubAlertFlow,
SentryPollFlow) with delivery-id/fingerprint idempotency via ingest_idempotency.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json as _json
import time as _time
import uuid as _uuid

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from temporalio.client import Client

from aegis.api.deps import get_settings
from aegis.api.routes.interactions import get_workflow_client
from aegis.clarify_note import AGENT_REPLY_PREFIX, CLARIFY_NOTE_PREFIX
from aegis.config import Settings
from aegis.observability import log_audit
from aegis.services.agents import resolve_tag
from aegis.services.alert_tasks import close_task_for_resolved_alert
from aegis.services.health import record_health_push
from aegis.services.observations import record_observation
from aegis.services.places import record_location_push

logger = structlog.get_logger()

router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])


def verify_hmac(
    secret: str,
    body: bytes,
    header: str | None,
    prefix: str = "sha256=",
    encoding: str = "hex",
) -> bool:
    """Constant-time HMAC-SHA256 verification shared across signed webhooks.

    Computes ``hmac.new(secret, body, sha256)`` and compares ``prefix +
    digest`` against ``header`` via ``hmac.compare_digest``. ``digest`` is
    hex (``encoding="hex"``) or base64 (``encoding="base64"``) depending on
    scheme.

    Schemes:
    - GitHub ``X-Hub-Signature-256``: hex, ``prefix="sha256="``.
    - Sentry ``Sentry-Hook-Signature``: hex, ``prefix=""``.
    - Todoist ``X-Todoist-Hmac-SHA256``: **base64** (not hex — Todoist's own
      docs specify base64-encoding the digest), ``prefix=""``.

    A missing header, or a non-empty ``prefix`` the header doesn't start
    with, is rejected before the comparison.
    """
    if not header:
        return False
    if prefix and not header.startswith(prefix):
        return False
    mac = hmac.new(secret.encode(), body, hashlib.sha256)
    digest = mac.hexdigest() if encoding == "hex" else base64.b64encode(mac.digest()).decode()
    return hmac.compare_digest(f"{prefix}{digest}", header)


def _safe_json(body: bytes) -> dict:
    """Parse a JSON request body, falling back to ``{}`` on any error."""
    try:
        return _json.loads(body)
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Life-data push (B4) — the one internet-facing door into the owner's personal
# data store. Everything below the signature check is plumbing; the signature
# check IS the feature.
# ---------------------------------------------------------------------------

# Source slug → Temporal workflow name, or ``None`` to record the payload
# inline as a ``life.observations`` row (no worker involvement).
#
# B4 ships the generic numeric lane; B5 adds ``location``, also inline. A
# source only needs a workflow when its push fans out into several rows or
# calls something external — the auth/idempotency/audit path below is
# source-agnostic either way.
#
# `location` is inline rather than flow-backed on purpose: a Temporal input is
# persisted in workflow history, so handing the raw payload to a flow would
# copy the owner's coordinates into a second store (and write a `workflow_runs`
# row per ping — a phone pushing every 5 minutes is ~290 workflow executions a
# day) in order to durably retry what is one haversine and one INSERT.
#
# `health` (B6) is inline for the same reason, and more strongly: a Health Auto
# Export batch is the owner's body data, and Temporal history keeps a flow
# argument verbatim, with its own retention and its own web UI. The B6 note
# asked for a `HealthIngestFlow`; that would have been the second copy.
LIFE_SOURCES: dict[str, str | None] = {
    "observation": None,
    "location": None,
    "health": None,
}

# Maximum accepted body. A phone/watch push is a few hundred bytes; 64 KiB is
# already generous. Enforced while STREAMING, not after — `await
# request.body()` would buffer an unbounded body into memory first.
LIFE_MAX_BODY_BYTES = 64 * 1024

# Replay window for X-Aegis-Timestamp, in seconds, either direction.
LIFE_TIMESTAMP_WINDOW_SECONDS = 300

# Same treatment for /alert, which is the one webhook that can be left
# unauthenticated (blank `alert_webhook_secret` is a documented legacy default),
# and whose work per request is unbounded: every element of the posted array can
# spawn an AlertInvestigationFlow, i.e. LLM spend plus Todoist/Slack writes.
#
# These are ABUSE ceilings, deliberately far above real traffic — not a way to
# shape it. Sizing matters in both directions:
#
#   * Too high and a small body of minimal alerts (~22 bytes each) mints
#     thousands of workflows.
#   * Too LOW and a genuine incident loses alerts, permanently. Truncation keeps
#     the FIRST N, and every kept alert claims `ingest_idempotency`; on
#     Alertmanager's `group_interval` resend the same first N are claimed and
#     skipped, so the dropped tail is never reached on a later attempt. A
#     whole-cluster outage is precisely when the group is largest and when
#     losing alerts costs most, so the ceiling has to clear that case with room
#     to spare: alertmanager groups by (alertname, cluster, service), so one
#     `DockerServiceDown` event can carry an entry per swarm service at once.
ALERT_MAX_BODY_BYTES = 1024 * 1024
ALERT_MAX_ALERTS_PER_REQUEST = 500

# One opaque failure for every authentication outcome — a missing header, a
# malformed one, a stale timestamp and a wrong signature are indistinguishable
# to the caller. Anything more specific is a probing oracle.
_LIFE_UNAUTHORIZED = "unauthorized"


def life_signing_input(source: str, timestamp: str, body: bytes) -> bytes:
    """Exact bytes a client must HMAC: ``"{source}.{timestamp}." + raw_body``.

    The source slug is bound into the signature so a captured push to one
    source cannot be re-pointed at another (health data replayed as a location
    fix), and the timestamp is bound in so a captured request expires. Both
    are what makes this more than "HMAC of the body".
    """
    return f"{source}.{timestamp}.".encode() + body


async def _read_bounded_body(request: Request, limit: int) -> bytes:
    """Read at most ``limit`` bytes, raising 413 the moment that is exceeded.

    Streams so an attacker cannot make the process buffer an arbitrarily large
    body before any check runs. Chunked transfers carry no Content-Length, so
    trusting that header alone would not bound anything.
    """
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise HTTPException(status_code=413, detail="body_too_large")
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/life/{source}", status_code=202)
async def life_webhook(
    source: str,
    request: Request,
    x_aegis_signature: str | None = Header(default=None),
    x_aegis_timestamp: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
    temporal: Client = Depends(get_workflow_client),
):
    """Signed life-data push from a phone / watch / home-automation client.

    Signature scheme (compute client-side, e.g. in an iOS Shortcut)::

        signed  = "{source}.{unix_seconds}." + raw_json_body
        headers = {
            "X-Aegis-Timestamp": unix_seconds,
            "X-Aegis-Signature": hex(hmac_sha256(AEGIS_LIFE_WEBHOOK_SECRET, signed)),
        }

    Both headers are REQUIRED. The observation doc suggested making the
    timestamp optional "so clients that don't send it still work" — that is
    not replay protection at all, since an attacker replaying a captured
    request simply drops the header and the body signature still verifies
    forever. Fail closed instead.

    Defence in depth, in order:

    1. **No secret configured ⇒ 503, always.** An empty secret can never mean
       "skip verification" on an endpoint that writes to personal data.
    2. Body bounded at ``LIFE_MAX_BODY_BYTES`` while streaming.
    3. ``X-Aegis-Timestamp`` must be present, numeric and within
       ±``LIFE_TIMESTAMP_WINDOW_SECONDS`` — a captured request dies in 5 min.
    4. ``hmac.compare_digest`` over the source+timestamp-bound input.
    5. ``ingest_idempotency`` claim, which collapses the residual in-window
       replay to a single no-op.

    Every failure in 3–4 returns the same opaque 401 body and nothing about
    the secret or the presented signature is ever logged.
    """
    # (1) Fail closed. Before the body is even read.
    if not settings.life_webhook_secret:
        logger.error("life_webhook_secret_missing", life_source=source)
        raise HTTPException(status_code=503, detail="life_webhook_secret_not_configured")

    # (2) Bounded read.
    body = await _read_bounded_body(request, LIFE_MAX_BODY_BYTES)

    # (3) Mandatory, bounded timestamp. The length guard comes first so a
    # multi-thousand-digit header can neither hit int()'s parse limit nor
    # overflow the float subtraction below.
    #
    # `isdecimal()`, NOT `isdigit()`: `isdigit()` is True for the Unicode
    # superscripts ¹ ² ³ (U+00B9/B2/B3), which are latin-1 representable and so
    # survive Starlette's header decoding intact — but `int()` rejects them.
    # With `isdigit()` the guard ADMITTED such a header and the `int()` below
    # escaped as an uncaught ValueError: an unauthenticated 500 reached before
    # the signature check, i.e. exactly the distinguishing oracle (and log-noise
    # generator) the "same opaque 401 for every failure" promise forbids.
    # Within latin-1, `isdecimal()` is True for ASCII 0-9 and nothing else, so
    # together with the length bound `int()` below cannot fail.
    raw_ts = x_aegis_timestamp or ""
    ts: int | None = None
    if raw_ts.isdecimal() and len(raw_ts) <= 12:
        ts = int(raw_ts)
    if ts is None or abs(_time.time() - ts) > LIFE_TIMESTAMP_WINDOW_SECONDS:
        logger.warning("life_webhook_rejected", life_source=source, stage="timestamp")
        raise HTTPException(status_code=401, detail=_LIFE_UNAUTHORIZED)

    # (4) Constant-time signature check over source + timestamp + body.
    # compare_digest rejects non-ASCII str input with TypeError, and a header
    # is latin-1 decoded, so a hostile byte would 500 instead of 401 — treat
    # any comparison failure as a failed verification.
    try:
        ok = verify_hmac(
            settings.life_webhook_secret,
            life_signing_input(source, raw_ts, body),
            x_aegis_signature,
            prefix="",
        )
    except TypeError:
        ok = False
    if not ok:
        logger.warning("life_webhook_rejected", life_source=source, stage="signature")
        raise HTTPException(status_code=401, detail=_LIFE_UNAUTHORIZED)

    # Unknown-source 404 deliberately sits AFTER verification, so an
    # unauthenticated prober cannot enumerate which life sources exist.
    if source not in LIFE_SOURCES:
        logger.warning("life_webhook_unknown_source", life_source=source)
        raise HTTPException(status_code=404, detail="unknown_source")

    try:
        payload = _json.loads(body)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="invalid_json") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="invalid_json")

    # (5) Idempotency. External id from the payload when the device supplies
    # one, else the signed input's digest — so the same body at the same
    # timestamp claims once, while a genuinely repeated reading at a later
    # timestamp is a new row.
    external_id = str(payload.get("id") or payload.get("_id") or "")[:128] or hashlib.sha256(
        life_signing_input(source, raw_ts, body)
    ).hexdigest()
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        claimed = await conn.fetchval(
            """
            INSERT INTO ingest_idempotency (source_type, external_id)
            VALUES ($1, $2)
            ON CONFLICT DO NOTHING
            RETURNING external_id
            """,
            f"life:{source}",
            external_id,
        )
    if claimed is None:
        logger.info("life_webhook_duplicate_skipped", life_source=source)
        return {"accepted": True, "duplicate": True, "external_id": external_id}

    async def _release_claim() -> None:
        """Hand the claim back so the device's retry is genuinely reprocessed.

        A claim held after a failed handling burns that `external_id` forever:
        the retry is answered `202 {"duplicate": true}` — indistinguishable
        from success — while nothing was ever recorded. So it is released on
        ANY failure below, not just the client-fixable ones.

        Safe because re-sending is idempotent one level down: every ingest path
        writes through `record_external_observation`, whose per-sample
        `(source, metric, external_id)` `ON CONFLICT DO NOTHING` means a batch
        that half-wrote before failing re-inserts only its missing samples.

        Best-effort: if the release itself fails there is nothing better to do
        than let the original failure surface.
        """
        try:
            await pool.execute(
                "DELETE FROM ingest_idempotency WHERE source_type = $1 AND external_id = $2",
                f"life:{source}",
                external_id,
            )
        except Exception:  # noqa: BLE001 — must not mask the original failure
            logger.warning("life_webhook_claim_release_failed", life_source=source)

    await log_audit(
        pool,
        actor=f"life:{source}",
        action="life_push_accepted",
        target_type="life_webhook",
        target_id=external_id,
        details={"bytes": len(body)},
    )

    workflow_name = LIFE_SOURCES[source]
    if workflow_name is None:
        # Inline lane: a bare numeric/categorical reading needs no durable
        # workflow, it is one INSERT. Free-text capture is NOT handled here —
        # POST /api/admin/capture (kind="life_fact") already owns that lane and
        # a second door to the same room is a second thing to get wrong.
        meta = payload.get("metadata")
        health: dict | None = None
        try:
            if source == "location":
                # Coordinates are resolved to a named place and dropped inside
                # `record_location_push`; they are never persisted or logged.
                # `None` = this external_id already produced a row.
                row = await record_location_push(pool, payload, external_id)
                if row is None:
                    return {"accepted": True, "duplicate": True, "external_id": external_id}
            elif source == "health":
                # A batch, not a reading: one push fans out to many samples,
                # each deduped on its own instant. Only counts come back —
                # nothing that could echo a value into a proxy log.
                health = await record_health_push(pool, payload)
            else:
                row = await record_observation(
                    pool,
                    source=str(payload.get("source") or source),
                    metric=str(payload.get("metric") or ""),
                    value=payload.get("value"),
                    metadata=meta if isinstance(meta, dict) else {},
                )
        except (ValueError, TypeError) as exc:
            # Release the idempotency claim: a malformed push is client-fixable,
            # and a device that sends its own `id` would otherwise be locked out
            # of that id forever by one bad payload.
            await _release_claim()
            logger.warning("life_webhook_bad_observation", life_source=source)
            raise HTTPException(status_code=422, detail="invalid_observation") from exc
        except Exception:
            # Anything else — an asyncpg hiccup partway through a health batch,
            # say. The push failed, so the claim must not survive it either.
            await _release_claim()
            logger.warning("life_webhook_record_failed", life_source=source)
            raise
        logger.info("life_webhook_observation_recorded", life_source=source)
        if health is not None:
            return {"accepted": True, "external_id": external_id, **health}
        return {
            "accepted": True,
            "external_id": external_id,
            "observation_id": str(row["id"]),
        }

    try:
        handle = await temporal.start_workflow(
            workflow_name,
            {"source": source, "external_id": external_id, "payload": payload},
            id=f"life-{source}-{external_id}",
            task_queue="aegis-main",
        )
    except Exception:
        # A Temporal outage must not burn the external_id: without this the
        # device's retry gets `{"duplicate": true}` and the push is lost.
        await _release_claim()
        logger.warning("life_webhook_dispatch_failed", life_source=source)
        raise
    logger.info(
        "life_webhook_flow_started", life_source=source, workflow_id=handle.id
    )
    return {"accepted": True, "external_id": external_id, "workflow_id": handle.id}


@router.post("/github")
async def github_webhook(
    request: Request,
    x_hub_signature_256: str | None = Header(default=None),
    x_github_event: str | None = Header(default=None),
    x_github_delivery: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
    temporal: Client = Depends(get_workflow_client),
):
    if not settings.github_webhook_secret:
        logger.error("github_webhook_secret_missing")
        raise HTTPException(status_code=503, detail="github_webhook_secret_not_configured")
    body = await request.body()
    if not verify_hmac(settings.github_webhook_secret, body, x_hub_signature_256):
        logger.warning("github_webhook_bad_signature", gh_event=x_github_event)
        raise HTTPException(status_code=401, detail="bad_signature")

    delivery_id = x_github_delivery or str(_uuid.uuid4())

    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        claimed = await conn.fetchval(
            """
            INSERT INTO ingest_idempotency (source_type, external_id)
            VALUES ('github', $1)
            ON CONFLICT DO NOTHING
            RETURNING external_id
            """,
            delivery_id,
        )
    if claimed is None:
        logger.info("github_webhook_duplicate_skipped", delivery_id=delivery_id)
        return {"accepted": True, "duplicate": True, "delivery_id": delivery_id}

    payload = _safe_json(body)

    agent_id = await resolve_tag(pool, "infra")
    if agent_id is None:
        logger.warning("github_webhook_no_infra_agent", delivery_id=delivery_id)
        return {"accepted": True, "skipped": "no_infra_agent", "delivery_id": delivery_id}

    handle = await temporal.start_workflow(
        "GitHubAlertFlow",
        {
            "agent_id": agent_id,
            "event": x_github_event or "",
            "delivery_id": delivery_id,
            "payload": payload,
        },
        id=f"github-{delivery_id}",
        task_queue="aegis-main",
    )
    logger.info(
        "github_webhook_flow_started",
        workflow_id=handle.id,
        gh_event=x_github_event,
        delivery_id=delivery_id,
    )
    return {
        "accepted": True,
        "event": x_github_event,
        "delivery_id": delivery_id,
        "workflow_id": handle.id,
    }


@router.post("/sentry")
async def sentry_webhook(
    request: Request,
    settings: Settings = Depends(get_settings),
    temporal: Client = Depends(get_workflow_client),
):
    if not settings.sentry_webhook_secret:
        raise HTTPException(status_code=503, detail="sentry_webhook_not_configured")
    body = await request.body()
    signature = request.headers.get("Sentry-Hook-Signature")
    if not verify_hmac(settings.sentry_webhook_secret, body, signature, prefix=""):
        raise HTTPException(status_code=401, detail="bad_signature")

    payload = _safe_json(body)

    issue = (payload.get("data") or {}).get("issue") or payload.get("issue") or {}
    issue_id = str(issue.get("id", "")) or str(payload.get("event_id", ""))
    if not issue_id:
        logger.warning("sentry_webhook_no_issue_id", keys=list(payload.keys()))
        return {"accepted": True, "skipped": "no_issue_id"}

    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        claimed = await conn.fetchval(
            """
            INSERT INTO ingest_idempotency (source_type, external_id)
            VALUES ('sentry', $1)
            ON CONFLICT DO NOTHING
            RETURNING external_id
            """,
            f"sentry:{issue_id}",
        )
    if claimed is None:
        logger.info("sentry_webhook_duplicate_skipped", issue_id=issue_id)
        return {"accepted": True, "duplicate": True, "issue_id": issue_id}

    agent_id = await resolve_tag(pool, "infra")
    if agent_id is None:
        logger.warning("sentry_webhook_no_infra_agent", issue_id=issue_id)
        return {"accepted": True, "skipped": "no_infra_agent", "issue_id": issue_id}

    handle = await temporal.start_workflow(
        "SentryPollFlow",
        {
            "agent_id": agent_id,
            "mode": "webhook",
            "issue": issue,
        },
        id=f"sentry-alert-{issue_id}",
        task_queue="aegis-main",
    )
    logger.info("sentry_webhook_flow_started", workflow_id=handle.id, issue_id=issue_id)
    return {
        "accepted": True,
        "issue_id": issue_id,
        "workflow_id": handle.id,
    }


@router.post("/alert")
async def alert_webhook(
    request: Request,
    settings: Settings = Depends(get_settings),
    temporal: Client = Depends(get_workflow_client),
):
    """Generic alert webhook (Alertmanager/Grafana).

    Alertmanager and Grafana don't sign their payloads, so there is no vendor
    HMAC to verify here. Set ``AEGIS_ALERT_WEBHOOK_SECRET`` to require a
    matching ``X-Alert-Token`` header; leaving it empty keeps the endpoint
    unauthenticated (the legacy default, kept for backward compatibility).

    An unauthenticated alert endpoint lets anyone who can reach the port mint
    fake alerts, each spawning an AlertInvestigationFlow that burns LLM budget
    and posts to Todoist/Slack — so set the secret whenever port 8080 is not
    fully fronted by an authenticating proxy (#88).

    Body: Alertmanager v2 or Grafana Unified Alerting JSON. Each alert
    in the payload spawns one AlertInvestigationFlow child. Dedup'd via
    ingest_idempotency on the alert's fingerprint (or a derived one if
    missing).
    """
    if settings.alert_webhook_secret:
        token = request.headers.get("X-Alert-Token") or ""
        if not hmac.compare_digest(token, settings.alert_webhook_secret):
            logger.warning("alert_webhook_bad_token")
            raise HTTPException(status_code=401, detail="bad_token")

    # Bounded + streamed, like /life/: `await request.body()` would buffer an
    # arbitrarily large body into memory before any check ran, and this endpoint
    # is reachable unauthenticated whenever the secret is unset.
    body = await _read_bounded_body(request, ALERT_MAX_BODY_BYTES)
    try:
        payload = _json.loads(body)
    except Exception as exc:
        logger.warning("alert_webhook_bad_json", size=len(body))
        raise HTTPException(status_code=400, detail="invalid_json") from exc

    # Normalise: alertmanager {alerts: [...]} OR bare list OR single-alert dict
    alerts_raw: list[dict]
    if isinstance(payload, list):
        alerts_raw = payload
    elif isinstance(payload, dict):
        alerts_raw = payload.get("alerts") or [payload]
    else:
        alerts_raw = []

    # Cap the fan-out. Each surviving alert spawns a child workflow that costs
    # LLM budget and writes to Todoist/Slack, so an oversized array is an
    # amplification vector rather than a big-but-harmless request. Truncate
    # loudly instead of silently: a genuine group this large is itself a signal
    # worth seeing in the logs.
    dropped = 0
    if len(alerts_raw) > ALERT_MAX_ALERTS_PER_REQUEST:
        dropped = len(alerts_raw) - ALERT_MAX_ALERTS_PER_REQUEST
        logger.warning(
            "alert_webhook_truncated",
            received=len(alerts_raw),
            cap=ALERT_MAX_ALERTS_PER_REQUEST,
            dropped=dropped,
        )
        alerts_raw = alerts_raw[:ALERT_MAX_ALERTS_PER_REQUEST]

    pool = request.app.state.db_pool
    started = 0
    skipped = 0

    for a in alerts_raw:
        if not isinstance(a, dict):
            continue
        labels = a.get("labels") or {}
        annotations = a.get("annotations") or {}
        fingerprint = a.get("fingerprint") or ""
        alertname = labels.get("alertname", "")
        instance = labels.get("instance", "")

        # Synthesize fingerprint if missing
        if not fingerprint:
            fingerprint = f"alertmanager:{alertname}:{instance}"

        # Only "firing" alerts trigger investigation; "resolved" ones are noise
        # for the pipeline — but we still record a resolved audit row (same
        # shape as record_heartbeat_resolved) so check_alert_resolved's
        # self-resolve machinery works for alertmanager alerts too, not just
        # heartbeat ones. Best-effort: a DB hiccup must never break the webhook.
        status = a.get("status", "firing")
        if status == "resolved":
            try:
                await log_audit(
                    pool,
                    actor="alert:alertmanager",
                    action="alert_received",
                    target_type="alert",
                    target_id=fingerprint,
                    details={"resolved": "true"},
                )
            except Exception:
                logger.warning("alert_webhook_resolved_audit_failed", fingerprint=fingerprint)
            # The audit row only re-arms dedup; close the task this alert
            # spawned too, or it outlives its incident indefinitely (#279).
            await close_task_for_resolved_alert(pool, fingerprint)
            skipped += 1
            continue

        # Idempotency claim
        async with pool.acquire() as conn:
            claimed = await conn.fetchval(
                """
                INSERT INTO ingest_idempotency (source_type, external_id)
                VALUES ('alertmanager', $1)
                ON CONFLICT DO NOTHING
                RETURNING external_id
                """,
                fingerprint,
            )
        if claimed is None:
            skipped += 1
            continue

        alert = {
            "source": "alertmanager",
            "title": annotations.get("summary") or alertname or "Alert",
            "fingerprint": fingerprint,
            "severity": labels.get("severity", "warning"),
            "service": instance or labels.get("job", ""),
            "description": annotations.get("description", ""),
            "labels": labels,
            "raw_payload": a,
        }

        await temporal.start_workflow(
            "AlertInvestigationFlow",
            alert,
            id=f"alertmanager-{fingerprint}",
            task_queue="aegis-main",
        )
        started += 1

    logger.info("alert_webhook_processed", started=started, skipped=skipped, dropped=dropped)
    return {"accepted": True, "started": started, "skipped": skipped, "dropped": dropped}


@router.post("/todoist")
async def todoist_webhook(
    request: Request,
    x_todoist_hmac_sha256: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
):
    """Todoist Pro webhook receiver.

    HMAC-SHA256 of the raw body using AEGIS_TODOIST_WEBHOOK_SECRET. Stores
    every received event in todoist_webhook_events (audit). Projection is
    updated in a follow-up activity to keep the handler fast.
    """
    if not settings.todoist_webhook_secret:
        logger.error("todoist_webhook_secret_missing")
        raise HTTPException(status_code=503, detail="todoist_webhook_secret_not_configured")

    body = await request.body()
    if not verify_hmac(
        settings.todoist_webhook_secret,
        body,
        x_todoist_hmac_sha256,
        prefix="",
        encoding="base64",
    ):
        logger.warning("todoist_webhook_bad_signature")
        raise HTTPException(status_code=401, detail="bad_signature")

    payload = _safe_json(body)

    event_name = payload.get("event_name", "unknown")
    event_data = payload.get("event_data", {})

    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO todoist_webhook_events (event_name, event_data) VALUES ($1, $2)",
            event_name,
            event_data,
        )

    # Phase 5 polish — instant-clarify on note:added/note:updated.
    # When a user comments on a Todoist task, bump todoist_tasks.last_note_at
    # so the next ClarifyFlow tick re-emerges the row, AND best-effort
    # trigger ClarifyFlow immediately via Temporal so the user gets
    # ~1s-latency supervision response instead of waiting up to 15min.
    #
    # Guard against AEGIS's own ClarifyFlow notes — bumping last_note_at
    # on those would loop. Prefix is shared via aegis.clarify_note so a
    # producer-side rename can't drift from this filter.
    if event_name in ("note:added", "note:updated"):
        item_id = (event_data or {}).get("item_id")
        content = (event_data or {}).get("content") or ""
        # Self-loop guard: ClarifyFlow's own notes AND agent-reply comments
        # (post_agent_reply_comment / post_agent_reply_error_comment) must
        # not bump last_note_at or kick ClarifyFlow.
        is_clarify_own = content.startswith(CLARIFY_NOTE_PREFIX) or content.startswith(
            AGENT_REPLY_PREFIX
        )
        if item_id and not is_clarify_own:
            try:
                async with pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE todoist_tasks SET last_note_at = now() WHERE id = $1",
                        str(item_id),
                    )
                logger.info(
                    "todoist_webhook_note_bumped_last_note_at",
                    todoist_event=event_name,
                    item_id=str(item_id)[:32],
                )
            except Exception as exc:
                logger.warning(
                    "todoist_webhook_note_bump_failed",
                    error=str(exc)[:200],
                )
            # Best-effort: kick ClarifyFlow now. Idempotent — flow query
            # respects last_clarified_at vs last_note_at; if no tasks need
            # clarify, the run is a 50ms no-op.
            if settings.temporal_host:
                try:
                    from datetime import timedelta as _td

                    from temporalio.client import Client as _Client

                    gtd_agent = await resolve_tag(pool, "gtd")
                    if gtd_agent is None:
                        logger.warning("todoist_webhook_clarify_skipped_no_gtd_agent")
                    else:
                        client = await _Client.connect(settings.temporal_host)
                        await client.start_workflow(
                            "ClarifyFlow",
                            {
                                "agent_id": gtd_agent,
                                "max_items": 20,
                                "activity_name": "gtd-clarify-webhook",
                            },
                            id=f"clarify-webhook-{_uuid.uuid4()}",
                            task_queue="aegis-main",
                            execution_timeout=_td(minutes=10),
                        )
                except Exception as exc:
                    logger.warning(
                        "todoist_webhook_clarify_trigger_failed",
                        error=str(exc)[:200],
                    )

    logger.info("todoist_webhook_received", event_name=event_name)
    return {"accepted": True}
