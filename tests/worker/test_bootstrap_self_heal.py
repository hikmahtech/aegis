"""Bootstrap detects missing managed-project keys and adds only those.

Targets the self-heal path: deployments seeded under an older managed_projects
list gain newly-introduced containers (e.g. next/someday) without a manual SQL
patch. Self-heal adopts a project by NAME when it already exists in Todoist
(e.g. created out-of-band by the projects→labels migration) instead of
creating a duplicate.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import yaml
from aegis_worker.activities.todoist import TodoistActivities

_SEED = {
    "managed_projects": [
        {"key": "next", "name": "Next"},
        {"key": "someday", "name": "Someday / Later"},
    ],
    "labels": {"assignees": [], "contexts": []},
    "filters": [],
}


def _write_seed(tmp_path: Path) -> str:
    seed_dir = tmp_path / "seed"
    seed_dir.mkdir()
    (seed_dir / "todoist.yaml").write_text(yaml.safe_dump(_SEED))
    return str(seed_dir)


def _patch_project_builder(monkeypatch) -> None:
    from aegis.connectors.todoist import TodoistConnector

    monkeypatch.setattr(
        TodoistConnector,
        "build_create_project_command",
        staticmethod(
            lambda name, parent_id=None: {
                "type": "project_add",
                "uuid": "u",
                "temp_id": "proj-stub",
                "args": {"name": name},
            }
        ),
    )


@pytest.mark.asyncio
async def test_bootstrap_creates_only_missing_key(
    db_pool, tmp_path: Path, monkeypatch
) -> None:
    # Existing settings: inbox + next. Missing: someday.
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO settings (key, value) VALUES "
            "('todoist_managed_project_ids', $1) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            {"inbox": "100", "next": "200"},
        )
    seed_dir = _write_seed(tmp_path)

    connector = AsyncMock()
    # Probe returns no "Someday / Later" → it must be created.
    connector.sync = AsyncMock(return_value={"ok": True, "data": {"projects": []}})
    connector.commands = AsyncMock(
        return_value={
            "ok": True,
            "data": {"sync_status": {}, "temp_id_mapping": {"proj-stub": "500"}},
        }
    )
    acts = TodoistActivities(db_pool=db_pool, connector=connector, seed_dir=seed_dir)
    _patch_project_builder(monkeypatch)

    result = await acts.bootstrap_if_empty()

    assert result["bootstrapped"] is True, result
    assert result.get("missing_keys") == ["someday"], result
    connector.commands.assert_awaited_once()
    sent = connector.commands.await_args[0][0]
    assert [c["args"]["name"] for c in sent] == ["Someday / Later"]
    async with db_pool.acquire() as conn:
        value = await conn.fetchval(
            "SELECT value FROM settings WHERE key='todoist_managed_project_ids'"
        )
    assert value["someday"] == "500"
    assert value["inbox"] == "100"  # untouched


@pytest.mark.asyncio
async def test_self_heal_adopts_existing_project_by_name(
    db_pool, tmp_path: Path, monkeypatch
) -> None:
    """If a managed project already exists in Todoist (created by the
    migration), self-heal adopts its id instead of creating a duplicate."""
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO settings (key, value) VALUES "
            "('todoist_managed_project_ids', $1) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            {"inbox": "100", "next": "200"},
        )
    seed_dir = _write_seed(tmp_path)

    connector = AsyncMock()
    # "Someday / Later" already exists live → adopt P_SOME, create nothing.
    connector.sync = AsyncMock(
        return_value={
            "ok": True,
            "data": {
                "projects": [
                    {"id": "P_SOME", "name": "Someday / Later", "is_archived": False},
                ]
            },
        }
    )
    connector.commands = AsyncMock()
    acts = TodoistActivities(db_pool=db_pool, connector=connector, seed_dir=seed_dir)
    _patch_project_builder(monkeypatch)

    result = await acts.bootstrap_if_empty()

    assert result["bootstrapped"] is True, result
    # Nothing created — adopted by name.
    connector.commands.assert_not_awaited()
    async with db_pool.acquire() as conn:
        value = await conn.fetchval(
            "SELECT value FROM settings WHERE key='todoist_managed_project_ids'"
        )
    assert value["someday"] == "P_SOME"
    assert value["next"] == "200"


@pytest.mark.asyncio
async def test_bootstrap_short_circuits_when_complete(db_pool, tmp_path) -> None:
    # All expected keys present → no Todoist call at all.
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO settings (key, value) VALUES "
            "('todoist_managed_project_ids', $1) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            {"inbox": "1", "next": "2", "someday": "3"},
        )
    seed_dir = _write_seed(tmp_path)
    connector = AsyncMock()
    acts = TodoistActivities(db_pool=db_pool, connector=connector, seed_dir=seed_dir)
    result = await acts.bootstrap_if_empty()
    assert result == {"bootstrapped": False, "reason": "already_done"}
    connector.commands.assert_not_awaited()
