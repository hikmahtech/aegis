"""Admin endpoints for the books: the index, the latest close, manual runs."""

from __future__ import annotations

import asyncio
import logging
from datetime import date, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from temporalio.client import Client as TemporalClient

from aegis.api.auth import verify_auth
from aegis.api.deps import get_settings
from aegis.api.routes._flow_trigger import require_temporal_client, start_named_workflow
from aegis.config import Settings
from aegis.services import books

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/admin/money",
    tags=["money"],
    dependencies=[Depends(verify_auth)],
)


_FLOW_NAMES = {
    "money_brief": "MoneyBriefFlow",
    "month_close": "MonthCloseFlow",
    "receipt_scan": "ReceiptIngestFlow",
}

# How far back an unexplained posting still counts as something to fix. The
# monthly close scopes the same count to its month; the page is a standing
# queue, so it takes a rolling window instead.
_UNKNOWN_DAYS = 60

_EVENT_LIMIT = 100
_REPORTS_REL = "reports/monthly"

# The columns the admin page renders, in the order the spec lists them.
_EVENT_COLUMNS = (
    "message_id", "mailbox", "entity", "kind", "direction", "amount", "currency",
    "payee", "account", "channel", "instrument", "occurred_on", "due_on", "parser",
    "confidence", "source_class", "journal_file", "linked_message_id", "todoist_ref",
)


async def _start_workflow(flow: str, cfg: dict, temporal_client: TemporalClient):
    return await start_named_workflow(flow, cfg, temporal_client, _FLOW_NAMES)


def _event(row) -> dict:
    """One index row, JSON-safe.

    `amount` leaves as the string Postgres stored, never a float: the journal
    is the record and this is only its index, so the page must reproduce the
    ledger's digits exactly rather than a binary approximation of them. A row
    the writer refused has no amount at all, and that stays `None` — `str()`
    on it would put the literal "None" in front of a currency symbol.
    """
    out = {k: row[k] for k in _EVENT_COLUMNS}
    out["amount"] = None if row["amount"] is None else str(row["amount"])
    for key in ("occurred_on", "due_on"):
        out[key] = out[key].isoformat() if out[key] else None
    out["confidence"] = None if row["confidence"] is None else float(row["confidence"])
    return out


@router.get("/state")
async def money_state(request: Request, settings: Settings = Depends(get_settings)) -> dict:
    """The books index: recent events, the two review queues, and repo state."""
    pool = request.app.state.db_pool
    since = date.today() - timedelta(days=_UNKNOWN_DAYS)
    async with pool.acquire() as conn:
        events = await conn.fetch(
            f"SELECT {', '.join(_EVENT_COLUMNS)} FROM finance.journal_index "
            # `coalesce` because a due has no `occurred_on` — ordering on that
            # column alone files every unpaid bill under a NULL. NULLS LAST so
            # a row with neither date does not masquerade as the newest.
            "ORDER BY coalesce(occurred_on, due_on) DESC NULLS LAST, created_at DESC "
            f"LIMIT {_EVENT_LIMIT}"
        )
        unknown_count = await conn.fetchval(
            "SELECT count(*) FROM finance.journal_index "
            "WHERE kind = 'transaction' AND account LIKE '%:unknown' AND occurred_on >= $1",
            since,
        )
        dues_open = await conn.fetchval(
            "SELECT count(*) FROM finance.journal_index "
            "WHERE kind IN ('due','failed') AND linked_message_id IS NULL"
        )
    cfg = books.config_from_settings(settings)
    try:
        unpushed = await books.unpushed_commits(cfg)
    except Exception as exc:  # noqa: BLE001 — a missing/degraded checkout is a
        # counter of 0, not a 500: everything else on this page comes from
        # Postgres and is still worth rendering.
        logger.warning("money_unpushed_commits_failed error=%s", str(exc)[:200])
        unpushed = 0
    return {
        "events": [_event(r) for r in events],
        "unknown_count": int(unknown_count or 0),
        "dues_open": int(dues_open or 0),
        "unpushed_commits": unpushed,
        "books_configured": bool(cfg.repo_url) or (cfg.path / ".git").exists(),
        "home_currency": settings.home_currency,
    }


def _latest_close_sync(base: Path) -> dict | None:
    """The newest `reports/monthly/*.md` in the books checkout, read verbatim.

    Newest is the highest filename: `MonthCloseFlow` writes `<YYYY-MM>.md`, so
    lexicographic order IS chronological order and it does not depend on an
    mtime that a fresh `git clone` rewrites to the checkout time.
    """
    try:
        names = sorted(p.name for p in base.iterdir() if p.is_file() and p.suffix == ".md")
    except OSError:
        return None  # no checkout yet, or reports/ has never been written
    if not names:
        return None
    newest = names[-1]
    try:
        text = (base / newest).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.warning("money_digest_read_failed file=%s error=%s", newest, str(exc)[:200])
        return None
    return {"path": f"{_REPORTS_REL}/{newest}", "markdown": text}


@router.get("/digest")
async def money_digest(settings: Settings = Depends(get_settings)) -> dict:
    """The latest monthly close, read off the books repo, or {digest: None}."""
    base = books.config_from_settings(settings).path / _REPORTS_REL
    return {"digest": await asyncio.to_thread(_latest_close_sync, base)}


@router.post("/{flow}/run")
async def trigger_flow(
    flow: str,
    request: Request,
    settings: Settings = Depends(get_settings),
) -> dict:
    """Manually trigger a money flow by name.

    Returns 503 when no Temporal client is connected, 409 when the
    feature flag is off, 400 for unknown flow names. Body is forwarded
    as the workflow config dict.
    """
    client = require_temporal_client(request)
    if not getattr(settings, "money_hygiene_enabled", False):
        raise HTTPException(
            status_code=409,
            detail="money_hygiene disabled — set AEGIS_MONEY_HYGIENE_ENABLED=true",
        )
    try:
        body = await request.json()
    except Exception:
        body = {}
    handle = await _start_workflow(flow, body or {}, client)
    return {"ok": True, "workflow_id": handle.id}
