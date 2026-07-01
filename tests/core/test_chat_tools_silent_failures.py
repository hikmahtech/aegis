"""Regression tests for the H1 chat-tool silent-failure fix.

Two contracts locked in here:

1. Permanent rejection (ITEM_NOT_FOUND etc.) → user-facing "Todoist error"
   string. No outbox queue (replaying won't help). The original PR #239 fix.
2. Retryable failure (envelope 5xx / network / per-cmd retryable) → outbox
   queue + "queued for retry" message. The follow-up fix that completed
   PR #239's intent (per the original PR's lesson, which overstated the
   actual code change — see PR title "GTD gap sweep").
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from aegis.connectors.todoist import TodoistConnector
from aegis.services.chat import (
    ToolContext,
    _exec_complete_task,
    _exec_defer_task,
    _exec_handoff_task,
    _exec_mark_waiting,
)


def _rejection_envelope(uuids: list[str]) -> dict:
    """Mimic Todoist returning HTTP 200 but per-command ITEM_NOT_FOUND."""
    return {
        "ok": True,
        "data": {
            "sync_status": {
                u: {
                    "error": "Item not found",
                    "error_code": 22,
                    "error_tag": "ITEM_NOT_FOUND",
                    "http_code": 404,
                }
                for u in uuids
            },
            "temp_id_mapping": {},
        },
    }


def _retryable_envelope() -> dict:
    """Envelope-level transient failure (5xx / timeout / rate-limit).

    Matches what TodoistConnector returns for HTTP 503 — the caller should
    outbox-queue and tell the user it's retried in the background.
    """
    return {
        "ok": False,
        "data": None,
        "error": "http_503",
        "retryable": True,
        "external_ref": None,
    }


async def _outbox_count(pool) -> int:
    async with pool.acquire() as conn:
        return await conn.fetchval("SELECT count(*) FROM todoist_outbox") or 0


@pytest.mark.asyncio
async def test_complete_task_surfaces_item_not_found(db_pool) -> None:
    """A deleted-in-Todoist task should NOT report 'Completed' to the user."""
    sent: list[list[dict]] = []

    class FakeConnector(TodoistConnector):
        def __init__(self, *a, **kw):
            pass

        async def commands(self, batch):
            sent.append(batch)
            uuids = [c["uuid"] for c in batch]
            return _rejection_envelope(uuids)

    fake_settings = MagicMock()
    fake_settings.todoist_api_key = "fake"

    with (
        patch("aegis.connectors.todoist.TodoistConnector", FakeConnector),
        patch("aegis.config.Settings", return_value=fake_settings),
    ):
        result = await _exec_complete_task(
            db_pool,
            {"task_id": "GHOST_TASK", "note": "done"},
            ToolContext(agent_id="sebas"),
        )

    assert "Todoist error" in result
    assert "ITEM_NOT_FOUND" in result
    assert not result.startswith("Completed")


@pytest.mark.asyncio
async def test_defer_task_surfaces_item_not_found(db_pool) -> None:
    class FakeConnector(TodoistConnector):
        def __init__(self, *a, **kw):
            pass

        async def commands(self, batch):
            uuids = [c["uuid"] for c in batch]
            return _rejection_envelope(uuids)

    fake_settings = MagicMock()
    fake_settings.todoist_api_key = "fake"

    with (
        patch("aegis.connectors.todoist.TodoistConnector", FakeConnector),
        patch("aegis.config.Settings", return_value=fake_settings),
    ):
        result = await _exec_defer_task(
            db_pool,
            {"task_id": "GHOST_TASK", "until": "tomorrow"},
            ToolContext(agent_id="sebas"),
        )

    assert "Todoist error" in result
    assert "ITEM_NOT_FOUND" in result
    assert not result.startswith("Deferred")


@pytest.mark.asyncio
async def test_handoff_task_surfaces_item_not_found(db_pool) -> None:
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO todoist_projects (id, name, is_managed, raw) "
            "VALUES ('P_HO', 'P_HO', false, '{}'::jsonb) "
            "ON CONFLICT (id) DO NOTHING"
        )
        await conn.execute(
            "INSERT INTO todoist_tasks (id, content, labels, project_id, "
            "is_completed) VALUES ('T_HO', 'x', $1, 'P_HO', false) "
            "ON CONFLICT (id) DO UPDATE SET labels = EXCLUDED.labels",
            ["@me"],
        )

    class FakeConnector(TodoistConnector):
        def __init__(self, *a, **kw):
            pass

        async def commands(self, batch):
            uuids = [c["uuid"] for c in batch]
            return _rejection_envelope(uuids)

    fake_settings = MagicMock()
    fake_settings.todoist_api_key = "fake"

    with (
        patch("aegis.connectors.todoist.TodoistConnector", FakeConnector),
        patch("aegis.config.Settings", return_value=fake_settings),
    ):
        result = await _exec_handoff_task(
            db_pool,
            {"task_id": "T_HO", "to_assignee": "@pandora"},
            ToolContext(agent_id="sebas"),
        )

    assert "Todoist error" in result
    assert "ITEM_NOT_FOUND" in result
    assert not result.startswith("Handed off")


@pytest.mark.asyncio
async def test_mark_waiting_surfaces_item_not_found(db_pool) -> None:
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO todoist_projects (id, name, is_managed, raw) "
            "VALUES ('P_MW', 'P_MW', false, '{}'::jsonb) "
            "ON CONFLICT (id) DO NOTHING"
        )
        await conn.execute(
            "INSERT INTO todoist_tasks (id, content, labels, project_id, "
            "is_completed) VALUES ('T_MW', 'x', $1, 'P_MW', false) "
            "ON CONFLICT (id) DO UPDATE SET labels = EXCLUDED.labels",
            ["@me"],
        )

    class FakeConnector(TodoistConnector):
        def __init__(self, *a, **kw):
            pass

        async def commands(self, batch):
            uuids = [c["uuid"] for c in batch]
            return _rejection_envelope(uuids)

    fake_settings = MagicMock()
    fake_settings.todoist_api_key = "fake"

    with (
        patch("aegis.connectors.todoist.TodoistConnector", FakeConnector),
        patch("aegis.config.Settings", return_value=fake_settings),
    ):
        result = await _exec_mark_waiting(
            db_pool,
            {"task_id": "T_MW", "who": "ops"},
            ToolContext(agent_id="sebas"),
        )

    assert "Todoist error" in result
    assert "ITEM_NOT_FOUND" in result
    assert not result.startswith("Marked")


# --- Retryable → outbox queue contract ---


@pytest.mark.asyncio
async def test_complete_task_retryable_queues_outbox(db_pool) -> None:
    """A 5xx during complete_task should outbox-queue, not surface as error."""

    class FakeConnector(TodoistConnector):
        def __init__(self, *a, **kw):
            pass

        async def commands(self, batch):
            return _retryable_envelope()

    fake_settings = MagicMock()
    fake_settings.todoist_api_key = "fake"

    before = await _outbox_count(db_pool)
    with (
        patch("aegis.connectors.todoist.TodoistConnector", FakeConnector),
        patch("aegis.config.Settings", return_value=fake_settings),
    ):
        result = await _exec_complete_task(
            db_pool,
            {"task_id": "T_RETRY_COMPLETE", "note": "x"},
            ToolContext(agent_id="sebas"),
        )
    after = await _outbox_count(db_pool)

    assert "queued for retry" in result
    assert "complete_task" in result
    assert not result.startswith("Completed")
    # 2 commands staged (item_complete + note_add)
    assert after - before == 2


@pytest.mark.asyncio
async def test_defer_task_retryable_queues_outbox(db_pool) -> None:
    class FakeConnector(TodoistConnector):
        def __init__(self, *a, **kw):
            pass

        async def commands(self, batch):
            return _retryable_envelope()

    fake_settings = MagicMock()
    fake_settings.todoist_api_key = "fake"

    before = await _outbox_count(db_pool)
    with (
        patch("aegis.connectors.todoist.TodoistConnector", FakeConnector),
        patch("aegis.config.Settings", return_value=fake_settings),
    ):
        result = await _exec_defer_task(
            db_pool,
            {"task_id": "T_RETRY_DEFER", "until": "tomorrow"},
            ToolContext(agent_id="sebas"),
        )
    after = await _outbox_count(db_pool)

    assert "queued for retry" in result
    assert "defer_task" in result
    assert not result.startswith("Deferred")
    assert after - before == 1


@pytest.mark.asyncio
async def test_mark_waiting_retryable_queues_outbox(db_pool) -> None:
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO todoist_projects (id, name, is_managed, raw) "
            "VALUES ('P_MWR', 'P_MWR', false, '{}'::jsonb) "
            "ON CONFLICT (id) DO NOTHING"
        )
        await conn.execute(
            "INSERT INTO todoist_tasks (id, content, labels, project_id, "
            "is_completed) VALUES ('T_MWR', 'x', $1, 'P_MWR', false) "
            "ON CONFLICT (id) DO UPDATE SET labels = EXCLUDED.labels",
            ["@me"],
        )

    class FakeConnector(TodoistConnector):
        def __init__(self, *a, **kw):
            pass

        async def commands(self, batch):
            return _retryable_envelope()

    fake_settings = MagicMock()
    fake_settings.todoist_api_key = "fake"

    before = await _outbox_count(db_pool)
    with (
        patch("aegis.connectors.todoist.TodoistConnector", FakeConnector),
        patch("aegis.config.Settings", return_value=fake_settings),
    ):
        result = await _exec_mark_waiting(
            db_pool,
            {"task_id": "T_MWR", "who": "legal"},
            ToolContext(agent_id="sebas"),
        )
    after = await _outbox_count(db_pool)

    assert "queued for retry" in result
    assert "mark_waiting" in result
    assert not result.startswith("Marked")
    # item_update + note_add
    assert after - before == 2


@pytest.mark.asyncio
async def test_handoff_task_retryable_queues_outbox(db_pool) -> None:
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO todoist_projects (id, name, is_managed, raw) "
            "VALUES ('P_HOR', 'P_HOR', false, '{}'::jsonb) "
            "ON CONFLICT (id) DO NOTHING"
        )
        await conn.execute(
            "INSERT INTO todoist_tasks (id, content, labels, project_id, "
            "is_completed) VALUES ('T_HOR', 'x', $1, 'P_HOR', false) "
            "ON CONFLICT (id) DO UPDATE SET labels = EXCLUDED.labels",
            ["@sebas"],
        )

    class FakeConnector(TodoistConnector):
        def __init__(self, *a, **kw):
            pass

        async def commands(self, batch):
            return _retryable_envelope()

    fake_settings = MagicMock()
    fake_settings.todoist_api_key = "fake"

    before = await _outbox_count(db_pool)
    with (
        patch("aegis.connectors.todoist.TodoistConnector", FakeConnector),
        patch("aegis.config.Settings", return_value=fake_settings),
    ):
        result = await _exec_handoff_task(
            db_pool,
            {"task_id": "T_HOR", "to_assignee": "@pandora"},
            ToolContext(agent_id="sebas"),
        )
    after = await _outbox_count(db_pool)

    assert "queued for retry" in result
    assert "handoff_task" in result
    assert not result.startswith("Handed off")
    assert after - before == 1


