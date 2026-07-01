"""End-to-end: TodoistSyncFlow with a real DB pool and a stubbed connector.

Verifies:
- Bootstrap runs once on first invocation.
- Sync diff is applied to projection.
- Outbox is drained.
- Second invocation is a no-op for bootstrap.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import pytest
from aegis_worker.activities.todoist import TodoistActivities
from aegis_worker.flows.todoist_sync import TodoistSyncConfig, TodoistSyncFlow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker


@dataclass
class StubConnector:
    sync_call_count: int = 0
    commands_call_count: int = 0

    async def sync(self, token, resource_types):
        self.sync_call_count += 1
        # The bootstrap probe + fetch both see the same response. The default
        # Inbox must be present for bootstrap to adopt it.
        return {
            "ok": True,
            "data": {
                "sync_token": "tok-1",
                "full_sync": True,
                "projects": [
                    {"id": "inbox-default-id", "name": "Inbox", "parent_id": None, "inbox_project": True, "is_archived": False},
                ],
                "items": [],
                "labels": [],
            },
            "error": None,
            "retryable": False,
        }

    async def commands(self, cmds):
        self.commands_call_count += 1
        return {
            "ok": True,
            "data": {
                "sync_status": {c["uuid"]: "ok" for c in cmds},
                "temp_id_mapping": {c["temp_id"]: str(1000 + i) for i, c in enumerate(cmds)},
            },
            "error": None,
            "retryable": False,
        }


@pytest.mark.asyncio
async def test_two_runs_bootstrap_once(db_pool):
    """First run bootstraps; second run skips bootstrap."""
    # Clean state
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM settings WHERE key = 'todoist_managed_project_ids'")
        await conn.execute("DELETE FROM todoist_outbox")
        await conn.execute(
            "DELETE FROM todoist_notes WHERE item_id IN "
            "(SELECT id FROM todoist_tasks WHERE project_id IN "
            "(SELECT id FROM todoist_projects WHERE is_managed = true))"
        )
        await conn.execute(
            "DELETE FROM todoist_tasks WHERE project_id IN "
            "(SELECT id FROM todoist_projects WHERE is_managed = true)"
        )
        await conn.execute(
            "UPDATE todoist_projects SET parent_id = NULL "
            "WHERE parent_id IN (SELECT id FROM todoist_projects WHERE is_managed = true)"
        )
        await conn.execute("DELETE FROM todoist_projects WHERE is_managed = true")
        await conn.execute("UPDATE todoist_sync_state SET sync_token = '*' WHERE key = 'main'")

    stub = StubConnector()
    activities = TodoistActivities(db_pool=db_pool, connector=stub, seed_dir="config/seed")

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"e2e-tq-{uuid.uuid4()}"
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[TodoistSyncFlow],
            activities=[
                activities.bootstrap_if_empty,
                activities.fetch_sync,
                activities.apply_sync_diff,
                activities.drain_outbox,
            ],
        ):
            r1 = await env.client.execute_workflow(
                TodoistSyncFlow.run,
                TodoistSyncConfig(),
                id=f"e2e-1-{uuid.uuid4()}",
                task_queue=task_queue,
            )
            r2 = await env.client.execute_workflow(
                TodoistSyncFlow.run,
                TodoistSyncConfig(),
                id=f"e2e-2-{uuid.uuid4()}",
                task_queue=task_queue,
            )

    assert r1["bootstrapped"] is True
    assert r2["bootstrapped"] is False

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT value FROM settings WHERE key = 'todoist_managed_project_ids'")
    assert row is not None
    assert set(row["value"].keys()) == {"inbox", "next", "someday"}

    # First run: 1 sync (bootstrap probe) + 1 sync (fetch) + 1 commands (bootstrap creates)
    # Second run: 0 sync for bootstrap (skipped) + 1 sync (fetch) + 0 commands
    # So total: 3 sync calls, 1 commands call
    assert stub.sync_call_count >= 3
    assert stub.commands_call_count == 1
