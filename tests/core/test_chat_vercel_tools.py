"""Vercel chat-tool executors — args normalization + connector dispatch."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest
from aegis.services.chat import (
    ToolContext,
    _exec_vercel_get_build_logs,
    _exec_vercel_get_deployment,
    _exec_vercel_get_project,
    _exec_vercel_list_deployments,
    _normalize_vercel_project,
)


def test_normalize_vercel_project_strips_slug_prefix():
    assert _normalize_vercel_project("vercel-example-site") == "example-site"
    assert _normalize_vercel_project("example-site") == "example-site"
    assert _normalize_vercel_project("  vercel-foo  ") == "foo"
    assert _normalize_vercel_project("") == ""


@pytest.mark.asyncio
async def test_vercel_get_project_without_connector_returns_error():
    ctx = ToolContext(vercel_connector=None)
    out = json.loads(await _exec_vercel_get_project(None, {"project": "example-site"}, ctx))
    assert out == {"error": "vercel_connector_not_configured"}


@pytest.mark.asyncio
async def test_vercel_get_project_requires_project_arg():
    ctx = ToolContext(vercel_connector=AsyncMock())
    out = json.loads(await _exec_vercel_get_project(None, {}, ctx))
    assert out == {"error": "project is required"}


@pytest.mark.asyncio
async def test_vercel_get_project_dispatches_normalized_name():
    conn = AsyncMock()
    conn.get_project.return_value = {"id": "prj_x", "name": "example-site"}
    ctx = ToolContext(vercel_connector=conn)
    out = json.loads(
        await _exec_vercel_get_project(None, {"project": "vercel-example-site"}, ctx)
    )
    conn.get_project.assert_awaited_once_with("example-site")
    assert out == {"id": "prj_x", "name": "example-site"}


@pytest.mark.asyncio
async def test_vercel_list_deployments_passes_filters_through():
    conn = AsyncMock()
    conn.list_deployments.return_value = {"deployments": [], "count": 0}
    ctx = ToolContext(vercel_connector=conn)
    await _exec_vercel_list_deployments(
        None,
        {
            "project": "vercel-example-site",
            "limit": 25,
            "since_hours": 24,
            "state": "ERROR",
        },
        ctx,
    )
    conn.list_deployments.assert_awaited_once_with(
        "example-site", limit=25, since_hours=24, state="ERROR"
    )


@pytest.mark.asyncio
async def test_vercel_list_deployments_rejects_bad_since_hours():
    ctx = ToolContext(vercel_connector=AsyncMock())
    out = json.loads(
        await _exec_vercel_list_deployments(
            None,
            {"project": "example-site", "since_hours": "not-a-number"},
            ctx,
        )
    )
    assert out == {"error": "since_hours must be an integer"}


@pytest.mark.asyncio
async def test_vercel_get_deployment_requires_id():
    ctx = ToolContext(vercel_connector=AsyncMock())
    out = json.loads(await _exec_vercel_get_deployment(None, {}, ctx))
    assert out == {"error": "deployment_id is required"}


@pytest.mark.asyncio
async def test_vercel_get_deployment_dispatches():
    conn = AsyncMock()
    conn.get_deployment.return_value = {"id": "dpl_1", "readyState": "READY"}
    ctx = ToolContext(vercel_connector=conn)
    out = json.loads(
        await _exec_vercel_get_deployment(None, {"deployment_id": "dpl_1"}, ctx)
    )
    conn.get_deployment.assert_awaited_once_with("dpl_1")
    assert out["readyState"] == "READY"


@pytest.mark.asyncio
async def test_vercel_get_build_logs_passes_errors_only_through():
    conn = AsyncMock()
    conn.get_build_logs.return_value = {"events": [], "count": 0}
    ctx = ToolContext(vercel_connector=conn)
    await _exec_vercel_get_build_logs(
        None,
        {"deployment_id": "dpl_x", "limit": 250, "errors_only": True},
        ctx,
    )
    conn.get_build_logs.assert_awaited_once_with("dpl_x", limit=250, errors_only=True)
