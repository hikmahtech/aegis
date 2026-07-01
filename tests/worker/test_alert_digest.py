"""Tests for alert digest accumulation and formatting."""

import json
from unittest.mock import AsyncMock

from aegis_worker.activities.alerts import AlertActivities


async def test_accumulate_digest_item():
    """Adds item to settings buffer."""
    pool = AsyncMock()
    # Simulate empty buffer (no existing setting)
    pool.fetchval.return_value = None

    act = AlertActivities(db_pool=pool)
    await act.accumulate_digest_item(
        {"type": "self_resolved", "title": "Service down", "source": "grafana"}
    )

    # Should have called upsert on settings
    pool.execute.assert_called_once()
    call_args = pool.execute.call_args
    sql = call_args[0][0]
    assert "alert_digest_buffer" in sql
    # No ::jsonb cast — passing Python dict directly via the asyncpg codec
    assert "::jsonb" not in sql
    written = call_args[0][1]
    assert isinstance(written, dict)
    assert len(written["items"]) == 1
    assert written["items"][0]["type"] == "self_resolved"


async def test_build_digest_message():
    """Formats digest from buffer with counts."""
    buffer = {
        "items": [
            {"type": "self_resolved", "title": "Service A down", "source": "grafana"},
            {"type": "self_resolved", "title": "Service A down", "source": "grafana"},
            {"type": "sentry_suppressed", "title": "NullPointer", "project": "bcp"},
            {"type": "not_actionable", "title": "Disk warning", "source": "alertmanager"},
        ]
    }
    pool = AsyncMock()
    pool.fetchval.return_value = json.dumps(buffer)

    act = AlertActivities(db_pool=pool)
    result = await act.build_alert_digest()

    assert result["count"] == 4
    assert "Self-Resolved" in result["message"]
    assert "Service A down" in result["message"]
    assert "Sentry Suppressed" in result["message"]
    assert "Not Actionable" in result["message"]

    # Buffer should be cleared
    pool.execute.assert_called_once()
    clear_sql = pool.execute.call_args[0][0]
    assert "alert_digest_buffer" in clear_sql


async def test_build_digest_empty():
    """Empty buffer returns empty message."""
    pool = AsyncMock()
    pool.fetchval.return_value = None

    act = AlertActivities(db_pool=pool)
    result = await act.build_alert_digest()

    assert result["count"] == 0
    assert result["message"] == ""
