"""Tests for per-agent tool set filtering."""

from aegis.services.chat import (
    _FALLBACK_TOOL_SET,
    AGENT_TOOL_SETS,
    TOOL_EXECUTORS,
    _get_agent_tools,
)


def test_seed_agents_have_tool_sets_defined():
    """The 4 seed agents ship with tool sets — but the dict is NOT locked to
    only them (a DB-created agent supplies its own via metadata.tool_set)."""
    assert {"sebas", "raphael", "pandoras-actor", "maou"} <= set(AGENT_TOOL_SETS.keys())


def test_agent_tool_sets_are_subsets_of_executors():
    """Every tool in an agent's set must exist in TOOL_EXECUTORS."""
    for agent_id, tools in AGENT_TOOL_SETS.items():
        missing = tools - set(TOOL_EXECUTORS.keys())
        assert not missing, f"{agent_id} has unknown tools: {missing}"


def test_sebas_has_operational_tools():
    tools = AGENT_TOOL_SETS["sebas"]
    assert "trigger_workflow" in tools
    assert "query_activities" in tools
    assert "get_quote" not in tools


def test_raphael_has_research_tools():
    tools = AGENT_TOOL_SETS["raphael"]
    assert "research_topic" in tools
    assert "search_knowledge" in tools


def test_pandoras_actor_has_infra_tools():
    tools = AGENT_TOOL_SETS["pandoras-actor"]
    assert "trigger_workflow" in tools
    assert "run_infra_script" in tools
    # Swarm (swarm homelab):
    assert "list_nodes" in tools
    assert "list_services" in tools
    assert "inspect_service" in tools
    assert "get_service_logs" in tools
    assert "restart_service" in tools
    # k8s + ArgoCD (acme):
    assert "list_pods" in tools
    assert "list_deployments" in tools
    assert "get_pod_logs" in tools
    assert "list_argocd_apps" in tools
    assert "sync_argocd_app" in tools
    # Out-of-domain tools must NOT be in pandora's set:
    assert "get_quote" not in tools
    assert "research_topic" not in tools


def test_maou_has_finance_tools():
    tools = AGENT_TOOL_SETS["maou"]
    assert "get_quote" in tools
    assert "get_market_overview" in tools
    assert "get_finance_news" in tools
    assert "trigger_workflow" not in tools


def test_get_agent_tools_filters_chat_tools():
    """_get_agent_tools returns only the CHAT_TOOLS matching the agent's set."""
    tools = _get_agent_tools("maou")
    tool_names = {t["function"]["name"] for t in tools}
    assert tool_names == AGENT_TOOL_SETS["maou"]


def test_get_agent_tools_unknown_agent_gets_minimal_safe_set():
    """An unconfigured/unknown agent gets the tiny safe fallback set — NOT
    Sebas's full GTD surface (issue #36 #1)."""
    tools = {t["function"]["name"] for t in _get_agent_tools("unknown-agent")}
    assert tools == set(_FALLBACK_TOOL_SET)
    # It must be a strict, small subset of Sebas's tools, not equal to them.
    sebas_tools = {t["function"]["name"] for t in _get_agent_tools("sebas")}
    assert tools < sebas_tools


def test_get_agent_tools_db_tool_set_wins_over_fallback():
    """A DB agent's metadata.tool_set is used verbatim, bypassing the fallback."""
    tools = {t["function"]["name"] for t in _get_agent_tools("custom", {"tool_set": ["get_quote"]})}
    assert tools == {"get_quote"}


def test_fallback_tool_set_all_have_executors():
    """Every tool in the safe fallback set must have an executor."""
    assert set(_FALLBACK_TOOL_SET) <= set(TOOL_EXECUTORS.keys())


def test_each_agent_has_remember_this():
    """All agents should have the remember_this tool."""
    for agent_id, tools in AGENT_TOOL_SETS.items():
        assert "remember_this" in tools, f"{agent_id} missing remember_this"


def test_tool_count_reduction():
    """Each agent should see fewer tools than the full set."""
    total = len(TOOL_EXECUTORS)
    for agent_id, tools in AGENT_TOOL_SETS.items():
        assert len(tools) < total, f"{agent_id} sees all {total} tools"


def test_pandora_has_vercel_tools():
    """Pandora is the only agent with the 4 vercel_* tools (read-only)."""
    pandora = AGENT_TOOL_SETS["pandoras-actor"]
    assert "vercel_get_project" in pandora
    assert "vercel_list_deployments" in pandora
    assert "vercel_get_deployment" in pandora
    assert "vercel_get_build_logs" in pandora


def test_other_agents_do_not_have_vercel_tools():
    """Vercel tools are pandora-scoped; sebas/raphael/maou must not see them."""
    for agent_id in ("sebas", "raphael", "maou"):
        tools = AGENT_TOOL_SETS[agent_id]
        for vt in (
            "vercel_get_project",
            "vercel_list_deployments",
            "vercel_get_deployment",
            "vercel_get_build_logs",
        ):
            assert vt not in tools, f"{agent_id} should not see {vt}"
