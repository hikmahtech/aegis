"""Real-DB tests for the shared status-digest aggregate.

get_status_digest reads whole-table sums/counts over workflow_runs and
llm_calls (no per-test scoping column to filter on), so — like
tests/core/test_llm_governor.py — each test wipes then reinserts its own
fixture rows inside a try/finally. Tests run sequentially (no xdist
configured) and the aegis-test flock serialises the whole pytest run against
other agents, so a full wipe of these call-telemetry tables is safe within
one run.
"""

from __future__ import annotations

from aegis.services.status_digest import get_status_digest


async def _cleanup(pool) -> None:
    await pool.execute("DELETE FROM workflow_runs")
    await pool.execute("DELETE FROM llm_calls")
    await pool.execute("DELETE FROM interactions")
    await pool.execute("DELETE FROM settings WHERE key = 'infra_heartbeat_state'")


async def test_runs_by_type_status_counts(db_pool):
    try:
        await _cleanup(db_pool)
        await db_pool.execute(
            "INSERT INTO workflow_runs "
            "(run_id, workflow_id, workflow_type, status, started_at, completed_at) VALUES "
            "('sd-r1','sd-w1','DailyBriefingFlow','completed', now(), now()), "
            "('sd-r2','sd-w2','DailyBriefingFlow','completed', now(), now()), "
            "('sd-r3','sd-w3','GmailIngestFlow','failed', now(), now())"
        )

        digest = await get_status_digest(db_pool, hours=24)

        counts = {
            (r["workflow_type"], r["status"]): r["count"] for r in digest["runs_by_type_status"]
        }
        assert counts[("DailyBriefingFlow", "completed")] == 2
        assert counts[("GmailIngestFlow", "failed")] == 1
        assert digest["window_hours"] == 24
    finally:
        await _cleanup(db_pool)


async def test_hard_failed_runs_surfaced(db_pool):
    """status='failed' (the interceptor's terminal-exception path)."""
    try:
        await _cleanup(db_pool)
        await db_pool.execute(
            "INSERT INTO workflow_runs "
            "(run_id, workflow_id, workflow_type, status, started_at, completed_at, error) VALUES "
            "('sd-r1','sd-w1','GmailIngestFlow','failed', now(), now(), 'boom')"
        )

        digest = await get_status_digest(db_pool, hours=24)

        assert len(digest["failed_runs"]) == 1
        assert digest["failed_runs"][0]["workflow_type"] == "GmailIngestFlow"
        assert digest["failed_runs"][0]["error"] == "boom"
    finally:
        await _cleanup(db_pool)


async def test_completed_but_failed_detection(db_pool):
    """The audit's signature defect: workflow_runs.status='completed' but the
    flow's own result_summary encodes a real failure. Must catch the
    '*_failed'/'error' convention flows actually use, and must NOT flag
    benign terminal statuses like 'skipped'/'ok', or rows with a 'reason'
    key but no failure-shaped 'status' (e.g. sentry_poll's skip reasons)."""
    try:
        await _cleanup(db_pool)
        await db_pool.execute(
            """
            INSERT INTO workflow_runs
                (run_id, workflow_id, workflow_type, status, started_at, completed_at, result_summary)
            VALUES
                ('sd-r1','sd-w1','RssIngestFlow','completed', now(), now(),
                 '{"status": "fetch_failed", "reason": "timeout"}'::jsonb),
                ('sd-r2','sd-w2','GmailIngestFlow','completed', now(), now(),
                 '{"status": "auth_failed"}'::jsonb),
                ('sd-r3','sd-w3','ClarifyFlow','completed', now(), now(),
                 '{"status": "permanent_error", "reason": "retries_exhausted"}'::jsonb),
                ('sd-r4','sd-w4','DriveSyncFlow','completed', now(), now(),
                 '{"status": "skipped", "reason": "no_folder_configured"}'::jsonb),
                ('sd-r5','sd-w5','SentryPollFlow','completed', now(), now(),
                 '{"mode": "webhook", "reason": "empty_issue"}'::jsonb),
                ('sd-r6','sd-w6','DailyBriefingFlow','completed', now(), now(),
                 '{"status": "delivered"}'::jsonb)
            """
        )

        digest = await get_status_digest(db_pool, hours=24)

        flagged = {r["workflow_type"] for r in digest["completed_but_failed"]}
        assert flagged == {"RssIngestFlow", "GmailIngestFlow", "ClarifyFlow"}

        rss = next(r for r in digest["completed_but_failed"] if r["workflow_type"] == "RssIngestFlow")
        assert rss["status"] == "fetch_failed"
        assert rss["reason"] == "timeout"

        # Hard-failure query is untouched by these (they're all status='completed').
        assert digest["failed_runs"] == []
    finally:
        await _cleanup(db_pool)


async def test_token_usage_pending_interactions_and_infra_stuck(db_pool):
    try:
        await _cleanup(db_pool)
        await db_pool.execute(
            "INSERT INTO llm_calls (model, input_tokens, output_tokens, latency_ms, purpose, status) "
            "VALUES ('sd-model-1', 100, 50, 10, 'chat', 'success'), "
            "('sd-model-2', 200, 100, 10, 'chat', 'success')"
        )
        await db_pool.execute(
            "INSERT INTO interactions (flow_run_id, agent_id, kind, origin, prompt, status) VALUES "
            "('sd-f1', 'sebas', 'approval', 'test', 'approve?', 'pending'), "
            "('sd-f2', 'sebas', 'approval', 'test', 'done already', 'resolved')"
        )
        await db_pool.execute(
            "INSERT INTO settings (key, value) VALUES ('infra_heartbeat_state', "
            "'{\"nodes\": {}, \"stuck\": [\"homelab-gitops\"], \"confirmed\": [\"noon\"], "
            "\"fail_count\": 1}'::jsonb)"
        )

        digest = await get_status_digest(db_pool, hours=24)

        assert digest["llm_calls"] == 2
        assert digest["llm_tokens"] == 450
        assert digest["pending_interactions"] == 1
        assert digest["infra_stuck"] == ["homelab-gitops"]
        assert digest["infra_confirmed"] == ["noon"]
    finally:
        await _cleanup(db_pool)


async def test_window_hours_excludes_old_rows(db_pool):
    try:
        await _cleanup(db_pool)
        await db_pool.execute(
            "INSERT INTO workflow_runs "
            "(run_id, workflow_id, workflow_type, status, started_at, completed_at) VALUES "
            "('sd-old','sd-w-old','OldFlow','completed', now() - interval '10 days', "
            " now() - interval '10 days')"
        )

        digest = await get_status_digest(db_pool, hours=24)

        assert digest["runs_by_type_status"] == []
        assert digest["failed_runs"] == []
        assert digest["completed_but_failed"] == []
    finally:
        await _cleanup(db_pool)


async def test_missing_infra_heartbeat_state_defaults_empty(db_pool):
    try:
        await _cleanup(db_pool)

        digest = await get_status_digest(db_pool, hours=24)

        assert digest["infra_stuck"] == []
        assert digest["infra_confirmed"] == []
        assert digest["pending_interactions"] == 0
        assert digest["llm_calls"] == 0
        assert digest["llm_tokens"] == 0
    finally:
        await _cleanup(db_pool)
