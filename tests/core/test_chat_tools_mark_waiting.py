"""_exec_mark_waiting: label + note, no project move (state-as-label model)."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from aegis.connectors.todoist import TodoistConnector
from aegis.services.chat import ToolContext, _exec_mark_waiting


@pytest.mark.asyncio
async def test_mark_waiting_emits_label_and_note_no_move(db_pool) -> None:
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO todoist_projects (id, name, is_managed, raw) "
            "VALUES ('P_BCP', 'BCP', false, '{}'::jsonb) "
            "ON CONFLICT (id) DO NOTHING"
        )
        await conn.execute(
            "INSERT INTO todoist_tasks (id, content, labels, project_id, "
            "is_completed) VALUES ('T_WAIT', 'pay invoice', $1, 'P_BCP', false) "
            "ON CONFLICT (id) DO UPDATE SET labels = EXCLUDED.labels",
            ["@me", "@area/finance"],
        )

    sent_batches: list[list[dict]] = []

    # Subclass the real connector so static build_* methods are inherited.
    class FakeConnector(TodoistConnector):
        def __init__(self, *a, **kw): pass
        async def commands(self, batch):
            sent_batches.append(batch)
            return {"ok": True, "data": {"sync_status": {}, "temp_id_mapping": {}}}

    fake_settings = MagicMock()
    fake_settings.todoist_api_key = "fake"

    with patch("aegis.connectors.todoist.TodoistConnector", FakeConnector), \
         patch("aegis.config.Settings", return_value=fake_settings):
        result = await _exec_mark_waiting(
            db_pool,
            {"task_id": "T_WAIT", "who": "legal", "expected_by": "2026-06-01"},
            ToolContext(agent_id="sebas"),
        )

    assert result.startswith("Marked T_WAIT waiting on legal")
    assert len(sent_batches) == 1
    batch = sent_batches[0]
    types = [c["type"] for c in batch]
    assert "item_move" not in types          # the key invariant
    assert types.count("item_update") == 1   # label edit
    assert types.count("note_add") == 1      # who/expected-by note
    upd = next(c for c in batch if c["type"] == "item_update")
    assert "@waiting" in upd["args"]["labels"]
    assert "@me" in upd["args"]["labels"]
    assert "@area/finance" in upd["args"]["labels"]
