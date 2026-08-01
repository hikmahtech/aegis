"""`knowledge_content.source_type` vocabulary — a single declared registry.

Historically `source_type` was an implicit, scattered string literal spread
across ~13 files (ingest activities, chat tools, admin routes) with nothing
enumerating what values exist or what they mean (improvement observation
C8). This module is that single place: `SOURCE_TYPES` maps each known value
to a `SourceTypeInfo` (description + optional per-type decay window), and
`warn_if_unknown` flags drift without ever rejecting a write — several call
sites pass free-form strings from user-facing forms (e.g. the
`/api/knowledge/upload` endpoint's `source_type: str = Form("upload")`), so
rejecting here would break normal usage.

`chat.py`'s `_apply_knowledge_decay` resolves each item's decay window via
`get_decay_days` instead of its own dict — this module is the only source of
truth for decay windows now.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog

logger = structlog.get_logger()

DEFAULT_DECAY_DAYS = 90


@dataclass(frozen=True)
class SourceTypeInfo:
    description: str
    decay_days: int | None = None


SOURCE_TYPES: dict[str, SourceTypeInfo] = {
    # --- decay windows pre-configured before this registry existed, moved
    # verbatim from chat.py's DECAY_WINDOWS (do not change these values —
    # see tests/core/services/test_source_types.py's regression test) ---
    "chat": SourceTypeInfo(
        "Chat turns saved via the remember_this tool (chat.py)", decay_days=30
    ),
    "task_outcome": SourceTypeInfo(
        "Reserved: task-outcome summaries. Decay window was pre-configured "
        "before this registry existed; no current write call site.",
        decay_days=60,
    ),
    "triage": SourceTypeInfo(
        "Reserved: triage summaries. Decay window was pre-configured before "
        "this registry existed; no current write call site.",
        decay_days=90,
    ),
    "content": SourceTypeInfo(
        "Generic ingested content — default for POST /api/knowledge/ingest "
        "and worker content.py's ingest_content when the caller gives none.",
        decay_days=180,
    ),
    "manual": SourceTypeInfo(
        "Reserved: manually-curated content. Decay window was pre-configured "
        "before this registry existed; no current write call site.",
        decay_days=365,
    ),
    # --- other verified write-path literals (grepped, not just doc-sourced);
    # no explicit decay window configured historically, so they fall back to
    # DEFAULT_DECAY_DAYS via get_decay_days() ---
    "calendar": SourceTypeInfo("Calendar events ingested by services/claims.py"),
    "drive": SourceTypeInfo(
        "Google Drive folder sync (services/drive.py, DriveSyncFlow, schedule_sync default)"
    ),
    "research": SourceTypeInfo("Synthesized answers from the research chat tool (chat.py)"),
    "runbook": SourceTypeInfo("Alert-runbook text captured via the runbook chat tool (chat.py)"),
    "reference": SourceTypeInfo(
        "GTD @reference captures routed to the knowledge store by ClarifyFlow"
    ),
    "upload": SourceTypeInfo(
        "Files uploaded via /api/knowledge/upload or a folder seed (default source_type)"
    ),
    "media": SourceTypeInfo("Transcribed audio/video (worker content.py _transcribe_media)"),
    "article": SourceTypeInfo(
        "HTML article extracted via content.py's detect_content_type/fetch_and_extract path"
    ),
    "pdf": SourceTypeInfo("PDF extracted via content.py's detect_content_type path"),
    "image": SourceTypeInfo(
        "Image URL detected by content.py's detect_content_type (no OCR; fallback text only)"
    ),
    "email": SourceTypeInfo("Gmail messages ingested by worker activities/gmail.py"),
    "intelligence": SourceTypeInfo(
        "Auto-ingested intel-scan findings (worker activities/briefing.py, intelligence.py)"
    ),
    "briefing": SourceTypeInfo("Daily briefing text (worker activities/briefing.py)"),
    "alert": SourceTypeInfo("Alert investigation summary (worker activities/alerts.py)"),
    "alert_investigation": SourceTypeInfo(
        "Full alert investigation transcript (worker activities/alerts.py)"
    ),
    "document": SourceTypeInfo(
        "Slack-forwarded document, ingested via comms' SlackCoreClient.knowledge_ingest "
        "(comms/src/aegis_comms/slack_inbound.py) — not in the original C8 audit list"
    ),
    # --- life-domain types, pre-registered for upcoming Themes A/B/C so
    # those features can cite a declared type from day one ---
    "people": SourceTypeInfo("Person profiles/notes (life-domain, planned)"),
    "observation": SourceTypeInfo("Ad-hoc observations about people/places (life-domain, planned)"),
    "expiring_item": SourceTypeInfo(
        "Items with an expiry/renewal date (life-domain, planned)"
    ),
    "asset": SourceTypeInfo("Household/asset records (life-domain, planned)"),
    "life_fact": SourceTypeInfo(
        "Standalone life facts captured via POST /api/admin/capture "
        "{kind:'life_fact'} — Slack `/remember`, chat, and downstream "
        "curated-signal ingest all land here"
    ),
    "wearable": SourceTypeInfo(
        "Wearable vendor metrics polled by WearableIngestFlow. Numeric "
        "readings go to life.observations (source='oura', not "
        "knowledge_content) — registered here so the vocabulary of ingest "
        "sources stays enumerated in one place."
    ),
    "daylog": SourceTypeInfo("Daily log entries (life-domain, planned)"),
    "daylog_rollup": SourceTypeInfo("Rolled-up daylog summaries (life-domain, planned)"),
}


def get_decay_days(source_type: str) -> int:
    """Decay window in days for `source_type`.

    Unknown types, and known types with no configured `decay_days`, fall
    back to `DEFAULT_DECAY_DAYS`.
    """
    info = SOURCE_TYPES.get(source_type)
    if info is not None and info.decay_days is not None:
        return info.decay_days
    return DEFAULT_DECAY_DAYS


def warn_if_unknown(source_type: str) -> None:
    """Log a warning when `source_type` isn't in the registry.

    Never raises and never rejects the write — this is a drift signal for
    logs/observability, not a validation gate. Call sites like the
    `/api/knowledge/upload` endpoint pass free-form user-supplied strings
    that legitimately fall outside the registry.
    """
    if source_type not in SOURCE_TYPES:
        logger.warning("unknown_source_type", source_type=source_type)
