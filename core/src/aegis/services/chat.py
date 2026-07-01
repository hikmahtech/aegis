"""Chat service — send messages to agents with tool calling support."""

from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import structlog
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as JSONSchemaValidationError

from aegis.llm import parse_llm_json
from aegis.llm.tier import resolve_model_for_agent
from aegis.observability import record_llm_call, record_tool_call
from aegis.services.todoist_config import resolve_todoist_api_key

logger = structlog.get_logger()


# Intent routing for the chat front door. Deterministic keyword map first
# (zero LLM cost); the LLM (fast tier) only resolves the keyword-less tail.
# ponytail: substring match — good enough; @mention override + persona icon
# make any mis-route visible and correctable.
_INTENT_KEYWORDS: dict[str, list[str]] = {
    "maou": ["money", "bill", "invoice", "subscription", "payment", "receipt",
             "spend", "cost", "budget", "renew", "charge", "refund", "expense",
             "price", "market", "stock", "crypto", "portfolio"],
    "pandoras-actor": ["server", "docker", "swarm", "k8s", "kubernetes", "deploy",
                       "infra", "drift", "backup", "cert", "argocd", "pod",
                       "container", "node", "homelab", "restart", "logs",
                       "grafana", "prometheus"],
    "raphael": ["research", "knowledge", "learn", "paper", "article", "summari",
                "remember", "recall", "explain"],
    "sebas": ["task", "todo", "inbox", "remind", "defer", "project",
              "next action", "calendar", "email", "waiting", "schedule",
              "follow up"],
}
# Tie-break: specific domains before the generalist.
_INTENT_PRECEDENCE = ["maou", "pandoras-actor", "raphael", "sebas"]


def _keyword_route(message: str, keyword_map: dict[str, list[str]] | None = None) -> str | None:
    """Pick an agent by keyword hit-count; None when no keyword matches.

    `keyword_map` is per-agent intent keywords (from agents.metadata, falling
    back to the shipped _INTENT_KEYWORDS defaults). Tie-break favours the known
    precedence, then any other agents.
    """
    if keyword_map is None:
        keyword_map = _INTENT_KEYWORDS
    low = (message or "").lower()
    scores = {a: sum(1 for kw in kws if kw in low) for a, kws in keyword_map.items()}
    if not scores:
        return None
    best = max(scores.values())
    if best == 0:
        return None
    order = _INTENT_PRECEDENCE + [a for a in keyword_map if a not in _INTENT_PRECEDENCE]
    for agent in order:
        if scores.get(agent) == best:
            return agent
    return None


async def _agent_keyword_map(pool) -> dict[str, list[str]]:
    """Build per-agent intent keywords from agents.metadata (data-driven), with
    the shipped _INTENT_KEYWORDS as fallback. Never raises."""
    if pool is None:
        return dict(_INTENT_KEYWORDS)
    try:
        rows = await pool.fetch("SELECT id, metadata FROM agents WHERE active = TRUE")
        out: dict[str, list[str]] = {}
        for r in rows:
            kws = (r["metadata"] or {}).get("intent_keywords") or _INTENT_KEYWORDS.get(r["id"])
            if kws:
                out[r["id"]] = kws
        return out or dict(_INTENT_KEYWORDS)
    except Exception as exc:  # noqa: BLE001 — routing must never break the front door
        logger.warning("agent_keyword_map_failed", error=str(exc)[:200])
        return dict(_INTENT_KEYWORDS)


def _build_intent_prompt(message: str) -> str:
    return (
        "Route this message to the single best AEGIS agent. Reply with STRICT "
        'JSON {"agent_id": "<id>", "reason": "<short>"}. Agents:\n'
        "- maou: finance, money, subscriptions, receipts, market\n"
        "- pandoras-actor: infrastructure, servers, deploys, homelab, logs\n"
        "- raphael: research, knowledge, learning, summarizing\n"
        "- sebas: tasks, GTD, calendar, email, general (the default)\n\n"
        f"Message: {message[:500]}"
    )


async def classify_intent(message: str, llm, settings, pool=None) -> dict:
    """Front-door intent routing: keyword map → fast-LLM fallback → default sebas.

    Keyword map is data-driven from agents.metadata (pool); never raises — on
    any ambiguity/failure returns sebas (the generalist).
    """
    keyword_map = await _agent_keyword_map(pool)
    kw = _keyword_route(message, keyword_map)
    if kw:
        return {"agent_id": kw, "reason": "keyword", "method": "keyword"}
    if llm is None:
        return {"agent_id": "sebas", "reason": "no_llm", "method": "default"}
    model = getattr(settings, "model_fast", "gemma4:e2b") if settings else "gemma4:e2b"
    try:
        result = await llm.think(
            _build_intent_prompt(message), model=model, max_tokens=300,
            purpose="intent_route",
        )
        raw = result.get("response", "") if isinstance(result, dict) else (result or "")
        parsed = parse_llm_json(raw) or {}
        agent = parsed.get("agent_id") or parsed.get("agent")
        if agent in keyword_map:
            return {"agent_id": agent, "reason": str(parsed.get("reason", ""))[:200], "method": "llm"}
    except Exception as exc:  # noqa: BLE001 — routing must never break the front door
        logger.warning("intent_route_llm_failed", error=str(exc)[:200])
    return {"agent_id": "sebas", "reason": "default", "method": "default"}


# Models served via max-proxy strip the `tools` array silently. When an agent
# has tools to call, swap the resolved model for `_TOOL_FALLBACK_MODEL` so the
# request actually carries tool definitions to a model that supports them.
# gpt-oss:20b (OpenAI open-weight) is OpenAI's tool-use-tuned release — MXFP4
# quant fits in ~12 GB on node-b's RTX 5060 Ti (16 GB) with comfortable headroom
# and the Harmony format produces stronger structured tool calls than the
# previous fallback (qwen3:14b). LiteLLM exposes it as `gpt-oss:20b` →
# `ollama_chat/gpt-oss:20b` with `supports_function_calling: true`
# (see infra-gitops/.../litellm-config.yaml.j2). Fallbacks in the LiteLLM
# router config let it degrade to qwen3:14b → claude-haiku if Ollama hiccups.
_TOOL_INCAPABLE_MODELS: frozenset[str] = frozenset({"claude-haiku", "claude-sonnet", "claude-opus"})
_TOOL_FALLBACK_MODEL: str = "gpt-oss:20b"


def _json_default(value: Any) -> Any:
    """Type-specific JSON encoder for tool result payloads.

    Coerces the common DB/HTTP types the LLM sees into shapes it understands:
    Decimal -> float, datetime/date -> ISO string, UUID -> str. Everything
    else falls back to ``str()`` so we never raise during serialization.
    """
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    return str(value)


# Passes used to progressively shrink a tool result that exceeds the byte
# budget. Each tuple is (n_items_to_keep, max_string_length). We try the
# loosest pass first (keep a lot, leave strings long) and tighten on each
# attempt, so results stay as useful as possible. Only when even the
# tightest pass overflows do we fall through to the minimal summary.
_SHRINK_PASSES: tuple[tuple[int, int], ...] = (
    (5, 1000),
    (5, 500),
    (3, 500),
    (3, 250),
    (1, 500),
    (1, 150),
)


def _shrink_strings(obj, max_str_len: int):
    """Recursively replace any string longer than ``max_str_len`` with a
    truncated-with-ellipsis version. Leaves other types untouched."""
    if isinstance(obj, str):
        if len(obj) > max_str_len:
            # Leave room for the marker so the caller can tell a cut happened.
            head = max(0, max_str_len - len("… [truncated]"))
            return obj[:head] + "… [truncated]"
        return obj
    if isinstance(obj, list):
        return [_shrink_strings(x, max_str_len) for x in obj]
    if isinstance(obj, dict):
        return {k: _shrink_strings(v, max_str_len) for k, v in obj.items()}
    return obj


def _smart_subset(data, n_items: int, max_str_len: int):
    """Keep the first ``n_items`` of a list or keys of a dict, shrinking any
    long string values inside the kept portion. Returns the trimmed object
    plus truncation metadata so the LLM knows content was dropped."""
    if isinstance(data, list):
        return {
            "results": [_shrink_strings(item, max_str_len) for item in data[:n_items]],
            "truncated": True,
            "total": len(data),
        }
    keys = list(data.keys())[:n_items]
    subset = {k: _shrink_strings(data[k], max_str_len) for k in keys}
    subset["_truncated"] = True
    subset["_total_keys"] = len(data)
    return subset


def _truncate_result(result_json: str, max_bytes: int = 4096) -> str:
    """Trim a JSON-serialised tool result so it fits within ``max_bytes``.

    Strategy, loosest → tightest:
    1. Return unchanged if already under budget.
    2. Parse. Non-JSON or scalar → raw byte slice.
    3. For a list/dict, iterate ``_SHRINK_PASSES``: keep first N items/keys
       and recursively shrink long string values inside them. Return the
       first pass whose serialisation fits.
    4. Fallback: minimal summary noting the total count.

    This is better than a byte-level slice because the LLM still sees
    structured data with sample content; it's better than the prior
    slice-then-give-up approach because deeply nested records (common for
    knowledge/search tool output) get their big string fields shrunk down
    rather than being replaced entirely by a "too large" note.
    """
    if len(result_json.encode()) <= max_bytes:
        return result_json

    try:
        data = json.loads(result_json)
    except (json.JSONDecodeError, TypeError):
        return result_json[:max_bytes]

    if not isinstance(data, (list, dict)):
        return result_json[:max_bytes]

    for n_items, max_str_len in _SHRINK_PASSES:
        candidate = _smart_subset(data, n_items, max_str_len)
        encoded = json.dumps(candidate, default=str)
        if len(encoded.encode()) <= max_bytes:
            return encoded

    # Even a single item with very short strings overflows — emit a minimal
    # summary. Keeps the tool result valid JSON and preserves the count.
    if isinstance(data, list):
        return json.dumps(
            {"truncated": True, "total": len(data), "note": "Results too large to display"}
        )
    return json.dumps(
        {"_truncated": True, "_total_keys": len(data), "note": "Results too large to display"}
    )


# Tool definitions for agent chat (OpenAI format)
CHAT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_knowledge",
            "description": "Search the knowledge base using semantic similarity. Returns relevant content with titles, summaries, and similarity scores.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Natural language search query"},
                    "limit": {
                        "type": "integer",
                        "description": "Max results (1-100)",
                        "default": 10,
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ask_knowledge",
            "description": "Ask a question and get a synthesized answer from the knowledge base with sources and confidence scores.",
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "Natural language question"},
                },
                "required": ["question"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remember_this",
            "description": "Store important information from this conversation in the knowledge base for future reference. Only call when something is worth remembering long-term.",
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": "Concise summary of what to remember",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Categorization tags",
                    },
                },
                "required": ["summary"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_activities",
            "description": "List scheduled activities and their recent run history",
            "parameters": {
                "type": "object",
                "properties": {
                    "active_only": {
                        "type": "boolean",
                        "default": True,
                        "description": "Only show active activities",
                    },
                    "limit": {"type": "integer", "default": 20},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "trigger_workflow",
            "description": "Trigger a Temporal workflow manually. Returns the workflow run ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "workflow_type": {
                        "type": "string",
                        "enum": ["daily_briefing", "weekly_review"],
                        "description": "Which workflow to trigger",
                    },
                    "params": {"type": "object", "description": "Optional workflow parameters"},
                },
                "required": ["workflow_type"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_market_regime",
            "description": "Get current market regime for equities (NIFTY 50) and crypto including fear/greed index.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_top_forecasts",
            "description": "Get the strongest trading signals (top forecasts) for a given asset class.",
            "parameters": {
                "type": "object",
                "properties": {
                    "asset_class": {
                        "type": "string",
                        "enum": ["equity", "crypto", "etf"],
                        "description": "Asset class to query",
                    },
                    "halal_only": {
                        "type": "boolean",
                        "description": "Filter to halal-screened assets only",
                        "default": False,
                    },
                    "direction": {
                        "type": "string",
                        "enum": ["long", "short"],
                        "description": "Optional filter: long (positive forecast) or short (negative forecast)",
                    },
                    "count": {
                        "type": "integer",
                        "description": "Number of results (max 50)",
                        "default": 10,
                    },
                },
                "required": ["asset_class"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_trade_decisions",
            "description": "Get execution-ready trade signals, ordered by conviction. Defaults to the latest available date.",
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {
                        "type": "string",
                        "description": "Date to query (YYYY-MM-DD). Defaults to latest available.",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_instrument_detail",
            "description": "Get a full picture of one instrument including forecasts, price stats, institutional signals, and halal status. Works for both equities (NSE symbols) and crypto.",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {
                        "type": "string",
                        "description": "Instrument symbol, e.g. RELIANCE for equity or BTC for crypto.",
                    },
                },
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_sector_overview",
            "description": "Get sector-level momentum and average forecasts for all sectors as of the latest available date.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_forecast_changes",
            "description": "Get instruments whose combined forecast changed significantly over a given lookback period. Useful for spotting momentum shifts.",
            "parameters": {
                "type": "object",
                "properties": {
                    "asset_class": {
                        "type": "string",
                        "enum": ["equity", "crypto"],
                        "description": "Asset class to query.",
                    },
                    "days": {
                        "type": "integer",
                        "description": "Lookback period in days (default 5, max 30).",
                    },
                    "threshold": {
                        "type": "number",
                        "description": "Minimum absolute forecast change percentage to include (default 5.0).",
                    },
                },
                "required": ["asset_class"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "research_topic",
            "description": "Research a topic by combining knowledge graph data with fresh web search results. Returns a synthesized analysis with sources.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "What to research"},
                    "depth": {
                        "type": "string",
                        "enum": ["quick", "thorough"],
                        "description": "Search depth (default: quick)",
                    },
                    "domains": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Limit web search to specific domains",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "track_topic",
            "description": "Subscribe to ongoing intelligence monitoring for a topic. AEGIS will periodically scan news sources and include findings in daily briefings.",
            "parameters": {
                "type": "object",
                "properties": {
                    "topic_name": {"type": "string", "description": "Name for this topic"},
                    "queries": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Search terms for this topic",
                    },
                    "priority": {
                        "type": "string",
                        "enum": ["high", "medium", "low"],
                        "description": "Monitoring priority (default: medium)",
                    },
                },
                "required": ["topic_name", "queries"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "configure_triage",
            "description": "Read or update triage configuration: ignored Sentry projects, ignored email domains, notification mode, burst threshold.",
            "parameters": {
                "type": "object",
                "properties": {
                    "setting": {
                        "type": "string",
                        "enum": [
                            "sentry_ignored_projects",
                            "email_ignored_domains",
                            "notification_mode",
                            "burst_threshold",
                        ],
                        "description": "Which triage setting to read or modify.",
                    },
                    "action": {
                        "type": "string",
                        "enum": ["add", "remove", "set", "get"],
                        "description": "add/remove items in a list, set a scalar value, or get the current value.",
                    },
                    "value": {
                        "type": ["string", "number"],
                        "description": "Value to add/remove/set. Omit for get.",
                    },
                },
                "required": ["setting", "action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_runbook",
            "description": "Update or add operational runbook knowledge for alert types or projects.",
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": "What to update, e.g. 'alert_type:ServiceDown', 'project:bcp'",
                    },
                    "content": {
                        "type": "string",
                        "description": "The runbook content to add",
                    },
                },
                "required": ["target", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_nodes",
            "description": (
                "List infrastructure cluster nodes and their status (up/down/drain). "
                "Use for checking Docker Swarm node health."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "context": {
                        "type": "string",
                        "enum": ["swarm"],
                        "description": "Infrastructure context. 'swarm' = homelab Docker Swarm.",
                    },
                },
                "required": ["context"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_services",
            "description": "List Docker Swarm services with replica counts, mode, and image versions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "context": {"type": "string", "enum": ["swarm"]},
                },
                "required": ["context"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "inspect_service",
            "description": "Inspect a Docker Swarm service: tasks, errors, update state, placement.",
            "parameters": {
                "type": "object",
                "properties": {
                    "context": {"type": "string", "enum": ["swarm"]},
                    "service_name": {
                        "type": "string",
                        "description": "Swarm service name (e.g. 'aegis_core')",
                    },
                },
                "required": ["context", "service_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_service_logs",
            "description": "Tail recent logs from a Docker Swarm service.",
            "parameters": {
                "type": "object",
                "properties": {
                    "context": {"type": "string", "enum": ["swarm"]},
                    "service_name": {"type": "string"},
                    "tail": {
                        "type": "integer",
                        "default": 50,
                        "description": "Number of log lines (1-500)",
                    },
                },
                "required": ["context", "service_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "restart_service",
            "description": (
                "Force-update (rolling restart) a Docker Swarm service. "
                "Mutating action — executes immediately."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "context": {"type": "string", "enum": ["swarm"]},
                    "service_name": {"type": "string"},
                },
                "required": ["context", "service_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_pods",
            "description": (
                "List Kubernetes pods on Acme EKS. Optionally filter by namespace "
                "and status (e.g. 'CrashLoopBackOff', 'Running', 'Pending')."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "context": {
                        "type": "string",
                        "enum": ["acme-prod", "acme-test"],
                    },
                    "namespace": {
                        "type": "string",
                        "description": "Kubernetes namespace (omit for all)",
                    },
                    "status": {
                        "type": "string",
                        "description": "Filter by phase or waiting reason",
                    },
                },
                "required": ["context"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_deployments",
            "description": "List Kubernetes deployments on Acme EKS with replica status.",
            "parameters": {
                "type": "object",
                "properties": {
                    "context": {"type": "string", "enum": ["acme-prod", "acme-test"]},
                    "namespace": {
                        "type": "string",
                        "description": "Kubernetes namespace (omit for all)",
                    },
                },
                "required": ["context"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_pod_logs",
            "description": "Tail recent logs from a Kubernetes pod on Acme EKS.",
            "parameters": {
                "type": "object",
                "properties": {
                    "context": {"type": "string", "enum": ["acme-prod", "acme-test"]},
                    "namespace": {"type": "string"},
                    "pod_name": {"type": "string"},
                    "tail": {
                        "type": "integer",
                        "default": 50,
                        "description": "Number of log lines (1-500)",
                    },
                    "container": {"type": "string", "description": "Optional container name"},
                },
                "required": ["context", "namespace", "pod_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_argocd_apps",
            "description": (
                "List ArgoCD applications with sync and health status. "
                "Optional filter: 'degraded', 'outofsync', 'synced', 'healthy'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "context": {"type": "string", "enum": ["acme-prod", "acme-test"]},
                    "filter": {"type": "string", "description": "Optional status filter"},
                },
                "required": ["context"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sync_argocd_app",
            "description": (
                "Trigger ArgoCD sync for an application. Mutating action — executes immediately."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "context": {"type": "string", "enum": ["acme-prod", "acme-test"]},
                    "app_name": {"type": "string"},
                },
                "required": ["context", "app_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_infra_script",
            "description": (
                "Run an infrastructure script from the predefined scripts/infra/ "
                "directory by name (without the .sh suffix). The context is passed "
                "as the script's first argument. Prefer the dedicated infra tools "
                "(list_nodes, list_services, ...) when one matches."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "context": {
                        "type": "string",
                        "enum": ["swarm", "acme-prod", "acme-test"],
                    },
                    "script_name": {
                        "type": "string",
                        "description": "Script file name, e.g. 'infra_list_nodes'",
                    },
                    "args": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Arguments passed to the script",
                    },
                },
                "required": ["context", "script_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "aegis_self_diagnose",
            "description": (
                "Investigate / fix AEGIS itself by driving the kimi CLI over SSH on node-a "
                "against the AEGIS source checkout. The kimi run has full Shell / Read / Glob / "
                "WriteFile permissions. Use this when the user asks about AEGIS's own behavior, "
                "bugs, or improvements. For code FIXES, kimi MUST create a branch (`aegis-fix/"
                "<slug>`), commit, push, and open a PR via `gh pr create` — never direct-commit "
                "to main. The tool waits up to 8 minutes for kimi's STATUS footer; if the run "
                "exceeds that, the partial output is returned with a `still_running` flag so the "
                "user can ask for a follow-up."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "issue": {
                        "type": "string",
                        "description": (
                            "What kimi should investigate or fix. Be concrete: file paths, "
                            "error messages, observed behavior, what 'good' looks like."
                        ),
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["investigate", "fix"],
                        "description": (
                            "`investigate` = read-only RCA + propose fix in chat. "
                            "`fix` = also commit + push + open PR. Both modes give kimi the "
                            "full toolset; the prompt enforces the convention."
                        ),
                    },
                },
                "required": ["issue", "mode"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_interactions",
            "description": (
                "List pending human-in-the-loop interactions (approvals, choices, "
                "inputs) for an agent. Use this when the user asks about pending "
                "decisions, approvals awaiting their response, or what needs their "
                "attention."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "string",
                        "description": "Agent to filter by (defaults to the caller's agent).",
                    },
                    "status": {
                        "type": "string",
                        "enum": ["pending", "resolved", "expired"],
                        "description": "Filter by interaction status (default: pending).",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 100,
                        "description": "Max rows to return (default 20, max 100).",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "capture_to_inbox",
            "description": "Drop a task into the Todoist Inbox. The task gets a #chat source tag by default unless 'source' is given.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Task title"},
                    "source": {
                        "type": "string",
                        "enum": ["chat", "manual"],
                        "default": "chat",
                        "description": "Where the capture originated (tags as #<source>)",
                    },
                    "description": {"type": "string", "description": "Optional longer body"},
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_next_actions",
            "description": "Read open (incomplete) Next Action tasks from the Todoist projection. Optional filters: assignee label, context label, due window.",
            "parameters": {
                "type": "object",
                "properties": {
                    "assignee": {
                        "type": "string",
                        "description": "Assignee label (e.g. @me, @sebas)",
                    },
                    "context": {
                        "type": "string",
                        "description": "Context label (e.g. @5min, @deep)",
                    },
                    "due": {
                        "type": "string",
                        "enum": ["today", "this_week", "overdue"],
                        "description": "Optional due-window filter",
                    },
                    "limit": {
                        "type": "integer",
                        "default": 25,
                        "description": "Max rows to return",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "whats_next",
            "description": (
                "Suggest what to work on now. Returns a short ranked list of "
                "your own next actions (excludes waiting/reference/reading and "
                "inbox/someday). Optionally tailor to available time and energy."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "minutes": {
                        "type": "integer",
                        "description": "Minutes available (<=5 prefers @5min tasks)",
                    },
                    "energy": {
                        "type": "string",
                        "enum": ["low", "high"],
                        "description": "low prefers light tasks; high prefers deep work",
                    },
                    "limit": {"type": "integer", "description": "Max items (default 5)"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_projects",
            "description": "List work-stream project/* labels with their open-task counts.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "complete_task",
            "description": "Mark a Todoist task complete. Optional 'note' is appended as a Todoist comment.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "note": {"type": "string"},
                },
                "required": ["task_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "defer_task",
            "description": "Reschedule a Todoist task to a new due date.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "until": {
                        "type": "string",
                        "description": "ISO date or natural string like 'tomorrow', 'next friday'",
                    },
                },
                "required": ["task_id", "until"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mark_waiting",
            "description": "Mark a task @waiting (with a 'who' note). Optionally include expected_by ISO date.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "who": {"type": "string", "description": "The person we're waiting on"},
                    "expected_by": {"type": "string", "description": "Optional ISO date"},
                },
                "required": ["task_id", "who"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "handoff_task",
            "description": "Reassign a task to a different personality assignee (e.g. @raphael).",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "to_assignee": {
                        "type": "string",
                        "enum": ["@me", "@sebas", "@raphael", "@maou", "@pandora"],
                    },
                },
                "required": ["task_id", "to_assignee"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_reference",
            "description": "Search the \U0001f516 Reference project + knowledge-service for relevant items.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "default": 10},
                },
                "required": ["query"],
            },
        },
    },
    # --- Vercel read-only (Pandora) ---
    # Project arg accepts either the bare Vercel project name (e.g. "drwhome")
    # or the resources-table slug ("vercel-drwhome"); the executor strips the
    # slug prefix before calling the connector.
    {
        "type": "function",
        "function": {
            "name": "vercel_get_project",
            "description": (
                "Look up a Vercel project's metadata: framework, production "
                "domain, linked GitHub repo, etc. Use this when you need basic "
                "context about a project before investigating deployments."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "project": {
                        "type": "string",
                        "description": (
                            "Vercel project name (e.g. 'drwhome') or resources "
                            "slug (e.g. 'vercel-drwhome')."
                        ),
                    },
                },
                "required": ["project"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "vercel_list_deployments",
            "description": (
                "List recent Vercel deployments for a project, with optional "
                "time-window and state filters. Use `state='ERROR'` to find "
                "failed deploys, `since_hours=24` to scope to the last day."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "project": {
                        "type": "string",
                        "description": "Vercel project name or 'vercel-<name>' slug.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max deployments returned (1-100). Default 10.",
                        "default": 10,
                    },
                    "since_hours": {
                        "type": "integer",
                        "description": (
                            "Only return deployments created within the last N hours. "
                            "Omit for no time filter."
                        ),
                    },
                    "state": {
                        "type": "string",
                        "description": (
                            "Filter by readyState: READY|ERROR|BUILDING|CANCELED|"
                            "INITIALIZING|QUEUED. Case-insensitive. Omit for any state."
                        ),
                    },
                },
                "required": ["project"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "vercel_get_deployment",
            "description": (
                "Fetch a single Vercel deployment by id (dpl_*). Surfaces "
                "errorCode/errorMessage/errorStep if the deploy ERROR'd, plus "
                "the git commit ref/sha/message that triggered it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "deployment_id": {
                        "type": "string",
                        "description": "Vercel deployment uid (starts with 'dpl_').",
                    },
                },
                "required": ["deployment_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "vercel_get_build_logs",
            "description": (
                "Fetch build event log for a Vercel deployment (newest first). "
                "Set errors_only=true to filter to stderr lines — useful for "
                "isolating the failure in a deploy that ERROR'd."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "deployment_id": {
                        "type": "string",
                        "description": "Vercel deployment uid (starts with 'dpl_').",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max events (1-1000). Default 100.",
                        "default": 100,
                    },
                    "errors_only": {
                        "type": "boolean",
                        "description": "If true, only return stderr-typed events.",
                        "default": False,
                    },
                },
                "required": ["deployment_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "investigate_resource",
            "description": (
                "Kick off a full code investigation (kimi over SSH) of a registered "
                "repository this task concerns. Use when the task or the user's comment "
                "clearly pertains to a specific repo in the resource list. Runs "
                "asynchronously: the findings and a fix-approval (Gate-2) card are posted "
                "back to THIS Todoist task in a few minutes. Only works when replying on a "
                "Todoist task (not a DM)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "repo": {
                        "type": "string",
                        "description": "The resource/repo the task is about, e.g. 'bcp'.",
                    },
                    "focus": {
                        "type": "string",
                        "description": "One line: what to investigate, derived from the task title and the user's comment.",
                    },
                },
                "required": ["repo", "focus"],
            },
        },
    },
]


@dataclass
class ToolContext:
    """Context passed to tool executor functions."""

    agent_id: str | None = None
    task_id: str | None = None
    knowledge_connector: Any | None = None
    clickhouse_connector: Any | None = None
    chat_context: dict | None = None
    settings: Any = None
    temporal_client: Any = None
    search_connector: Any | None = None
    llm_client: Any | None = None
    remote_script_connector: Any | None = None
    vercel_connector: Any | None = None
    model_light: str = "gemma4:e2b"


# --- Individual tool executor functions ---

# --- Infrastructure tool helpers ---

_INFRA_CONTEXTS_SWARM = {"swarm"}
_INFRA_CONTEXTS_K8S = {"acme-prod", "acme-test"}
_INFRA_CONTEXTS_ALL = _INFRA_CONTEXTS_SWARM | _INFRA_CONTEXTS_K8S

_INFRA_SAFE_NAME = re.compile(r"^[a-zA-Z0-9_\-\.]+$")


def _validate_infra_name(value: str, field: str) -> str | None:
    """Return an error string if invalid, else None."""
    if not value:
        return f"{field} is required"
    if not _INFRA_SAFE_NAME.match(value):
        return f"{field} contains invalid characters (allowed: a-z, A-Z, 0-9, _, -, .)"
    return None


async def _run_infra_script(
    ctx: ToolContext,
    script_name: str,
    args: list[str],
    timeout: int = 30,
) -> str:
    """Shared helper: run an infra/*.sh script on node-a via SSH."""
    if not ctx.remote_script_connector:
        return json.dumps({"error": "Remote script connector not available"})
    try:
        result = await ctx.remote_script_connector.run_script(
            f"infra/{script_name}", args, timeout=timeout
        )
    except Exception as exc:
        return json.dumps({"error": f"script_exception: {exc}"})
    if result.get("status") != "succeeded":
        return json.dumps(
            {
                "error": result.get("stderr", "").strip() or "Script failed",
                "exit_code": result.get("exit_code"),
            }
        )
    stdout = result.get("stdout", "").strip()
    return stdout or json.dumps({"result": "ok"})


# The 10 infra executors are one context-check → arg-validate → run-script
# pipeline differing only in data. `_INFRA_SPECS` holds that data and `_exec_infra`
# is the shared driver; the named `_exec_*` callables are `partial`s of it so the
# `TOOL_EXECUTORS` registry and the test imports keep their exact identities.
#
# spec = (script, contexts, ctx_default, ctx_err, timeout, arg_fields)
#   ctx_err   "for_tool" → "Unsupported context for {tool}: {ctx}", else "Unsupported context: {ctx}"
#   arg_fields tuple of (name, kind) appended to the script args in order; kind is:
#     "name"    required name field, always validated via `_validate_infra_name`
#     "optname" optional name field; validated only when non-empty
#     "tail"    int(args["tail"] or 50) clamped to [1, 500], passed as str
_SWARM, _K8S = _INFRA_CONTEXTS_SWARM, _INFRA_CONTEXTS_K8S
_INFRA_SPECS: dict[str, tuple] = {
    "list_nodes": ("infra_list_nodes", _SWARM, "swarm", "for_tool", 30, ()),
    "list_services": ("infra_list_services", _SWARM, "swarm", "for_tool", 30, ()),
    "inspect_service": (
        "infra_inspect_service", _SWARM, "swarm", "bare", 30,
        (("service_name", "name"),),
    ),
    "get_service_logs": (
        "infra_get_service_logs", _SWARM, "swarm", "bare", 60,
        (("service_name", "name"), ("tail", "tail")),
    ),
    "restart_service": (
        "infra_restart_service", _SWARM, "swarm", "bare", 120,
        (("service_name", "name"),),
    ),
    "list_pods": (
        "infra_list_pods", _K8S, "", "for_tool", 30,
        (("namespace", "optname"), ("status", "optname")),
    ),
    "list_deployments": (
        "infra_list_deployments", _K8S, "", "for_tool", 30,
        (("namespace", "optname"),),
    ),
    "get_pod_logs": (
        "infra_get_pod_logs", _K8S, "", "bare", 60,
        (("namespace", "name"), ("pod_name", "name"), ("tail", "tail"), ("container", "optname")),
    ),
    "list_argocd_apps": (
        "infra_list_argocd_apps", _K8S, "", "bare", 30,
        (("filter", "optname"),),
    ),
    "sync_argocd_app": (
        "infra_sync_argocd_app", _K8S, "", "bare", 120,
        (("app_name", "name"),),
    ),
}


async def _exec_infra(tool: str, pool: asyncpg.Pool, args: dict, ctx: ToolContext) -> str:
    """Shared driver for the data-described infra executors (`_INFRA_SPECS`)."""
    script, contexts, ctx_default, ctx_err, timeout, arg_fields = _INFRA_SPECS[tool]
    context = args.get("context", ctx_default)
    if context not in contexts:
        if ctx_err == "for_tool":
            return json.dumps({"error": f"Unsupported context for {tool}: {context}"})
        return json.dumps({"error": f"Unsupported context: {context}"})
    script_args = [context]
    for field, kind in arg_fields:
        if kind == "tail":
            tail = max(1, min(int(args.get("tail", 50)), 500))
            script_args.append(str(tail))
            continue
        value = args.get(field, "")
        if kind == "optname":
            value = value or ""
        if kind == "name" or value:
            err = _validate_infra_name(value, field)
            if err:
                return json.dumps({"error": err})
        script_args.append(value)
    return await _run_infra_script(ctx, script, script_args, timeout=timeout)


# Named callables for the registry + test imports. `partial` of a coroutine
# function is itself awaitable, so `await _exec_list_nodes(pool, args, ctx)` works.
_exec_list_nodes = functools.partial(_exec_infra, "list_nodes")
_exec_list_services = functools.partial(_exec_infra, "list_services")
_exec_inspect_service = functools.partial(_exec_infra, "inspect_service")
_exec_get_service_logs = functools.partial(_exec_infra, "get_service_logs")
_exec_restart_service = functools.partial(_exec_infra, "restart_service")
_exec_list_pods = functools.partial(_exec_infra, "list_pods")
_exec_list_deployments = functools.partial(_exec_infra, "list_deployments")
_exec_get_pod_logs = functools.partial(_exec_infra, "get_pod_logs")
_exec_list_argocd_apps = functools.partial(_exec_infra, "list_argocd_apps")
_exec_sync_argocd_app = functools.partial(_exec_infra, "sync_argocd_app")


_KIMI_STATUS_RE_CHAT = re.compile(r"^STATUS:\s*\S+", re.MULTILINE)
_AEGIS_SELF_DIAGNOSE_MAX_WAIT = 480  # 8 minutes; leaves headroom under synthesize_reply's 600s
_AEGIS_SELF_DIAGNOSE_POLL = 15  # poll interval in seconds
_AEGIS_SELF_DIAGNOSE_OUTPUT_CAP = 8 * 1024  # last N chars returned to the LLM


def _slugify_issue(text: str, max_len: int = 32) -> str:
    """Stable slug for `aegis-fix/<slug>` branch names. Lowercase a-z0-9-, capped."""
    base = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (base or "issue")[:max_len].strip("-") or "issue"


def _build_aegis_self_diagnose_prompt(issue: str, mode: str, fix_branch: str) -> str:
    """Compose the kimi prompt for AEGIS self-investigation / self-fix.

    The convention mirrors `_build_alert_investigation_prompt` in the worker's
    alerts.py but is workspace-aware (AEGIS's own source) and adds the
    branch+PR convention for `fix` mode. The STATUS footer is REQUIRED so
    the polling loop terminates cleanly.
    """
    prompt = (
        "You are pandora-as-kimi, debugging AEGIS itself. Workspace: this repo, "
        "rooted at the current directory. Use Shell, Read, Glob, and other tools to "
        "gather concrete evidence — never speculate.\n\n"
        f"Mode: {mode}\nIssue:\n{issue}\n\n"
        "Steps:\n"
        "1. Identify the relevant files / flows / activities.\n"
        "2. Read enough source to understand the actual behavior.\n"
        "3. Diagnose the root cause (or confirm the user's hypothesis).\n"
    )
    if mode == "fix":
        prompt += (
            f"4. Implement the fix. Create branch `{fix_branch}`, commit with a clear "
            "message, push to origin, then `gh pr create --draft` with a summary + "
            "test plan. Output a line: `BRANCH: aegis:<branch_name>` and "
            "`PR: <url>`. Do NOT commit speculative or untested changes. "
            "Do NOT commit directly to main.\n"
        )
    else:
        prompt += (
            "4. Propose the fix as a unified diff or file-targeted change list in your "
            "final assistant message. Do NOT modify files in this mode.\n"
        )
    prompt += (
        "5. The LAST line of your output MUST be exactly one of:\n"
        "     STATUS: investigated\n"
        "     STATUS: proposed\n"
        "     STATUS: shipped\n"
        "     STATUS: insufficient_evidence: <what you could not check>\n"
        "     STATUS: unactionable: <why this isn't fixable>\n"
    )
    return prompt


async def _exec_aegis_self_diagnose(pool: asyncpg.Pool, args: dict, ctx: ToolContext) -> str:
    """Drive kimi against the AEGIS self-repo on node-a.

    Phases:
      1. start_kimi_run with the baked self-diagnose prompt
      2. poll fetch_kimi_run_output every 15s until STATUS footer OR 8 min
      3. return parsed transcript (last 8KB) + run_id + status flag
    """
    issue = (args.get("issue") or "").strip()
    mode = args.get("mode") or "investigate"
    if not issue:
        return json.dumps({"error": "issue is required"})
    if mode not in {"investigate", "fix"}:
        return json.dumps({"error": "mode must be 'investigate' or 'fix'"})
    if not ctx.remote_script_connector:
        return json.dumps({"error": "remote_script connector not available"})
    if ctx.settings is None:
        return json.dumps({"error": "settings not threaded into ToolContext"})

    settings = ctx.settings
    repo = settings.aegis_self_repo_path or "personal/aegis"
    kimi_binary = settings.kimi_cli_binary_path
    fix_branch = f"aegis-fix/{_slugify_issue(issue)}"
    prompt = _build_aegis_self_diagnose_prompt(issue, mode, fix_branch)

    try:
        run_result = await ctx.remote_script_connector.start_kimi_run(
            repo, prompt, kimi_binary=kimi_binary
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("aegis_self_diagnose_start_failed", error=str(exc))
        return json.dumps({"error": f"kimi launch failed: {str(exc)[:200]}"})

    if run_result.get("status") == "failed":
        return json.dumps({"error": run_result.get("error", "kimi launch failed")})

    output_file = run_result.get("output_file", "")
    run_id = run_result.get("run_id", "")
    deadline = time.monotonic() + _AEGIS_SELF_DIAGNOSE_MAX_WAIT
    latest_raw = ""
    while time.monotonic() < deadline:
        raw = await ctx.remote_script_connector.fetch_kimi_run_output(
            output_file, host=run_result.get("host", "")
        )
        if raw:
            latest_raw = raw
            if _KIMI_STATUS_RE_CHAT.search(raw):
                return json.dumps(
                    {
                        "status": "completed",
                        "run_id": run_id,
                        "output_file": output_file,
                        "transcript": raw[-_AEGIS_SELF_DIAGNOSE_OUTPUT_CAP:],
                        "fix_branch": fix_branch if mode == "fix" else None,
                    }
                )
        await asyncio.sleep(_AEGIS_SELF_DIAGNOSE_POLL)
    return json.dumps(
        {
            "status": "still_running",
            "run_id": run_id,
            "output_file": output_file,
            "transcript": latest_raw[-_AEGIS_SELF_DIAGNOSE_OUTPUT_CAP:]
            if latest_raw
            else "(no output yet — kimi may still be initialising)",
            "note": "Run exceeded 8min. Use the run_id / output_file to follow up.",
            "fix_branch": fix_branch if mode == "fix" else None,
        }
    )


async def _exec_investigate_resource(pool: asyncpg.Pool, args: dict, ctx: ToolContext) -> str:
    """Spawn AlertInvestigationFlow against a registered repo the task concerns.

    Pandora-only, comment-channel-only. Fire-and-forget: the durable flow posts
    the verdict + plain-text kimi transcript back to the current Todoist task and
    fires the Gate-2 approval card. Returns immediately. source='todoist-chat'
    (non-Jira) keeps Gate-2 ON and kimi fix-capable; todoist_task_id attaches the
    run to this card AND bypasses the alert-signature dedup.
    """
    repo = (args.get("repo") or "").strip()
    focus = (args.get("focus") or "").strip()
    if not repo or not focus:
        return json.dumps({"error": "repo and focus are required"})
    task_id = (ctx.task_id or "").strip()
    if not task_id:
        return json.dumps(
            {"error": "investigate_resource only works when replying on a Todoist task (not a DM)"}
        )
    if not ctx.temporal_client:
        return json.dumps({"error": "temporal client not available"})

    # Validate repo against registered resources (basename of github_repo, or path).
    try:
        rows = await pool.fetch(
            "SELECT metadata->>'github_repo' AS gh, metadata->>'path' AS rp FROM resources"
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("investigate_resource_lookup_failed", error=str(exc)[:200])
        return json.dumps({"error": f"resource lookup failed: {str(exc)[:200]}"})
    target = repo.lower()
    matched = False
    available: set[str] = set()
    for r in rows:
        gh = (r["gh"] or "").strip()
        rp = (r["rp"] or "").strip()
        gh_base = gh.rsplit("/", 1)[-1].lower() if gh else ""
        # path is workspace-relative and may be nested ("acme/bcp") —
        # match on its basename too.
        rp_base = rp.rsplit("/", 1)[-1].lower() if rp else ""
        if gh_base:
            available.add(gh_base)
        elif rp_base:
            available.add(rp_base)
        if target and target in {gh_base, rp.lower(), rp_base, gh.lower()}:
            matched = True
    if not matched:
        return json.dumps({"error": f"unknown repo '{repo}'", "available_repos": sorted(available)})

    from temporalio.exceptions import WorkflowAlreadyStartedError

    workflow_id = f"chat-investigate-{task_id}"
    alert = {
        "title": focus[:200],
        "description": f"{focus}\n\n(triggered by pandora on Todoist task {task_id})"[:2000],
        "source": "todoist-chat",
        "service": repo,
        "severity": "normal",
        "fingerprint": f"chat-investigate-{task_id}",
        "labels": {"alertname": focus[:100], "service": repo},
        "requires_approval": False,
        "todoist_task_id": task_id,
    }
    try:
        await ctx.temporal_client.start_workflow(
            "AlertInvestigationFlow",
            alert,
            id=workflow_id,
            task_queue="aegis-main",
        )
    except WorkflowAlreadyStartedError:
        return json.dumps({"status": "already_investigating", "workflow_id": workflow_id, "repo": repo})
    except Exception as exc:  # noqa: BLE001
        logger.warning("investigate_resource_spawn_failed", repo=repo, error=str(exc)[:200])
        return json.dumps({"error": f"failed to start investigation: {str(exc)[:200]}"})
    return json.dumps({"status": "investigation_started", "workflow_id": workflow_id, "repo": repo})


async def _exec_run_infra_script(pool: asyncpg.Pool, args: dict, ctx: ToolContext) -> str:
    # Runs scripts/infra/<script_name>.sh on the remote host with `context`
    # as the first argument — the same surface the dedicated infra tools use.
    # (The original implementation looked scripts up in the `resources` table
    # via a column that never existed, so this tool errored on every call.)
    context = args.get("context", "")
    if context not in _INFRA_CONTEXTS_ALL:
        return json.dumps({"error": f"Unsupported context: {context}"})
    script_name = args.get("script_name", "")
    err = _validate_infra_name(script_name, "script_name")
    if err:
        return json.dumps({"error": err})

    script_args = args.get("args") or []
    if not isinstance(script_args, list):
        return json.dumps({"error": "args must be an array"})
    script_args = [str(a) for a in script_args]

    return await _run_infra_script(ctx, script_name, [context, *script_args], timeout=120)


def _knowledge_unavailable(detail: str = "Knowledge service not available") -> str:
    """Return a clearly-labeled 'service down' status.

    Distinct from an empty successful search so the LLM can decide whether to
    retry, apologise to the user, or fall back to another tool instead of
    treating the gap as "no results found".
    """
    return json.dumps({"status": "unavailable", "error": detail, "retry_suggested": True})


async def _exec_search_knowledge(pool: asyncpg.Pool, args: dict, ctx: ToolContext) -> str:
    if not ctx.knowledge_connector:
        return _knowledge_unavailable()
    query = args.get("query", "")
    limit = args.get("limit", 10)
    try:
        results = await ctx.knowledge_connector.search(query, limit=limit)
    except Exception as exc:
        logger.warning("search_knowledge_unreachable", error=str(exc))
        return _knowledge_unavailable(f"search failed: {exc}")
    return json.dumps(results, default=_json_default)


async def _exec_ask_knowledge(pool: asyncpg.Pool, args: dict, ctx: ToolContext) -> str:
    if not ctx.knowledge_connector:
        return _knowledge_unavailable()
    question = args.get("question", "")
    try:
        result = await ctx.knowledge_connector.ask(question)
    except Exception as exc:
        logger.warning("ask_knowledge_unreachable", error=str(exc))
        return _knowledge_unavailable(f"ask failed: {exc}")
    return json.dumps(result, default=_json_default)




async def _exec_list_interactions(pool: Any, args: dict, ctx: ToolContext) -> str:
    """Return interactions for an agent filtered by status."""
    agent_id = args.get("agent_id") or ctx.agent_id
    if not agent_id:
        return json.dumps([])
    # Schema enum enforces this in production; guard is belt-and-suspenders
    # for direct test calls that bypass _validate_tool_args.
    status = args.get("status", "pending")
    if status not in ("pending", "resolved", "expired"):
        status = "pending"
    limit = max(1, min(int(args.get("limit", 20)), 100))
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, kind, origin, prompt, status, created_at, resolved_at
            FROM interactions
            WHERE agent_id = $1 AND status = $2
            ORDER BY created_at DESC
            LIMIT $3
            """,
            agent_id,
            status,
            limit,
        )
    result = [
        {
            "id": str(r["id"]),
            "kind": r["kind"],
            "origin": r["origin"],
            "prompt": r["prompt"],
            "status": r["status"],
            "created_at": r["created_at"].isoformat(),  # NOT NULL per schema
            "resolved_at": r["resolved_at"].isoformat() if r["resolved_at"] else None,
        }
        for r in rows
    ]
    return json.dumps(result, default=str)


async def _exec_remember_this(pool: asyncpg.Pool, args: dict, ctx: ToolContext) -> str:
    if not ctx.knowledge_connector:
        return json.dumps({"error": "Knowledge service not available"})
    chat_ctx = ctx.chat_context or {}
    summary = args.get("summary", "")
    thread_id = chat_ctx.get("thread_id", "unknown")
    timestamp = int(time.time())
    raw_text = f"User: {chat_ctx.get('user_message', '')}\nSummary: {summary}"
    try:
        result = await ctx.knowledge_connector.ingest_content(
            url=f"aegis://chat/{thread_id}/{timestamp}",
            title=summary,
            summary=summary,
            source_type="chat",
            raw_text=raw_text,
            tags=args.get("tags", []),
        )
        return json.dumps({"stored": True, **result}, default=str)
    except Exception as exc:
        logger.warning("remember_this_failed", error=str(exc))
        return json.dumps({"stored": False, "error": str(exc)})


async def _exec_query_activities(pool: asyncpg.Pool, args: dict, ctx: ToolContext) -> str:
    active_only = args.get("active_only", True)
    limit = args.get("limit", 20)
    if active_only:
        rows = await pool.fetch(
            "SELECT id, name, schedule_cron, active, last_run_at, agent_id "
            "FROM activities WHERE active = TRUE ORDER BY name LIMIT $1",
            limit,
        )
    else:
        rows = await pool.fetch(
            "SELECT id, name, schedule_cron, active, last_run_at, agent_id "
            "FROM activities ORDER BY name LIMIT $1",
            limit,
        )
    return json.dumps([dict(r) for r in rows], default=str)


async def _exec_trigger_workflow(pool: asyncpg.Pool, args: dict, ctx: ToolContext) -> str:
    if not ctx.temporal_client:
        return json.dumps({"error": "Temporal client not available"})
    from aegis.services.workflows import trigger_workflow

    result = await trigger_workflow(
        ctx.temporal_client, args.get("workflow_type", ""), args.get("params")
    )
    return json.dumps(result, default=str)




async def _exec_get_market_regime(pool: asyncpg.Pool, args: dict, ctx: ToolContext) -> str:
    if not ctx.clickhouse_connector:
        return json.dumps({"error": "Market data not configured"})
    ch = ctx.clickhouse_connector

    equity_rows = await ch.query(
        "SELECT date, close, regime_label FROM index_statistics FINAL"
        " WHERE index_name = 'Nifty 50' ORDER BY date DESC LIMIT 1",
        {},
    )
    crypto_rows = await ch.query(
        "SELECT date, fgi_value, fgi_classification, total_market_cap_usd"
        " FROM crypto_global_market FINAL ORDER BY date DESC LIMIT 1",
        {},
    )

    equity = None
    if equity_rows:
        r = equity_rows[0]
        equity = {
            "regime": r.get("regime_label"),
            "index": "NIFTY 50",
            "date": r.get("date"),
            "close": r.get("close"),
        }

    crypto = None
    if crypto_rows:
        r = crypto_rows[0]
        crypto = {
            "fear_greed": r.get("fgi_value"),
            "fear_greed_label": r.get("fgi_classification"),
            "market_cap_usd": r.get("total_market_cap_usd"),
            "date": r.get("date"),
        }

    return json.dumps({"equity": equity, "crypto": crypto}, default=str)


async def _exec_get_top_forecasts(pool: asyncpg.Pool, args: dict, ctx: ToolContext) -> str:
    if not ctx.clickhouse_connector:
        return json.dumps({"error": "Market data not configured"})

    asset_class = args.get("asset_class", "")
    if asset_class not in ("equity", "crypto", "etf"):
        return json.dumps({"error": "asset_class must be one of: equity, crypto, etf"})

    halal_only = args.get("halal_only", False)
    direction = args.get("direction")
    count = min(int(args.get("count", 10)), 50)

    # Build SQL from hardcoded fragments — no user input interpolated into SQL
    dir_where = (
        "AND f.combined_forecast > 0"
        if direction == "long"
        else "AND f.combined_forecast < 0"
        if direction == "short"
        else ""
    )

    if asset_class == "equity":
        halal_where = "AND f.is_halal = 1" if halal_only else ""
        sql = (
            "SELECT f.nse_symbol AS symbol, f.date, f.combined_forecast, f.confidence,"
            " f.signal_agreement, f.close_price, f.vol_mixed AS volatility,"
            " f.target_weight, p.sharpe_21d"
            " FROM equity_forecasts AS f FINAL"
            " LEFT JOIN price_statistics AS p FINAL ON f.nse_symbol = p.nse_symbol AND f.date = p.date"
            " WHERE f.date = (SELECT max(date) FROM equity_forecasts)"
            f" {dir_where} {halal_where}"
            " ORDER BY abs(f.combined_forecast) DESC LIMIT {count:UInt32}"
        )
    elif asset_class == "crypto":
        sql = (
            "SELECT f.symbol, f.date, f.combined_forecast, f.prediction_confidence AS confidence,"
            " f.close_price, f.vol_mixed AS volatility, f.liquidity_risk_score,"
            " f.target_weight"
            " FROM crypto_forecasts AS f FINAL"
            " WHERE f.date = (SELECT max(date) FROM crypto_forecasts)"
            f" {dir_where}"
            " ORDER BY abs(f.combined_forecast) DESC LIMIT {count:UInt32}"
        )
    else:  # etf
        sql = (
            "SELECT f.symbol, f.date, f.combined_forecast, f.confidence,"
            " p.close AS last_close, p.vol_mixed AS volatility"
            " FROM etf_forecasts AS f FINAL"
            " LEFT JOIN etf_price_statistics AS p FINAL ON f.symbol = p.symbol AND f.date = p.date"
            " WHERE f.date = (SELECT max(date) FROM etf_forecasts)"
            f" {dir_where}"
            " ORDER BY abs(f.combined_forecast) DESC LIMIT {count:UInt32}"
        )

    rows = await ctx.clickhouse_connector.query(sql, {"count": count})
    return json.dumps(rows, default=str)


async def _exec_get_trade_decisions(pool: asyncpg.Pool, args: dict, ctx: ToolContext) -> str:
    if not ctx.clickhouse_connector:
        return json.dumps({"error": "Market data not configured"})
    ch = ctx.clickhouse_connector

    date = args.get("date")
    if not date:
        date_rows = await ch.query("SELECT max(data_date) AS max_date FROM trade_decisions", {})
        if date_rows and date_rows[0].get("max_date"):
            date = str(date_rows[0]["max_date"])
        else:
            return json.dumps([])

    rows = await ch.query(
        "SELECT symbol, asset_class, data_date, direction, target_weight,"
        " combined_forecast, confidence, selection_score, halal_status,"
        " regime_label, decision_status"
        " FROM trade_decisions FINAL"
        " WHERE data_date = {date:String}"
        " ORDER BY abs(combined_forecast) DESC",
        {"date": date},
    )
    return json.dumps(rows, default=str)


async def _exec_get_instrument_detail(pool: asyncpg.Pool, args: dict, ctx: ToolContext) -> str:
    if not ctx.clickhouse_connector:
        return json.dumps({"error": "Market data not configured"})
    symbol = args.get("symbol", "").strip().upper()
    if not symbol:
        return json.dumps({"error": "symbol is required"})
    ch = ctx.clickhouse_connector

    equity_rows = await ch.query(
        "SELECT f.nse_symbol, f.date, f.combined_forecast, f.confidence,"
        " f.signal_agreement, f.close_price, f.vol_mixed, f.target_weight,"
        " f.is_halal, f.regime_label, f.sector, f.industry,"
        " p.sharpe_21d, p.ewmac_16_64, p.ewmac_32_128, p.rsi_14d,"
        " i.accumulation_intensity, i.delivery_pct_zscore,"
        " fm.market_cap, fm.company_name"
        " FROM equity_forecasts AS f FINAL"
        " LEFT JOIN price_statistics AS p FINAL ON f.nse_symbol = p.nse_symbol AND f.date = p.date"
        " LEFT JOIN institutional_signals AS i FINAL ON f.nse_symbol = i.symbol AND f.date = i.date"
        " LEFT JOIN screener_fundamentals_meta AS fm FINAL ON f.nse_symbol = fm.nse_code"
        " WHERE f.nse_symbol = {symbol:String}"
        " ORDER BY f.date DESC LIMIT 1",
        {"symbol": symbol},
    )
    if equity_rows:
        return json.dumps(equity_rows[0], default=str)

    crypto_rows = await ch.query(
        "SELECT f.symbol, f.date, f.combined_forecast, f.prediction_confidence AS confidence,"
        " f.close_price, f.vol_mixed, f.liquidity_risk_score,"
        " f.target_weight, f.regime_label"
        " FROM crypto_forecasts AS f FINAL"
        " WHERE f.symbol = {symbol:String}"
        " ORDER BY f.date DESC LIMIT 1",
        {"symbol": symbol},
    )
    if crypto_rows:
        return json.dumps(crypto_rows[0], default=str)

    return json.dumps({"error": f"Instrument '{symbol}' not found in equity or crypto data"})


async def _exec_get_sector_overview(pool: asyncpg.Pool, args: dict, ctx: ToolContext) -> str:
    if not ctx.clickhouse_connector:
        return json.dumps({"error": "Market data not configured"})
    ch = ctx.clickhouse_connector
    rows = await ch.query(
        "SELECT sector, date, mean_z_score_20d, momentum_signal,"
        " mean_sharpe_21d, n_stocks, sector_rank"
        " FROM sector_statistics FINAL"
        " WHERE date = (SELECT max(date) FROM sector_statistics)"
        " ORDER BY sector_rank ASC",
        {},
    )
    return json.dumps(rows, default=str)


async def _exec_get_forecast_changes(pool: asyncpg.Pool, args: dict, ctx: ToolContext) -> str:
    if not ctx.clickhouse_connector:
        return json.dumps({"error": "Market data not configured"})
    asset_class = args.get("asset_class", "").strip().lower()
    if asset_class not in ("equity", "crypto"):
        return json.dumps({"error": "asset_class must be 'equity' or 'crypto'"})

    days = min(int(args.get("days", 5)), 30)
    threshold = float(args.get("threshold", 5.0))

    if asset_class == "equity":
        symbol_col = "nse_symbol"
        table = "equity_forecasts"
    else:
        symbol_col = "symbol"
        table = "crypto_forecasts"

    ch = ctx.clickhouse_connector
    rows = await ch.query(
        f"WITH latest AS ("
        f"  SELECT {symbol_col}, date, combined_forecast"
        f"  FROM {table} FINAL"
        f"  WHERE date = (SELECT max(date) FROM {table})"
        f"), prior AS ("
        f"  SELECT {symbol_col}, date, combined_forecast"
        f"  FROM {table} FINAL"
        f"  WHERE date = (SELECT max(date) FROM {table} WHERE date <= today() - {{days:UInt32}})"
        f") SELECT l.{symbol_col} AS symbol,"
        f"  l.combined_forecast AS current_forecast,"
        f"  p.combined_forecast AS prior_forecast,"
        f"  l.combined_forecast - p.combined_forecast AS change,"
        f"  l.date AS current_date, p.date AS prior_date"
        f" FROM latest l JOIN prior p ON l.{symbol_col} = p.{symbol_col}"
        f" WHERE abs(l.combined_forecast - p.combined_forecast) >= {{threshold:Float64}}"
        f" ORDER BY abs(l.combined_forecast - p.combined_forecast) DESC LIMIT 20",
        {"days": days, "threshold": threshold},
    )
    return json.dumps(rows, default=str)


async def _exec_research_topic(pool: asyncpg.Pool, args: dict, ctx: ToolContext) -> str:
    """Research a topic by combining KG data with fresh web search results."""
    if not ctx.search_connector or not ctx.llm_client:
        return json.dumps(
            {"error": "Search connector and LLM client are required for research_topic"}
        )

    query = args.get("query", "").strip()
    depth = args.get("depth", "quick")
    domains = args.get("domains") or []

    # Build web search query with optional domain restrictions
    web_query = query
    if domains:
        site_terms = " OR ".join(f"site:{d}" for d in domains)
        web_query = f"{query} ({site_terms})"

    search_limit = 20 if depth == "thorough" else 10

    # Parallel: KG search + web search
    kg_results: list[dict] = []
    web_results: list[dict] = []

    try:
        if ctx.knowledge_connector:
            kg_results = await ctx.knowledge_connector.search(query, limit=5)
    except Exception as exc:
        logger.warning("research_topic_kg_error", error=str(exc))

    try:
        web_results = await ctx.search_connector.search(web_query, limit=search_limit)
    except Exception as exc:
        logger.warning("research_topic_web_error", error=str(exc))

    # Build synthesis prompt
    kg_section = ""
    if kg_results:
        kg_lines = "\n".join(
            f"- {r.get('title', 'Unknown')}: {(r.get('summary') or r.get('text') or '')[:300]}"
            for r in kg_results[:5]
        )
        kg_section = f"## Knowledge Graph\n{kg_lines}\n\n"

    web_section = ""
    if web_results:
        web_lines = "\n".join(
            f"- {r.get('title', 'Unknown')} ({r.get('url', '')}): {r.get('content', '')[:300]}"
            for r in web_results[:10]
        )
        web_section = f"## Web Search Results\n{web_lines}\n\n"

    if not kg_section and not web_section:
        return json.dumps(
            {
                "synthesis": "No results found.",
                "sources": {"knowledge_graph": 0, "web_search": 0},
                "top_urls": [],
            }
        )

    prompt = (
        f"Synthesize the following research on: {query}\n\n"
        f"{kg_section}{web_section}"
        "Provide a concise, factual synthesis in 2-4 paragraphs. Focus on key findings, patterns, and actionable insights."
    )

    synthesis = ""
    try:
        result = await ctx.llm_client.think(prompt=prompt, model=ctx.model_light, max_tokens=600)
        synthesis = result.get("response", "")
    except Exception as exc:
        logger.warning("research_topic_synthesis_error", error=str(exc))
        synthesis = f"Research gathered {len(kg_results)} KG results and {len(web_results)} web results but synthesis failed."

    # Fire-and-forget: ingest synthesis into KG
    if ctx.knowledge_connector and synthesis:
        try:
            import time as _time

            asyncio.create_task(
                ctx.knowledge_connector.ingest_content(
                    url=f"aegis://research/{int(_time.time())}",
                    title=f"Research: {query}",
                    summary=synthesis,
                    source_type="research",
                    raw_text=synthesis,
                    tags=["research", "chat_tool"],
                )
            )
        except Exception:
            pass

    top_urls = [r.get("url", "") for r in web_results[:5] if r.get("url")]

    return json.dumps(
        {
            "synthesis": synthesis,
            "sources": {"knowledge_graph": len(kg_results), "web_search": len(web_results)},
            "top_urls": top_urls,
        },
        default=str,
    )


async def _exec_track_topic(pool: asyncpg.Pool, args: dict, ctx: ToolContext) -> str:
    """Subscribe to ongoing intelligence monitoring for a topic."""
    topic_name = args.get("topic_name", "").strip()
    queries = args.get("queries", [])
    priority = args.get("priority", "medium")

    if not topic_name or not queries:
        return json.dumps({"error": "topic_name and queries are required"})

    # Load current intelligence_topics from settings
    row = await pool.fetchrow("SELECT value FROM settings WHERE key = 'intelligence_topics'")
    existing_data: dict = {}
    if row and row["value"]:
        existing_data = row["value"] if isinstance(row["value"], dict) else {}

    topics: list[dict] = existing_data.get("topics", [])

    # Check if topic already exists
    status = "added"
    updated_topics = []
    found = False
    for t in topics:
        if t.get("name", "").lower() == topic_name.lower():
            updated_topics.append({"name": topic_name, "queries": queries, "priority": priority})
            status = "updated"
            found = True
        else:
            updated_topics.append(t)

    if not found:
        updated_topics.append({"name": topic_name, "queries": queries, "priority": priority})

    new_value = {"topics": updated_topics}
    await pool.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES ('intelligence_topics', $1, NOW()) "
        "ON CONFLICT (key) DO UPDATE SET value = $1, updated_at = NOW()",
        new_value,
    )

    return json.dumps(
        {
            "status": status,
            "topic": topic_name,
            "query_count": len(queries),
            "total_topics": len(updated_topics),
        }
    )


_TRIAGE_SETTING_KEYS = {
    "sentry_ignored_projects": "triage_sentry_ignored_projects",
    "email_ignored_domains": "triage_ignored_email_domains",
    "notification_mode": "triage_notification_mode",
    "burst_threshold": "triage_burst_threshold",
}
_TRIAGE_LIST_SETTINGS = {"sentry_ignored_projects", "email_ignored_domains"}


async def _exec_configure_triage(pool: asyncpg.Pool, args: dict, ctx: ToolContext) -> str:
    """Read or update triage configuration via settings table."""
    setting = args.get("setting", "")
    action = args.get("action", "")
    value = args.get("value")

    if setting not in _TRIAGE_SETTING_KEYS:
        return json.dumps({"error": f"Unknown setting: {setting}"})

    db_key = _TRIAGE_SETTING_KEYS[setting]
    row = await pool.fetchrow("SELECT value FROM settings WHERE key = $1", db_key)

    if action == "get":
        current = row["value"] if row else ([] if setting in _TRIAGE_LIST_SETTINGS else None)
        return json.dumps({"setting": setting, "current": current})

    if setting in _TRIAGE_LIST_SETTINGS:
        current = (row["value"] if row else None) or []
        if not isinstance(current, list):
            current = []
        if action == "add":
            if value is None:
                return json.dumps({"error": "value required for add"})
            item = str(value).strip()
            if item not in current:
                current = [*current, item]
        elif action == "remove":
            if value is None:
                return json.dumps({"error": "value required for remove"})
            current = [x for x in current if x != str(value).strip()]
        else:
            return json.dumps({"error": f"Use add/remove/get for list settings, not '{action}'"})
        new_val = current
    else:
        if action != "set":
            return json.dumps({"error": f"Use set/get for scalar settings, not '{action}'"})
        if value is None:
            return json.dumps({"error": "value required for set"})
        new_val = value

    await pool.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES ($1, $2, NOW()) "
        "ON CONFLICT (key) DO UPDATE SET value = $2, updated_at = NOW()",
        db_key,
        new_val,
    )
    return json.dumps({"ok": True, "setting": setting, "action": action, "current": new_val})


async def _exec_update_runbook(pool: asyncpg.Pool, args: dict, ctx: ToolContext) -> str:
    """Update or add operational runbook knowledge for alert types or projects."""
    if not ctx.knowledge_connector:
        return json.dumps({"error": "Knowledge service not available"})

    target = args.get("target", "")
    content = args.get("content", "")
    if not target or not content:
        return json.dumps({"error": "Both target and content are required"})

    # ponytail: runbook knowledge is stored as a searchable content chunk
    # (no knowledge graph). gather_alert_knowledge finds it via chunk search.
    try:
        await ctx.knowledge_connector.ingest_content(
            url=f"aegis://runbook/{target}",
            title=f"Runbook: {target}",
            source_type="runbook",
            raw_text=content,
            tags=["runbook", target],
        )
        return json.dumps({"ok": True, "target": target})
    except Exception as exc:
        logger.warning("update_runbook_failed", error=str(exc))
        return json.dumps({"ok": False, "error": str(exc)})


# --- GTD / Todoist tool executors (Phase 3) ---


async def _capture_to_inbox_impl(
    pool,
    source_tag: str,
    external_id: str,
    title: str,
    description: str | None,
    extra_labels: list[str] | None = None,
) -> str | None:
    """Thin wrapper that lets tests monkeypatch the capture core.

    In production this delegates to the same logic as
    CaptureActivities.capture_to_inbox; we keep the HTTP-facing service
    layer decoupled from the worker activity module so chat-tool calls
    don't pull worker imports into Core.

    `extra_labels` are appended to the `[source_tag]` label set (dedup-
    preserving) — used to assign a captured task to an agent (e.g.
    `@pandora`) so it anchors that agent's downstream workflows.
    """
    from aegis.connectors.todoist import TodoistConnector

    if pool is None:
        return None
    async with pool.acquire() as conn:
        kill = await conn.fetchval(
            "SELECT value FROM settings WHERE key = 'todoist_capture_enabled'"
        )
        if kill is False or (isinstance(kill, dict) and kill.get("value") is False):
            return None
        managed = await conn.fetchval(
            "SELECT value FROM settings WHERE key = 'todoist_managed_project_ids'"
        )
        inbox_id = (managed or {}).get("inbox") if isinstance(managed, dict) else None
        if not inbox_id:
            return None
        inserted = await conn.fetchval(
            "INSERT INTO todoist_capture_idempotency (source_tag, external_id) "
            "VALUES ($1,$2) ON CONFLICT DO NOTHING RETURNING captured_at",
            source_tag,
            external_id,
        )
        if inserted is None:
            existing = await conn.fetchval(
                "SELECT todoist_task_ref FROM todoist_capture_idempotency "
                "WHERE source_tag=$1 AND external_id=$2",
                source_tag,
                external_id,
            )
            return existing

    from aegis.config import Settings

    settings = Settings()
    _tk = await resolve_todoist_api_key(pool, settings)
    if not _tk:
        return None
    connector = TodoistConnector(api_key=_tk, db_pool=pool, timeout=10.0)
    item_labels = [source_tag]
    for lbl in extra_labels or []:
        if lbl and lbl not in item_labels:
            item_labels.append(lbl)
    cmd = TodoistConnector.build_create_item_command(
        project_id=inbox_id,
        content=title[:120],
        description=description,
        labels=item_labels,
    )
    result = await connector.commands([cmd])
    status = TodoistConnector.check_sync_status(result, [cmd["uuid"]])
    ref: str | None = None
    if status["ok"]:
        mapping = (result.get("data") or {}).get("temp_id_mapping", {}) or {}
        ref = mapping.get(cmd["temp_id"])
    elif status["retryable"] or status["rejected_retryable"]:
        # Transient failure — queue for drain_outbox to retry.
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO todoist_outbox (temp_id, command, status) "
                "VALUES ($1,$2,'pending') ON CONFLICT (temp_id) DO NOTHING",
                cmd["temp_id"],
                cmd,
            )
        ref = cmd["temp_id"]
    # Permanent rejection (ITEM_NOT_FOUND / INVALID_ARGUMENT etc.) leaves
    # ref=None so the idempotency row keeps todoist_task_ref NULL — the
    # caller surfaces "no ref" to the user instead of poisoning the outbox.
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE todoist_capture_idempotency SET todoist_task_ref=$1 "
            "WHERE source_tag=$2 AND external_id=$3",
            ref,
            source_tag,
            external_id,
        )
    return ref


async def _exec_capture_to_inbox(pool: asyncpg.Pool, args: dict, ctx: ToolContext) -> str:
    text = (args.get("text") or "").strip()
    if not text:
        return "Refused: empty text"
    source = args.get("source") or "chat"
    description = args.get("description")
    # Deterministic external id from (agent, text) so identical re-asks
    # dedupe; including agent_id keeps separate personalities independent.
    import hashlib

    agent = (ctx.agent_id if ctx else None) or "chat"
    ext_id = f"chat:{agent}:{hashlib.sha256(text.encode()).hexdigest()[:16]}"
    ref = await _capture_to_inbox_impl(
        pool=pool,
        source_tag=f"#{source}",
        external_id=ext_id,
        title=text,
        description=description,
    )
    if ref is None:
        return "Capture skipped (kill switch off, missing inbox, or no api key)"
    return f"Captured: {ref}"


async def _exec_list_next_actions(pool: asyncpg.Pool, args: dict, ctx: ToolContext) -> str:
    assignee = args.get("assignee")
    context = args.get("context")
    limit = int(args.get("limit") or 25)
    where = ["NOT t.is_completed"]
    params: list[object] = []
    if assignee:
        params.append(assignee)
        where.append(f"t.assignee_label = ${len(params)}")
    if context:
        params.append(context)
        where.append(f"${len(params)} = ANY(t.labels)")
    if pool is None:
        return "No DB pool"
    async with pool.acquire() as conn:
        inbox_id = await conn.fetchval(
            "SELECT value->>'inbox' FROM settings WHERE key='todoist_managed_project_ids'"
        )
        if inbox_id:
            params.append(inbox_id)
            where.append(f"t.project_id <> ${len(params)}")
        params.append(limit)
        sql = (
            "SELECT t.id, t.content, t.assignee_label, t.labels, t.due_date "
            "FROM todoist_tasks t "
            f"WHERE {' AND '.join(where)} "
            "ORDER BY COALESCE(t.due_date,'9999-12-31'::date), t.updated_at DESC "
            f"LIMIT ${len(params)}"
        )
        rows = await conn.fetch(sql, *params)
    if not rows:
        return "No matching next actions."
    lines = []
    for r in rows:
        due = f" due {r['due_date'].isoformat()}" if r["due_date"] else ""
        lines.append(f"- [{r['id']}] {r['content']} ({r['assignee_label'] or '@me'}){due}")
    return "\n".join(lines)


async def _exec_whats_next(pool: asyncpg.Pool, args: dict, ctx: ToolContext) -> str:
    if pool is None:
        return "No DB pool"
    minutes = args.get("minutes")
    energy = (args.get("energy") or "").lower()
    # ponytail: tiny inline minutes/energy->context map; not worth a module.
    contexts: list[str] = []
    if minutes is not None and int(minutes) <= 5:
        contexts = ["@5min"]
    elif energy == "low":
        contexts = ["@5min", "@email", "@reading"]
    elif energy == "high":
        contexts = ["@deep", "@code"]
    where = [
        "NOT t.is_completed",
        "(t.assignee_label='@me' OR t.assignee_label IS NULL)",
        # State labels mirror aegis_worker.activities.review._STATE_LABELS (cross-package; keep in sync).
        "NOT (t.labels && ARRAY['@waiting','@reference','@to-read'])",
    ]
    params: list = []
    async with pool.acquire() as conn:
        managed = await conn.fetchval(
            "SELECT value FROM settings WHERE key='todoist_managed_project_ids'"
        )
        exclude = []
        if isinstance(managed, dict):
            exclude = [e for e in (managed.get("inbox"), managed.get("someday")) if e]
        if exclude:
            params.append(exclude)
            where.append(
                f"(t.project_id IS NULL OR t.project_id <> ALL(${len(params)}::text[]))"
            )
        if contexts:
            params.append(contexts)
            where.append(f"t.labels && ${len(params)}::text[]")
        params.append(int(args.get("limit") or 5))
        sql = (
            "SELECT t.id, t.content, t.due_date FROM todoist_tasks t "
            f"WHERE {' AND '.join(where)} "
            "ORDER BY (t.due_date IS NULL), t.due_date ASC, "
            "t.priority DESC NULLS LAST, t.updated_at DESC "
            f"LIMIT ${len(params)}"
        )
        rows = await conn.fetch(sql, *params)
    if not rows:
        return "Nothing queued that fits — inbox may be clear or everything's @waiting."
    lines = []
    for r in rows:
        due = f" (due {r['due_date'].isoformat()})" if r["due_date"] else ""
        lines.append(f"- [{r['id']}] {r['content']}{due}")
    return "\n".join(lines)


async def _exec_list_projects(pool: asyncpg.Pool, args: dict, ctx: ToolContext) -> str:
    """List work-stream `project/*` labels with open-task counts."""
    if pool is None:
        return "No DB pool"
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT l.name, "
            "  (SELECT count(*) FROM todoist_tasks t "
            "   WHERE l.name = ANY(t.labels) AND NOT t.is_completed) AS open_n "
            "FROM todoist_labels l "
            "WHERE l.name LIKE 'project/%' "
            "ORDER BY l.name"
        )
    if not rows:
        return "No project labels."
    return "\n".join(f"- {r['name']} ({r['open_n']})" for r in rows)


async def _stage_chat_tool_outbox(
    pool: asyncpg.Pool | None,
    commands: list[dict],
    status: dict,
    op: str,
) -> str | None:
    """Inspect a `check_sync_status()` envelope for a chat-tool command batch.

    Three outcomes:
    - Status OK → returns None; caller proceeds to its success path.
    - Failure is retryable (envelope 5xx-class OR per-cmd transient rejection)
      → stage each command in `todoist_outbox` and return a user-facing
      "queued for retry" string so the user can stop waiting on the chat
      reply.
    - Failure is permanent (envelope 4xx OR per-cmd ITEM_NOT_FOUND etc.)
      → return a user-facing "Todoist error" string. No outbox stage —
      replaying a malformed command just burns retries.

    Matches the outbox-queue contract that `_capture_to_inbox_impl`,
    `CaptureActivities.capture_to_inbox`, and `ClarifyActivities.apply_outcome`
    already use, so transient Todoist outages don't silently drop user
    intent across any code path.
    """
    if status["ok"]:
        return None
    if status["retryable"] or status["rejected_retryable"]:
        if pool is None:
            return f"Todoist transient error ({op}); no pool to queue retry"
        import uuid as _uuid

        async with pool.acquire() as conn:
            for cmd in commands:
                temp_id = cmd.get("temp_id") or f"chattool-{op}-{_uuid.uuid4()}"
                await conn.execute(
                    "INSERT INTO todoist_outbox (temp_id, command, status) "
                    "VALUES ($1, $2, 'pending') ON CONFLICT (temp_id) DO NOTHING",
                    temp_id,
                    cmd,
                )
        logger.warning(
            "chat_tool_outbox_queued",
            op=op,
            count=len(commands),
            envelope_error=status["envelope_error"],
        )
        return f"Todoist hiccup ({op}); queued for retry"
    return f"Todoist error ({op}): {status['envelope_error'] or status['rejected']}"


async def _exec_complete_task(pool: asyncpg.Pool, args: dict, ctx: ToolContext) -> str:
    import uuid as _uuid

    from aegis.config import Settings
    from aegis.connectors.todoist import TodoistConnector

    task_id = (args.get("task_id") or "").strip()
    note_text = args.get("note")
    if not task_id:
        return "Refused: task_id required"
    settings = Settings()
    _tk = await resolve_todoist_api_key(pool, settings)
    if not _tk:
        return "Todoist not configured"
    connector = TodoistConnector(api_key=_tk, db_pool=pool, timeout=10.0)
    commands = [
        {"type": "item_complete", "uuid": str(_uuid.uuid4()), "args": {"id": task_id}},
    ]
    if note_text:
        commands.append(TodoistConnector.build_note_add_command(task_id, note_text))
    result = await connector.commands(commands)
    status = TodoistConnector.check_sync_status(result, [c["uuid"] for c in commands])
    fail_msg = await _stage_chat_tool_outbox(pool, commands, status, "complete_task")
    if fail_msg is not None:
        return fail_msg
    return f"Completed {task_id}"


async def _exec_defer_task(pool: asyncpg.Pool, args: dict, ctx: ToolContext) -> str:
    from aegis.config import Settings
    from aegis.connectors.todoist import TodoistConnector

    task_id = (args.get("task_id") or "").strip()
    until = (args.get("until") or "").strip()
    if not task_id or not until:
        return "Refused: task_id and until required"
    settings = Settings()
    _tk = await resolve_todoist_api_key(pool, settings)
    if not _tk:
        return "Todoist not configured"
    connector = TodoistConnector(api_key=_tk, db_pool=pool, timeout=10.0)
    # Todoist accepts natural-language strings under args.due.string
    cmd = TodoistConnector.build_item_update_command(task_id, due={"string": until})
    result = await connector.commands([cmd])
    status = TodoistConnector.check_sync_status(result, [cmd["uuid"]])
    fail_msg = await _stage_chat_tool_outbox(pool, [cmd], status, "defer_task")
    if fail_msg is not None:
        return fail_msg
    return f"Deferred {task_id} until {until}"


async def _exec_mark_waiting(pool: asyncpg.Pool, args: dict, ctx: ToolContext) -> str:
    from aegis.config import Settings
    from aegis.connectors.todoist import TodoistConnector

    task_id = (args.get("task_id") or "").strip()
    who = (args.get("who") or "").strip()
    expected = args.get("expected_by")
    if not task_id or not who:
        return "Refused: task_id and who required"
    if pool is None:
        return "No DB pool"
    async with pool.acquire() as conn:
        existing_labels = await conn.fetchval(
            "SELECT labels FROM todoist_tasks WHERE id=$1", task_id
        )
    if existing_labels is None:
        return f"Unknown task {task_id}"
    settings = Settings()
    _tk = await resolve_todoist_api_key(pool, settings)
    if not _tk:
        return "Todoist not configured"
    connector = TodoistConnector(api_key=_tk, db_pool=pool, timeout=10.0)
    new_labels = list({*(existing_labels or []), "@waiting"})
    note_body = f"Waiting on {who}" + (f" (expected by {expected})" if expected else "")
    commands = [
        TodoistConnector.build_item_update_command(task_id, labels=new_labels),
        TodoistConnector.build_note_add_command(task_id, note_body),
    ]
    result = await connector.commands(commands)
    status = TodoistConnector.check_sync_status(result, [c["uuid"] for c in commands])
    fail_msg = await _stage_chat_tool_outbox(pool, commands, status, "mark_waiting")
    if fail_msg is not None:
        return fail_msg
    return f"Marked {task_id} waiting on {who}"


async def _exec_handoff_task(pool: asyncpg.Pool, args: dict, ctx: ToolContext) -> str:
    from aegis.config import Settings
    from aegis.connectors.todoist import TodoistConnector

    task_id = (args.get("task_id") or "").strip()
    to_assignee = (args.get("to_assignee") or "").strip()
    if not task_id or to_assignee not in {"@me", "@sebas", "@raphael", "@maou", "@pandora"}:
        return "Refused: valid task_id + to_assignee required"
    if pool is None:
        return "No DB pool"
    async with pool.acquire() as conn:
        existing_labels = await conn.fetchval(
            "SELECT labels FROM todoist_tasks WHERE id=$1", task_id
        )
    if existing_labels is None:
        return f"Unknown task {task_id}"
    # Strip any existing @assignee, add the new one
    kept = [
        lab
        for lab in (existing_labels or [])
        if lab not in {"@me", "@sebas", "@raphael", "@maou", "@pandora"}
    ]
    new_labels = [*kept, to_assignee]
    settings = Settings()
    _tk = await resolve_todoist_api_key(pool, settings)
    if not _tk:
        return "Todoist not configured"
    connector = TodoistConnector(api_key=_tk, db_pool=pool, timeout=10.0)
    cmd = TodoistConnector.build_item_update_command(task_id, labels=new_labels)
    result = await connector.commands([cmd])
    status = TodoistConnector.check_sync_status(result, [cmd["uuid"]])
    fail_msg = await _stage_chat_tool_outbox(pool, [cmd], status, "handoff_task")
    if fail_msg is not None:
        return fail_msg
    return f"Handed off {task_id} to {to_assignee}"


async def _exec_find_reference(pool: asyncpg.Pool, args: dict, ctx: ToolContext) -> str:
    """Two sources: tasks labeled @reference + knowledge-service semantic
    search filtered to source_type='reference'.

    Phase 5: KS gains a real reference corpus when ClarifyFlow classifies
    items as 'reference' (ingest_reference_to_ks pushes body + URL + tags).
    This tool searches THAT corpus, with a Todoist title ILIKE fallback
    for items not yet ingested. Post-GTD-restructure the Todoist query
    is by @reference label, not project_id.
    """
    query = (args.get("query") or "").strip()
    limit = int(args.get("limit") or 10)
    if not query:
        return "Refused: empty query"
    out: list[str] = []
    if pool is not None:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, content FROM todoist_tasks "
                "WHERE '@reference' = ANY(labels) "
                "AND NOT is_completed "
                "AND content ILIKE $1 "
                "ORDER BY updated_at DESC LIMIT $2",
                f"%{query}%",
                limit,
            )
            for r in rows:
                out.append(f"- [reference:{r['id']}] {r['content']}")
    # KS pass — semantic search the reference corpus directly.
    if ctx.knowledge_connector:
        try:
            ks_results = await ctx.knowledge_connector.search(
                query,
                limit=limit,
                source_type="reference",
            )
            if ks_results:
                out.append("Semantic matches (Reference KB):")
                for item in ks_results[:limit]:
                    title = (item.get("title") or "").strip()[:120]
                    score = item.get("score") or item.get("similarity") or 0.0
                    cid = item.get("content_id") or item.get("id") or ""
                    out.append(f"- [{cid}] {title} (score={score:.2f})")
        except Exception as exc:  # noqa: BLE001
            logger.warning("find_reference_ks_failed", error=str(exc)[:200])
    if not out:
        return "No reference matches."
    return "\n".join(out)


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


# --- Vercel read-only tools (Pandora) ---


# `vercel-<name>` slugs in the resources table strip to the bare Vercel project
# name, which is what the v9/projects/{id_or_name} endpoint expects.
def _normalize_vercel_project(value: str) -> str:
    value = (value or "").strip()
    if value.startswith("vercel-"):
        return value[len("vercel-") :]
    return value


async def _exec_vercel_get_project(pool: asyncpg.Pool, args: dict, ctx: ToolContext) -> str:
    if not ctx.vercel_connector:
        return json.dumps({"error": "vercel_connector_not_configured"})
    project = _normalize_vercel_project(args.get("project", ""))
    if not project:
        return json.dumps({"error": "project is required"})
    result = await ctx.vercel_connector.get_project(project)
    return json.dumps(result)


async def _exec_vercel_list_deployments(pool: asyncpg.Pool, args: dict, ctx: ToolContext) -> str:
    if not ctx.vercel_connector:
        return json.dumps({"error": "vercel_connector_not_configured"})
    project = _normalize_vercel_project(args.get("project", ""))
    if not project:
        return json.dumps({"error": "project is required"})
    limit = int(args.get("limit", 10))
    since_hours = args.get("since_hours")
    if since_hours is not None:
        try:
            since_hours = int(since_hours)
        except (TypeError, ValueError):
            return json.dumps({"error": "since_hours must be an integer"})
    state = args.get("state")
    result = await ctx.vercel_connector.list_deployments(
        project, limit=limit, since_hours=since_hours, state=state
    )
    return json.dumps(result)


async def _exec_vercel_get_deployment(pool: asyncpg.Pool, args: dict, ctx: ToolContext) -> str:
    if not ctx.vercel_connector:
        return json.dumps({"error": "vercel_connector_not_configured"})
    deployment_id = (args.get("deployment_id") or "").strip()
    if not deployment_id:
        return json.dumps({"error": "deployment_id is required"})
    result = await ctx.vercel_connector.get_deployment(deployment_id)
    return json.dumps(result)


async def _exec_vercel_get_build_logs(pool: asyncpg.Pool, args: dict, ctx: ToolContext) -> str:
    if not ctx.vercel_connector:
        return json.dumps({"error": "vercel_connector_not_configured"})
    deployment_id = (args.get("deployment_id") or "").strip()
    if not deployment_id:
        return json.dumps({"error": "deployment_id is required"})
    limit = int(args.get("limit", 100))
    errors_only = bool(args.get("errors_only", False))
    result = await ctx.vercel_connector.get_build_logs(
        deployment_id, limit=limit, errors_only=errors_only
    )
    return json.dumps(result)


# --- Dispatch dict mapping tool names to executor functions ---

TOOL_EXECUTORS: dict[str, Any] = {
    "search_knowledge": _exec_search_knowledge,
    "ask_knowledge": _exec_ask_knowledge,
    "remember_this": _exec_remember_this,
    "query_activities": _exec_query_activities,
    "trigger_workflow": _exec_trigger_workflow,
    "get_market_regime": _exec_get_market_regime,
    "get_top_forecasts": _exec_get_top_forecasts,
    "get_trade_decisions": _exec_get_trade_decisions,
    "get_instrument_detail": _exec_get_instrument_detail,
    "get_sector_overview": _exec_get_sector_overview,
    "get_forecast_changes": _exec_get_forecast_changes,
    "research_topic": _exec_research_topic,
    "track_topic": _exec_track_topic,
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
    "list_argocd_apps": _exec_list_argocd_apps,
    "sync_argocd_app": _exec_sync_argocd_app,
    "run_infra_script": _exec_run_infra_script,
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
    "find_reference": _exec_find_reference,
    # Vercel read-only (Pandora) — see PR for design notes.
    "vercel_get_project": _exec_vercel_get_project,
    "vercel_list_deployments": _exec_vercel_list_deployments,
    "vercel_get_deployment": _exec_vercel_get_deployment,
    "vercel_get_build_logs": _exec_vercel_get_build_logs,
}

# --- Per-agent tool sets ---
# Each agent only sees tools relevant to their domain.
# Unknown agents fall back to Sebas (coordinator = catch-all).

AGENT_TOOL_SETS: dict[str, set[str]] = {
    "sebas": {
        "query_activities",
        "trigger_workflow",
        "search_knowledge",
        "configure_triage",
        "remember_this",
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
        "find_reference",
    },
    "raphael": {
        "search_knowledge",
        "ask_knowledge",
        "research_topic",
        "track_topic",
        "remember_this",
        # Phase 3 GTD tools (research-leaning subset)
        "capture_to_inbox",
        "list_next_actions",
        "list_projects",
        "complete_task",
        "handoff_task",
        "find_reference",
    },
    "pandoras-actor": {
        "trigger_workflow",
        "search_knowledge",
        "update_runbook",
        "configure_triage",
        "remember_this",
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
        "list_argocd_apps",
        "sync_argocd_app",
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
    },
    "maou": {
        "get_market_regime",
        "get_top_forecasts",
        "get_trade_decisions",
        "get_instrument_detail",
        "get_sector_overview",
        "get_forecast_changes",
        "search_knowledge",
        "remember_this",
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
    },
}


def _get_agent_tools(agent_id: str, metadata: dict | None = None) -> list[dict]:
    """Return CHAT_TOOLS filtered to the agent's allowed tool set.

    Tool set is data-driven from agents.metadata.tool_set when present, falling
    back to the shipped AGENT_TOOL_SETS (then sebas) for the example agents.
    """
    allowed = (metadata or {}).get("tool_set")
    if not allowed:
        allowed = AGENT_TOOL_SETS.get(agent_id) or AGENT_TOOL_SETS["sebas"]
    allowed = set(allowed)
    return [t for t in CHAT_TOOLS if t["function"]["name"] in allowed]


def _validate_agent_tool_sets() -> None:
    """Boot-time check: every tool name in AGENT_TOOL_SETS has an executor.

    Raises RuntimeError on orphan references so the process refuses to start.
    Logs a warning for executors that are not referenced by any agent — those
    are soft-dead (kept for future use or in-flight deprecation).
    """
    declared: set[str] = set()
    for agent_id, tools in AGENT_TOOL_SETS.items():
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


_computed_personality_dir = os.environ.get("AEGIS_PERSONALITY_DIR") or str(
    Path(__file__).resolve().parents[4] / "personalities"
)
PERSONALITY_DIR = (
    _computed_personality_dir if Path(_computed_personality_dir).is_dir() else "/app/personalities"
)


def _persona_section(db_value: str | None, agent_dir: Path, filename: str) -> str:
    """A persona section, DB-first (agents column) with the .md file as fallback
    (the file is the first-boot seed; DB wins once populated / UI-edited)."""
    if db_value and db_value.strip():
        return db_value.strip()
    f = agent_dir / filename
    if f.exists():
        return f.read_text().strip()
    return ""


def _build_agent_system_prompt(
    agent_id: str,
    fallback: str,
    tool_descriptions: str | None = None,
    persona: dict | None = None,
) -> str:
    """Build a structured system prompt from the agent's persona.

    Persona prose is read DB-first (the agents.{soul,operating_notes,user_context}
    columns via `persona`) and falls back to personalities/<id>/{SOUL,AGENTS,USER}.md.
    Returns `fallback` (the DB system_prompt) when nothing is available.
    """
    persona = persona or {}
    agent_dir = Path(PERSONALITY_DIR) / agent_id

    soul = _persona_section(persona.get("soul"), agent_dir, "SOUL.md")
    ops = _persona_section(persona.get("operating_notes"), agent_dir, "AGENTS.md")
    usr = _persona_section(persona.get("user_context"), agent_dir, "USER.md")

    sections: list[str] = []
    if soul:
        sections.append(f"## Identity\n\n{soul}")
    if ops:
        sections.append(f"## Operational Boundaries\n\n{ops}")
    if usr:
        sections.append(f"## User Context\n\n{usr}")

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




_KNOWN_AGENT_IDS = {"sebas", "raphael", "pandoras-actor", "maou"}

AGENT_KNOWLEDGE_DOMAINS: dict[str, list[str]] = {
    "sebas": ["task", "decision", "briefing", "digest", "calendar", "task_outcome"],
    "raphael": ["article", "feed", "email", "research"],
    "pandoras-actor": ["alert", "sentry", "github", "task_outcome"],
    "maou": ["market", "finance", "trade"],
}

def _extract_query_entities(message: str) -> list[str]:
    """Extract likely entity terms from a message. Lightweight, no NLP."""
    import re

    entities: list[str] = []

    # Quoted strings
    for match in re.findall(r'"([^"]+)"', message):
        if len(match) > 2:
            entities.append(match)

    # Known agent IDs
    lower = message.lower()
    for aid in _KNOWN_AGENT_IDS:
        if aid in lower:
            entities.append(aid)

    # Capitalized multi-word phrases (2+ words starting with uppercase)
    for match in re.findall(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b", message):
        if match not in entities:
            entities.append(match)

    return entities[:2]


# --- Knowledge decay ---

DECAY_WINDOWS = {
    "chat": 30,
    "task_outcome": 60,
    "triage": 90,
    "content": 180,
    "manual": 365,
}
DEFAULT_DECAY_WINDOW = 90


def _apply_knowledge_decay(items: list[dict]) -> list[dict]:
    """Apply time-based decay to knowledge items based on source type.

    When days_since_referenced is unknown, assume item is fresh (0 days).
    Decay is only meaningful when age data is available from the knowledge store.
    """
    for item in items:
        source_type = item.get("source_type", "unknown")
        decay_window = DECAY_WINDOWS.get(source_type, DEFAULT_DECAY_WINDOW)
        # Default to 0 (fresh) when age is unknown — don't penalize items without age data
        days = item.get("days_since_referenced", 0)
        decay_factor = max(0.1, 1.0 - (days / decay_window))
        # similarity can be None (BM25-only chunks from knowledge-service);
        # coerce so the multiply doesn't break.
        item["effective_score"] = (item.get("similarity") or 0) * decay_factor
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
    db_pool: Any = None,
) -> tuple[str | None, list[dict]]:
    """Search knowledge base for context relevant to the user's message.

    Semantic chunk search only (no knowledge graph). Never raises.
    Returns (formatted_context_string, injected_items_metadata).
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

        # Agent-scoped boosting
        domains = (
            knowledge_domains
            if knowledge_domains is not None
            else AGENT_KNOWLEDGE_DOMAINS.get(agent_id or "", [])
        )
        for r in results:
            boost = 0.2 if r.get("source_type") in domains else 0.0
            r["_score"] = (r.get("similarity") or 0) + boost

        # Apply time-based decay (sets effective_score)
        results = _apply_knowledge_decay(results)

        # Load per-source thresholds from DB
        source_thresholds: dict[str, float] = {}
        if db_pool:
            try:
                rows = await db_pool.fetch(
                    "SELECT source_type, auto_confidence FROM knowledge_source_quality"
                )
                source_thresholds = {r["source_type"]: r["auto_confidence"] for r in rows}
            except Exception as exc:
                logger.warning("knowledge_source_quality_lookup_failed", error=str(exc))

        # Filter by per-source threshold (fallback to score_threshold).
        filtered = []
        for r in results:
            st = r.get("source_type", "unknown")
            threshold = source_thresholds.get(st, score_threshold)
            score = r.get("effective_score") or r.get("_score") or r.get("similarity") or 0
            if score >= threshold:
                filtered.append(r)
        results = filtered

        if not results:
            return (None, [])

        # Sort by effective_score for final ranking
        results.sort(
            key=lambda r: r.get("effective_score") or r.get("_score") or 0,
            reverse=True,
        )

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
        logger.warning("knowledge_context_error", error=str(exc))
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
    clickhouse_connector: Any = None,
    search_connector: Any = None,
    remote_script_connector: Any = None,
    vercel_connector: Any = None,
    background_tasks: set[asyncio.Task] | None = None,
    user_metadata: dict | None = None,
) -> dict[str, Any]:
    """Send a message to an agent with tool calling support.

    `user_metadata` (optional): JSON-serialisable dict written to the
    user chat_history row's metadata column — used by the Telegram bot
    to record `chat_id` + `telegram_message_id` on the user's incoming
    turn so the 30-day cleanup activity can deleteMessage from
    Telegram later.

    Response includes `assistant_message_id` so the caller can patch the
    assistant row's metadata with the OUTGOING Telegram message_id
    after the reply lands.
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

    # v3 agents table has `system_prompt_path` (file path); personality files
    # are loaded by `_build_agent_system_prompt` below. Empty fallback is only
    # used if the personality dir is missing on disk.
    system_prompt = ""

    # Proactive knowledge context is injected once, after the personality
    # prompt is built (see below) — building the prompt overwrites
    # `system_prompt`, so appending here would be discarded.
    injected_items: list[dict] = []

    # Load recent history. role='dispatch' rows are outbound Telegram
    # messages the user saw (briefings, interaction cards, alert notices)
    # — fold them in as assistant turns with a [Sent to you on Telegram]
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
                    "content": f"[Sent to you on Telegram]\n{content}",
                }
            )
        elif role in {"user", "assistant", "system", "tool"}:
            history.append({"role": role, "content": content})

    if not llm_client:
        return {"error": "LLM not available", "response": ""}

    # Config
    # Resolve per-agent model via `agents.model_tier` → config/models.yaml.
    # Falls back to 'balanced' tier for unknown agents.
    model = await resolve_model_for_agent(pool, agent_id) if pool else "qwen3:14b"
    tools_enabled = getattr(settings, "tool_calling_enabled", True) if settings else True
    max_iter = getattr(settings, "tool_max_iterations", 5) if settings else 5
    max_bytes = getattr(settings, "tool_result_max_bytes", 4096) if settings else 4096
    timeout = getattr(settings, "tool_timeout_seconds", 30) if settings else 30

    # Build agent-specific tool list and structured prompt
    agent_tools = _get_agent_tools(agent_id, metadata=agent_meta) if tools_enabled else []

    # Tool-calling routing: the smart-tier models (claude-haiku/sonnet/opus)
    # are served via max-proxy (Claude-Code-subscription bridge), which
    # silently strips the `tools` array from the upstream request — the model
    # never sees the tool definitions and responds in plain text (often
    # hallucinating that no tools are available). qwen3:14b via ollama_chat
    # IS function-calling-capable, so swap the resolved model for qwen3:14b
    # whenever the agent actually has tools to call. Reasoning quality on
    # synthesis-heavy chat takes a hit but this is the only way tool calls
    # actually fire today. See cmemory lesson — empty chat_tool_calls table
    # for 7d across all agents was the diagnostic signature.
    if tools_enabled and agent_tools and model in _TOOL_INCAPABLE_MODELS:
        logger.info(
            "chat_model_substituted_for_tools",
            agent_id=agent_id,
            from_model=model,
            to_model=_TOOL_FALLBACK_MODEL,
            tool_count=len(agent_tools),
        )
        model = _TOOL_FALLBACK_MODEL

    tool_desc_lines = [
        f"- {t['function']['name']}: {t['function']['description']}" for t in agent_tools
    ]
    tool_desc = "\n".join(tool_desc_lines) if tool_desc_lines else None

    system_prompt = _build_agent_system_prompt(
        agent_id,
        fallback=system_prompt,
        tool_descriptions=tool_desc,
        persona={
            "soul": agent.get("soul"),
            "operating_notes": agent.get("operating_notes"),
            "user_context": agent.get("user_context"),
        },
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
            db_pool=pool,
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
        clickhouse_connector=clickhouse_connector,
        chat_context={"user_message": message, "thread_id": thread_id},
        settings=settings,
        temporal_client=temporal_client,
        search_connector=search_connector,
        llm_client=llm_client,
        remote_script_connector=remote_script_connector,
        vercel_connector=vercel_connector,
        model_light=getattr(settings, "model_fast", "gemma4:e2b"),
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
                        timeout=timeout,
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
                    tool_result = json.dumps(
                        {"error": f"Tool '{_tc_name}' timed out after {timeout}s"}
                    )
                    tool_status = "timeout"
                except Exception as exc:
                    tool_result = json.dumps({"error": str(exc)})
                    tool_status = "error"

                tool_latency = int((time.monotonic() - tool_start) * 1000)

                # Truncate result
                tool_result = _truncate_result(tool_result, max_bytes=max_bytes)

                messages.append({"role": "tool", "tool_call_id": _tc_id, "content": tool_result})
                tool_calls_made.append({"name": _tc_name, "args": args})
                logger.info(
                    "chat_tool_executed",
                    tool=_tc_name,
                    agent=agent_id,
                    status=tool_status,
                    latency_ms=tool_latency,
                )

                # Record observability
                try:
                    result_dict = json.loads(tool_result)
                except (json.JSONDecodeError, TypeError):
                    result_dict = {"raw": tool_result[:500]}
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
                logger.warning("chat_final_no_tools_failed", error=str(exc))
                response = ""
            if not response:
                response = (
                    "I wasn't able to complete that — could you rephrase "
                    "or narrow it down?"
                )

    except Exception as exc:
        logger.error("chat_llm_failed", error=str(exc))
        return {"error": str(exc), "response": ""}

    # Save to history. User row may carry the incoming Telegram message_id
    # via `user_metadata` so the cleanup activity can deleteMessage it later.
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
                error=str(exc),
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
    temporal_client: Any = None,
    remote_script_connector: Any = None,
) -> dict:
    """Non-Telegram chat entry point for two surfaces:

    - Todoist comment channel (task_id is set) — invoked by AgentChatReplyFlow
      after ClarifyFlow's per-agent short-circuit fires.
    - Telegram DM @mention (task_id is None) — invoked by the bot via the
      `/api/chat/agent-reply/trigger` route. Same agent, same tools, no
      Todoist anchor.

    Reuses send_message so the agent personality, tool surface, and
    chat-history persistence all behave identically to a Telegram chat —
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
        "surface": "telegram_dm" if task_id is None else "todoist_comment",
    }
    if task_id is not None:
        user_metadata["task_id"] = task_id
    # send_message handles auth/personality/tooling/history. Any non-transient
    # failure (agent not found, refusal) lands in the returned dict's "error"
    # field. Transient failures raise.
    resp = await send_message(
        pool=pool,
        llm_client=llm_client,
        agent_id=agent_id,
        message=message,
        thread_id=thread_id,
        user_metadata=user_metadata,
        temporal_client=temporal_client,
        remote_script_connector=remote_script_connector,
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
