"""Infrastructure chat tool executors — swarm, k8s, argocd, cloud accounts.

Every executor here is built on the same script-host + infra-registry helper
base (`_INFRA_SPECS` / `_run_infra_script` / `_validate_infra_name` /
`_registry_k8s_id`), and that shared base is what draws the module boundary.
Pandora's other two tools — `aegis_self_diagnose` and `investigate_resource` —
use none of it and live in `tools/agents.py`.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Literal

import asyncpg

from aegis.services.tools.base import ToolContext
from aegis.services.tools.registry import aegis_tool

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
# is the shared driver; the named `_exec_*` tools below are typed shells that
# pass their own key into it.
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


# Named callables for the registry + test imports. Each is a thin typed shell
# over `_exec_infra` whose docstring IS the advertised schema — a decorator
# cannot be applied to a `functools.partial`, and the wording has to live
# somewhere a human reads. The `_INFRA_SPECS` key each one passes is what
# `test_chat_infra_tools.py` pins, script name by script name.


@aegis_tool
async def _exec_list_nodes(
    pool: asyncpg.Pool, ctx: ToolContext, *, context: Literal["swarm"]
) -> str:
    """List infrastructure cluster nodes and their status (up/down/drain). Use for checking Docker Swarm node health.

    Args:
        context: Infrastructure context. 'swarm' = homelab Docker Swarm.
    """
    return await _exec_infra("list_nodes", pool, {"context": context}, ctx)


@aegis_tool
async def _exec_list_services(
    pool: asyncpg.Pool, ctx: ToolContext, *, context: Literal["swarm"]
) -> str:
    """List Docker Swarm services with replica counts, mode, and image versions."""
    return await _exec_infra("list_services", pool, {"context": context}, ctx)


@aegis_tool
async def _exec_inspect_service(
    pool: asyncpg.Pool, ctx: ToolContext, *, context: Literal["swarm"], service_name: str
) -> str:
    """Inspect a Docker Swarm service: tasks, errors, update state, placement.

    Args:
        service_name: Swarm service name (e.g. 'aegis_core')
    """
    return await _exec_infra(
        "inspect_service", pool, {"context": context, "service_name": service_name}, ctx
    )


@aegis_tool
async def _exec_get_service_logs(
    pool: asyncpg.Pool,
    ctx: ToolContext,
    *,
    context: Literal["swarm"],
    service_name: str,
    tail: int = 50,
) -> str:
    """Tail recent logs from a Docker Swarm service.

    Args:
        tail: Number of log lines (1-500)
    """
    return await _exec_infra(
        "get_service_logs",
        pool,
        {"context": context, "service_name": service_name, "tail": tail},
        ctx,
    )


@aegis_tool
async def _exec_restart_service(
    pool: asyncpg.Pool, ctx: ToolContext, *, context: Literal["swarm"], service_name: str
) -> str:
    """Force-update (rolling restart) a Docker Swarm service. Mutating action — executes immediately; refused when the matching infrastructure entry is marked read-only."""
    return await _exec_infra(
        "restart_service", pool, {"context": context, "service_name": service_name}, ctx
    )


@aegis_tool
async def _exec_list_pods(
    pool: asyncpg.Pool,
    ctx: ToolContext,
    *,
    context: str,
    namespace: str | None = None,
    status: str | None = None,
) -> str:
    """List Kubernetes pods. Optionally filter by namespace and status (e.g. 'CrashLoopBackOff', 'Running', 'Pending').

    Args:
        context: Cluster: a script-host context (AEGIS_SCRIPT_HOST_K8S_CONTEXTS) or the slug of a registered kind=k8s infrastructure entry
        namespace: Kubernetes namespace (omit for all)
        status: Filter by phase or waiting reason
    """
    return await _exec_infra(
        "list_pods", pool, {"context": context, "namespace": namespace, "status": status}, ctx
    )


@aegis_tool
async def _exec_list_deployments(
    pool: asyncpg.Pool, ctx: ToolContext, *, context: str, namespace: str | None = None
) -> str:
    """List Kubernetes deployments with replica status.

    Args:
        context: Cluster: a script-host context (AEGIS_SCRIPT_HOST_K8S_CONTEXTS) or the slug of a registered kind=k8s infrastructure entry
        namespace: Kubernetes namespace (omit for all)
    """
    return await _exec_infra(
        "list_deployments", pool, {"context": context, "namespace": namespace}, ctx
    )


@aegis_tool
async def _exec_get_pod_logs(
    pool: asyncpg.Pool,
    ctx: ToolContext,
    *,
    context: str,
    namespace: str,
    pod_name: str,
    tail: int = 50,
    container: str | None = None,
) -> str:
    """Tail recent logs from a Kubernetes pod.

    Args:
        context: Cluster: a script-host context (AEGIS_SCRIPT_HOST_K8S_CONTEXTS) or the slug of a registered kind=k8s infrastructure entry
        tail: Number of log lines (1-500)
        container: Optional container name
    """
    return await _exec_infra(
        "get_pod_logs",
        pool,
        {
            "context": context,
            "namespace": namespace,
            "pod_name": pod_name,
            "tail": tail,
            "container": container,
        },
        ctx,
    )


@aegis_tool
async def _exec_list_argocd_apps(
    pool: asyncpg.Pool, ctx: ToolContext, *, context: str, filter: str | None = None
) -> str:
    """List ArgoCD applications with sync and health status. Optional filter: 'degraded', 'outofsync', 'synced', 'healthy'.

    Args:
        context: k8s cluster context: a configured script-host context (AEGIS_SCRIPT_HOST_K8S_CONTEXTS) with the argocd CLI
        filter: Optional status filter
    """
    return await _exec_infra(
        "list_argocd_apps", pool, {"context": context, "filter": filter}, ctx
    )


@aegis_tool
async def _exec_sync_argocd_app(
    pool: asyncpg.Pool, ctx: ToolContext, *, context: str, app_name: str
) -> str:
    """Trigger ArgoCD sync for an application. Mutating action — executes immediately.

    Args:
        context: k8s cluster context: a configured script-host context (AEGIS_SCRIPT_HOST_K8S_CONTEXTS) with the argocd CLI
    """
    return await _exec_infra(
        "sync_argocd_app", pool, {"context": context, "app_name": app_name}, ctx
    )


@aegis_tool
async def _exec_restart_deployment(
    pool: asyncpg.Pool, ctx: ToolContext, *, context: str, namespace: str, deployment_name: str
) -> str:
    """Rolling-restart a Kubernetes deployment (kubectl rollout restart) on a registered k8s infrastructure entry. Mutating action — executes immediately; refused when the entry is marked read-only.

    Args:
        context: Slug of a registered k8s infrastructure entry

    Returns:
        Registry-only k8s tool (no script-host equivalent). Read-only entries refuse it.
    """
    args = {"context": context, "namespace": namespace, "deployment_name": deployment_name}
    infra_id = await _registry_k8s_id(pool, context)
    if infra_id is None:
        return json.dumps(
            {
                "error": f"Unknown k8s cluster: {context!r} — register it as a kind=k8s "
                "infrastructure entry first"
            }
        )
    return await _exec_registry_k8s("restart_deployment", pool, args, ctx, infra_id)


@aegis_tool
async def _exec_list_cloud_accounts(pool: asyncpg.Pool, ctx: ToolContext) -> str:
    """List registered cloud provider accounts (AWS accounts, GCP projects) from the infrastructure registry: slug, provider, status, and the account id / project recorded at the last provision. Read-only."""
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


@aegis_tool
async def _exec_cloud_identity(
    pool: asyncpg.Pool, ctx: ToolContext, *, slug: str, profile: str | None = None
) -> str:
    """Run a live identity check for a registered cloud account (`aws sts get-caller-identity` / GCP access-token check) and report which principal the stored credentials resolve to. Read-only.

    Args:
        slug: Slug of a registered cloud account (kind=cloud)
        profile: AWS profile override; omit to use the account's default profile

    Returns:
        Every failure mode (unknown slug, missing CLI, bad credentials) comes
        back as a clear error envelope, never an exception.
    """
    from aegis.services import infra as infra_service

    if pool is None:
        return json.dumps({"error": "database not available"})
    slug = (slug or "").strip()
    if err := _validate_infra_name(slug, "slug"):
        return json.dumps({"error": err})
    row = await infra_service.get_infra_by_slug(pool, slug, include_credentials=True)
    if not row or row.get("kind") != "cloud":
        return json.dumps(
            {"error": f"Unknown cloud account: {slug!r} — see list_cloud_accounts"}
        )
    secret_key = getattr(ctx.settings, "secret_key", "") or ""
    profile = (profile or "").strip() or None
    result = await infra_service.cloud_identity_check(row, secret_key, profile=profile)
    if not result.get("ok"):
        return json.dumps({"error": result.get("error", "identity check failed")})
    return json.dumps(
        {"slug": slug, "provider": result["provider"], "identity": result["identity"]}
    )


@aegis_tool
async def _exec_run_infra_script(
    pool: asyncpg.Pool,
    ctx: ToolContext,
    *,
    context: str,
    script_name: str,
    args: list[str] | None = None,
) -> str:
    """Run an infrastructure script from the predefined scripts/infra/ directory by name (without the .sh suffix). The context is passed as the script's first argument. Prefer the dedicated infra tools (list_nodes, list_services, ...) when one matches.

    Args:
        context: 'swarm', or a configured script-host k8s context
        script_name: Script file name, e.g. 'infra_list_nodes'
        args: Arguments passed to the script
    """
    # Runs scripts/infra/<script_name>.sh on the remote host with `context`
    # as the first argument — the same surface the dedicated infra tools use.
    # (The original implementation looked scripts up in the `resources` table
    # via a column that never existed, so this tool errored on every call.)
    if context not in _INFRA_CONTEXTS_ALL:
        return json.dumps({"error": f"Unsupported context: {context}"})
    err = _validate_infra_name(script_name, "script_name")
    if err:
        return json.dumps({"error": err})

    script_args = args or []
    if not isinstance(script_args, list):
        return json.dumps({"error": "args must be an array"})
    script_args = [str(a) for a in script_args]

    return await _run_infra_script(ctx, script_name, [context, *script_args], timeout=120)
