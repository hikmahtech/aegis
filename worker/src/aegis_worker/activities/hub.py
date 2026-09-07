"""Worker-side activities over the problem hub (`aegis.services.hub`).

Thin by design: the logic lives in core's `services/hub.py` and
`services/hub_project.py`, which both packages import, so a workflow reaches
the hub through these and never carries SQL of its own.

`ingest_alert` is the seam every alert producer crosses (heartbeat, Sentry,
clarify's content routes; the alertmanager webhook calls the same core
functions from core). It records the event, links a caller-supplied Todoist
task, projects, and reports whether the hub wants an investigation — the
*producer* then starts `AlertInvestigationFlow`, because only a workflow can
start a child workflow.
"""

from __future__ import annotations

from datetime import UTC, datetime

import asyncpg
from aegis.services import hub, hub_project, hub_watch
from temporalio import activity


class HubActivities:
    def __init__(self, db_pool: asyncpg.Pool | None) -> None:
        self.db_pool = db_pool

    @activity.defn
    async def ingest_alert(self, alert: dict, resolved: bool = False) -> dict:
        """Record an alert dict (the shape every producer builds) as a problem
        event. Returns the ingest result plus `problem_id`, `todoist_task_id`
        and `investigate`. A caller-supplied `alert["todoist_task_id"]` (the
        clarify and chat paths) becomes the problem's task when it has none.

        Without a pool (tests without a DB) it reports a fresh, investigable
        problem with no id, so a flow still runs end to end."""
        if self.db_pool is None:
            return {
                "problem_id": None,
                "action": "created",
                "investigate": not resolved,
                "todoist_task_id": alert.get("todoist_task_id"),
                "suppressed": False,
                "muted": False,
            }
        now = datetime.now(UTC)
        event = hub.event_from_alert(alert, occurred_at=now, resolved=resolved)
        result = await hub.ingest_event(self.db_pool, event, now=now)
        task_id = str(alert.get("todoist_task_id") or "") or None
        if result.problem_id and task_id:
            await hub_project.link_task(self.db_pool, result.problem_id, task_id)
        projected: dict = {}
        if result.problem_id:
            try:
                projected = await hub_project.project(self.db_pool, result.problem_id, now=now)
            except Exception as exc:  # noqa: BLE001 — the sweep retries projection
                activity.logger.warning(
                    "ingest_alert_project_failed problem=%s err=%s",
                    result.problem_id,
                    str(exc)[:200],
                )
        return {
            **result.to_dict(),
            "todoist_task_id": task_id or projected.get("task_id"),
        }

    @activity.defn
    async def verification_delay(self, alert: dict) -> dict:
        """Seconds an investigation waits before spending effort, by the
        alert's class (`hub.verify_seconds`). An activity rather than a pure
        call in the flow so a test can shorten it."""
        labels = alert.get("labels") if isinstance(alert.get("labels"), dict) else {}
        return {"delay_seconds": hub.verify_seconds(str(labels.get("alertname") or ""))}

    @activity.defn
    async def ingest_finding(self, inp: dict) -> dict:
        """One finding from a workflow that has no activity of its own on the
        hub (the LLM governor's budget edge). `inp` carries `source`, `klass`,
        `subject`, `subject_kind`, `title`, `severity`, `payload` and
        `resolved`; the occurrence id is the finding at this instant."""
        if self.db_pool is None:
            return {"problem_id": None, "action": "no_pool", "investigate": False}
        now = datetime.now(UTC)
        resolved = bool(inp.get("resolved"))
        klass, subject = hub.slug(str(inp.get("klass") or "")), hub.slug(str(inp.get("subject") or ""))
        result = await hub.ingest_event(
            self.db_pool,
            hub.Event(
                source=str(inp.get("source") or "manual"),
                external_id=f"{inp.get('source')}:{klass}:{subject}@{now.isoformat()}"
                + ("@resolved" if resolved else ""),
                kind="resolved" if resolved else "occurrence",
                title=str(inp.get("title") or f"{klass}: {subject}")[:500],
                subject=subject,
                subject_kind=str(inp.get("subject_kind") or "service"),
                klass=klass,
                severity=str(inp.get("severity") or "warning"),
                payload=dict(inp.get("payload") or {}),
                occurred_at=now,
            ),
            now=now,
        )
        if result.problem_id:
            try:
                await hub_project.project(self.db_pool, result.problem_id, now=now)
            except Exception as exc:  # noqa: BLE001 — the sweep retries projection
                activity.logger.warning(
                    "ingest_finding_project_failed problem=%s err=%s",
                    result.problem_id,
                    str(exc)[:200],
                )
        return result.to_dict()

    @activity.defn
    async def reconcile_findings(self, inp: dict) -> dict:
        """A watchdog's current findings in, fresh problems and recoveries out
        (`hub_watch.reconcile_findings`). `inp` carries `source`,
        `subject_kind`, `classes` and `findings`. Without a pool every finding
        is fresh, so a flow still runs end to end."""
        findings = list(inp.get("findings") or [])
        if self.db_pool is None:
            return {
                "fresh": [{**f, "problem_id": None} for f in findings],
                "attached": 0,
                "muted": 0,
                "suppressed": 0,
                "resolved": [],
            }
        return await hub_watch.reconcile_findings(
            self.db_pool,
            source=str(inp.get("source") or "manual"),
            subject_kind=str(inp.get("subject_kind") or "service"),
            classes=[str(c) for c in (inp.get("classes") or [])],
            findings=findings,
        )

    @activity.defn
    async def problem_status(self, problem_id: str) -> dict:
        """What the hub currently knows about a problem — the investigation
        flow's replacement for polling `audit_log` for a resolved row."""
        p = await hub.get_problem(self.db_pool, problem_id) if self.db_pool and problem_id else None
        if p is None:
            return {"found": False, "status": "", "resolved": False, "todoist_task_id": None}
        return {
            "found": True,
            "status": p["status"],
            "resolved": p["status"] in {"resolved", "closed"},
            "occurrences": p["occurrences"],
            "todoist_task_id": p["todoist_task_id"],
            "muted": p["muted_until"] is not None and p["muted_until"] > datetime.now(UTC),
        }

    @activity.defn
    async def record_investigation(self, inp: dict) -> dict:
        """Write an `investigation` event (and move the problem to
        `inp["status"]` when given), then project. One dict argument because
        Temporal activities take positional args only.

        Keys: `problem_id`, `status`, `text`, `external_id` (idempotency),
        `posted` (True = the flow already put this text on the task, so the
        projector must not repeat it), `payload` (extra; `pr_urls` become
        `github_pr` links)."""
        problem_id = str(inp.get("problem_id") or "")
        if self.db_pool is None or not problem_id:
            return {"recorded": False}
        status = str(inp.get("status") or "")
        text = str(inp.get("text") or "")
        payload = dict(inp.get("payload") or {})
        now = datetime.now(UTC)
        await hub.ingest_event(
            self.db_pool,
            hub.Event(
                source="investigation",
                external_id=str(inp.get("external_id") or f"{problem_id}:{now.isoformat()}"),
                kind="investigation",
                title=text[:200] or status or "investigation",
                payload={
                    **payload,
                    "text": text[:4000],
                    "status": status,
                    "posted": bool(inp.get("posted", True)),
                },
                occurred_at=now,
                problem_id=problem_id,
            ),
            now=now,
        )
        for url in payload.get("pr_urls") or []:
            await self.db_pool.execute(
                "INSERT INTO problem_links (problem_id, link_kind, ref) "
                "VALUES ($1::uuid, 'github_pr', $2) ON CONFLICT DO NOTHING",
                problem_id,
                str(url)[:300],
            )
        moved = False
        if status:
            moved = await hub.set_status(
                self.db_pool, problem_id, status, reason=text[:300], now=now
            )
        try:
            await hub_project.project(self.db_pool, problem_id, now=now)
        except Exception as exc:  # noqa: BLE001
            activity.logger.warning(
                "record_investigation_project_failed problem=%s err=%s", problem_id, str(exc)[:200]
            )
        return {"recorded": True, "status_changed": moved}

    @activity.defn
    async def record_plan(self, inp: dict) -> dict:
        """Record a coding turn's plan on the task's problem, creating the
        problem when the task is a plain `@code` one. The projector turns two
        or more steps into subtasks the operator (or a later turn) ticks off.

        Keys: `task_id`, `steps` (list), `text` (the plan as posted, already on
        the task as a comment), `external_id` (idempotency).

        Best-effort by contract: a turn that ran and posted its plan must not
        fail because the checklist could not be written."""
        task_id = str(inp.get("task_id") or "")
        steps = [str(s) for s in (inp.get("steps") or [])]
        if self.db_pool is None or not task_id or len(steps) < 2:
            return {"recorded": False, "steps": len(steps)}
        now = datetime.now(UTC)
        try:
            problem = await hub_project.ensure_problem_for_task(
                self.db_pool, task_id, subject=str(inp.get("subject") or "")
            )
            if problem is None:
                return {"recorded": False, "steps": len(steps)}
            await hub.ingest_event(
                self.db_pool,
                hub.Event(
                    source="session",
                    external_id=str(inp.get("external_id") or f"plan:{task_id}:{now.isoformat()}"),
                    kind="plan",
                    title=problem["title"],
                    severity="info",
                    problem_id=problem["id"],
                    payload={
                        "text": str(inp.get("text") or "")[:2000],
                        "steps": steps,
                        # The turn already posted the plan as a task comment.
                        "posted": True,
                    },
                    occurred_at=now,
                ),
                now=now,
            )
            await hub_project.project(self.db_pool, problem["id"], now=now)
        except Exception as exc:  # noqa: BLE001 — the turn's own output is what matters
            activity.logger.warning(
                "record_plan_failed task_id=%s err=%s", task_id, str(exc)[:200]
            )
            return {"recorded": False, "steps": len(steps)}
        return {"recorded": True, "steps": len(steps), "problem_id": problem["id"]}

    @activity.defn
    async def mute_problem(self, problem_id: str, hours: float, by: str = "gate2") -> dict:
        if self.db_pool is None or not problem_id:
            return {"muted_until": None}
        until = await hub.mute_problem(self.db_pool, problem_id, hours=hours, by=by)
        return {"muted_until": until.isoformat() if until else None}

    @activity.defn
    async def stale_stuck_problems(self, subjects: list[str], hours: float) -> list[dict]:
        """Among `subjects` (services the heartbeat sees stuck right now), the
        ones whose open problem is older than `hours` and has had no
        investigation event in that long — due for a re-investigation."""
        if self.db_pool is None or not subjects:
            return []
        rows = await hub.stale_open_problems(
            self.db_pool, [hub.slug(x) for x in subjects], hours=hours
        )
        return rows

    @activity.defn
    async def promote_expired_suppressions(self) -> dict:
        """Open every `suppressed` problem whose deploy/maintenance window has
        passed without a resolution. Run by `HubSweepFlow`."""
        if self.db_pool is None:
            return {"promoted": 0, "problem_ids": []}
        ids = await hub.promote_expired_suppressions(self.db_pool)
        return {"promoted": len(ids), "problem_ids": ids}

    @activity.defn
    async def project_pending(self) -> dict:
        """Bring every problem's Todoist task up to date with its events
        (`hub_project.project_pending`). Run by `HubSweepFlow`."""
        if self.db_pool is None:
            return {"projected": 0, "created": 0, "errors": 0}
        results = await hub_project.project_pending(self.db_pool)
        return {
            "projected": sum(1 for r in results if "skipped" not in r and "error" not in r),
            "created": sum(1 for r in results if r.get("created")),
            "errors": sum(1 for r in results if "error" in r),
        }

    @activity.defn
    async def clear_converged_deploys(self, stuck_services: list[str]) -> dict:
        """End `deploying` windows for services the heartbeat sees converged.
        `stuck_services` is the heartbeat's current below-desired list."""
        if self.db_pool is None:
            return {"cleared": []}
        cleared = await hub.clear_converged_deploys(self.db_pool, list(stuck_services or []))
        return {"cleared": cleared}
