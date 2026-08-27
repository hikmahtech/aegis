"""JiraActivities — close Todoist tasks whose issue is resolved in Jira.

Why this exists when `email_task_links` already closes tasks from Jira mail:
**Jira does not email you about transitions you make yourself.** Measured on the
author's own account, 7 of 15 open `APP-` tasks had no Gmail message about them
at ALL, ever, and the other 8 had mail carrying no resolution. Email can only
ever close the tickets somebody else resolved. Asking Jira is the signal that
covers your own.

Two activities, deliberately split read from write:

``fetch_jira_task_states``
    Read-only. Finds open Todoist tasks whose title starts with an issue key,
    asks Jira for their real state, returns the resolved ones.
``close_resolved_jira_tasks``
    The only mutating half. Leaves a note naming the resolution, then completes.

Resolution, not status, is the test. Status names are per-workflow ("Deployed",
"Committed", "Waiting to Validate") and a fork's differ; `fields.resolution`
is non-null exactly when Jira considers the issue closed, whatever the board
calls that column.

Unconfigured is a no-op, not an error: blank base URL / email / token returns
``reason="not_configured"`` so the schedule can ship active and inert.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from typing import Any

import asyncpg
import httpx
from temporalio import activity

from aegis_worker.shared.todoist_write import submit_or_queue

#: Jira issue key at the start of a Todoist task title — `APP-11399: Fix thing`.
#: Postgres ARE for the DB filter, Python `re` for the extraction; the syntax
#: below means the same in both.
DEFAULT_KEY_PATTERN = "^[A-Z][A-Z0-9]+-[0-9]+"

_TIMEOUT = httpx.Timeout(30.0)
#: Jira Cloud replaced GET /search with POST /search/jql. Try the new endpoint
#: first and fall back, so this works on both without the operator knowing which
#: their instance serves.
_SEARCH_PATHS = ("/rest/api/3/search/jql", "/rest/api/3/search")


@dataclass
class JiraActivities:
    db_pool: asyncpg.Pool | None
    connector: Any = None  # TodoistConnector at runtime
    base_url: str = ""
    email: str = ""
    api_token: str = ""

    # ------------------------------------------------------------------ read

    @activity.defn
    async def fetch_jira_task_states(
        self, key_pattern: str = DEFAULT_KEY_PATTERN, max_tasks: int = 100
    ) -> dict:
        """Open Todoist tasks carrying an issue key, paired with Jira's verdict.

        Returns ``{"checked", "resolved": [{task_id, key, title, resolution,
        status}], "unresolved", "reason"}``. Read-only.
        """
        if not (self.base_url and self.email and self.api_token):
            return {"checked": 0, "resolved": [], "unresolved": 0, "reason": "not_configured"}
        if self.db_pool is None:
            return {"checked": 0, "resolved": [], "unresolved": 0, "reason": "no_pool"}

        async with self.db_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, content, labels FROM todoist_tasks "
                "WHERE NOT is_completed AND content ~ $1 "
                "ORDER BY updated_at DESC LIMIT $2",
                key_pattern,
                max_tasks,
            )
        by_key: dict[str, dict] = {}
        compiled = re.compile(key_pattern)
        for r in rows:
            m = compiled.match(r["content"] or "")
            if m:
                # Newest row wins for a duplicated key — ORDER BY put it first.
                by_key.setdefault(m.group(0), {"task_id": r["id"], "title": r["content"][:80]})
        if not by_key:
            return {"checked": 0, "resolved": [], "unresolved": 0, "reason": "no_tasks"}

        try:
            states = await self._search_issues(sorted(by_key))
        except Exception as exc:
            activity.logger.warning("jira_search_failed err=%s", str(exc)[:200])
            return {
                "checked": len(by_key),
                "resolved": [],
                "unresolved": 0,
                "reason": f"jira_error: {str(exc)[:120]}",
            }

        resolved, unresolved = [], 0
        for key, task in by_key.items():
            state = states.get(key)
            if state is None:
                continue  # key not in Jira (typo, moved project) — leave it alone
            if state["resolution"]:
                resolved.append({**task, "key": key, **state})
            else:
                unresolved += 1
        activity.logger.info(
            "jira_states_fetched checked=%d resolved=%d unresolved=%d",
            len(by_key),
            len(resolved),
            unresolved,
        )
        return {
            "checked": len(by_key),
            "resolved": resolved,
            "unresolved": unresolved,
            "reason": "ok",
        }

    async def _search_issues(self, keys: list[str]) -> dict[str, dict]:
        """`{key: {status, resolution}}` for the keys Jira knows about.

        One request for every key — the whole point of using the API over
        per-issue GETs.
        """
        jql = "key IN ({})".format(",".join(keys))
        auth = base64.b64encode(f"{self.email}:{self.api_token}".encode()).decode()
        headers = {"Authorization": f"Basic {auth}", "Accept": "application/json"}
        body = {"jql": jql, "fields": ["status", "resolution"], "maxResults": len(keys)}

        last_exc: Exception | None = None
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            for path in _SEARCH_PATHS:
                url = self.base_url.rstrip("/") + path
                try:
                    resp = await client.post(url, headers=headers, json=body)
                    if resp.status_code in (404, 405, 410):
                        # Wrong endpoint for this instance — try the other one.
                        last_exc = RuntimeError(f"{path} -> HTTP {resp.status_code}")
                        continue
                    resp.raise_for_status()
                    return _parse_issues(resp.json())
                except httpx.HTTPStatusError as exc:
                    # 401/403 are configuration problems; retrying the other
                    # path just repeats them, so surface immediately.
                    if exc.response.status_code in (401, 403):
                        raise RuntimeError(
                            f"jira auth rejected (HTTP {exc.response.status_code})"
                        ) from exc
                    last_exc = exc
                except httpx.HTTPError as exc:
                    last_exc = exc
        raise last_exc or RuntimeError("jira search failed")

    # ----------------------------------------------------------------- write

    @activity.defn
    async def close_resolved_jira_tasks(self, items: list[dict]) -> dict:
        """Complete each task in `items`, leaving a note naming the resolution.

        `items` is `fetch_jira_task_states()["resolved"]`. Best-effort per task:
        one failure never stops the rest.
        """
        from aegis.connectors.todoist import TodoistConnector

        if not items or self.connector is None or self.db_pool is None:
            return {"closed": 0, "queued": 0, "failed": 0}

        closed = queued = failed = 0
        for it in items:
            task_id, key = it.get("task_id"), it.get("key")
            if not task_id or not key:
                continue
            note = (
                f"✅ Closed by Jira: {key} is {it.get('resolution') or 'resolved'}"
                f" (status: {it.get('status') or 'unknown'})"
            )
            if self.base_url:
                note += f"\n{self.base_url.rstrip('/')}/browse/{key}"
            cmds = [
                TodoistConnector.build_note_add_command(task_id, note),
                TodoistConnector.build_item_complete_command(task_id),
            ]
            res = await submit_or_queue(self.db_pool, self.connector, cmds, f"jira_close:{key}")
            if res["ok"]:
                closed += 1
                activity.logger.info(
                    "jira_task_closed key=%s task=%s resolution=%s",
                    key,
                    task_id,
                    it.get("resolution"),
                )
            elif res["queued"]:
                queued += 1
            else:
                failed += 1
        return {"closed": closed, "queued": queued, "failed": failed}


def _parse_issues(payload: dict) -> dict[str, dict]:
    """Jira search response -> `{key: {status, resolution}}`.

    `resolution` is None for an unresolved issue; both endpoints nest the fields
    identically.
    """
    out: dict[str, dict] = {}
    for issue in payload.get("issues") or []:
        key = issue.get("key")
        if not key:
            continue
        fields = issue.get("fields") or {}
        resolution = fields.get("resolution") or None
        status = fields.get("status") or {}
        out[key] = {
            "resolution": (resolution or {}).get("name") if resolution else None,
            "status": status.get("name") if isinstance(status, dict) else None,
        }
    return out
