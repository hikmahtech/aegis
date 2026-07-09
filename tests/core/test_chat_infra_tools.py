"""Tests for Pandora's Actor infrastructure chat tools."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import asyncpg  # noqa: F401  (type hint)
import pytest
from aegis.services import chat
from aegis.services.chat import (
    AGENT_TOOL_SETS,
    CHAT_TOOLS,
    TOOL_EXECUTORS,
    ToolContext,
    _exec_get_pod_logs,
    _exec_get_service_logs,
    _exec_inspect_service,
    _exec_list_argocd_apps,
    _exec_list_deployments,
    _exec_list_nodes,
    _exec_list_pods,
    _exec_list_services,
    _exec_restart_service,
    _exec_run_infra_script,
    _exec_sync_argocd_app,
)


@pytest.fixture(autouse=True)
def _acme_script_host_contexts():
    """These tests use 'acme-prod'/'acme-test' as stand-in script-host k8s
    context names (issue #51 made the real set configurable via
    AEGIS_SCRIPT_HOST_K8S_CONTEXTS, default empty). Mutate the module-level
    set IN PLACE — not reassign — so the reference `_INFRA_SPECS` captured at
    import time and the `contexts is _INFRA_CONTEXTS_K8S` identity check in
    `_exec_infra` still line up."""
    added = {"acme-prod", "acme-test"} - chat._INFRA_CONTEXTS_K8S
    chat._INFRA_CONTEXTS_K8S.update(added)
    try:
        yield
    finally:
        chat._INFRA_CONTEXTS_K8S.difference_update(added)


def test_tool_context_has_remote_script_connector_field():
    """ToolContext must carry a remote_script_connector field."""
    ctx = ToolContext()
    assert hasattr(ctx, "remote_script_connector")
    assert ctx.remote_script_connector is None

    mock_connector = MagicMock()
    ctx2 = ToolContext(remote_script_connector=mock_connector)
    assert ctx2.remote_script_connector is mock_connector


@pytest.mark.asyncio
async def test_exec_list_nodes_calls_script_and_returns_stdout():
    mock_connector = MagicMock()
    mock_connector.run_script = AsyncMock(
        return_value={
            "status": "succeeded",
            "exit_code": 0,
            "stdout": '[{"Hostname":"node-a","Status":"Ready"}]',
            "stderr": "",
        }
    )
    ctx = ToolContext(remote_script_connector=mock_connector)

    result = await _exec_list_nodes(None, {"context": "swarm"}, ctx)

    mock_connector.run_script.assert_awaited_once_with(
        "infra/infra_list_nodes", ["swarm"], timeout=30
    )
    assert '"Hostname":"node-a"' in result


@pytest.mark.asyncio
async def test_exec_list_nodes_returns_error_when_connector_missing():
    ctx = ToolContext(remote_script_connector=None)
    result = await _exec_list_nodes(None, {"context": "swarm"}, ctx)
    assert "error" in json.loads(result)


@pytest.mark.asyncio
async def test_exec_list_nodes_returns_error_on_script_failure():
    mock_connector = MagicMock()
    mock_connector.run_script = AsyncMock(
        return_value={
            "status": "failed",
            "exit_code": 1,
            "stdout": "",
            "stderr": "docker: connection refused",
        }
    )
    ctx = ToolContext(remote_script_connector=mock_connector)

    result = await _exec_list_nodes(None, {"context": "swarm"}, ctx)
    data = json.loads(result)
    assert "error" in data
    assert "connection refused" in data["error"]


@pytest.mark.asyncio
async def test_exec_list_services_calls_script():
    mock_connector = MagicMock()
    mock_connector.run_script = AsyncMock(
        return_value={
            "status": "succeeded",
            "exit_code": 0,
            "stdout": '[{"Name":"aegis_core","Replicas":"1/1"}]',
            "stderr": "",
        }
    )
    ctx = ToolContext(remote_script_connector=mock_connector)

    result = await _exec_list_services(None, {"context": "swarm"}, ctx)

    mock_connector.run_script.assert_awaited_once_with(
        "infra/infra_list_services", ["swarm"], timeout=30
    )
    assert "aegis_core" in result


@pytest.mark.asyncio
async def test_exec_inspect_service_passes_service_name():
    mock_connector = MagicMock()
    mock_connector.run_script = AsyncMock(
        return_value={
            "status": "succeeded",
            "exit_code": 0,
            "stdout": '{"ID":"abc","Name":"aegis_core"}',
            "stderr": "",
        }
    )
    ctx = ToolContext(remote_script_connector=mock_connector)

    result = await _exec_inspect_service(
        None, {"context": "swarm", "service_name": "aegis_core"}, ctx
    )

    mock_connector.run_script.assert_awaited_once_with(
        "infra/infra_inspect_service", ["swarm", "aegis_core"], timeout=30
    )
    assert "aegis_core" in result


@pytest.mark.asyncio
async def test_exec_inspect_service_validates_service_name():
    ctx = ToolContext(remote_script_connector=MagicMock())
    result = await _exec_inspect_service(
        None, {"context": "swarm", "service_name": "bad;name"}, ctx
    )
    assert "error" in json.loads(result)


@pytest.mark.asyncio
async def test_exec_get_service_logs_passes_tail():
    mock_connector = MagicMock()
    mock_connector.run_script = AsyncMock(
        return_value={
            "status": "succeeded",
            "exit_code": 0,
            "stdout": "2026-04-04 log line\n",
            "stderr": "",
        }
    )
    ctx = ToolContext(remote_script_connector=mock_connector)

    await _exec_get_service_logs(
        None, {"context": "swarm", "service_name": "aegis_core", "tail": 100}, ctx
    )

    mock_connector.run_script.assert_awaited_once_with(
        "infra/infra_get_service_logs",
        ["swarm", "aegis_core", "100"],
        timeout=60,
    )


@pytest.mark.asyncio
async def test_exec_get_service_logs_defaults_tail_to_50():
    mock_connector = MagicMock()
    mock_connector.run_script = AsyncMock(
        return_value={
            "status": "succeeded",
            "exit_code": 0,
            "stdout": "",
            "stderr": "",
        }
    )
    ctx = ToolContext(remote_script_connector=mock_connector)

    await _exec_get_service_logs(None, {"context": "swarm", "service_name": "aegis_core"}, ctx)

    mock_connector.run_script.assert_awaited_once_with(
        "infra/infra_get_service_logs",
        ["swarm", "aegis_core", "50"],
        timeout=60,
    )


@pytest.mark.asyncio
async def test_exec_restart_service_uses_longer_timeout():
    mock_connector = MagicMock()
    mock_connector.run_script = AsyncMock(
        return_value={
            "status": "succeeded",
            "exit_code": 0,
            "stdout": '{"result":"restarted","service":"aegis_core"}',
            "stderr": "",
        }
    )
    ctx = ToolContext(remote_script_connector=mock_connector)

    await _exec_restart_service(None, {"context": "swarm", "service_name": "aegis_core"}, ctx)

    mock_connector.run_script.assert_awaited_once_with(
        "infra/infra_restart_service", ["swarm", "aegis_core"], timeout=120
    )


@pytest.mark.asyncio
async def test_exec_list_pods_all_namespaces_by_default():
    mock_connector = MagicMock()
    mock_connector.run_script = AsyncMock(
        return_value={
            "status": "succeeded",
            "exit_code": 0,
            "stdout": "[]",
            "stderr": "",
        }
    )
    ctx = ToolContext(remote_script_connector=mock_connector)

    await _exec_list_pods(None, {"context": "acme-prod"}, ctx)

    mock_connector.run_script.assert_awaited_once_with(
        "infra/infra_list_pods",
        ["acme-prod", "", ""],
        timeout=30,
    )


@pytest.mark.asyncio
async def test_exec_list_pods_with_namespace_and_status_filter():
    mock_connector = MagicMock()
    mock_connector.run_script = AsyncMock(
        return_value={
            "status": "succeeded",
            "exit_code": 0,
            "stdout": "[]",
            "stderr": "",
        }
    )
    ctx = ToolContext(remote_script_connector=mock_connector)

    await _exec_list_pods(
        None,
        {"context": "acme-test", "namespace": "app", "status": "CrashLoopBackOff"},
        ctx,
    )

    mock_connector.run_script.assert_awaited_once_with(
        "infra/infra_list_pods",
        ["acme-test", "app", "CrashLoopBackOff"],
        timeout=30,
    )


@pytest.mark.asyncio
async def test_exec_list_pods_rejects_unknown_context():
    ctx = ToolContext(remote_script_connector=MagicMock())
    result = await _exec_list_pods(None, {"context": "swarm"}, ctx)
    assert "error" in json.loads(result)


@pytest.mark.asyncio
async def test_exec_list_deployments_with_namespace():
    mock_connector = MagicMock()
    mock_connector.run_script = AsyncMock(
        return_value={
            "status": "succeeded",
            "exit_code": 0,
            "stdout": "[]",
            "stderr": "",
        }
    )
    ctx = ToolContext(remote_script_connector=mock_connector)

    await _exec_list_deployments(None, {"context": "acme-prod", "namespace": "app"}, ctx)

    mock_connector.run_script.assert_awaited_once_with(
        "infra/infra_list_deployments",
        ["acme-prod", "app"],
        timeout=30,
    )


@pytest.mark.asyncio
async def test_exec_get_pod_logs_passes_all_args():
    mock_connector = MagicMock()
    mock_connector.run_script = AsyncMock(
        return_value={
            "status": "succeeded",
            "exit_code": 0,
            "stdout": "log line\n",
            "stderr": "",
        }
    )
    ctx = ToolContext(remote_script_connector=mock_connector)

    await _exec_get_pod_logs(
        None,
        {
            "context": "acme-prod",
            "namespace": "app",
            "pod_name": "core-api-5f8b9",
            "tail": 100,
            "container": "core-api",
        },
        ctx,
    )

    mock_connector.run_script.assert_awaited_once_with(
        "infra/infra_get_pod_logs",
        ["acme-prod", "app", "core-api-5f8b9", "100", "core-api"],
        timeout=60,
    )


@pytest.mark.asyncio
async def test_exec_get_pod_logs_optional_container():
    mock_connector = MagicMock()
    mock_connector.run_script = AsyncMock(
        return_value={
            "status": "succeeded",
            "exit_code": 0,
            "stdout": "",
            "stderr": "",
        }
    )
    ctx = ToolContext(remote_script_connector=mock_connector)

    await _exec_get_pod_logs(
        None,
        {"context": "acme-test", "namespace": "bcp", "pod_name": "batch-1"},
        ctx,
    )

    mock_connector.run_script.assert_awaited_once_with(
        "infra/infra_get_pod_logs",
        ["acme-test", "bcp", "batch-1", "50", ""],
        timeout=60,
    )


@pytest.mark.asyncio
async def test_exec_list_argocd_apps_with_filter():
    mock_connector = MagicMock()
    mock_connector.run_script = AsyncMock(
        return_value={
            "status": "succeeded",
            "exit_code": 0,
            "stdout": "[]",
            "stderr": "",
        }
    )
    ctx = ToolContext(remote_script_connector=mock_connector)

    await _exec_list_argocd_apps(None, {"context": "acme-prod", "filter": "degraded"}, ctx)

    mock_connector.run_script.assert_awaited_once_with(
        "infra/infra_list_argocd_apps",
        ["acme-prod", "degraded"],
        timeout=30,
    )


@pytest.mark.asyncio
async def test_exec_list_argocd_apps_no_filter():
    mock_connector = MagicMock()
    mock_connector.run_script = AsyncMock(
        return_value={
            "status": "succeeded",
            "exit_code": 0,
            "stdout": "[]",
            "stderr": "",
        }
    )
    ctx = ToolContext(remote_script_connector=mock_connector)

    await _exec_list_argocd_apps(None, {"context": "acme-test"}, ctx)

    mock_connector.run_script.assert_awaited_once_with(
        "infra/infra_list_argocd_apps",
        ["acme-test", ""],
        timeout=30,
    )


@pytest.mark.asyncio
async def test_exec_sync_argocd_app_calls_script():
    mock_connector = MagicMock()
    mock_connector.run_script = AsyncMock(
        return_value={
            "status": "succeeded",
            "exit_code": 0,
            "stdout": '{"result":"synced","app":"core-api"}',
            "stderr": "",
        }
    )
    ctx = ToolContext(remote_script_connector=mock_connector)

    await _exec_sync_argocd_app(None, {"context": "acme-test", "app_name": "core-api"}, ctx)

    mock_connector.run_script.assert_awaited_once_with(
        "infra/infra_sync_argocd_app",
        ["acme-test", "core-api"],
        timeout=120,
    )


@pytest.mark.asyncio
async def test_exec_run_infra_script_runs_named_infra_script():
    """run_infra_script routes through scripts/infra/<name>.sh with context as
    the first argument — same surface as the dedicated infra tools. (The old
    implementation queried a `resources.type` column that never existed, so
    the tool errored on every call.)"""
    mock_connector = MagicMock()
    mock_connector.run_script = AsyncMock(
        return_value={
            "status": "succeeded",
            "exit_code": 0,
            "stdout": "[]",
            "stderr": "",
        }
    )
    ctx = ToolContext(remote_script_connector=mock_connector)

    result = await _exec_run_infra_script(
        None,
        {"context": "swarm", "script_name": "infra_list_nodes", "args": ["extra"]},
        ctx,
    )

    mock_connector.run_script.assert_awaited_once_with(
        "infra/infra_list_nodes", ["swarm", "extra"], timeout=120
    )
    assert "[]" in result


@pytest.mark.asyncio
async def test_exec_run_infra_script_rejects_bad_context_and_name():
    ctx = ToolContext(remote_script_connector=MagicMock())

    result = await _exec_run_infra_script(
        None, {"context": "prod-everything", "script_name": "infra_list_nodes"}, ctx
    )
    assert "Unsupported context" in json.loads(result)["error"]

    result = await _exec_run_infra_script(
        None, {"context": "swarm", "script_name": "../etc/passwd"}, ctx
    )
    assert "error" in json.loads(result)


def test_pandoras_actor_has_all_infra_tools():
    # Full infra surface across Swarm (swarm homelab) and k8s/ArgoCD (acme).
    expected_infra_tools = {
        "list_nodes",
        "list_services",
        "inspect_service",
        "get_service_logs",
        "restart_service",
        "list_pods",
        "list_deployments",
        "get_pod_logs",
        "list_argocd_apps",
        "sync_argocd_app",
        "run_infra_script",
    }
    pa_tools = AGENT_TOOL_SETS["pandoras-actor"]
    missing = expected_infra_tools - pa_tools
    assert not missing, f"Missing tools for pandoras-actor: {missing}"


def test_all_infra_tools_registered_in_executors():
    expected = {
        "list_nodes",
        "list_services",
        "inspect_service",
        "get_service_logs",
        "restart_service",
        "list_pods",
        "list_deployments",
        "get_pod_logs",
        "list_argocd_apps",
        "sync_argocd_app",
        "run_infra_script",
    }
    for name in expected:
        assert name in TOOL_EXECUTORS, f"{name} not in TOOL_EXECUTORS"


def test_all_infra_tools_have_schema_definitions():
    expected = {
        "list_nodes",
        "list_services",
        "inspect_service",
        "get_service_logs",
        "restart_service",
        "list_pods",
        "list_deployments",
        "get_pod_logs",
        "list_argocd_apps",
        "sync_argocd_app",
        "run_infra_script",
    }
    schema_names = {t["function"]["name"] for t in CHAT_TOOLS if t["type"] == "function"}
    missing = expected - schema_names
    assert not missing, f"Missing tool schemas: {missing}"


def test_k8s_tool_schemas_have_no_hardcoded_context_enum():
    """issue #51: k8s/argocd cluster context names are self-hoster-configurable
    (AEGIS_SCRIPT_HOST_K8S_CONTEXTS) plus registered kind=k8s infra slugs, so
    the tool schemas must not pin `context` to a fixed enum (previously
    ["acme-prod", "acme-test"] / ["swarm", "acme-prod", "acme-test"])."""
    k8s_tools = {
        "list_pods",
        "list_deployments",
        "get_pod_logs",
        "list_argocd_apps",
        "sync_argocd_app",
        "run_infra_script",
    }
    by_name = {t["function"]["name"]: t["function"] for t in CHAT_TOOLS if t["type"] == "function"}
    for name in k8s_tools:
        context_schema = by_name[name]["parameters"]["properties"]["context"]
        assert "enum" not in context_schema, f"{name}'s context schema still has a hardcoded enum"
