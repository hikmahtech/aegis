"""Infrastructure ops endpoints — mirror pandoras-actor's chat tools.

Each route delegates to the same executor function used by the chat tool,
so the UI and chat share one implementation. Context defaults to ``swarm``
(Swarm) for service routes and blank for pod/argocd routes — callers must
pass an explicit ``context`` query param, either a configured script-host
k8s context (``AEGIS_SCRIPT_HOST_K8S_CONTEXTS``) or the slug of a registered
``kind=k8s`` infra entry.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from aegis.api.auth import verify_auth
from aegis.services.chat import ToolContext

router = APIRouter(prefix="/api/infra", dependencies=[Depends(verify_auth)])


def _tool_context(request: Request) -> ToolContext:
    state = request.app.state
    return ToolContext(
        remote_script_connector=getattr(state, "remote_script_connector", None),
        settings=getattr(state, "settings", None),
    )


async def _call_tool(executor, request: Request, args: dict[str, Any]) -> Any:
    """Invoke an executor and parse its JSON-string response into dict/list."""
    pool = request.app.state.db_pool
    ctx = _tool_context(request)
    raw = await executor(pool, args, ctx)
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        # Some scripts return plain text; wrap for consistent consumption.
        return {"output": raw}


@router.get("/services")
async def list_services(request: Request, context: str = "swarm") -> Any:
    from aegis.services.chat import _exec_list_services

    return await _call_tool(_exec_list_services, request, {"context": context})


@router.get("/services/{name}")
async def inspect_service(request: Request, name: str, context: str = "swarm") -> Any:
    from aegis.services.chat import _exec_inspect_service

    return await _call_tool(
        _exec_inspect_service, request, {"context": context, "service_name": name}
    )


@router.get("/services/{name}/logs")
async def service_logs(
    request: Request, name: str, context: str = "swarm", tail: int = 200
) -> Any:
    from aegis.services.chat import _exec_get_service_logs

    return await _call_tool(
        _exec_get_service_logs,
        request,
        {"context": context, "service_name": name, "tail": tail},
    )


@router.post("/services/{name}/restart")
async def restart_service(request: Request, name: str, context: str = "swarm") -> Any:
    from aegis.services.chat import _exec_restart_service

    return await _call_tool(
        _exec_restart_service, request, {"context": context, "service_name": name}
    )


@router.get("/pods")
async def list_pods(
    request: Request, context: str = "", namespace: str = "default"
) -> Any:
    from aegis.services.chat import _exec_list_pods

    return await _call_tool(_exec_list_pods, request, {"context": context, "namespace": namespace})


@router.get("/deployments")
async def list_deployments(
    request: Request, context: str = "", namespace: str = "default"
) -> Any:
    from aegis.services.chat import _exec_list_deployments

    return await _call_tool(
        _exec_list_deployments, request, {"context": context, "namespace": namespace}
    )


@router.get("/pods/{namespace}/{name}/logs")
async def pod_logs(
    request: Request,
    namespace: str,
    name: str,
    context: str = "",
    tail: int = 200,
) -> Any:
    from aegis.services.chat import _exec_get_pod_logs

    return await _call_tool(
        _exec_get_pod_logs,
        request,
        {
            "context": context,
            "namespace": namespace,
            "pod_name": name,
            "tail": tail,
        },
    )


@router.get("/argocd/apps")
async def list_argocd_apps(
    request: Request, context: str = "", filter: str = ""
) -> Any:
    from aegis.services.chat import _exec_list_argocd_apps

    return await _call_tool(_exec_list_argocd_apps, request, {"context": context, "filter": filter})


@router.post("/argocd/apps/{name}/sync")
async def sync_argocd_app(request: Request, name: str, context: str = "") -> Any:
    # sync_argocd_app may not be wired as an executor — surface useful error.
    from aegis.services.chat import TOOL_EXECUTORS

    executor = TOOL_EXECUTORS.get("sync_argocd_app")
    if executor is None:
        raise HTTPException(501, "sync_argocd_app not available")
    return await _call_tool(executor, request, {"context": context, "app_name": name})
