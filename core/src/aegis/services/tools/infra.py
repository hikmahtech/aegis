"""Infrastructure chat tool executors — swarm, k8s, argocd, cloud accounts.

Every executor here is built on the same script-host + infra-registry helper
base (`_INFRA_SPECS` / `_run_infra_script` / `_validate_infra_name` /
`_registry_k8s_id`), and that shared base is what draws the module boundary.
Pandora's other two tools — `aegis_self_diagnose` and `investigate_resource` —
use none of it and stay in `services/chat.py`.
"""

from __future__ import annotations

import functools
import json
import os
import re
from typing import Any

import asyncpg

from aegis.services.tools.base import ToolContext

_INFRA_CONTEXTS_SWARM = {"swarm"}
# k8s "context" names that exist on the remote script host (the host that
# runs scripts/infra/*.sh + the argocd CLI), read once at import from
# AEGIS_SCRIPT_HOST_K8S_CONTEXTS.
# Blank ⇒ empty set: script-host k8s/argocd tools then have no valid context
# and pod/deployment/log ops resolve only via registered kind=k8s infra
# entries (by slug). Read via os.getenv rather than Settings() so importing
# this module never requires a full (DB-bearing) settings object.
_INFRA_CONTEXTS_K8S = {
    c.strip() for c in os.getenv("AEGIS_SCRIPT_HOST_K8S_CONTEXTS", "").split(",") if c.strip()
}
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


async def _registry_k8s_id(pool: asyncpg.Pool | None, slug: str) -> Any | None:
    """id of a registered kind=k8s infra entry matching `slug`, else None."""
    if pool is None or not slug:
        return None
    try:
        return await pool.fetchval("SELECT id FROM infra WHERE slug = $1 AND kind = 'k8s'", slug)
    except Exception:  # noqa: BLE001 — fall back to the script-host path
        return None


async def _swarm_context_read_only(pool: asyncpg.Pool | None, context: str) -> bool:
    """True when a registered swarm/docker infra entry mapping to `context`
    (by slug or docker_context) is marked read_only — mutating swarm ops are
    refused for it. Unregistered contexts are unaffected."""
    if pool is None or not context:
        return False
    try:
        return bool(
            await pool.fetchval(
                "SELECT bool_or(read_only) FROM infra WHERE kind IN ('swarm', 'docker') "
                "AND (slug = $1 OR docker_context = $1)",
                context,
            )
        )
    except Exception:  # noqa: BLE001 — fail open: unregistered/unreachable registry
        return False


async def _exec_registry_k8s(
    tool: str, pool: asyncpg.Pool, args: dict, ctx: ToolContext, infra_id: Any
) -> str:
    """Run a k8s chat tool directly against a registry entry's stored
    kubeconfig (services/infra.py) instead of the remote script host."""
    from aegis.services import infra as infra_service

    secret_key = getattr(ctx.settings, "secret_key", "") or ""
    namespace = args.get("namespace") or ""

    if tool == "list_pods":
        result = await infra_service.k8s_list_pods(pool, infra_id, secret_key, namespace)
        if result.get("ok") and args.get("status"):
            want = str(args["status"]).lower()
            result["pods"] = [p for p in result["pods"] if want in p["phase"].lower()]
    elif tool == "list_deployments":
        result = await infra_service.k8s_list_deployments(pool, infra_id, secret_key, namespace)
    elif tool == "get_pod_logs":
        result = await infra_service.k8s_pod_logs(
            pool,
            infra_id,
            secret_key,
            namespace,
            args.get("pod_name", ""),
            tail=int(args.get("tail", 50) or 50),
            container=args.get("container") or None,
        )
    elif tool == "restart_deployment":
        result = await infra_service.k8s_restart_deployment(
            pool, infra_id, secret_key, namespace, args.get("deployment_name", "")
        )
    else:
        # argocd tools need the argocd CLI on the script host — not available
        # through a bare kubeconfig.
        return json.dumps(
            {
                "error": (
                    f"{tool} is not available for registry k8s clusters (script-host only); "
                    "configure AEGIS_SCRIPT_HOST_K8S_CONTEXTS with a context that has the "
                    "argocd CLI"
                )
            }
        )

    if not result.get("ok"):
        return json.dumps({"error": result.get("error", "k8s op failed")})
    result.pop("ok", None)
    result.pop("status_code", None)
    return json.dumps(result, default=str)


async def _exec_infra(tool: str, pool: asyncpg.Pool, args: dict, ctx: ToolContext) -> str:
    """Shared driver for the data-described infra executors (`_INFRA_SPECS`)."""
    script, contexts, ctx_default, ctx_err, timeout, arg_fields = _INFRA_SPECS[tool]
    context = args.get("context", ctx_default)
    if context not in contexts:
        # A k8s context that isn't a script-host one may be the slug of a
        # registered kind=k8s infra entry — run kubectl directly for those.
        if contexts is _INFRA_CONTEXTS_K8S:
            infra_id = await _registry_k8s_id(pool, context)
            if infra_id is not None:
                return await _exec_registry_k8s(tool, pool, args, ctx, infra_id)
        if ctx_err == "for_tool":
            return json.dumps({"error": f"Unsupported context for {tool}: {context}"})
        return json.dumps({"error": f"Unsupported context: {context}"})
    if tool == "restart_service" and await _swarm_context_read_only(pool, context):
        return json.dumps(
            {
                "error": f"context {context!r} is read-only — restart_service is disabled "
                "(infra registry read_only flag)"
            }
        )
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


async def _exec_restart_deployment(pool: asyncpg.Pool, args: dict, ctx: ToolContext) -> str:
    """Registry-only k8s tool (no script-host equivalent): rolling-restart a
    deployment on a registered kind=k8s entry. Read-only entries refuse it."""
    context = args.get("context", "")
    infra_id = await _registry_k8s_id(pool, context)
    if infra_id is None:
        return json.dumps(
            {
                "error": f"Unknown k8s cluster: {context!r} — register it as a kind=k8s "
                "infrastructure entry first"
            }
        )
    return await _exec_registry_k8s("restart_deployment", pool, args, ctx, infra_id)


async def _exec_list_cloud_accounts(pool: asyncpg.Pool, args: dict, ctx: ToolContext) -> str:
    """Read-only listing of registered cloud accounts (kind=cloud entries)."""
    from aegis.services import infra as infra_service

    if pool is None:
        return json.dumps({"error": "database not available"})
    accounts = await infra_service.list_cloud_accounts(pool)
    if not accounts:
        return json.dumps(
            {
                "accounts": [],
                "note": "no cloud accounts registered — add a kind=cloud infrastructure entry",
            }
        )
    return json.dumps({"accounts": accounts}, default=str)


async def _exec_cloud_identity(pool: asyncpg.Pool, args: dict, ctx: ToolContext) -> str:
    """Live identity check for one registered cloud account. Read-only; all
    failure modes (unknown slug, missing CLI, bad credentials) come back as a
    clear error envelope, never an exception."""
    from aegis.services import infra as infra_service

    if pool is None:
        return json.dumps({"error": "database not available"})
    slug = (args.get("slug") or "").strip()
    if err := _validate_infra_name(slug, "slug"):
        return json.dumps({"error": err})
    row = await infra_service.get_infra_by_slug(pool, slug, include_credentials=True)
    if not row or row.get("kind") != "cloud":
        return json.dumps(
            {"error": f"Unknown cloud account: {slug!r} — see list_cloud_accounts"}
        )
    secret_key = getattr(ctx.settings, "secret_key", "") or ""
    profile = (args.get("profile") or "").strip() or None
    result = await infra_service.cloud_identity_check(row, secret_key, profile=profile)
    if not result.get("ok"):
        return json.dumps({"error": result.get("error", "identity check failed")})
    return json.dumps(
        {"slug": slug, "provider": result["provider"], "identity": result["identity"]}
    )


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
