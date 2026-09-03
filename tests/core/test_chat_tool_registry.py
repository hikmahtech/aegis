"""Registry-integrity guards for the `services/tools/` decomposition (D7).

The executors moved out of `services/chat.py` are almost entirely covered by
incumbent tests, but three failure modes of the *move itself* are not:

1. a tool name silently disappearing from `TOOL_EXECUTORS` — which fails in
   production, not in a default-config test, because a DB
   `agents.metadata.tool_set` can reference a tool no seed agent declares;
2. a `functools.partial` rebinding to the wrong `_INFRA_SPECS` key while the
   registry key stays right, so `list_pods` quietly runs `list_nodes`;
3. the compat re-export in `chat.py` binding a *copy* rather than the same
   object — `tests/core/test_chat_infra_tools.py` mutates
   `chat._INFRA_CONTEXTS_K8S` IN PLACE and relies on `_exec_infra`'s
   `contexts is _INFRA_CONTEXTS_K8S` identity check still seeing it.

The name list below is a committed snapshot, deliberately hand-written rather
than derived, so a dropped or renamed tool has to be acknowledged here.
"""

from __future__ import annotations

import functools
import json
from unittest.mock import AsyncMock, MagicMock

from aegis.services import chat
from aegis.services.chat import TOOL_EXECUTORS, ToolContext, _execute_tool
from aegis.services.tools import infra as tools_infra
from aegis.services.tools import vercel as tools_vercel

# Every chat tool as of the services/tools split. Sorted.
EXPECTED_TOOL_NAMES = [
    "aegis_self_diagnose",
    "ask_knowledge",
    "call_mcp_tool",
    "capture_to_inbox",
    "cloud_identity",
    "comment_on_task",
    "complete_task",
    "configure_triage",
    "create_schedule",
    "defer_task",
    "dispatch_agent_run",
    "find_reference",
    "get_finance_news",
    "get_market_overview",
    "get_pod_logs",
    "get_quote",
    "get_service_logs",
    "handoff_task",
    "inspect_service",
    "investigate_resource",
    "last_contact_with_person",
    "list_argocd_apps",
    "list_cloud_accounts",
    "list_coding_sessions",
    "list_deployments",
    "list_interactions",
    "list_next_actions",
    "list_nodes",
    "list_pods",
    "list_projects",
    "list_services",
    "list_social_channels",
    "mark_waiting",
    "pdf_to_text",
    "query_activities",
    "query_observations",
    "remember_this",
    "research_topic",
    "restart_deployment",
    "restart_service",
    "run_infra_script",
    "search_knowledge",
    "social_timeline",
    "stop_agent_run",
    "sync_argocd_app",
    "system_status",
    "track_topic",
    "trigger_workflow",
    "update_runbook",
    "vercel_get_build_logs",
    "vercel_get_deployment",
    "vercel_get_project",
    "vercel_list_deployments",
    "whats_next",
    "youtube_transcript",
]

# name -> executor __qualname__, module path deliberately excluded so this
# survives a later move; `_exec_infra` partials carry their bound tool key.
EXPECTED_EXECUTOR_IDENTITY = {
    "cloud_identity": "_exec_cloud_identity",
    "get_pod_logs": "_exec_infra('get_pod_logs')",
    "get_service_logs": "_exec_infra('get_service_logs')",
    "inspect_service": "_exec_infra('inspect_service')",
    "list_argocd_apps": "_exec_infra('list_argocd_apps')",
    "list_cloud_accounts": "_exec_list_cloud_accounts",
    "list_deployments": "_exec_infra('list_deployments')",
    "list_nodes": "_exec_infra('list_nodes')",
    "list_pods": "_exec_infra('list_pods')",
    "list_services": "_exec_infra('list_services')",
    "restart_deployment": "_exec_restart_deployment",
    "restart_service": "_exec_infra('restart_service')",
    "run_infra_script": "_exec_run_infra_script",
    "sync_argocd_app": "_exec_infra('sync_argocd_app')",
    "vercel_get_build_logs": "_exec_vercel_get_build_logs",
    "vercel_get_deployment": "_exec_vercel_get_deployment",
    "vercel_get_project": "_exec_vercel_get_project",
    "vercel_list_deployments": "_exec_vercel_list_deployments",
}


def _identity(executor) -> str:
    if isinstance(executor, functools.partial):
        inner = ", ".join(repr(a) for a in executor.args)
        return f"{executor.func.__qualname__}({inner})"
    return executor.__qualname__


def test_no_tool_lost_in_the_services_tools_split():
    """A dropped/renamed executor fails here, not in production."""
    assert sorted(TOOL_EXECUTORS) == EXPECTED_TOOL_NAMES


def test_chat_tools_schema_names_match_the_registry():
    """Every advertised schema has an executor and vice versa."""
    assert sorted(t["function"]["name"] for t in chat.CHAT_TOOLS) == EXPECTED_TOOL_NAMES


def test_moved_executor_identities_are_unchanged():
    """Guards a partial silently rebinding to the wrong _INFRA_SPECS key."""
    actual = {n: _identity(TOOL_EXECUTORS[n]) for n in EXPECTED_EXECUTOR_IDENTITY}
    assert actual == EXPECTED_EXECUTOR_IDENTITY


def test_moved_executors_live_in_their_domain_modules():
    """The split actually happened, and chat.py re-exports the SAME objects."""
    assert TOOL_EXECUTORS["run_infra_script"] is tools_infra._exec_run_infra_script
    assert TOOL_EXECUTORS["vercel_get_project"] is tools_vercel._exec_vercel_get_project
    assert chat._exec_run_infra_script is tools_infra._exec_run_infra_script
    assert chat._normalize_vercel_project is tools_vercel._normalize_vercel_project


def test_infra_k8s_context_set_is_shared_not_copied():
    """`chat._INFRA_CONTEXTS_K8S` must BE the set `_exec_infra` checks against —
    test_chat_infra_tools.py steers the infra tools by mutating it in place."""
    assert chat._INFRA_CONTEXTS_K8S is tools_infra._INFRA_CONTEXTS_K8S
    added = {"d7-probe-context"} - tools_infra._INFRA_CONTEXTS_K8S
    chat._INFRA_CONTEXTS_K8S.update(added)
    try:
        assert "d7-probe-context" in tools_infra._INFRA_SPECS["list_pods"][1]
    finally:
        chat._INFRA_CONTEXTS_K8S.difference_update(added)


async def test_moved_infra_executor_reachable_through_real_dispatch():
    """`_execute_tool` → registry → moved module → remote script connector."""
    connector = MagicMock()
    connector.run_script = AsyncMock(
        return_value={"status": "succeeded", "stdout": '{"nodes": ["baa"]}', "exit_code": 0}
    )
    ctx = ToolContext(remote_script_connector=connector)

    raw = await _execute_tool(None, "list_nodes", {"context": "swarm"}, ctx)

    assert json.loads(raw) == {"nodes": ["baa"]}
    script, script_args = connector.run_script.call_args[0][:2]
    assert script == "infra/infra_list_nodes"
    assert script_args == ["swarm"]


async def test_moved_vercel_executor_reachable_through_real_dispatch():
    """Same, for the other extracted domain."""
    connector = MagicMock()
    connector.get_project = AsyncMock(return_value={"name": "example-site"})
    ctx = ToolContext(vercel_connector=connector)

    raw = await _execute_tool(None, "vercel_get_project", {"project": "vercel-example-site"}, ctx)

    assert json.loads(raw) == {"name": "example-site"}
    # the `vercel-` slug prefix is stripped by the moved helper
    connector.get_project.assert_awaited_once_with("example-site")
