"""Status digest — one aggregate read answering "what ran / what broke /
what's pending on me / what did we spend" from data that already exists.

Built after a 2026-07 prod audit found the owner had no way to answer that
from any surface: the admin SPA only shows last-run timestamps, and the only
failure surface was a 2-5 sentence LLM-compressed daily briefing, up to 23h
late. This is the shared read behind two surfaces: the `system_status` chat
tool (services/chat.py) and the Slack `/status` command (via
GET /api/observability/status-digest — comms has no aegis-core dependency so
it reaches this over HTTP).

No new tables/columns: reuses workflow_runs, llm_calls, interactions, and the
settings row keyed 'infra_heartbeat_state' (written by
worker/activities/homelab.py's infra-heartbeat flow) exactly as they exist.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

# Matches the "*_failed" / "error" / "permanent_error" convention flows use in
# result_summary->>'status' when they finish WITHOUT raising (workflow_runs.
# status stays 'completed') but the real outcome was a failure — the audit's
# signature defect. Deliberately does not match benign terminal statuses like
# "skipped"/"duplicate"/"muted"/"timed_out" so this doesn't drown in noise.
_INNER_FAILURE_PATTERN = "(error|fail)"


async def get_status_digest(pool: Any, hours: int = 24) -> dict[str, Any]:
    """Aggregate dict for the status surfaces.

    `hours` bounds the window for run counts / failures / token spend.
    Pending interactions and stuck infra are always current — they're queues,
    not time-windowed.
    """
    cutoff = datetime.now(UTC) - timedelta(hours=hours)

    runs_by_type_status = await pool.fetch(
        "SELECT workflow_type, status, COUNT(*) AS count FROM workflow_runs "
        "WHERE started_at > $1 GROUP BY workflow_type, status "
        "ORDER BY workflow_type, status",
        cutoff,
    )

    # Hard failures: the workflow raised and the interceptor recorded
    # status='failed' (worker/interceptors.py). Same query as
    # briefing.py's gather_changes_since (worker/activities/briefing.py).
    failed_rows = await pool.fetch(
        "SELECT workflow_type, error, completed_at FROM workflow_runs "
        "WHERE completed_at > $1 AND status = 'failed' "
        "ORDER BY completed_at DESC LIMIT 10",
        cutoff,
    )

    # Soft failures: the workflow returned normally but its own result
    # encodes an error (see _INNER_FAILURE_PATTERN above).
    completed_but_failed_rows = await pool.fetch(
        "SELECT workflow_type, result_summary->>'status' AS inner_status, "
        "result_summary->>'reason' AS reason, completed_at FROM workflow_runs "
        "WHERE completed_at > $1 AND status = 'completed' "
        f"AND result_summary->>'status' ~* '{_INNER_FAILURE_PATTERN}' "
        "ORDER BY completed_at DESC LIMIT 10",
        cutoff,
    )

    token_row = await pool.fetchrow(
        "SELECT COUNT(*) AS calls, "
        "COALESCE(SUM(input_tokens), 0) + COALESCE(SUM(output_tokens), 0) AS total_tokens "
        "FROM llm_calls WHERE created_at > $1",
        cutoff,
    )

    pending_interactions = await pool.fetchval(
        "SELECT COUNT(*) FROM interactions WHERE status = 'pending'"
    )

    heartbeat_row = await pool.fetchrow(
        "SELECT value FROM settings WHERE key = 'infra_heartbeat_state'"
    )
    heartbeat = heartbeat_row["value"] if heartbeat_row and heartbeat_row["value"] else {}
    if not isinstance(heartbeat, dict):
        heartbeat = {}

    return {
        "window_hours": hours,
        "runs_by_type_status": [dict(r) for r in runs_by_type_status],
        "failed_runs": [
            {
                "workflow_type": r["workflow_type"],
                "error": (r["error"] or "")[:200],
                "completed_at": r["completed_at"].isoformat() if r["completed_at"] else None,
            }
            for r in failed_rows
        ],
        "completed_but_failed": [
            {
                "workflow_type": r["workflow_type"],
                "status": r["inner_status"],
                "reason": (r["reason"] or "")[:200] if r["reason"] else None,
                "completed_at": r["completed_at"].isoformat() if r["completed_at"] else None,
            }
            for r in completed_but_failed_rows
        ],
        "llm_calls": int(token_row["calls"]) if token_row else 0,
        "llm_tokens": int(token_row["total_tokens"]) if token_row else 0,
        "pending_interactions": int(pending_interactions or 0),
        "infra_stuck": heartbeat.get("stuck") or [],
        "infra_confirmed": heartbeat.get("confirmed") or [],
    }
