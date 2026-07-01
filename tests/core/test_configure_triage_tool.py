"""Tests for configure_triage chat tool."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from aegis.services.chat import ToolContext, _exec_configure_triage


@pytest.fixture
def mock_pool():
    pool = AsyncMock()
    pool.fetchrow = AsyncMock(return_value=None)
    pool.execute = AsyncMock()
    return pool


@pytest.fixture
def ctx():
    ctx = ToolContext()
    ctx.agent_id = "pandoras-actor"
    ctx.settings = MagicMock()
    return ctx


async def test_get_sentry_ignored_empty(mock_pool, ctx):
    result = json.loads(
        await _exec_configure_triage(
            mock_pool, {"setting": "sentry_ignored_projects", "action": "get"}, ctx
        )
    )
    assert result["current"] == []


async def test_add_sentry_project(mock_pool, ctx):
    result = json.loads(
        await _exec_configure_triage(
            mock_pool,
            {"setting": "sentry_ignored_projects", "action": "add", "value": "php_core_api"},
            ctx,
        )
    )
    assert result["ok"] is True
    assert result["current"] == ["php_core_api"]
    mock_pool.execute.assert_called_once()
    call_args = mock_pool.execute.call_args[0]
    stored = call_args[2]
    assert stored == ["php_core_api"]


async def test_add_sentry_project_existing(mock_pool, ctx):
    mock_pool.fetchrow = AsyncMock(return_value={"value": ["php_core_api"]})
    result = json.loads(
        await _exec_configure_triage(
            mock_pool,
            {"setting": "sentry_ignored_projects", "action": "add", "value": "another_project"},
            ctx,
        )
    )
    assert result["current"] == ["php_core_api", "another_project"]


async def test_add_sentry_project_dedup(mock_pool, ctx):
    mock_pool.fetchrow = AsyncMock(return_value={"value": ["php_core_api"]})
    result = json.loads(
        await _exec_configure_triage(
            mock_pool,
            {"setting": "sentry_ignored_projects", "action": "add", "value": "php_core_api"},
            ctx,
        )
    )
    assert result["current"] == ["php_core_api"]  # no duplicate


async def test_remove_sentry_project(mock_pool, ctx):
    mock_pool.fetchrow = AsyncMock(return_value={"value": ["php_core_api", "other"]})
    result = json.loads(
        await _exec_configure_triage(
            mock_pool,
            {"setting": "sentry_ignored_projects", "action": "remove", "value": "php_core_api"},
            ctx,
        )
    )
    assert result["current"] == ["other"]


async def test_set_notification_mode(mock_pool, ctx):
    result = json.loads(
        await _exec_configure_triage(
            mock_pool, {"setting": "notification_mode", "action": "set", "value": "per_item"}, ctx
        )
    )
    assert result["ok"] is True
    assert result["current"] == "per_item"


async def test_set_burst_threshold(mock_pool, ctx):
    result = json.loads(
        await _exec_configure_triage(
            mock_pool, {"setting": "burst_threshold", "action": "set", "value": 5}, ctx
        )
    )
    assert result["ok"] is True
    assert result["current"] == 5


async def test_invalid_setting(mock_pool, ctx):
    result = json.loads(
        await _exec_configure_triage(mock_pool, {"setting": "nonexistent", "action": "get"}, ctx)
    )
    assert "error" in result


async def test_add_email_domain(mock_pool, ctx):
    result = json.loads(
        await _exec_configure_triage(
            mock_pool,
            {"setting": "email_ignored_domains", "action": "add", "value": "spam.com"},
            ctx,
        )
    )
    assert result["ok"] is True
    assert "spam.com" in result["current"]


async def test_wrong_action_for_list(mock_pool, ctx):
    result = json.loads(
        await _exec_configure_triage(
            mock_pool, {"setting": "sentry_ignored_projects", "action": "set", "value": "x"}, ctx
        )
    )
    assert "error" in result


async def test_wrong_action_for_scalar(mock_pool, ctx):
    result = json.loads(
        await _exec_configure_triage(
            mock_pool, {"setting": "notification_mode", "action": "add", "value": "digest"}, ctx
        )
    )
    assert "error" in result
