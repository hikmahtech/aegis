"""Workflow-run recorder activity — fed by WorkflowRunRecorderInterceptor.

Separate from the v2 `runs.py` module (which records activity start/complete
pairs) so we can delete runs.py in Phase 4 without breaking the interceptor.

The interceptor records a single terminal row per workflow (success or
failure) — see `interceptors.py` for the rationale on why we don't split
into start+complete activities.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import asyncpg
from temporalio import activity


@dataclass
class RecordRunInput:
    run_id: str
    workflow_id: str
    workflow_type: str
    agent_id: str | None
    parent_run_id: str | None
    started_at: datetime
    completed_at: datetime
    duration_ms: int
    status: str
    error: str | None
    input_summary: dict[str, Any] | None
    result_summary: dict[str, Any] | None
    # Todoist task this workflow run is anchored to (when applicable).
    # Set from the first arg's `todoist_task_id` attribute/key by the
    # WorkflowRunRecorderInterceptor. Surfaces in the schema as
    # workflow_runs.todoist_task_ref (migration 009).
    todoist_task_ref: str | None = None


class RunRecorderActivities:
    def __init__(self, db_pool: asyncpg.Pool):
        self._pool = db_pool

    @activity.defn(name="record_workflow_run")
    async def record_workflow_run(self, input: RecordRunInput) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO workflow_runs
                    (run_id, workflow_id, workflow_type, agent_id, parent_run_id,
                     status, started_at, completed_at, duration_ms, error,
                     input_summary, result_summary, todoist_task_ref)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
                ON CONFLICT (run_id) DO UPDATE SET
                    status = EXCLUDED.status,
                    completed_at = EXCLUDED.completed_at,
                    duration_ms = EXCLUDED.duration_ms,
                    error = EXCLUDED.error,
                    result_summary = EXCLUDED.result_summary,
                    todoist_task_ref = COALESCE(EXCLUDED.todoist_task_ref, workflow_runs.todoist_task_ref)
                """,
                input.run_id,
                input.workflow_id,
                input.workflow_type,
                input.agent_id,
                input.parent_run_id,
                input.status,
                input.started_at,
                input.completed_at,
                input.duration_ms,
                input.error,
                input.input_summary,
                input.result_summary,
                input.todoist_task_ref,
            )
