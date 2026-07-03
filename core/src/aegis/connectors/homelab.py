"""HomelabConnector - Docker Swarm ops + generic TLS probing.

Every public method returns the standard envelope:
    {"ok": bool, "data": Any, "error": str|None, "retryable": bool,
     "external_ref": str|None}
"""

from __future__ import annotations

import asyncio
import json
import shlex
from typing import Any

import structlog

from aegis.connectors._subprocess import kill_and_wait

logger = structlog.get_logger()

_DOCKER_TIMEOUT_S = 30


def _envelope(
    ok: bool,
    data: Any = None,
    error: str | None = None,
    retryable: bool = False,
    external_ref: str | None = None,
) -> dict:
    return {
        "ok": ok,
        "data": data,
        "error": error,
        "retryable": retryable,
        "external_ref": external_ref,
    }


def _parse_replicas(s: str) -> tuple[int, int]:
    """Parse '0/1' to (actual=0, desired=1). Returns (0,0) on unparseable."""
    if not s or "/" not in s:
        return (0, 0)
    try:
        a, d = s.split("/", 1)
        return (int(a), int(d))
    except (ValueError, IndexError):
        return (0, 0)


class HomelabConnector:
    def __init__(self, docker_context: str):
        self._docker_context = docker_context

    async def _docker(self, *args: str, timeout: int = _DOCKER_TIMEOUT_S) -> tuple[int, str, str]:
        # When docker_context is empty, rely on the DOCKER_HOST env var
        # (preferred inside the worker container where no local contexts exist).
        cmd = ["docker"]
        if self._docker_context:
            cmd += ["--context", self._docker_context]
        cmd += list(args)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            try:
                out, err = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout
                )
            except TimeoutError:
                return (-1, "", "timeout")
            return (
                proc.returncode or 0,
                out.decode("utf-8", "replace"),
                err.decode("utf-8", "replace"),
            )
        finally:
            await kill_and_wait(proc)

    async def list_services(self) -> dict:
        """Return list of services with parsed replica counts.

        Shape per item: {name, stack, replicas_actual, replicas_desired,
                         image, mode, id}
        """
        rc, out, err = await self._docker("service", "ls", "--format", "{{json .}}")
        if rc != 0:
            return _envelope(False, error=f"docker ls failed: {err[:200]}", retryable=True)
        services = []
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                s = json.loads(line)
            except json.JSONDecodeError:
                continue
            name = s.get("Name", "")
            stack = name.split("_", 1)[0] if "_" in name else ""
            a, d = _parse_replicas(s.get("Replicas", ""))
            services.append(
                {
                    "name": name,
                    "stack": stack,
                    "replicas_actual": a,
                    "replicas_desired": d,
                    "image": s.get("Image", ""),
                    "mode": s.get("Mode", ""),
                    "id": s.get("ID", ""),
                }
            )
        return _envelope(True, data=services)

    async def service_ps(self, service_name: str) -> dict:
        """Return recent task list for a service. Shape per item:
        {task_id, current_state, desired_state, error, image, node}."""
        rc, out, err = await self._docker(
            "service",
            "ps",
            service_name,
            "--no-trunc",
            "--format",
            "{{json .}}",
        )
        if rc != 0:
            return _envelope(False, error=f"docker ps failed: {err[:200]}", retryable=True)
        tasks = []
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                t = json.loads(line)
            except json.JSONDecodeError:
                continue
            tasks.append(
                {
                    "task_id": t.get("ID", ""),
                    "current_state": t.get("CurrentState", ""),
                    "desired_state": t.get("DesiredState", ""),
                    "error": t.get("Error", ""),
                    "image": t.get("Image", ""),
                    "node": t.get("Node", ""),
                }
            )
        return _envelope(True, data=tasks)

    async def restart_service(self, service_name: str) -> dict:
        """Force-restart a swarm service (idempotent: reschedules its tasks
        without changing the spec).

        `--detach` returns as soon as the update is submitted instead of
        blocking until convergence, so the caller polls `list_services` for
        recovery on its own clock. Used by the alert auto-remediation path to
        recover a service whose running tasks fell below desired.
        """
        rc, out, err = await self._docker(
            "service", "update", "--force", "--detach", service_name
        )
        if rc != 0:
            return _envelope(
                False, error=f"docker service update --force failed: {err[:200]}", retryable=True
            )
        return _envelope(True, data={"output": out[-500:]})

    async def probe_tls(self, domain: str, port: int = 443) -> dict:
        """Probe TLS and parse notAfter + serial via openssl x509."""
        from datetime import datetime

        pipeline = (
            f"echo | openssl s_client -servername {shlex.quote(domain)} "
            f"-connect {shlex.quote(domain)}:{port} 2>/dev/null "
            f"| openssl x509 -noout -enddate -serial"
        )
        cmd = ["bash", "-c", pipeline]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            try:
                out, err = await asyncio.wait_for(proc.communicate(), timeout=20)
            except TimeoutError:
                return _envelope(False, error="tls timeout", retryable=True)
            if proc.returncode != 0:
                return _envelope(
                    False, error=f"tls probe failed: {err.decode()[:120]}", retryable=True
                )
        finally:
            await kill_and_wait(proc)
        text = out.decode("utf-8", "replace")
        not_after: datetime | None = None
        serial = ""
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("notAfter="):
                raw = line.split("=", 1)[1].strip()
                try:
                    not_after = datetime.strptime(raw, "%b %d %H:%M:%S %Y %Z")
                except ValueError:
                    pass
            elif line.startswith("serial="):
                serial = line.split("=", 1)[1].strip()
        if not_after is None or not serial:
            return _envelope(False, error="tls parse failed", retryable=False)
        return _envelope(True, data={"domain": domain, "not_after": not_after, "serial": serial})
