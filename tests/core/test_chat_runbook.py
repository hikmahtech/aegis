"""Tests for update_runbook chat tool."""

import json
from unittest.mock import AsyncMock

from aegis.services.chat import ToolContext, _exec_update_runbook


async def test_update_runbook_alert_type():
    """Updates a runbook for an alert type, verifies it is captured as content."""
    pool = AsyncMock()
    kc = AsyncMock()
    kc.ingest_content.return_value = {"status": "ok"}
    ctx = ToolContext(knowledge_connector=kc)

    result = await _exec_update_runbook(
        pool,
        {"target": "alert_type:ServiceDown", "content": "Restart the service and check logs."},
        ctx,
    )
    data = json.loads(result)
    assert data["ok"] is True
    assert data["target"] == "alert_type:ServiceDown"

    kc.ingest_content.assert_called_once()
    call_kwargs = kc.ingest_content.call_args.kwargs
    assert call_kwargs["source_type"] == "runbook"
    assert "runbook" in call_kwargs["tags"]
    assert "Restart" in call_kwargs["raw_text"]


async def test_update_runbook_no_knowledge():
    """Without knowledge connector, returns error."""
    pool = AsyncMock()
    ctx = ToolContext(knowledge_connector=None)

    result = await _exec_update_runbook(
        pool,
        {"target": "alert_type:ServiceDown", "content": "Restart."},
        ctx,
    )
    data = json.loads(result)
    assert "error" in data
