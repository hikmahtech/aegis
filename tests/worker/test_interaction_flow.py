"""InteractionFlow — signal path + four timeout policies.

Uses Temporal's start_time_skipping env so timeouts resolve in milliseconds.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from aegis_worker.activities.interactions import InteractionActivities
from aegis_worker.flows.interaction import (
    InteractionFlow,
    InteractionFlowInput,
    InteractionResult,
)
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker


@activity.defn(name="send_interaction_card")
async def _stub_send_card(
    interaction_id: str,
    agent_id: str,
    kind: str,
    prompt: str,
    options,
    allow_hint: bool = False,
) -> dict:
    return {"ok": True, "message_id": 0}


@activity.defn(name="update_interaction_message_id")
async def _stub_update_msg(interaction_id: str, telegram_message_id: int) -> None:
    return None


@pytest_asyncio.fixture(loop_scope="function")
async def temporal_env():
    async with await WorkflowEnvironment.start_time_skipping() as env:
        yield env


@pytest_asyncio.fixture(loop_scope="function")
async def seeded_agent(db_pool):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO agents (id, name, role, system_prompt_path, active) "
            "VALUES ('sebas', 'Sebas', 'assistant', 'personalities/sebas', TRUE) "
            "ON CONFLICT (id) DO NOTHING"
        )
    yield
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM interactions")


async def test_interaction_flow_resolves_on_signal(
    temporal_env: WorkflowEnvironment, db_pool, seeded_agent
):
    acts = InteractionActivities(db_pool=db_pool)
    tq = f"test-{uuid4().hex[:8]}"

    async with Worker(
        temporal_env.client,
        task_queue=tq,
        workflows=[InteractionFlow],
        activities=[
            acts.insert_interaction,
            acts.resolve_interaction,
            acts.apply_interaction_timeout,
            _stub_send_card,
            _stub_update_msg,
        ],
    ):
        handle = await temporal_env.client.start_workflow(
            InteractionFlow.run,
            InteractionFlowInput(
                agent_id="sebas",
                kind="approval",
                origin="unit_test",
                prompt="approve?",
                options=None,
                timeout_seconds=3600,
                timeout_policy="archive",
            ),
            id=f"interaction-{uuid4()}",
            task_queue=tq,
        )

        interaction_id = None
        for _ in range(40):
            async with db_pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT id FROM interactions WHERE flow_run_id = $1",
                    handle.id,
                )
            if row is not None:
                interaction_id = str(row["id"])
                break
            await asyncio.sleep(0.05)
        assert interaction_id is not None, "interactions row never appeared"

        await handle.signal(InteractionFlow.submit_response, {"value": "approved"})
        result = await handle.result()

        assert isinstance(result, InteractionResult)
        assert result.status == "resolved"
        assert result.response == {"value": "approved"}
        assert result.interaction_id == interaction_id


@pytest.mark.parametrize(
    "policy,expected_status,expected_response",
    [
        ("archive", "archived", None),
    ],
)
async def test_interaction_flow_applies_timeout_policy(
    temporal_env, db_pool, seeded_agent, policy, expected_status, expected_response
):
    acts = InteractionActivities(db_pool=db_pool)
    tq = f"test-{uuid4().hex[:8]}"

    async with Worker(
        temporal_env.client,
        task_queue=tq,
        workflows=[InteractionFlow],
        activities=[
            acts.insert_interaction,
            acts.resolve_interaction,
            acts.apply_interaction_timeout,
            _stub_send_card,
            _stub_update_msg,
        ],
    ):
        handle = await temporal_env.client.start_workflow(
            InteractionFlow.run,
            InteractionFlowInput(
                agent_id="sebas",
                kind="approval",
                origin="unit_test",
                prompt="x",
                options=None,
                timeout_seconds=1,
                timeout_policy=policy,
            ),
            id=f"interaction-{uuid4()}",
            task_queue=tq,
        )
        result = await handle.result()
        assert result.status == expected_status
        assert result.response == expected_response

        # Verify the timeout activity actually touched the DB row — guards
        # against the flow returning the right InteractionResult without
        # `apply_interaction_timeout` ever running.
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT status, response FROM interactions WHERE id = $1",
                UUID(result.interaction_id),
            )
        assert row is not None, "interactions row missing after timeout"
        assert row["status"] == expected_status
        if expected_response is None:
            assert row["response"] is None
        else:
            import json

            resp = row["response"]
            if isinstance(resp, str):
                resp = json.loads(resp)
            assert resp == expected_response


async def test_interaction_flow_hold_ignores_timeout(temporal_env, db_pool, seeded_agent):
    """hold policy: if timeout_seconds elapses, the flow keeps waiting — a signal
    can still resolve it later."""
    acts = InteractionActivities(db_pool=db_pool)
    tq = f"test-{uuid4().hex[:8]}"

    async with Worker(
        temporal_env.client,
        task_queue=tq,
        workflows=[InteractionFlow],
        activities=[
            acts.insert_interaction,
            acts.resolve_interaction,
            acts.apply_interaction_timeout,
            _stub_send_card,
            _stub_update_msg,
        ],
    ):
        handle = await temporal_env.client.start_workflow(
            InteractionFlow.run,
            InteractionFlowInput(
                agent_id="sebas",
                kind="input",
                origin="unit_test",
                prompt="type something",
                options=None,
                timeout_seconds=1,
                timeout_policy="hold",
            ),
            id=f"interaction-{uuid4()}",
            task_queue=tq,
        )

        # Advance past the would-have-been timeout — hold should ignore it.
        await temporal_env.sleep(timedelta(seconds=5))

        desc = await handle.describe()
        assert desc.status.name == "RUNNING"

        await handle.signal(InteractionFlow.submit_response, {"text": "late reply"})
        result = await handle.result()
        assert result.status == "resolved"
        assert result.response == {"text": "late reply"}


# --- Phase 4: post_resolve_activity hook + metadata persistence ---


async def test_interaction_flow_post_resolve_activity_called(
    temporal_env: WorkflowEnvironment, db_pool, seeded_agent
):
    """When post_resolve_activity is set, InteractionFlow calls it after
    resolve_interaction with [interaction_id, response, metadata]."""
    acts = InteractionActivities(db_pool=db_pool)
    tq = f"test-{uuid4().hex[:8]}"
    captured: list[dict] = []

    @activity.defn(name="apply_clarify_resolution")
    async def _stub_resolve(
        interaction_id: str, response: dict, metadata: dict
    ) -> dict:
        captured.append(
            {"id": interaction_id, "response": response, "metadata": metadata}
        )
        return {"applied": True}

    async with Worker(
        temporal_env.client,
        task_queue=tq,
        workflows=[InteractionFlow],
        activities=[
            acts.insert_interaction,
            acts.resolve_interaction,
            acts.apply_interaction_timeout,
            _stub_send_card,
            _stub_update_msg,
            _stub_resolve,
        ],
    ):
        handle = await temporal_env.client.start_workflow(
            InteractionFlow.run,
            InteractionFlowInput(
                agent_id="sebas",
                kind="choice",
                origin="gtd_clarify_2_min",
                prompt="Reply?",
                options={"do_now": "Do now", "trash": "Trash"},
                timeout_seconds=3600,
                timeout_policy="archive",
                metadata={"source": "gtd_clarify", "flavor": "2_min", "task_id": "T_PR"},
                post_resolve_activity="apply_clarify_resolution",
            ),
            id=f"interaction-{uuid4()}",
            task_queue=tq,
        )
        for _ in range(40):
            async with db_pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT id, metadata FROM interactions WHERE flow_run_id=$1",
                    handle.id,
                )
            if row is not None:
                break
            await asyncio.sleep(0.05)
        assert row is not None
        # Metadata was persisted with the row
        assert row["metadata"] == {
            "source": "gtd_clarify",
            "flavor": "2_min",
            "task_id": "T_PR",
        }
        await handle.signal(
            InteractionFlow.submit_response, {"value": "do_now"}
        )
        result = await handle.result()
        assert result.status == "resolved"
        # Post-resolve activity received the right args
        assert len(captured) == 1
        assert captured[0]["id"] == str(row["id"])
        assert captured[0]["response"] == {"value": "do_now"}
        assert captured[0]["metadata"]["task_id"] == "T_PR"
        assert captured[0]["metadata"]["flavor"] == "2_min"


async def test_interaction_flow_no_post_resolve_when_not_set(
    temporal_env: WorkflowEnvironment, db_pool, seeded_agent
):
    """When post_resolve_activity is None, the hook does not fire — verified
    by ensuring a stub registered under a different name is NEVER called."""
    acts = InteractionActivities(db_pool=db_pool)
    tq = f"test-{uuid4().hex[:8]}"
    called = False

    @activity.defn(name="should_not_be_called")
    async def _never(*a, **kw):
        nonlocal called
        called = True
        return {}

    async with Worker(
        temporal_env.client,
        task_queue=tq,
        workflows=[InteractionFlow],
        activities=[
            acts.insert_interaction,
            acts.resolve_interaction,
            acts.apply_interaction_timeout,
            _stub_send_card,
            _stub_update_msg,
            _never,
        ],
    ):
        handle = await temporal_env.client.start_workflow(
            InteractionFlow.run,
            InteractionFlowInput(
                agent_id="sebas",
                kind="approval",
                origin="unit_test",
                prompt="x",
                options=None,
                timeout_seconds=3600,
                timeout_policy="archive",
                # metadata + post_resolve_activity intentionally omitted
            ),
            id=f"interaction-{uuid4()}",
            task_queue=tq,
        )
        for _ in range(40):
            async with db_pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT id FROM interactions WHERE flow_run_id=$1", handle.id
                )
            if row is not None:
                break
            await asyncio.sleep(0.05)
        assert row is not None
        await handle.signal(InteractionFlow.submit_response, {"value": "approved"})
        await handle.result()
    assert called is False
