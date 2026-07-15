"""Tests for interaction DB activities."""

from __future__ import annotations

from uuid import UUID

import pytest
import pytest_asyncio
from aegis_worker.activities.interactions import (
    ApplyTimeoutInput,
    InsertInteractionInput,
    InteractionActivities,
    ResolveInteractionInput,
)
from temporalio.testing import ActivityEnvironment


@pytest_asyncio.fixture(loop_scope="function")
async def activities(db_pool):
    # Seed an agent row the FK will reference
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO agents (id, name, role, system_prompt_path, active) "
            "VALUES ('sebas', 'Sebas', 'assistant', 'personalities/sebas', TRUE) "
            "ON CONFLICT (id) DO NOTHING"
        )
    yield InteractionActivities(db_pool=db_pool)
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM interactions")


async def test_insert_interaction_persists_row(activities, db_pool):
    env = ActivityEnvironment()
    out = await env.run(
        activities.insert_interaction,
        InsertInteractionInput(
            flow_run_id="run-abc",
            agent_id="sebas",
            kind="approval",
            origin="ping_test",
            prompt="Approve ping?",
            options=None,
            timeout_policy="archive",
            timeout_at=None,
        ),
    )
    assert isinstance(UUID(out.interaction_id), UUID)

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT kind, status, origin, prompt FROM interactions WHERE id = $1",
            UUID(out.interaction_id),
        )
    assert row["status"] == "pending"
    assert row["kind"] == "approval"
    assert row["origin"] == "ping_test"
    assert row["prompt"] == "Approve ping?"


async def test_resolve_interaction_updates_row(activities, db_pool):
    env = ActivityEnvironment()
    inserted = await env.run(
        activities.insert_interaction,
        InsertInteractionInput(
            flow_run_id="run-xyz",
            agent_id="sebas",
            kind="approval",
            origin="ping_test",
            prompt="x",
            options={"choices": ["yes", "no"]},
            timeout_policy="archive",
            timeout_at=None,
        ),
    )
    await env.run(
        activities.resolve_interaction,
        ResolveInteractionInput(
            interaction_id=inserted.interaction_id,
            response={"value": "approved"},
        ),
    )
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status, options, response, resolved_at, "
            "       jsonb_typeof(options) AS options_type, "
            "       jsonb_typeof(response) AS response_type "
            "FROM interactions WHERE id = $1",
            UUID(inserted.interaction_id),
        )
    assert row["status"] == "resolved"
    # Round-trip as object, not as a JSON-scalar string (the double-encoding bug).
    assert row["options_type"] == "object"
    assert row["response_type"] == "object"
    assert row["options"] == {"choices": ["yes", "no"]}
    assert row["response"] == {"value": "approved"}
    assert row["resolved_at"] is not None


async def test_resolve_is_idempotent(activities, db_pool):
    env = ActivityEnvironment()
    inserted = await env.run(
        activities.insert_interaction,
        InsertInteractionInput(
            flow_run_id="run-idem",
            agent_id="sebas",
            kind="approval",
            origin="t",
            prompt="x",
            options=None,
            timeout_policy="archive",
            timeout_at=None,
        ),
    )
    first = await env.run(
        activities.resolve_interaction,
        ResolveInteractionInput(interaction_id=inserted.interaction_id, response={"v": 1}),
    )
    second = await env.run(
        activities.resolve_interaction,
        ResolveInteractionInput(interaction_id=inserted.interaction_id, response={"v": 2}),
    )
    assert first.already_resolved is False
    assert second.already_resolved is True


@pytest.mark.parametrize(
    "policy,expected_status,expected_response",
    [
        ("archive", "archived", None),
    ],
)
async def test_apply_timeout_each_policy(
    activities, db_pool, policy, expected_status, expected_response
):
    env = ActivityEnvironment()
    inserted = await env.run(
        activities.insert_interaction,
        InsertInteractionInput(
            flow_run_id=f"run-{policy}",
            agent_id="sebas",
            kind="approval",
            origin="t",
            prompt="x",
            options=None,
            timeout_policy=policy,
            timeout_at=None,
        ),
    )
    await env.run(
        activities.apply_interaction_timeout,
        ApplyTimeoutInput(interaction_id=inserted.interaction_id, policy=policy),
    )
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status, response FROM interactions WHERE id = $1",
            UUID(inserted.interaction_id),
        )
    assert row["status"] == expected_status
    if expected_response is None:
        assert row["response"] is None
    else:
        import json

        resp = row["response"] if isinstance(row["response"], dict) else json.loads(row["response"])
        assert resp == expected_response


async def test_apply_timeout_hold_is_noop(activities, db_pool):
    """hold policy leaves the row pending — parent flow is expected to keep waiting."""
    env = ActivityEnvironment()
    inserted = await env.run(
        activities.insert_interaction,
        InsertInteractionInput(
            flow_run_id="run-hold",
            agent_id="sebas",
            kind="approval",
            origin="t",
            prompt="x",
            options=None,
            timeout_policy="hold",
            timeout_at=None,
        ),
    )
    await env.run(
        activities.apply_interaction_timeout,
        ApplyTimeoutInput(interaction_id=inserted.interaction_id, policy="hold"),
    )
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status FROM interactions WHERE id = $1", UUID(inserted.interaction_id)
        )
    assert row["status"] == "pending"


@pytest.mark.parametrize("policy", ["auto_reject", "auto_approve", "unknown"])
async def test_apply_timeout_rejects_removed_policies(activities, db_pool, policy):
    """auto_reject / auto_approve were removed 2026-05-28. Any policy other than
    `archive` or `hold` must raise — the flow never reaches this branch in
    normal operation, so the activity should fail loud."""
    env = ActivityEnvironment()
    inserted = await env.run(
        activities.insert_interaction,
        InsertInteractionInput(
            flow_run_id=f"run-{policy}",
            agent_id="sebas",
            kind="approval",
            origin="t",
            prompt="x",
            options=None,
            timeout_policy="archive",
            timeout_at=None,
        ),
    )
    with pytest.raises(ValueError, match="unknown timeout policy"):
        await env.run(
            activities.apply_interaction_timeout,
            ApplyTimeoutInput(interaction_id=inserted.interaction_id, policy=policy),
        )


async def test_insert_interaction_stores_timeout_at(activities, db_pool):
    """timeout_at is persisted exactly as passed — external sweeps depend on it."""
    from datetime import UTC, datetime

    deadline = datetime(2030, 1, 1, 12, 0, 0, tzinfo=UTC)
    env = ActivityEnvironment()
    inserted = await env.run(
        activities.insert_interaction,
        InsertInteractionInput(
            flow_run_id="run-timeout-at",
            agent_id="sebas",
            kind="approval",
            origin="t",
            prompt="x",
            options=None,
            timeout_policy="archive",
            timeout_at=deadline,
        ),
    )
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT timeout_at FROM interactions WHERE id = $1",
            UUID(inserted.interaction_id),
        )
    assert row["timeout_at"] == deadline


async def test_update_delivery_ref_writes_and_skips_resolved(activities, db_pool):
    """update_interaction_delivery_ref persists the channel-neutral ref on a
    pending row (Slack delivery has no numeric message_id), and — like the
    message_id bridge — must NOT clobber a row that has already resolved."""
    env = ActivityEnvironment()
    inserted = await env.run(
        activities.insert_interaction,
        InsertInteractionInput(
            flow_run_id="run-dref",
            agent_id="sebas",
            kind="choice",
            origin="t",
            prompt="x",
            options=None,
            timeout_policy="archive",
            timeout_at=None,
        ),
    )
    ref = {"adapter": "slack", "channel": "C9", "ts": "2.2"}
    await env.run(activities.update_interaction_delivery_ref, inserted.interaction_id, ref)
    async with db_pool.acquire() as conn:
        stored = await conn.fetchval(
            "SELECT delivery_ref FROM interactions WHERE id = $1",
            UUID(inserted.interaction_id),
        )
    assert stored == ref

    # Resolve, then a late ref write must be a no-op (status-guarded).
    await env.run(
        activities.resolve_interaction,
        ResolveInteractionInput(interaction_id=inserted.interaction_id, response={"v": 1}),
    )
    await env.run(
        activities.update_interaction_delivery_ref,
        inserted.interaction_id,
        {"adapter": "slack", "channel": "LATE", "ts": "9.9"},
    )
    async with db_pool.acquire() as conn:
        after = await conn.fetchval(
            "SELECT delivery_ref FROM interactions WHERE id = $1",
            UUID(inserted.interaction_id),
        )
    assert after == ref  # unchanged
