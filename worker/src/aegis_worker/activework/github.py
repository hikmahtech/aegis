"""Read-only GitHub signals for the active-work guard, via `gh` over SSH.

Reuses the remote_script connector's host auth (the same path create_github_pr
uses). Host is chosen by org routing: claude-org (e.g. acme) repos run on
the base host whose `gh` is logged into the org account; others on the kimi host.
Degrades to [] on any failure — never raises, never causes a skip-on-error.
"""

from __future__ import annotations

import json
import logging
import shlex
from datetime import datetime

logger = logging.getLogger(__name__)

_TIMEOUT = 20


def _parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


async def _host_for(connector, repo: str) -> str:
    """Pick the host whose gh auth covers this repo's org."""
    await connector.ensure_config()  # DB-first config may have changed the hosts
    if connector._engine_for(repo) == "claude":
        return connector._host
    host, _ = await connector._resolve_kimi_host()
    return host


async def _gh(connector, repo: str, cmd: str) -> list | None:
    """Run a `gh` command over SSH on the org's auth host; parse JSON stdout."""
    if connector is None:
        return None
    try:
        host = await _host_for(connector, repo)
        result = await connector.run_on_host(host, cmd, timeout=_TIMEOUT)
        if result.get("status") != "succeeded":
            logger.warning("activework_gh_failed repo=%s status=%s", repo, result.get("status"))
            return None
        return json.loads(result.get("stdout") or "[]")
    except Exception as exc:  # noqa: BLE001 — best-effort signal
        logger.warning("activework_gh_error repo=%s err=%s", repo, str(exc)[:200])
        return None


async def open_prs(connector, repo: str) -> list[dict]:
    cmd = (
        f"gh pr list --repo {shlex.quote(repo)} --state open "
        f"--json number,author,headRefName,updatedAt"
    )
    data = await _gh(connector, repo, cmd)
    if not data:
        return []
    return [
        {
            "number": p.get("number"),
            "user": (p.get("author") or {}).get("login", ""),
            "updated_at": p.get("updatedAt", ""),
            "head_ref": p.get("headRefName", ""),
        }
        for p in data
    ]


async def recent_pushes(connector, repo: str, since_iso: str) -> list[dict]:
    cmd = f"gh api {shlex.quote(f'repos/{repo}/events')}"
    data = await _gh(connector, repo, cmd)
    if not data:
        return []
    try:
        since = _parse_ts(since_iso)
    except Exception:
        return []
    out: list[dict] = []
    for e in data:
        if e.get("type") != "PushEvent":
            continue
        created = e.get("created_at", "")
        try:
            if not created or _parse_ts(created) <= since:
                continue
        except Exception:
            continue
        out.append(
            {
                "ref": (e.get("payload") or {}).get("ref", ""),
                "actor": (e.get("actor") or {}).get("login", ""),
                "created_at": created,
            }
        )
    return out
