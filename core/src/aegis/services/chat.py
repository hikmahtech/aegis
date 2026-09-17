"""Chat service — send messages to agents with tool calling support."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from typing import Any
from uuid import uuid4

import asyncpg
import structlog
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as JSONSchemaValidationError

from aegis.agent_tags import GENERALIST_TAG
from aegis.errors import error_text
from aegis.llm import parse_llm_json
from aegis.llm.tier import resolve_model_for_agent, tier_to_model, tier_to_model_or
from aegis.observability import record_llm_call, record_tool_call
from aegis.services.knowledge_ranking import DEFAULT_RANKING, Ranking, get_ranking
from aegis.services.library import LIBRARY_READ_TIMEOUT_S
from aegis.services.research import FETCH_TOOL_TIMEOUT_S, RESEARCH_TOOL_TIMEOUT_S
from aegis.services.source_types import DEFAULT_DECAY_DAYS
from aegis.services.tools.agents import (  # noqa: F401 — re-export: imported from here by tests
    _AEGIS_SELF_DIAGNOSE_FETCH_TIMEOUT,
    _AEGIS_SELF_DIAGNOSE_MAX_WAIT,
    _AEGIS_SELF_DIAGNOSE_OUTPUT_CAP,
    _AEGIS_SELF_DIAGNOSE_POLL,
    _build_aegis_self_diagnose_prompt,
    _exec_aegis_self_diagnose,
    _exec_dispatch_agent_run,
    _exec_investigate_resource,
    _exec_list_coding_sessions,
    _exec_stop_agent_run,
    _run_timeout_minutes,
    _slugify_issue,
)
from aegis.services.tools.base import (
    _MAX_LISTED_DROPPED_KEYS,  # noqa: F401 — re-export: kept importable from here
    _SHRINK_PASSES,  # noqa: F401 — re-export: imported from here by tests
    _TRUNCATION_MARKER,  # noqa: F401 — re-export: kept importable from here
    ToolContext,
    _json_default,  # noqa: F401 — re-export: kept importable from here
    _payload_rank,  # noqa: F401 — re-export: kept importable from here
    _shrink_strings,  # noqa: F401 — re-export: kept importable from here
    _smart_subset,  # noqa: F401 — re-export: imported from here by tests
    _truncate_result,
    _truncate_text,  # noqa: F401 — re-export: routes/mcp_server.py imports it here
    recorded_result,
)
from aegis.services.tools.content import (  # noqa: F401 — re-export: imported from here by tests
    _deliver_documents,
    _exec_pdf_to_text,
    _exec_youtube_transcript,
)
from aegis.services.tools.feeds import (
    _exec_follow_feed,
    _exec_list_feeds,
    _exec_unsubscribe_feed,
)
from aegis.services.tools.gtd import (
    _assignee_labels,  # noqa: F401 — re-export: imported from here by tests
    _capture_to_inbox_impl,  # noqa: F401 — re-export: routes/chat.py + routes/capture.py
    _exec_capture_to_inbox,
    _exec_comment_on_task,
    _exec_complete_task,
    _exec_defer_task,
    _exec_find_reference,
    _exec_handoff_task,
    _exec_list_next_actions,
    _exec_list_projects,
    _exec_mark_waiting,
    _exec_whats_next,
)
from aegis.services.tools.hub import (
    _exec_merge_problems,
    _exec_report_progress,
    _exec_set_service_state,
    _exec_task_context,
)
from aegis.services.tools.infra import (
    _INFRA_CONTEXTS_K8S,  # noqa: F401 — re-export: tests mutate this set in place
    _exec_cloud_identity,
    _exec_get_pod_logs,
    _exec_get_service_logs,
    _exec_inspect_service,
    _exec_list_argocd_apps,
    _exec_list_cloud_accounts,
    _exec_list_deployments,
    _exec_list_nodes,
    _exec_list_pods,
    _exec_list_services,
    _exec_restart_deployment,
    _exec_restart_service,
    _exec_run_infra_script,
    _exec_sync_argocd_app,
)
from aegis.services.tools.knowledge import (
    _exec_ask_knowledge,
    _exec_remember_this,
    _exec_search_knowledge,
    _knowledge_unavailable,  # noqa: F401 — re-export: kept importable from here
)
from aegis.services.tools.ledger import (  # noqa: F401 — re-export: imported from here by tests
    LEDGER_TOOL_TIMEOUT_S,
    _exec_ledger_add_rule,
    _exec_ledger_post,
    _exec_ledger_query,
    _exec_ledger_reclassify,
)
from aegis.services.tools.library import (
    _exec_library_book,
    _exec_library_read,
    _exec_library_search,
    _exec_library_suggest,
)
from aegis.services.tools.life import (
    _exec_last_contact_with_person,
    _exec_query_observations,
)
from aegis.services.tools.market import (
    _exec_get_finance_news,
    _exec_get_market_overview,
    _exec_get_quote,
)
from aegis.services.tools.notes import (
    NOTE_READ_TIMEOUT_S,
    NOTES_TOOL_TIMEOUT_S,
    _exec_note_link,
    _exec_note_read,
    _exec_note_search,
    _exec_note_write,
)
from aegis.services.tools.registry import TOOL_REGISTRY
from aegis.services.tools.research import (  # noqa: F401 — re-export: imported from here by tests
    _exec_paper_read,
    _exec_paper_search,
    _exec_read_url,
    _exec_research_topic,
    _exec_web_search,
)
from aegis.services.tools.social import (
    _exec_list_social_channels,
    _exec_social_timeline,
)
from aegis.services.tools.system import (  # noqa: F401 — re-export: imported from here by tests
    _TRIAGE_LIST_SETTINGS,
    _TRIAGE_SETTING_KEYS,
    _exec_configure_triage,
    _exec_create_schedule,
    _exec_list_interactions,
    _exec_query_activities,
    _exec_system_status,
    _exec_trigger_workflow,
    _exec_update_runbook,
)
from aegis.services.tools.topics import _exec_track_topic, _exec_untrack_topic
from aegis.services.tools.vercel import (
    _exec_vercel_get_build_logs,
    _exec_vercel_get_deployment,
    _exec_vercel_get_project,
    _exec_vercel_list_deployments,
    _normalize_vercel_project,  # noqa: F401 — re-export: imported from here by tests
)

logger = structlog.get_logger()


def _registry_schema(name: str) -> dict:
    """The advertised schema for one `@aegis_tool`-registered executor.

    Lets `CHAT_TOOLS` keep its hand-laid order — the list IS the LLM's prompt —
    while every schema is generated from that tool's typed signature plus
    docstring instead of being duplicated here. `KeyError` on an unknown name
    is deliberate: a rename must fail at import, not silently drop the tool
    from the surface the model can see.
    """
    tool = TOOL_REGISTRY[name]
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters,
        },
    }


# Intent routing for the chat front door. Deterministic keyword map first
# (zero LLM cost); the LLM (fast tier) only resolves the keyword-less tail.
# ponytail: substring match — good enough; @mention override + persona icon
# make any mis-route visible and correctable.
#
# Every input here is read from the active agents' rows (#556): the keywords
# from metadata.intent_keywords, the LLM router's one-liners from
# metadata.intent_description, and the generalist — who tie-breaks last and
# takes whatever nobody else claims — from the `gtd` behavior tag. The example
# agents' values live in config/seed/agents.yaml, not here, so a fork that
# renames its agents keeps every routing behaviour. The constant itself lives
# in `agent_tags` beside the rest of the tag vocabulary, so a tool module can
# read it without importing chat.


def _route_order(agent_ids, generalists=frozenset()) -> list[str]:
    """Tie-break order: specific domains before the generalist. Agents holding
    the `gtd` tag go last; within each group, by id (deterministic)."""
    return sorted(agent_ids, key=lambda a: (a in generalists, a))


def _keyword_route(
    message: str,
    keyword_map: dict[str, list[str]] | None = None,
    generalists: frozenset[str] | set[str] = frozenset(),
) -> str | None:
    """Pick an agent by keyword hit-count; None when no keyword matches.

    `keyword_map` is per-agent intent keywords (agents.metadata). A tie goes to
    a specific agent over a `gtd` generalist, then to the lower id.
    """
    low = (message or "").lower()
    scores = {a: sum(1 for kw in kws if kw in low) for a, kws in (keyword_map or {}).items()}
    if not scores:
        return None
    best = max(scores.values())
    if best == 0:
        return None
    for agent in _route_order(scores, generalists):
        if scores[agent] == best:
            return agent
    return None


async def _routing_agents(pool) -> list[dict]:
    """Active agents as `{id, capabilities, metadata}`, by id. Empty without a
    pool or on a failed read — routing must never break the front door."""
    if pool is None:
        return []
    try:
        rows = await pool.fetch(
            "SELECT id, capabilities, metadata FROM agents WHERE active = TRUE ORDER BY id"
        )
    except Exception as exc:  # noqa: BLE001 — routing must never break the front door
        logger.warning("agent_routing_read_failed", error=error_text(exc))
        return []
    return [
        {"id": r["id"], "capabilities": r["capabilities"] or [], "metadata": r["metadata"] or {}}
        for r in rows
    ]


def _keywords_of(agents: list[dict]) -> dict[str, list[str]]:
    return {a["id"]: list(kws) for a in agents if (kws := a["metadata"].get("intent_keywords"))}


def _descriptions_of(agents: list[dict]) -> dict[str, str]:
    # An agent with no description (the virtual `system` agent) is never offered.
    return {
        a["id"]: str(desc) for a in agents if (desc := a["metadata"].get("intent_description"))
    }


def _generalists_of(agents: list[dict]) -> set[str]:
    return {a["id"] for a in agents if GENERALIST_TAG in (a["capabilities"] or [])}


async def _agent_keyword_map(pool) -> dict[str, list[str]]:
    """Per-agent intent keywords from agents.metadata. Never raises."""
    return _keywords_of(await _routing_agents(pool))


async def _agent_intent_descriptions(pool) -> dict[str, str]:
    """Per-agent one-line intent descriptions for the LLM router prompt, from
    agents.metadata.intent_description. Agents without one are omitted (e.g.
    the virtual `system` agent), so they never become a routing target."""
    return _descriptions_of(await _routing_agents(pool))


def _build_intent_prompt(
    message: str,
    descriptions: dict[str, str] | None = None,
    generalists: frozenset[str] | set[str] = frozenset(),
) -> str:
    """Prompt the fast LLM to pick the best agent. The agent list is built from
    `descriptions` (per-agent intent_description), specific agents first and
    the `gtd` generalist last, so custom/renamed agents are offered too."""
    descriptions = descriptions or {}
    lines = "\n".join(
        f"- {aid}: {descriptions[aid]}" for aid in _route_order(descriptions, generalists)
    )
    return (
        "Route this message to the single best AEGIS agent. Reply with STRICT "
        'JSON {"agent_id": "<id>", "reason": "<short>"}. Agents:\n'
        f"{lines}\n\n"
        f"Message: {message[:500]}"
    )


def _default_agent(agents: list[dict], generalists: set[str]) -> str:
    """Who gets a message nobody claims: the `gtd` generalist (first by id).
    With no holder there is no default — "" makes the caller fall back to its
    own (comms: the channel's agent) rather than to an id a fork may not have."""
    for a in agents:
        if a["id"] in generalists:
            return a["id"]
    if agents:
        logger.warning("intent_route_no_generalist", tag=GENERALIST_TAG)
    return ""


async def classify_intent(message: str, llm, settings, pool=None) -> dict:
    """Front-door intent routing: keyword map → fast-LLM fallback → the generalist.

    Keyword map, descriptions and the default are data-driven from the active
    agents' rows (pool); never raises — on any ambiguity/failure returns the
    holder of the `gtd` tag.
    """
    agents = await _routing_agents(pool)
    keyword_map = _keywords_of(agents)
    generalists = _generalists_of(agents)
    kw = _keyword_route(message, keyword_map, generalists)
    if kw:
        return {"agent_id": kw, "reason": "keyword", "method": "keyword"}
    default = _default_agent(agents, generalists)
    if llm is None:
        return {"agent_id": default, "reason": "no_llm", "method": "default"}
    # The tier map, not the raw env field it falls back to (#414).
    stale = getattr(settings, "model_fast", "gemma4:e2b") if settings else "gemma4:e2b"
    model = tier_to_model_or("fast", stale)
    descriptions = _descriptions_of(agents)
    # Accept any routable active agent the LLM names — keyword map OR intent
    # description — so a custom agent reachable only via intent_description isn't
    # silently rejected.
    routable = set(keyword_map) | set(descriptions)
    try:
        result = await llm.think(
            _build_intent_prompt(message, descriptions, generalists), model=model,
            max_tokens=300, purpose="intent_route",
        )
        raw = result.get("response", "") if isinstance(result, dict) else (result or "")
        parsed = parse_llm_json(raw) or {}
        agent = parsed.get("agent_id") or parsed.get("agent")
        if agent in routable:
            return {"agent_id": agent, "reason": str(parsed.get("reason", ""))[:200], "method": "llm"}
    except Exception as exc:  # noqa: BLE001 — routing must never break the front door
        logger.warning("intent_route_llm_failed", error=error_text(exc))
    return {"agent_id": default, "reason": "default", "method": "default"}


# Two very different things share the "claude-" name in the LiteLLM config
# (infra: ansible/roles/ollama/templates/litellm-config.yaml.j2), and it
# matters which one a tier resolves to:
#   - Bridge aliases (bare names, no version): claude-haiku, claude-sonnet,
#     claude-opus. These proxy through max-proxy (the Claude-Code-subscription
#     bridge, api_base http://<max_proxy>/v1) and silently strip the `tools`
#     array from the upstream request — the model never sees the tool
#     definitions and responds in plain text (often hallucinating that no
#     tools are available). THESE are what `_TOOL_INCAPABLE_MODELS` matches.
#   - Real Anthropic-API aliases (versioned names such as claude-sonnet-5,
#     claude-haiku-4.5) hit `anthropic/...` with a real key and are fully
#     tool-capable. They were REMOVED from the proxy on 2026-09-06 (key
#     retired after a pay-as-you-go bill). `smart` now resolves to
#     bedrock-kimi-k2.5 (config/models.yaml) — tool-capable, so it must
#     stay OUT of this set; the bridge alias claude-opus held the tier for a
#     single day and swapped every tool-bearing turn down to balanced.
# Do NOT turn this into a `claude-` prefix check — that would also catch a
# versioned, tool-capable name and silently downgrade every tool-bearing
# smart-tier chat turn to the balanced tier for no reason. Match must stay
# an exact-name set of the three bridge aliases.
# When an agent has tools to call and the resolved model is one of these,
# swap in whatever the live `balanced` tier currently resolves to
# (`aegis/llm/tier.py::tier_to_model`) rather than a hardcoded model name,
# so the fallback always tracks config/models.yaml / the DB-configured
# backend instead of silently going stale (the previous hardcoded fallback,
# `gpt-oss:20b`, has its host down indefinitely per config/models.yaml).
_TOOL_INCAPABLE_MODELS: frozenset[str] = frozenset({"claude-haiku", "claude-sonnet", "claude-opus"})


# Tool definitions for agent chat (OpenAI format)
CHAT_TOOLS = [
    _registry_schema("search_knowledge"),
    _registry_schema("ask_knowledge"),
    _registry_schema("remember_this"),
    _registry_schema("query_activities"),
    _registry_schema("trigger_workflow"),
    _registry_schema("create_schedule"),
    _registry_schema("get_quote"),
    _registry_schema("get_market_overview"),
    _registry_schema("get_finance_news"),
    _registry_schema("research_topic"),
    _registry_schema("track_topic"),
    # Stop tracking one (#513), generated from services/tools/topics.py.
    _registry_schema("untrack_topic"),
    # The research lane's four reads (#509), generated from services/tools/research.py.
    _registry_schema("web_search"),
    _registry_schema("read_url"),
    _registry_schema("paper_search"),
    _registry_schema("paper_read"),
    # The feed list (#511), generated from services/tools/feeds.py.
    _registry_schema("list_feeds"),
    _registry_schema("subscribe_feed"),
    _registry_schema("unsubscribe_feed"),
    # The Calibre library (#510), generated from services/tools/library.py.
    _registry_schema("library_search"),
    _registry_schema("library_book"),
    _registry_schema("library_read"),
    _registry_schema("library_suggest"),
    # The Obsidian vault (#514), generated from services/tools/notes.py.
    _registry_schema("note_search"),
    _registry_schema("note_read"),
    _registry_schema("note_write"),
    _registry_schema("note_link"),
    _registry_schema("configure_triage"),
    _registry_schema("update_runbook"),
    _registry_schema("list_nodes"),
    _registry_schema("list_services"),
    _registry_schema("inspect_service"),
    _registry_schema("get_service_logs"),
    _registry_schema("restart_service"),
    _registry_schema("list_pods"),
    _registry_schema("list_deployments"),
    _registry_schema("get_pod_logs"),
    _registry_schema("restart_deployment"),
    _registry_schema("list_argocd_apps"),
    _registry_schema("sync_argocd_app"),
    _registry_schema("list_cloud_accounts"),
    _registry_schema("cloud_identity"),
    _registry_schema("run_infra_script"),
    # Problem hub — deploy / maintenance windows, the session registry and
    # merges; `services/tools/hub.py`.
    _registry_schema("set_service_state"),
    _registry_schema("task_context"),
    _registry_schema("report_progress"),
    _registry_schema("merge_problems"),
    _registry_schema("aegis_self_diagnose"),
    _registry_schema("list_interactions"),
    # GTD / Todoist — schemas generated from the typed `@aegis_tool` executors
    # in `services/tools/gtd.py`; the order here is still the order the LLM sees.
    _registry_schema("capture_to_inbox"),
    _registry_schema("list_next_actions"),
    _registry_schema("whats_next"),
    _registry_schema("list_projects"),
    _registry_schema("complete_task"),
    _registry_schema("defer_task"),
    _registry_schema("mark_waiting"),
    _registry_schema("handoff_task"),
    _registry_schema("comment_on_task"),
    _registry_schema("find_reference"),
    # The books (Maou) — every write goes through `books.py`'s locked,
    # `check --strict`-guarded writer; `services/tools/ledger.py`.
    _registry_schema("ledger_query"),
    _registry_schema("ledger_post"),
    _registry_schema("ledger_reclassify"),
    _registry_schema("ledger_add_rule"),
    _registry_schema("last_contact_with_person"),
    _registry_schema("query_observations"),
    # --- Vercel read-only (Pandora) ---
    # Project arg accepts either the bare Vercel project name (e.g. "example-site")
    # or the resources-table slug ("vercel-example-site"); the executor strips the
    # slug prefix before calling the connector.
    _registry_schema("vercel_get_project"),
    _registry_schema("vercel_list_deployments"),
    _registry_schema("vercel_get_deployment"),
    _registry_schema("vercel_get_build_logs"),
    _registry_schema("investigate_resource"),
    _registry_schema("dispatch_agent_run"),
    _registry_schema("stop_agent_run"),
    _registry_schema("youtube_transcript"),
    _registry_schema("pdf_to_text"),
    _registry_schema("list_coding_sessions"),
    _registry_schema("system_status"),
    _registry_schema("social_timeline"),
    _registry_schema("list_social_channels"),
]


# --- Individual tool executor functions ---

# Per-tool executor-timeout overrides (seconds). The default chat tool timeout
# (settings.tool_timeout_seconds, 30s) guillotines legitimately long-running
# tools: aegis_self_diagnose waits on a remote coding-CLI run for up to
# _AEGIS_SELF_DIAGNOSE_MAX_WAIT, so it could NEVER finish inside 30s — and each
# LLM retry then orphaned another kimi run on the coding host.
#
# The three ledger writers no longer do the write on this budget at all: they
# hand it to `BooksWriteFlow` and wait `LEDGER_WRITE_WAIT_S` (issue #388). Their
# override is only a floor under that wait, so a deployment that lowers
# `tool_timeout_seconds` cannot cut it short and turn a normal write into a
# reported timeout.
_TOOL_TIMEOUT_OVERRIDES: dict[str, int] = {
    "aegis_self_diagnose": _AEGIS_SELF_DIAGNOSE_MAX_WAIT + 60,
    "ledger_post": LEDGER_TOOL_TIMEOUT_S,
    "ledger_reclassify": LEDGER_TOOL_TIMEOUT_S,
    "ledger_add_rule": LEDGER_TOOL_TIMEOUT_S,
    # `research_topic` hands the research to `ResearchFlow` and waits
    # `RESEARCH_WAIT_S` for it (#509); this is the floor under that wait. The
    # three fetch tools read one page or PDF, or query two paper engines, which
    # the 30s default cannot always fit.
    "research_topic": RESEARCH_TOOL_TIMEOUT_S,
    "read_url": FETCH_TOOL_TIMEOUT_S,
    "paper_search": FETCH_TOOL_TIMEOUT_S,
    "paper_read": FETCH_TOOL_TIMEOUT_S,
    # subscribe_feed fetches the URL to check it is a feed (#511).
    "subscribe_feed": FETCH_TOOL_TIMEOUT_S,
    # The library tools reach calibre-web; a read downloads one book and
    # extracts it, which a long PDF can stretch well past a minute (#510).
    "library_search": FETCH_TOOL_TIMEOUT_S,
    "library_book": FETCH_TOOL_TIMEOUT_S,
    "library_read": LIBRARY_READ_TIMEOUT_S,
    "library_suggest": FETCH_TOOL_TIMEOUT_S,
    # The vault (#514): a read may pull first; the two writers wait on
    # NotesWriteFlow, as the ledger writers wait on BooksWriteFlow.
    "note_read": NOTE_READ_TIMEOUT_S,
    "note_write": NOTES_TOOL_TIMEOUT_S,
    "note_link": NOTES_TOOL_TIMEOUT_S,
}


# --- Tool-arg validation ---


class ChatToolValidationError(Exception):
    """Raised when a tool call's args fail JSONSchema validation twice in a row."""

    def __init__(self, tool_name: str, message: str, schema_summary: str):
        self.tool_name = tool_name
        self.message = message
        self.schema_summary = schema_summary
        super().__init__(f"{tool_name}: {message}")


def _validate_tool_args(name: str, args: dict, *, schema: dict | None = None) -> None:
    """Validate `args` against the tool's JSONSchema. Raises JSONSchemaValidationError.

    Pass `schema` explicitly (cheap fast path) or let the function look it up
    from CHAT_TOOLS when invoked in production.
    """
    if schema is None:
        for tool in CHAT_TOOLS:
            fn = tool.get("function", {})
            if fn.get("name") == name:
                schema = fn.get("parameters") or {}
                break
        else:
            # No schema known → nothing to validate.
            return
    Draft202012Validator(schema).validate(args)


def _schema_hint(name: str) -> str:
    """Compact reminder of a tool's expected arguments (required fields +
    enum values), appended to a validation-failure message so the model can
    self-correct on retry instead of giving up to prose.

    gpt-oss (the tool-calling fallback model) frequently omits a required arg
    or picks an out-of-enum value; the raw jsonschema message ("'context' is a
    required property") doesn't say what `context` should be. Spelling out the
    contract gives the retry a real chance to land. Looks the schema up from
    CHAT_TOOLS the same way `_validate_tool_args` does; returns "" if unknown.
    """
    schema: dict | None = None
    for tool in CHAT_TOOLS:
        fn = tool.get("function", {})
        if fn.get("name") == name:
            schema = fn.get("parameters") or {}
            break
    if not schema:
        return ""
    required = set(schema.get("required") or [])
    props = schema.get("properties") or {}
    parts: list[str] = []
    for pname, spec in props.items():
        spec = spec if isinstance(spec, dict) else {}
        bits = [str(spec.get("type", "any"))]
        if "enum" in spec:
            bits.append("one of " + ", ".join(str(e) for e in spec["enum"]))
        flag = "required" if pname in required else "optional"
        parts.append(f"{pname} ({flag}; {'; '.join(bits)})")
    if not parts:
        return ""
    return "Expected arguments — " + "; ".join(parts)


async def _dispatch_tool_call_with_retry(
    pool: Any,
    name: str,
    tool_call_id: str,
    initial_args: dict,
    messages: list[dict],
    retry_args_provider: Any,
    executor: Any,
    ctx: Any,
) -> Any:
    """Validate args; on ValidationError, append a tool error message and retry once.

    `retry_args_provider(error_message)` returns the new args for the retry —
    in production this is backed by the LLM re-invocation; in tests it's a
    deterministic callable. On second failure, raise ChatToolValidationError.
    """
    args = initial_args
    attempt = 0
    while True:
        try:
            _validate_tool_args(name, args)
            return await executor(pool, args, ctx)
        except JSONSchemaValidationError as exc:
            if attempt >= 1:
                raise ChatToolValidationError(
                    tool_name=name,
                    message=exc.message,
                    schema_summary=str(exc.schema)[:200],
                ) from exc
            err_msg = f"Validation error on tool `{name}`: {exc.message}."
            hint = _schema_hint(name)
            if hint:
                err_msg += f" {hint}. Call `{name}` again with corrected arguments."
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": err_msg,
                }
            )
            result_or_coro = retry_args_provider(err_msg)
            if asyncio.iscoroutine(result_or_coro):
                args = await result_or_coro
            else:
                args = result_or_coro
            attempt += 1


async def _retry_via_llm(
    llm_client: Any,
    messages: list[dict],
    model: str,
    tools: list[dict] | None,
    original_tool_name: str,
    error_msg: str,
) -> dict:
    """Re-ask the LLM for new args after a validation failure."""
    retry_result = await llm_client.chat(messages=messages, model=model, tools=tools)
    # chat() returns tool calls in the flat shape {id, name, arguments} — not the
    # nested {function: {...}} of an outbound assistant message.
    for tc in retry_result.get("tool_calls", []) or []:
        if tc.get("name") == original_tool_name:
            return json.loads(tc["arguments"])
    # LLM didn't produce a tool call this time — return empty to force surface.
    logger.warning("chat_tool_retry_no_matching_call", tool=original_tool_name)
    return {}


# --- Dispatch dict mapping tool names to executor functions ---

TOOL_EXECUTORS: dict[str, Any] = {
    "search_knowledge": _exec_search_knowledge,
    "ask_knowledge": _exec_ask_knowledge,
    "remember_this": _exec_remember_this,
    "query_activities": _exec_query_activities,
    "trigger_workflow": _exec_trigger_workflow,
    "dispatch_agent_run": _exec_dispatch_agent_run,
    "stop_agent_run": _exec_stop_agent_run,
    "create_schedule": _exec_create_schedule,
    "get_quote": _exec_get_quote,
    "get_market_overview": _exec_get_market_overview,
    "get_finance_news": _exec_get_finance_news,
    "research_topic": _exec_research_topic,
    "track_topic": _exec_track_topic,
    "untrack_topic": _exec_untrack_topic,
    "web_search": _exec_web_search,
    "read_url": _exec_read_url,
    "paper_search": _exec_paper_search,
    "paper_read": _exec_paper_read,
    "list_feeds": _exec_list_feeds,
    "subscribe_feed": _exec_follow_feed,
    "unsubscribe_feed": _exec_unsubscribe_feed,
    "library_search": _exec_library_search,
    "library_book": _exec_library_book,
    "library_read": _exec_library_read,
    "library_suggest": _exec_library_suggest,
    "note_search": _exec_note_search,
    "note_read": _exec_note_read,
    "note_write": _exec_note_write,
    "note_link": _exec_note_link,
    "configure_triage": _exec_configure_triage,
    "update_runbook": _exec_update_runbook,
    "list_nodes": _exec_list_nodes,
    "list_services": _exec_list_services,
    "inspect_service": _exec_inspect_service,
    "get_service_logs": _exec_get_service_logs,
    "restart_service": _exec_restart_service,
    "list_pods": _exec_list_pods,
    "list_deployments": _exec_list_deployments,
    "get_pod_logs": _exec_get_pod_logs,
    "restart_deployment": _exec_restart_deployment,
    "list_argocd_apps": _exec_list_argocd_apps,
    "sync_argocd_app": _exec_sync_argocd_app,
    "list_cloud_accounts": _exec_list_cloud_accounts,
    "cloud_identity": _exec_cloud_identity,
    "run_infra_script": _exec_run_infra_script,
    "set_service_state": _exec_set_service_state,
    "task_context": _exec_task_context,
    "report_progress": _exec_report_progress,
    "merge_problems": _exec_merge_problems,
    "aegis_self_diagnose": _exec_aegis_self_diagnose,
    "investigate_resource": _exec_investigate_resource,
    "list_interactions": _exec_list_interactions,
    "capture_to_inbox": _exec_capture_to_inbox,
    "list_next_actions": _exec_list_next_actions,
    "whats_next": _exec_whats_next,
    "list_projects": _exec_list_projects,
    "complete_task": _exec_complete_task,
    "defer_task": _exec_defer_task,
    "mark_waiting": _exec_mark_waiting,
    "handoff_task": _exec_handoff_task,
    "comment_on_task": _exec_comment_on_task,
    "find_reference": _exec_find_reference,
    "ledger_query": _exec_ledger_query,
    "ledger_post": _exec_ledger_post,
    "ledger_reclassify": _exec_ledger_reclassify,
    "ledger_add_rule": _exec_ledger_add_rule,
    "last_contact_with_person": _exec_last_contact_with_person,
    "query_observations": _exec_query_observations,
    # Vercel read-only (Pandora) — see PR for design notes.
    "vercel_get_project": _exec_vercel_get_project,
    "vercel_list_deployments": _exec_vercel_list_deployments,
    "vercel_get_deployment": _exec_vercel_get_deployment,
    "vercel_get_build_logs": _exec_vercel_get_build_logs,
    "youtube_transcript": _exec_youtube_transcript,
    "pdf_to_text": _exec_pdf_to_text,
    "list_coding_sessions": _exec_list_coding_sessions,
    "system_status": _exec_system_status,
    "social_timeline": _exec_social_timeline,
    "list_social_channels": _exec_list_social_channels,
}

# --- The example agents' tool sets ---
# The four example agents' tool sets as code. Nothing reads it at runtime
# (#579): an agent's tools are its `metadata.tool_set`, and an agent without
# one gets `_FALLBACK_TOOL_SET` whatever its id. It is not what a fresh install
# gets either — config/seed/agents.yaml seeds `metadata.tool_set` — and it has
# drifted from that file (it grants a few tools the yaml does not). It stays
# for `_validate_agent_tool_sets` (a grant naming a tool with no executor
# refuses to boot) and for the tests that pin the example grants.

AGENT_TOOL_SETS: dict[str, set[str]] = {
    "sebas": {
        "query_activities",
        "trigger_workflow",
        # Heavy lane: hand multi-step work to a headless CLI run (AgentRunFlow),
        # result delivered to the channel later.
        "dispatch_agent_run",
        "search_knowledge",
        "configure_triage",
        "remember_this",
        # Problem hub, the session registry: read a task's context, register
        # a session on it, fold a duplicate problem away.
        "task_context",
        "report_progress",
        "merge_problems",
        "list_interactions",  # NEW (Phase 5 PR 1)
        # Phase 3 GTD tools
        "capture_to_inbox",
        "list_next_actions",
        "whats_next",
        "list_projects",
        "complete_task",
        "defer_task",
        "mark_waiting",
        "handoff_task",
        "comment_on_task",
        "find_reference",
        # Read-only over the books; the three write tools are Maou's alone.
        "ledger_query",
        # People registry (life.people) — "when did I last talk to X?"
        "last_contact_with_person",
        # Life metrics (life.observations) — "how's my weight trending?"
        "query_observations",
        # Document-attachment tools
        "youtube_transcript",
        "pdf_to_text",
        "system_status",
        "social_timeline",
        "list_social_channels",
    },
    "raphael": {
        "search_knowledge",
        "ask_knowledge",
        "research_topic",
        # The research lane's reads (#509): search, a page, papers.
        "web_search",
        "read_url",
        "paper_search",
        "paper_read",
        # The feed list (#511): see it, add a feed, drop one.
        "list_feeds",
        "subscribe_feed",
        "unsubscribe_feed",
        # The Calibre library (#510): search it, read from it, suggest books.
        "library_search",
        "library_book",
        "library_read",
        "library_suggest",
        # Raphael's notes (#514): the vault is his record. Search and read
        # it; write only under raphael/ (through NotesWriteFlow).
        "note_search",
        "note_read",
        "note_write",
        "note_link",
        "track_topic",
        # Tracked topics' rounds in the hub (#513): stop tracking one.
        "untrack_topic",
        "remember_this",
        # Problem hub, the session registry: read a task's context, register
        # a session on it, fold a duplicate problem away.
        "task_context",
        "report_progress",
        "merge_problems",
        # Phase 3 GTD tools (research-leaning subset)
        "capture_to_inbox",
        "list_next_actions",
        "list_projects",
        "complete_task",
        "handoff_task",
        "find_reference",
        # Document-attachment tools
        "youtube_transcript",
        "pdf_to_text",
    },
    "pandoras-actor": {
        "trigger_workflow",
        # Problem hub: declare a deploy/maintenance window so the hub
        # records what it sees there without raising it.
        "set_service_state",
        # Heavy lane, repo-agnostic: investigate/analyse anything in a headless
        # CLI run. investigate_resource stays the code-fix-with-Gate-2 path.
        "dispatch_agent_run",
        "create_schedule",
        "search_knowledge",
        "update_runbook",
        "configure_triage",
        "remember_this",
        # Problem hub, the session registry: read a task's context, register
        # a session on it, fold a duplicate problem away.
        "task_context",
        "report_progress",
        "merge_problems",
        "list_interactions",
        # Infrastructure tools — full surface across swarm swarm + acme k8s/argocd:
        "list_nodes",
        "list_services",
        "inspect_service",
        "get_service_logs",
        "restart_service",
        "list_pods",
        "list_deployments",
        "get_pod_logs",
        "restart_deployment",
        "list_argocd_apps",
        "sync_argocd_app",
        # Cloud accounts (read-only): registry listing + live sts/ADC identity
        # check for kind=cloud entries. Gated on CLI availability in the image.
        "list_cloud_accounts",
        "cloud_identity",
        "run_infra_script",
        # AEGIS self-healing — drives kimi over SSH against the AEGIS source
        # clone on node-a. Used when the user asks pandora about AEGIS's own
        # behavior / bugs / improvements (via DM @pandora or Todoist comment).
        "aegis_self_diagnose",
        # Agent-initiated investigation of any registered repo the task concerns:
        # spawns AlertInvestigationFlow (fix-capable kimi + Gate-2), posts back to
        # the current task. Comment-channel only.
        "investigate_resource",
        # Vercel read-only — project metadata, deployments (filter by time/state),
        # single deployment incl error fields, build logs (filter to stderr).
        "vercel_get_project",
        "vercel_list_deployments",
        "vercel_get_deployment",
        "vercel_get_build_logs",
        # Phase 3 GTD tools (no mark_waiting / find_reference — ops doesn't
        # use the waiting-for list and has its own runbook lookup)
        "capture_to_inbox",
        "list_next_actions",
        "list_projects",
        "complete_task",
        "defer_task",
        "handoff_task",
        "comment_on_task",
    },
    "maou": {
        "get_quote",
        "get_market_overview",
        "get_finance_news",
        "search_knowledge",
        "remember_this",
        # Problem hub, the session registry: read a task's context, register
        # a session on it, fold a duplicate problem away.
        "task_context",
        "report_progress",
        "merge_problems",
        "list_interactions",  # NEW (Phase 5 PR 1)
        # Phase 3 GTD tools (full set minus find_reference — maou queries
        # market data instead of the reference store)
        "capture_to_inbox",
        "list_next_actions",
        "list_projects",
        "complete_task",
        "defer_task",
        "mark_waiting",
        "handoff_task",
        # The books — the only agent that may write them.
        "ledger_query",
        "ledger_post",
        "ledger_reclassify",
        "ledger_add_rule",
    },
}


# Minimal safe surface for an agent with no configured tool set. Deliberately
# NOT Sebas's full GTD surface — a custom/unknown agent should get a small
# read-mostly starter set (search + capture), not silently inherit the
# coordinator's tools. Configure the real set via agents.metadata.tool_set
# (admin Behavior tab). Every name here must exist in TOOL_EXECUTORS.
_FALLBACK_TOOL_SET: frozenset[str] = frozenset(
    {"search_knowledge", "capture_to_inbox", "list_next_actions"}
)


def _get_agent_tools(agent_id: str, metadata: dict | None = None) -> list[dict]:
    """Return CHAT_TOOLS filtered to the agent's allowed tool set.

    The set is the agent's `metadata.tool_set` (admin Agents → Behavior). An
    agent without one gets the tiny safe `_FALLBACK_TOOL_SET` whatever its id:
    `AGENT_TOOL_SETS` is not read here, because an example id is not a grant
    (#579). `agent_id` is kept for the callers; it decides nothing.
    """
    allowed = set((metadata or {}).get("tool_set") or _FALLBACK_TOOL_SET)
    return [t for t in CHAT_TOOLS if t["function"]["name"] in allowed]


def _validate_agent_tool_sets() -> None:
    """Boot-time check: every tool name in AGENT_TOOL_SETS has an executor.

    Raises RuntimeError on orphan references so the process refuses to start.
    Logs a warning for executors that are not referenced by any agent — those
    are soft-dead (kept for future use or in-flight deprecation).
    """
    declared: set[str] = set()
    for agent_id, tools in {**AGENT_TOOL_SETS, "_fallback": _FALLBACK_TOOL_SET}.items():
        for tool_name in tools:
            if tool_name not in TOOL_EXECUTORS:
                raise RuntimeError(
                    f"chat tool orphan: agent '{agent_id}' references tool "
                    f"'{tool_name}' but no TOOL_EXECUTORS entry exists"
                )
            declared.add(tool_name)

    unused = set(TOOL_EXECUTORS) - declared
    for name in sorted(unused):
        logger.warning("chat_tool_unused", tool=name)


def _build_agent_system_prompt(
    agent_id: str,
    fallback: str,
    tool_descriptions: str | None = None,
    persona: dict | None = None,
) -> str:
    """Build a structured system prompt from the agent's persona.

    `persona` is the kind→content dict from
    `aegis.services.personalities.get_personality` (DB-first; starter .md files
    only when the agent has no rows yet). Returns `fallback` (the DB
    system_prompt) when every kind is empty.
    """
    persona = persona or {}

    sections: list[str] = []
    for kind, heading in (
        ("soul", "Identity"),
        ("agents", "Operational Boundaries"),
        ("user", "User Context"),
        ("memory", "Memory"),
    ):
        content = (persona.get(kind) or "").strip()
        if content:
            sections.append(f"## {heading}\n\n{content}")

    if not sections:
        return fallback

    if tool_descriptions:
        sections.append(f"## Available Tools\n\n{tool_descriptions}")

    return "\n\n".join(sections)


async def _execute_tool(
    pool: asyncpg.Pool,
    name: str,
    args: dict,
    ctx: ToolContext | None = None,
    knowledge_connector: Any = None,
    chat_context: dict | None = None,
) -> str:
    """Execute a tool call and return the result as a string."""
    if ctx is None:
        ctx = ToolContext(knowledge_connector=knowledge_connector, chat_context=chat_context)
    else:
        if knowledge_connector and not ctx.knowledge_connector:
            ctx.knowledge_connector = knowledge_connector
        if chat_context and not ctx.chat_context:
            ctx.chat_context = chat_context

    executor = TOOL_EXECUTORS.get(name)
    if not executor:
        return json.dumps({"error": f"Unknown tool: {name}"})
    return await executor(pool, args, ctx)




# The knowledge boost is data-driven: `_gather_knowledge_context` receives
# `agent_meta.knowledge_domains` from the DB (see the caller). An agent that
# sets metadata.knowledge_domains (admin Agents → Behavior; the example
# agents' lists are in config/seed/agents.yaml) is boosted; one that doesn't
# simply gets no boost rather than one keyed on its id (#556).

def _extract_query_entities(message: str, agent_ids=()) -> list[str]:
    """Extract likely entity terms from a message. Lightweight, no NLP.

    `agent_ids` are the ids worth spotting by name (the active agents)."""
    import re

    entities: list[str] = []

    # Quoted strings
    for match in re.findall(r'"([^"]+)"', message):
        if len(match) > 2:
            entities.append(match)

    # Known agent IDs
    lower = message.lower()
    for aid in agent_ids:
        if aid and aid.lower() in lower:
            entities.append(aid)

    # Capitalized multi-word phrases (2+ words starting with uppercase)
    for match in re.findall(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b", message):
        if match not in entities:
            entities.append(match)

    return entities[:2]


# --- Knowledge decay ---
# Registry (types + per-type decay window) now lives in source_types.py.
# DEFAULT_DECAY_WINDOW kept as a back-compat name — tests import it directly.
DEFAULT_DECAY_WINDOW = DEFAULT_DECAY_DAYS


def _apply_knowledge_decay(items: list[dict], ranking: Ranking | None = None) -> list[dict]:
    """Apply time-based decay to knowledge items based on source type.

    When days_since_referenced is unknown, assume item is fresh (0 days).
    Decay is only meaningful when age data is available from the knowledge store.
    `ranking` carries the per-type decay window and rank boost — the
    `knowledge_ranking` row over the registry; None is the registry alone.
    """
    ranking = ranking or DEFAULT_RANKING
    for item in items:
        source_type = item.get("source_type", "unknown")
        decay_window = ranking.decay_days(source_type)
        # Default to 0 (fresh) when age is unknown — don't penalize items without age data
        days = item.get("days_since_referenced", 0)
        decay_factor = max(0.1, 1.0 - (days / decay_window))
        # Start from the domain-boosted `_score` when the caller set one, so
        # the boost reaches the threshold and the order (#579); else from
        # similarity, which can be None (BM25-only chunks) and is coerced. The
        # rank boost is 1.0 for every type but the user's own notes, which rank
        # above raw documents (#514), unless the row says otherwise.
        base = item["_score"] if item.get("_score") is not None else (item.get("similarity") or 0)
        item["effective_score"] = base * decay_factor * ranking.rank_boost(source_type)
    return items


# --- Knowledge injection feedback helpers ---

_STOP_WORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "have",
        "has",
        "had",
        "do",
        "does",
        "did",
        "will",
        "would",
        "could",
        "should",
        "may",
        "might",
        "can",
        "shall",
        "of",
        "in",
        "to",
        "for",
        "with",
        "on",
        "at",
        "from",
        "by",
        "and",
        "or",
        "but",
        "not",
        "no",
        "if",
        "then",
        "that",
        "this",
        "it",
        "its",
        "as",
        "so",
        "up",
        "out",
        "about",
    }
)


def _content_hash(text: str) -> str:
    """Short content hash for dedup."""
    return hashlib.md5(text.encode()).hexdigest()[:12]


def _extract_keywords(text: str, max_words: int = 5) -> list[str]:
    """Extract significant keywords for reference detection."""
    words = [w.lower().strip(".,;:!?\"'()[]{}") for w in text.split()]
    significant = [w for w in words if len(w) > 2 and w not in _STOP_WORDS]
    return significant[:max_words]


def _check_knowledge_references(injected: list[dict], response: str) -> list[dict]:
    """Check which injected items were referenced in the LLM response.

    Uses keyword overlap (no LLM call).
    """
    response_lower = response.lower()
    results = []
    for item in injected:
        keywords = item.get("keywords", [])
        matches = sum(1 for kw in keywords if kw.lower() in response_lower)
        referenced = matches >= 2 if len(keywords) >= 2 else matches >= 1
        results.append({"content_hash": item["content_hash"], "referenced": referenced})
    return results


# --- Document reference detection ---

_DOC_MARKER_RE = re.compile(r"\[Document: (.+?)\]")
_CONTENT_ID_RE = re.compile(r"content_id: ([a-f0-9-]+)")
_GENERIC_DOC_PHRASES = {
    "the document",
    "the contract",
    "the agreement",
    "the pdf",
    "that document",
    "that contract",
    "that agreement",
    "that file",
    "this document",
    "this contract",
    "this agreement",
}
_DOC_STOP_WORDS = {
    "the",
    "and",
    "for",
    "from",
    "with",
    "this",
    "that",
    "pdf",
    "docx",
    "doc",
    "document",
    "file",
    "what",
    "does",
    "about",
    "have",
    "many",
    "work",
    "give",
    "gave",
    "earlier",
    "right",
    "says",
    "tell",
}
_DOC_MAX_CHARS = 4000


async def _detect_document_reference(
    message: str,
    history: list[dict],
    knowledge_connector: Any,
) -> str | None:
    """Detect if the user's message references a previously uploaded document.

    Scans chat history for document upload markers, matches against the user's
    message via keyword overlap / generic references / context matching.
    When matched, fetches relevant chunks via content_id-scoped search.

    Returns formatted context string or None.
    """
    if not history or knowledge_connector is None:
        return None

    # Step 1: Find documents in history
    docs: list[dict] = []  # {title, content_id, context_text}
    for i, msg in enumerate(history):
        content = msg.get("content", "")
        title_match = _DOC_MARKER_RE.search(content)
        id_match = _CONTENT_ID_RE.search(content)
        if title_match and id_match:
            # Gather surrounding context (this message + next assistant response)
            context_parts = [content]
            if i + 1 < len(history):
                context_parts.append(history[i + 1].get("content", ""))
            docs.append(
                {
                    "title": title_match.group(1),
                    "content_id": id_match.group(1),
                    "context": " ".join(context_parts).lower(),
                }
            )

    if not docs:
        return None

    # Step 2: Match user message to a document
    msg_lower = message.lower()
    matched: dict | None = None

    # 2a: Title keyword match
    for doc in docs:
        title_words = re.findall(r"[a-z]{4,}", doc["title"].lower())
        keywords = [w for w in title_words if w not in _DOC_STOP_WORDS]
        if any(kw in msg_lower for kw in keywords):
            matched = doc
            break

    # 2b: Generic reference match (only if exactly one document)
    if (
        matched is None
        and len(docs) == 1
        and any(phrase in msg_lower for phrase in _GENERIC_DOC_PHRASES)
    ):
        matched = docs[0]

    # 2c: Context match — check if message keywords appear in surrounding context
    if matched is None:
        msg_words = set(re.findall(r"[a-z]{4,}", msg_lower)) - _DOC_STOP_WORDS
        for doc in docs:
            if any(w in doc["context"] for w in msg_words):
                matched = doc
                break

    if matched is None:
        return None

    # Step 3: Fetch relevant chunks via content_id-scoped search
    try:
        results = await knowledge_connector.search(
            message, limit=5, content_id=matched["content_id"]
        )
    except Exception:
        logger.warning("document_context_search_failed", content_id=matched["content_id"])
        return None

    if not results:
        return None

    # Step 4: Format (respect max chars)
    lines = [f"From document: {matched['title']}"]
    total = len(lines[0])
    for r in results:
        chunk = r.get("chunk_text", "")
        header = r.get("section_header")
        prefix = f"[{header}] " if header else ""
        line = f"- {prefix}{chunk}"
        if total + len(line) > _DOC_MAX_CHARS:
            remaining = _DOC_MAX_CHARS - total - 10
            if remaining > 100:
                lines.append(f"- {prefix}{chunk[:remaining]}...")
            break
        lines.append(line)
        total += len(line) + 1

    return "\n".join(lines)


async def _gather_knowledge_context(
    knowledge_connector: Any,
    message: str,
    agent_id: str | None = None,
    knowledge_domains: list[str] | None = None,
    score_threshold: float = 0.5,
    max_results: int = 5,
    max_chars: int = 2000,
    timeout: float = 5.0,
    ranking: Ranking | None = None,
) -> tuple[str | None, list[dict]]:
    """Search knowledge base for context relevant to the user's message.

    Semantic chunk search only (no knowledge graph). Never raises.
    Returns (formatted_context_string, injected_items_metadata).

    A result's score is `(similarity + domain boost) * decay * rank boost`;
    the threshold and the order both read it. `ranking` is the turn's
    `knowledge_ranking` (the caller reads the row once); None is the defaults.
    """
    if knowledge_connector is None:
        return (None, [])

    try:
        # Semantic search of chunks
        search_results = await asyncio.wait_for(
            knowledge_connector.search(message, limit=max_results), timeout=timeout
        )
        results = search_results if isinstance(search_results, list) else []

        if not results:
            return (None, [])

        ranking = ranking or DEFAULT_RANKING
        # Agent-scoped boosting: the agent's own domains get `domain_boost`.
        domains = set(knowledge_domains or [])
        for r in results:
            boost = ranking.domain_boost if r.get("source_type") in domains else 0.0
            r["_score"] = (r.get("similarity") or 0) + boost

        # Decay starts from `_score`, so `effective_score` carries the boost
        # into the threshold and the sort (#579) — before, it restarted from
        # raw similarity and the boost changed nothing a prompt saw.
        results = _apply_knowledge_decay(results, ranking)
        results = [r for r in results if r["effective_score"] >= score_threshold]

        if not results:
            return (None, [])

        results.sort(key=lambda r: r["effective_score"], reverse=True)

        # Format + build injection metadata
        lines: list[str] = []
        injected_meta: list[dict] = []
        total_len = 0
        for r in results[:max_results]:
            source_type = r.get("source_type", "unknown")
            title = r.get("title", "Untitled")
            snippet = r.get("summary") or r.get("text") or r.get("url") or ""
            if len(snippet) > 300:
                snippet = snippet[:297] + "..."
            line = f"- [{source_type}] {title}: {snippet}"
            if total_len + len(line) > max_chars:
                break
            lines.append(line)
            total_len += len(line) + 1

            # Track metadata for injection feedback
            content = f"{title}: {snippet}"
            injected_meta.append(
                {
                    "content_hash": _content_hash(content),
                    "content_id": r.get("content_id"),
                    "source_type": source_type,
                    "score": r.get("_score", r.get("similarity", 0)),
                    "keywords": _extract_keywords(content),
                }
            )

        if not lines:
            return (None, [])

        header = "The following information from the knowledge base may be relevant:"
        footer = "Use this context if relevant to the user's question. You can still use knowledge tools for deeper queries."
        formatted = f"{header}\n" + "\n".join(lines) + f"\n\n{footer}"
        return (formatted, injected_meta)

    except TimeoutError:
        logger.warning("knowledge_context_timeout", message_len=len(message))
        return (None, [])
    except Exception as exc:
        logger.warning("knowledge_context_error", error=error_text(exc, 500))
        return (None, [])


async def send_message(
    pool: asyncpg.Pool,
    llm_client: Any,
    agent_id: str,
    message: str,
    thread_id: str | None = None,
    knowledge_connector: Any = None,
    settings: Any = None,
    temporal_client: Any = None,
    finance_connector: Any = None,
    search_connector: Any = None,
    remote_script_connector: Any = None,
    vercel_connector: Any = None,
    background_tasks: set[asyncio.Task] | None = None,
    user_metadata: dict | None = None,
    tier_override: str | None = None,
) -> dict[str, Any]:
    """Send a message to an agent with tool calling support.

    `user_metadata` (optional): JSON-serialisable dict written to the
    user chat_history row's metadata column — used by chat channels to
    record the incoming message ref (e.g. `delivery_ref`) so the 30-day
    cleanup activity can channel-delete it later.

    Response includes `assistant_message_id` so the caller can patch the
    assistant row's metadata with the outgoing message ref after the
    reply lands.
    """
    # v3 chat_history.thread_id is NOT NULL. Callers that don't pass one (e.g.
    # ad-hoc curl, unauthenticated pings) get an ephemeral thread.
    if not thread_id:
        thread_id = str(uuid4())

    # Load agent
    agent = await pool.fetchrow("SELECT * FROM agents WHERE id = $1", agent_id)
    if not agent:
        return {"error": f"Agent '{agent_id}' not found", "response": ""}
    # Per-agent routing config (tool set, knowledge domains) — data-driven from
    # agents.metadata, with the shipped defaults as fallback (see chat dicts).
    agent_meta = dict(agent.get("metadata") or {})

    # The persona lives in the agent_personalities table (admin-UI-managed;
    # see aegis.services.personalities) and is rendered into the system prompt
    # by `_build_agent_system_prompt` below. Empty fallback is only used when
    # the agent has no persona content at all.
    system_prompt = ""

    # Proactive knowledge context is injected once, after the personality
    # prompt is built (see below) — building the prompt overwrites
    # `system_prompt`, so appending here would be discarded.
    injected_items: list[dict] = []

    # Load recent history. role='dispatch' rows are outbound chat
    # messages the user saw (briefings, interaction cards, alert notices)
    # — fold them in as assistant turns with a [Sent to you in chat]
    # prefix so the model can reason about them when the user replies
    # referring to something they were shown. The OpenAI chat spec only
    # accepts system/user/assistant/tool, so the synthetic prefix is the
    # mechanism that surfaces dispatches as assistant turns without
    # losing the "the user actually saw this" signal.
    history_rows = await pool.fetch(
        "SELECT role, content FROM chat_history "
        "WHERE agent_id = $1 AND thread_id = $2 "
        "ORDER BY created_at DESC LIMIT 20",
        agent_id,
        thread_id,
    )
    history: list[dict[str, Any]] = []
    for r in reversed(history_rows):
        role = r["role"]
        content = r["content"] or ""
        if role == "dispatch":
            history.append(
                {
                    "role": "assistant",
                    "content": f"[Sent to you in chat]\n{content}",
                }
            )
        elif role in {"user", "assistant", "system", "tool"}:
            history.append({"role": role, "content": content})

    if not llm_client:
        return {"error": "LLM not available", "response": ""}

    # Config
    # Resolve per-agent model via `agents.model_tier` → config/models.yaml.
    # Falls back to 'balanced' tier for unknown agents. A per-message
    # `tier_override` (fast/balanced/smart) from the chat UI wins when valid;
    # an unknown tier is ignored and we fall back to the agent's default.
    model = None
    if tier_override:
        try:
            model = tier_to_model(tier_override)
        except KeyError:
            logger.warning("chat_tier_override_unknown", tier=tier_override)
            model = None
    if model is None:
        model = await resolve_model_for_agent(pool, agent_id) if pool else "qwen3:14b"
    tools_enabled = getattr(settings, "tool_calling_enabled", True) if settings else True
    max_iter = getattr(settings, "tool_max_iterations", 5) if settings else 5
    max_bytes = getattr(settings, "tool_result_max_bytes", 4096) if settings else 4096
    timeout = getattr(settings, "tool_timeout_seconds", 30) if settings else 30

    # Build agent-specific tool list and structured prompt
    agent_tools = _get_agent_tools(agent_id, metadata=agent_meta) if tools_enabled else []

    # Tool-calling routing: see the `_TOOL_INCAPABLE_MODELS` comment above —
    # only the three bare max-proxy bridge aliases strip tools; versioned
    # Anthropic-API names (claude-sonnet-5, claude-haiku-4.5) are tool-capable
    # and must NOT match here. Swap in whatever the live `balanced` tier
    # resolves to whenever the agent has tools to call and the resolved model
    # is one of the bridge aliases. If the `balanced` tier isn't resolvable
    # (e.g. tiers not yet loaded at boot), degrade safely and leave the model
    # unchanged rather than crash the chat request. See cmemory lesson —
    # empty chat_tool_calls table for 7d across all agents was the diagnostic
    # signature that motivated this guard in the first place.
    if tools_enabled and agent_tools and model in _TOOL_INCAPABLE_MODELS:
        try:
            fallback_model = tier_to_model("balanced")
        except KeyError:
            fallback_model = None
        if fallback_model is not None and fallback_model != model:
            logger.info(
                "chat_model_substituted_for_tools",
                agent_id=agent_id,
                from_model=model,
                to_model=fallback_model,
                tool_count=len(agent_tools),
            )
            model = fallback_model

    tool_desc_lines = [
        f"- {t['function']['name']}: {t['function']['description']}" for t in agent_tools
    ]
    tool_desc = "\n".join(tool_desc_lines) if tool_desc_lines else None

    from aegis.services.personalities import get_personality, read_personality_files

    try:
        persona = await get_personality(pool, agent_id)
    except Exception:  # noqa: BLE001 — persona read must never break chat
        logger.warning("agent_persona_load_failed", agent_id=agent_id)
        persona = read_personality_files(agent_id)
    system_prompt = _build_agent_system_prompt(
        agent_id,
        fallback=system_prompt,
        tool_descriptions=tool_desc,
        persona=persona,
    )

    # Learning loop (Phase 4): surface the agent's durable lessons from past
    # human corrections so it gets better at the owner over time.
    try:
        from aegis.services.memory import format_memories, recent_memories

        mem = await recent_memories(pool, agent_id, limit=8)
        if mem:
            system_prompt = system_prompt + format_memories(mem)
    except Exception:  # noqa: BLE001 — memory is best-effort, never break chat
        logger.warning("agent_memory_inject_failed", agent_id=agent_id)

    # Document context injection — detect references to uploaded documents
    if knowledge_connector and history:
        try:
            doc_context = await _detect_document_reference(message, history, knowledge_connector)
            if doc_context:
                system_prompt = system_prompt + "\n\n## Document Context\n" + doc_context
        except Exception:
            logger.warning("document_reference_detection_failed")

    # Proactive knowledge context injection (after prompt building so it's always appended)
    if knowledge_connector and getattr(settings, "knowledge_context_enabled", True):
        knowledge_context, injected_items = await _gather_knowledge_context(
            knowledge_connector,
            message,
            agent_id=agent_id,
            knowledge_domains=agent_meta.get("knowledge_domains"),
            score_threshold=getattr(settings, "knowledge_context_score_threshold", 0.5),
            max_results=getattr(settings, "knowledge_context_max_results", 5),
            max_chars=getattr(settings, "knowledge_context_max_chars", 2000),
            timeout=getattr(settings, "knowledge_context_timeout_seconds", 5.0),
            # Read once per turn (30s cache); the per-result math is sync.
            ranking=await get_ranking(pool),
        )
        if knowledge_context:
            system_prompt = system_prompt + "\n\n## Relevant Knowledge\n" + knowledge_context

    # Build messages
    messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
    messages.extend(history)
    messages.append({"role": "user", "content": message})

    tools = agent_tools if tools_enabled else None

    # Build tool context
    ctx = ToolContext(
        agent_id=agent_id,
        task_id=(user_metadata or {}).get("task_id"),
        knowledge_connector=knowledge_connector,
        finance_connector=finance_connector,
        chat_context={
            "user_message": message,
            "thread_id": thread_id,
            "delivery_ref": (user_metadata or {}).get("delivery_ref"),
        },
        settings=settings,
        temporal_client=temporal_client,
        search_connector=search_connector,
        llm_client=llm_client,
        remote_script_connector=remote_script_connector,
        vercel_connector=vercel_connector,
        model_light=tier_to_model_or("fast", getattr(settings, "model_fast", "gemma4:e2b")),
    )

    # Tool-calling loop
    tool_calls_made: list[dict[str, Any]] = []
    response = ""
    # Early-stop guard: if the model calls the SAME tool with the SAME args
    # this many times across the loop, stop calling tools and force a final
    # text answer. Without this a model that loops on one tool/args pair
    # burns the whole iteration budget and returns nothing useful.
    _repeat_signatures: dict[str, int] = {}
    _repeat_limit = 3
    _stop_tools = False
    try:
        for _ in range(max_iter):
            start = time.monotonic()
            result = await llm_client.chat(
                messages=messages,
                model=model,
                tools=tools,
            )
            latency_ms = int((time.monotonic() - start) * 1000)
            await record_llm_call(
                pool,
                model=result.get("model", model),
                prompt_tokens=result.get("prompt_tokens", 0),
                completion_tokens=result.get("completion_tokens", 0),
                latency_ms=latency_ms,
                purpose="chat",
                agent_id=agent_id,
            )

            tool_calls = result.get("tool_calls", [])

            if not tool_calls:
                response = result.get("response", "")
                break

            # Add assistant message with tool calls
            messages.append(
                {
                    "role": "assistant",
                    "content": result.get("response") or None,
                    "tool_calls": [
                        {
                            "id": tc["id"],
                            "type": "function",
                            "function": {"name": tc["name"], "arguments": tc["arguments"]},
                        }
                        for tc in tool_calls
                    ],
                }
            )

            for tc in tool_calls:
                # Parse arguments with malformed JSON handling
                try:
                    args = (
                        json.loads(tc["arguments"])
                        if isinstance(tc["arguments"], str)
                        else tc["arguments"]
                    )
                except json.JSONDecodeError:
                    tool_result = json.dumps({"error": "Invalid arguments JSON"})
                    messages.append(
                        {"role": "tool", "tool_call_id": tc["id"], "content": tool_result}
                    )
                    await record_tool_call(
                        pool,
                        agent_id=agent_id,
                        thread_id=thread_id,
                        tool_name=tc["name"],
                        tool_args={},
                        tool_result={"error": "Invalid arguments JSON"},
                        status="error",
                        latency_ms=0,
                    )
                    continue

                # Early-stop on repeated identical tool calls (name + args).
                _sig = f"{tc['name']}:{json.dumps(args, sort_keys=True, default=str)}"
                _repeat_signatures[_sig] = _repeat_signatures.get(_sig, 0) + 1
                if _repeat_signatures[_sig] >= _repeat_limit:
                    logger.warning(
                        "chat_tool_repeat_stop",
                        agent=agent_id,
                        tool=tc["name"],
                        count=_repeat_signatures[_sig],
                    )
                    _stop_tools = True

                # Execute with timeout + jsonschema validation/retry
                tool_start = time.monotonic()
                _tc_name = tc["name"]
                _tc_id = tc["id"]

                async def _exec_with_timeout(
                    _pool: Any, _args: dict, _ctx: Any, _name: str = _tc_name
                ) -> str:
                    return await asyncio.wait_for(
                        _execute_tool(_pool, _name, _args, _ctx),
                        timeout=_TOOL_TIMEOUT_OVERRIDES.get(_name, timeout),
                    )

                try:
                    tool_result = await _dispatch_tool_call_with_retry(
                        pool=pool,
                        name=_tc_name,
                        tool_call_id=_tc_id,
                        initial_args=args,
                        messages=messages,
                        retry_args_provider=lambda err, _name=_tc_name: _retry_via_llm(
                            llm_client, messages, model, tools, _name, err
                        ),
                        executor=_exec_with_timeout,
                        ctx=ctx,
                    )
                    tool_status = "success"
                except ChatToolValidationError as exc:
                    logger.warning(
                        "chat_tool_validation_failed",
                        tool=exc.tool_name,
                        message=exc.message,
                        schema=exc.schema_summary,
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": _tc_id,
                            "content": f"Tool `{exc.tool_name}` failed validation after retry: {exc.message}",
                        }
                    )
                    await record_tool_call(
                        pool,
                        agent_id=agent_id,
                        thread_id=thread_id,
                        tool_name=_tc_name,
                        tool_args=args,
                        tool_result={"error": exc.message},
                        status="validation_failed",
                        latency_ms=int((time.monotonic() - tool_start) * 1000),
                    )
                    continue
                except TimeoutError:
                    _applied_timeout = _TOOL_TIMEOUT_OVERRIDES.get(_tc_name, timeout)
                    tool_result = json.dumps(
                        {"error": f"Tool '{_tc_name}' timed out after {_applied_timeout}s"}
                    )
                    tool_status = "timeout"
                except Exception as exc:
                    tool_result = json.dumps({"error": error_text(exc, 500)})
                    tool_status = "error"

                tool_latency = int((time.monotonic() - tool_start) * 1000)

                # Truncate result
                tool_result = _truncate_result(tool_result, max_bytes=max_bytes)

                messages.append({"role": "tool", "tool_call_id": _tc_id, "content": tool_result})
                tool_calls_made.append({"name": _tc_name, "args": args})

                result_dict = recorded_result(tool_result)

                # An executor reports failure by RETURNING an error envelope, not
                # by raising: `_exec_infra` turns a non-zero exit into
                # {"error": ..., "exit_code": ...} so the MODEL can read and relay
                # it. Only a raise reached the `except` arms above, so every such
                # failure was stored as status='success' with the error sitting in
                # `result` — and every "which tools are failing?" query answered
                # "none". That is precisely how infra tools returning exit 127
                # stayed invisible from 2026-07-16 to 08-28.
                #
                # Detection is deliberately narrow: a JSON object with a truthy
                # `error`. A tool that returns a prose apology ("the coding host is
                # not configured") is indistinguishable from a successful answer at
                # this layer, and guessing from prose would be worse than the gap.
                if (
                    tool_status == "success"
                    and isinstance(result_dict, dict)
                    and result_dict.get("error")
                ):
                    tool_status = "error"

                logger.info(
                    "chat_tool_executed",
                    tool=_tc_name,
                    agent=agent_id,
                    status=tool_status,
                    latency_ms=tool_latency,
                )
                await record_tool_call(
                    pool,
                    agent_id=agent_id,
                    thread_id=thread_id,
                    tool_name=_tc_name,
                    tool_args=args,
                    tool_result=result_dict,
                    status=tool_status,
                    latency_ms=tool_latency,
                )

            if _stop_tools:
                # Repeated-identical-tool-call loop detected: stop calling
                # tools and fall through to the graceful no-tools finalizer.
                break
        # for-else NOT used: when the loop runs the full max_iter without an
        # early break (model kept asking for tools every turn), `response`
        # stays "" and the graceful finalizer below produces a text answer.

        # Graceful exhaustion: the tool loop ended (max_iter hit or repeat
        # early-stop) without the model producing a final text answer. Make
        # ONE final no-tools call to force a text response instead of
        # returning the old bare "Max tool iterations reached." placeholder.
        if not response:
            try:
                final = await llm_client.chat(messages=messages, model=model, tools=None)
                response = (final.get("response") or "").strip()
            except Exception as exc:
                logger.warning("chat_final_no_tools_failed", error=error_text(exc, 500))
                response = ""
            if not response:
                response = (
                    "I wasn't able to complete that — could you rephrase "
                    "or narrow it down?"
                )

    except Exception as exc:
        logger.error("chat_llm_failed", error=error_text(exc, 500))
        return {"error": error_text(exc, 500), "response": ""}

    # Save to history. User row may carry the incoming message ref
    # via `user_metadata` so the cleanup activity can channel-delete it later.
    # Assistant row id is returned to the caller so it can be patched once
    # the reply's outgoing message_id is known.
    await pool.execute(
        "INSERT INTO chat_history (agent_id, thread_id, role, content, metadata) "
        "VALUES ($1, $2, $3, $4, $5)",
        agent_id,
        thread_id,
        "user",
        message,
        user_metadata or None,  # falsy metadata stores SQL NULL (same as the old 4-col form)
    )
    assistant_row_id = await pool.fetchval(
        "INSERT INTO chat_history (agent_id, thread_id, role, content, metadata) "
        "VALUES ($1, $2, $3, $4, $5) RETURNING id",
        agent_id,
        thread_id,
        "assistant",
        response,
        {"tool_calls": tool_calls_made} if tool_calls_made else {},
    )

    # Log knowledge injection feedback (fire-and-forget)
    if injected_items and pool:
        try:
            referenced = _check_knowledge_references(injected_items, response)
            content_ids = [item["content_id"] for item in injected_items if item.get("content_id")]
            await pool.execute(
                "INSERT INTO knowledge_injection_log "
                "(agent_id, thread_id, workflow_run_id, source, content_ids, triples_used) "
                "VALUES ($1, $2, NULL, 'chat', $3, $4)",
                agent_id or "unknown",
                thread_id,
                content_ids,
                {"injected": injected_items, "referenced": referenced},
            )
        except Exception as exc:
            logger.warning(
                "knowledge_injection_log_failed",
                error=error_text(exc, 500),
                agent_id=agent_id,
                thread_id=thread_id,
            )
            # never block chat on logging failure


    return {
        "agent_id": agent_id,
        "response": response,
        "thread_id": thread_id,
        "tool_calls": tool_calls_made,
        "assistant_message_id": str(assistant_row_id) if assistant_row_id else None,
    }


async def synthesize_agent_reply(
    *,
    pool: asyncpg.Pool,
    llm_client: Any,
    agent_id: str,
    message: str,
    thread_id: str,
    task_id: str | None = None,
    settings: Any = None,
    temporal_client: Any = None,
    knowledge_connector: Any = None,
    finance_connector: Any = None,
    search_connector: Any = None,
    remote_script_connector: Any = None,
    vercel_connector: Any = None,
) -> dict:
    """Chat entry point for two surfaces:

    - Todoist comment channel (task_id is set) — invoked by AgentChatReplyFlow
      after ClarifyFlow's per-agent short-circuit fires.
    - chat DM @mention (task_id is None) — invoked by the comms bot via
      the `/api/chat/agent-reply/trigger` route. Same agent, same tools,
      no Todoist anchor.

    Reuses send_message so the agent personality, tool surface, and
    chat-history persistence all behave identically to a web chat —
    only the surface tag in metadata differs.

    Returns:
        {
            "reply_text": str,                # empty on agent-not-found or refusal
            "tool_trace_summary": str,        # comma-joined tool names
            "llm_model": str,                 # model id reported by send_message
            "error": str | None,              # human-readable on failure
            "error_is_transient": bool,       # currently False on the return path;
                                              # transient is signalled via raise.
        }

    Raises:
        httpx.HTTPError / proxy connect / timeout — transient LLM-proxy
        failures bubble up so the route returns 5xx and the worker
        activity retries per its STANDARD policy.
    """
    user_metadata: dict[str, Any] = {
        "surface": "chat_dm" if task_id is None else "todoist_comment",
    }
    if task_id is not None:
        user_metadata["task_id"] = task_id
    # send_message handles auth/personality/tooling/history. Any non-transient
    # failure (agent not found, refusal) lands in the returned dict's "error"
    # field. Transient failures raise.
    # EVERY dependency send_message accepts is forwarded. The docstring above
    # promises this surface behaves identically to a web chat, and for months it
    # did not: `settings` and four connectors were dropped here, so a Slack or
    # Todoist-comment ask got a half-populated ToolContext. The tools degraded
    # silently and differently from the admin UI — `aegis_self_diagnose`
    # returned "settings not threaded into ToolContext", and the knowledge,
    # money, search and vercel tools ran without their connectors.
    # `test_agent_reply_forwards_every_dependency` pins the two signatures
    # together so a newly added dependency cannot be dropped here again.
    resp = await send_message(
        pool=pool,
        llm_client=llm_client,
        agent_id=agent_id,
        message=message,
        thread_id=thread_id,
        user_metadata=user_metadata,
        settings=settings,
        temporal_client=temporal_client,
        knowledge_connector=knowledge_connector,
        finance_connector=finance_connector,
        search_connector=search_connector,
        remote_script_connector=remote_script_connector,
        vercel_connector=vercel_connector,
    )

    if resp.get("error"):
        return {
            "reply_text": "",
            "tool_trace_summary": "",
            "llm_model": resp.get("model", ""),
            "error": resp["error"],
            "error_is_transient": False,
        }

    tool_calls = resp.get("tool_calls") or []
    tool_summary = ", ".join(tc.get("name") or "" for tc in tool_calls if tc.get("name"))

    return {
        "reply_text": resp.get("response", "") or "",
        "tool_trace_summary": tool_summary,
        "llm_model": resp.get("model", ""),
        "error": None,
        "error_is_transient": False,
    }
