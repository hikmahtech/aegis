"""HomelabConnector - unified wrapper over Docker Swarm, SSH, Dagster, Traefik.

Every public method returns the standard envelope:
    {"ok": bool, "data": Any, "error": str|None, "retryable": bool,
     "external_ref": str|None}
"""

from __future__ import annotations

import asyncio
import json
import shlex
from typing import Any

import httpx
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
    def __init__(
        self,
        docker_context: str,
        dagster_graphql_url: str,
        traefik_api_url: str,
        ssh_host: str,
        ssh_user: str,
        ssh_key_file: str,
        http_client: httpx.AsyncClient | None = None,
    ):
        self._docker_context = docker_context
        self._dagster_url = dagster_graphql_url
        self._traefik_url = traefik_api_url.rstrip("/")
        self._ssh_host = ssh_host
        self._ssh_user = ssh_user
        self._ssh_key_file = ssh_key_file
        self._http = http_client

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

    _DAGSTER_QUERY = """
    query Schedules {
      schedulesOrError {
        ... on Schedules {
          results {
            name
            scheduleState { status }
            ticks(limit: 1) { timestamp status }
          }
        }
      }
    }"""

    async def list_dagster_schedules(self) -> dict:
        if not self._dagster_url:
            return _envelope(False, error="dagster not configured", retryable=False)
        if self._http is None:
            return _envelope(False, error="no http client", retryable=False)
        try:
            r = await self._http.post(
                self._dagster_url, json={"query": self._DAGSTER_QUERY}, timeout=15.0
            )
        except httpx.HTTPError as exc:
            return _envelope(False, error=f"dagster http: {exc}", retryable=True)
        if r.status_code >= 400:
            return _envelope(
                False, error=f"dagster {r.status_code}", retryable=(r.status_code >= 500)
            )
        body = r.json()
        results = body.get("data", {}).get("schedulesOrError", {}).get("results")
        if results is None:
            return _envelope(False, error="dagster: unexpected shape", retryable=False)
        schedules = []
        for s in results:
            state = s.get("scheduleState") or {}
            ticks = s.get("ticks") or []
            last = ticks[0] if ticks else {}
            schedules.append(
                {
                    "name": s.get("name", ""),
                    "status": state.get("status", "UNKNOWN"),
                    "last_run_at": last.get("timestamp"),
                    "last_run_ok": last.get("status") == "SUCCESS" if last else None,
                }
            )
        return _envelope(True, data=schedules)

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

    async def list_backups(self, subpath: str) -> dict:
        """List backup files recursively under an NFS subpath via SSH.

        Returns items with {name, size_bytes, mtime_epoch}. `name` is the path
        relative to `subpath` (%P), so the per-table clickhouse layout
        (<db>/<table>.native) stays distinct rather than colliding on basename.
        """
        if not self._ssh_host:
            return _envelope(False, error="no ssh host", retryable=False)
        remote = f"find {shlex.quote(subpath)} -type f -printf '%P|%s|%T@\\n'"
        ssh_cmd = [
            "ssh",
            "-i",
            self._ssh_key_file,
            "-o",
            "StrictHostKeyChecking=accept-new",
            f"{self._ssh_user}@{self._ssh_host}",
            remote,
        ]
        proc = await asyncio.create_subprocess_exec(
            *ssh_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            try:
                out, err = await asyncio.wait_for(proc.communicate(), timeout=30)
            except TimeoutError:
                return _envelope(False, error="ssh timeout", retryable=True)
            if proc.returncode != 0:
                return _envelope(
                    False, error=f"ssh failed: {err.decode()[:200]}", retryable=True
                )
        finally:
            await kill_and_wait(proc)
        items = []
        for line in out.decode().splitlines():
            parts = line.strip().split("|")
            if len(parts) != 3:
                continue
            try:
                items.append(
                    {"name": parts[0], "size_bytes": int(parts[1]), "mtime_epoch": float(parts[2])}
                )
            except ValueError:
                continue
        return _envelope(True, data=items)
