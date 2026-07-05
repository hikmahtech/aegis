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
import uuid as _uuid

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from temporalio.client import Client

from aegis.api.deps import get_settings
from aegis.api.routes.interactions import get_workflow_client
from aegis.clarify_note import AGENT_REPLY_PREFIX, CLARIFY_NOTE_PREFIX
from aegis.config import Settings
from aegis.services.agents import resolve_tag

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

    Unsigned — rely on Traefik IP-whitelist for access control
    (internal network only, same as the deleted n8n route it replaces).

    Body: Alertmanager v2 or Grafana Unified Alerting JSON. Each alert
    in the payload spawns one AlertInvestigationFlow child. Dedup'd via
    ingest_idempotency on the alert's fingerprint (or a derived one if
    missing).
    """
    body = await request.body()
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

    pool = request.app.state.db_pool
    started = 0
    skipped = 0

    for a in alerts_raw:
        if not isinstance(a, dict):
            continue
        # Only "firing" alerts trigger investigation; "resolved" ones are noise
        status = a.get("status", "firing")
        if status == "resolved":
            skipped += 1
            continue

        labels = a.get("labels") or {}
        annotations = a.get("annotations") or {}
        fingerprint = a.get("fingerprint") or ""
        alertname = labels.get("alertname", "")
        instance = labels.get("instance", "")

        # Synthesize fingerprint if missing
        if not fingerprint:
            fingerprint = f"alertmanager:{alertname}:{instance}"

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

    logger.info("alert_webhook_processed", started=started, skipped=skipped)
    return {"accepted": True, "started": started, "skipped": skipped}


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
