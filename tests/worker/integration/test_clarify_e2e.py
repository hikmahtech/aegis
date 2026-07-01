"""End-to-end ClarifyFlow integration with real DB + stubbed LLM + stubbed
Todoist connector. Verifies the whole pipeline lands the right side
effects."""

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock

import pytest
from aegis_worker.activities.clarify import ClarifyActivities
from aegis_worker.flows.clarify import ClarifyConfig, ClarifyFlow
from temporalio.client import Client
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker


@pytest.mark.asyncio
async def test_clarify_e2e_inbox_task_gets_classified(db_pool) -> None:
    # --- Seed ---
    # Only override todoist_managed_project_ids with synthetic ids the test
    # owns; gtd_clarify_enabled / gtd_2min_rule_enabled / user_timezone are
    # already seeded by migration 012 and asserted on by sibling tests, so
    # we deliberately leave those alone.
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO settings (key, value) VALUES "
            "('todoist_managed_project_ids', $1) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            {
                "inbox": "P_IN",
                "projects": "P_PRJ",
                "single_actions": "P_SA",
            },
        )
        for pid in ("P_IN", "P_PRJ", "P_SA"):
            await conn.execute(
                "INSERT INTO todoist_projects (id, name, is_managed, raw) "
                "VALUES ($1, $1, true, '{}'::jsonb) "
                "ON CONFLICT (id) DO UPDATE SET is_managed = true",
                pid,
            )
        # Clean slate for the test task / log. Also clean any tasks left
        # over from sibling tests pointing at our P_PRJ etc. that would
        # FK-block the project deletion in teardown.
        await conn.execute("DELETE FROM gtd_clarify_log WHERE todoist_task_id='E2E'")
        await conn.execute("DELETE FROM todoist_notes WHERE item_id='E2E'")
        await conn.execute("DELETE FROM todoist_tasks WHERE id='E2E'")
        await conn.execute(
            "INSERT INTO todoist_tasks "
            "(id, project_id, content, labels, source_tag, is_completed, raw) "
            "VALUES ('E2E','P_IN','Reply to legal counsel',"
            "ARRAY['#email'],'#email',false,'{}'::jsonb)"
        )

    # --- Stub LLM client ---
    llm = AsyncMock()
    llm.think = AsyncMock(
        return_value=json.dumps(
            {
                "classification": "next_action",
                "confidence": 0.88,
                "assignee": "@sebas",
                "contexts": ["@email", "@5min"],
                "reason": "reply due tomorrow",
            }
        )
    )

    # --- Stub Todoist connector ---
    sent_batches: list[list[dict]] = []

    class _StubConnector:
        async def commands(self, cmds):
            sent_batches.append(cmds)
            return {"ok": True, "data": {"sync_status": {}, "temp_id_mapping": {}}}

    acts = ClarifyActivities(
        db_pool=db_pool, todoist_connector=_StubConnector(), llm_client=llm
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        client: Client = env.client
        async with Worker(
            client,
            task_queue="aegis-clarify-e2e",
            workflows=[ClarifyFlow],
            activities=[
                acts.find_unclassified_items,
                acts.classify_one,
                acts.apply_outcome,
                acts.log_classification,
            ],
        ):
            result = await client.execute_workflow(
                ClarifyFlow.run,
                ClarifyConfig(agent_id="sebas", max_items=10),
                id=f"clarify-e2e-{uuid.uuid4()}",
                task_queue="aegis-clarify-e2e",
            )

    # --- Asserts ---
    assert result["found"] == 1
    assert result["applied"] == 1

    # Connector saw an item_update + a note_add
    sent_types = [c["type"] for batch in sent_batches for c in batch]
    assert "item_update" in sent_types
    assert "note_add" in sent_types

    # Log row exists with applied=true
    async with db_pool.acquire() as conn:
        log = await conn.fetchrow(
            "SELECT classification, applied, pass FROM gtd_clarify_log "
            "WHERE todoist_task_id='E2E'"
        )
        last_clar = await conn.fetchval(
            "SELECT last_clarified_at FROM todoist_tasks WHERE id='E2E'"
        )
    assert log["classification"] == "next_action"
    assert log["applied"] is True
    assert log["pass"] == 1
    assert last_clar is not None

    # --- Teardown: remove the synthetic rows so sibling bootstrap tests
    # don't FK-violate when they try DELETE FROM todoist_projects WHERE
    # is_managed=true. Tasks referencing these projects from OTHER tests
    # are cleaned too. Order: notes/log → tasks → null parent refs → projects.
    async with db_pool.acquire() as conn:
        target_ids = ("P_IN", "P_PRJ", "P_WAIT", "P_SOM", "P_REF")
        await conn.execute("DELETE FROM gtd_clarify_log WHERE todoist_task_id='E2E'")
        await conn.execute(
            "DELETE FROM todoist_notes WHERE item_id IN "
            "(SELECT id FROM todoist_tasks WHERE project_id = ANY($1))",
            list(target_ids),
        )
        await conn.execute(
            "DELETE FROM todoist_tasks WHERE project_id = ANY($1)", list(target_ids)
        )
        await conn.execute(
            "UPDATE todoist_projects SET parent_id = NULL WHERE parent_id = ANY($1)",
            list(target_ids),
        )
        await conn.execute(
            "DELETE FROM todoist_projects WHERE id = ANY($1)", list(target_ids)
        )
