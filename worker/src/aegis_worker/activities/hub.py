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

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
import httpx
from aegis.errors import error_text, logged_failure
from aegis.services import hub, hub_cards, hub_fix, hub_group, hub_project, hub_watch
from temporalio import activity
from temporalio.service import RPCError, RPCStatusCode

from aegis_worker.activities.delivery import safe_send_message

# The briefing message names this many problems; the rest are a count.
_DIGEST_LIST_CAP = 12
# How many members a grouping judge is shown. Enough to see a pattern; a
# hundred stuck posts do not read differently from twelve.
_GROUP_PROMPT_CAP = 12
# Two small reads of alertmanager, on the LAN. Short: the sweep runs every five
# minutes and a monitoring stack that cannot answer in this long is one the
# reconciliation must decline to act on anyway.
_ALERTMANAGER_TIMEOUT_S = 8.0
# The producers that start an investigation for a problem they raise, so a
# problem of theirs a window held back gets one when the window ends (#630).
_INVESTIGATED_ON_PROMOTE = frozenset({"alertmanager", "heartbeat"})


def _uptime_since(raw: str, now: datetime) -> timedelta | None:
    """How long alertmanager has been up, from its `/api/v2/status` `uptime`.

    That field is a START TIMESTAMP in RFC 3339 (`2026-09-12T21:04:27.879Z`),
    not a duration — measured against the live instance, not assumed. `None`
    when it cannot be read, which the caller treats as "do not reconcile":
    without a trustworthy uptime there is no way to tell a healthy empty alert
    set from one a restart has just emptied.
    """
    text = (raw or "").strip()
    if not text:
        return None
    try:
        started = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    return now - started


class HubActivities:
    def __init__(
        self,
        db_pool: asyncpg.Pool | None,
        llm_client: Any = None,
        model: str = "",
        delivery: Any = None,
        temporal_client: Any = None,
    ) -> None:
        self.db_pool = db_pool
        # Only the grouping judge needs a model. Everything else here is SQL,
        # so a worker with no LLM wired still runs the whole sweep — it just
        # never proposes a group.
        self.llm_client = llm_client
        self.model = model
        self.delivery = delivery
        # Only `retire_cards` needs it: it signals a retired card's waiting
        # `InteractionFlow`, which may belong to any run. Without one the card
        # is still retired and edited, and its run waits out its own timeout.
        self.temporal_client = temporal_client

    async def _infra_agent(self) -> str:
        """The `infra` holder, who judges and announces a group — never an
        example id (#579). "" when nobody holds the tag or there is no pool."""
        if self.db_pool is None:
            return ""
        from aegis.services.agents import resolve_tag

        try:
            return await resolve_tag(self.db_pool, "infra") or ""
        except Exception as exc:  # noqa: BLE001 — an owner lookup never breaks the sweep
            activity.logger.warning("hub_infra_agent_lookup_failed error=%s", error_text(exc))
            return ""

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
        # An occurrence id that survives a retry of THIS activity task. Temporal
        # keeps the activity id across attempts, so the second attempt of an
        # ingest that already committed comes back `duplicate` instead of
        # minting a second occurrence and answering `investigate=False`.
        occurrence_key = ""
        if activity.in_activity():
            info = activity.info()
            occurrence_key = f"{info.workflow_id}:{info.activity_id}"
        event = hub.event_from_alert(
            alert, occurred_at=now, resolved=resolved, occurrence_key=occurrence_key
        )
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
                    error_text(exc),
                )
        return {
            **result.to_dict(),
            "todoist_task_id": task_id or projected.get("task_id"),
        }

    @activity.defn
    async def verification_delay(self, alert: dict) -> dict:
        """Seconds an investigation waits before spending effort, by the
        alert's class (`hub.verify_seconds`, with the operator's
        `hub_settle_seconds` overrides). An activity rather than a pure call in
        the flow so a test can shorten it, and so the override is read live.

        It is the same number the projector waits out before a problem earns a
        Todoist task (#537) — "long enough to believe this is real" is one
        question, so it has one answer."""
        labels = alert.get("labels") if isinstance(alert.get("labels"), dict) else {}
        alertname = str(labels.get("alertname") or "")
        if self.db_pool is None:
            return {"delay_seconds": hub.verify_seconds(alertname)}
        return {"delay_seconds": await hub.verify_seconds_for(self.db_pool, alertname)}

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
                    error_text(exc),
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
    async def project_problem(self, problem_id: str) -> dict:
        """Bring the problem's task up to date and report the task's id.

        How the investigation flow learns a task the settle window deferred
        (#537): it asks for this once its verification delay is over, which is
        the same window, so the projection mints the task and hands back the id
        the flow needs for every comment it is about to post. Idempotent — the
        projector is re-runnable by design — and quiet about a problem that has
        nothing to project.
        """
        if self.db_pool is None or not problem_id:
            return {"task_id": "", "skipped": "no_pool"}
        projected = await hub_project.project(
            self.db_pool, problem_id, now=datetime.now(UTC)
        )
        return {
            "task_id": str(projected.get("task_id") or ""),
            "skipped": str(projected.get("skipped") or ""),
        }

    @activity.defn
    async def record_investigation(self, inp: dict) -> dict:
        """Write an `investigation` event (and move the problem to
        `inp["status"]` when given), then project. One dict argument because
        Temporal activities take positional args only.

        A problem the alert already resolved stays resolved: the event is
        still written, and `status_changed` comes back False (`hub.set_status`,
        #484).

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
        task_id = ""
        try:
            projected = await hub_project.project(self.db_pool, problem_id, now=now)
            task_id = str(projected.get("task_id") or "")
        except Exception as exc:  # noqa: BLE001
            activity.logger.warning(
                "record_investigation_project_failed problem=%s err=%s", problem_id, error_text(exc)
            )
        # `task_id` is reported because THIS projection is what mints the task
        # when a settle window held it back (#537): the status this call just
        # moved is what stops the projector deferring. Without it the flow goes
        # on holding the None it was handed at step 0, and every comment it
        # posts afterwards — the start note, the restart evidence, the verdict,
        # the transcript — lands on an empty id and is silently dropped.
        return {"recorded": True, "status_changed": moved, "task_id": task_id}

    @activity.defn
    async def follow_fix_pr(self, pr: dict) -> dict:
        """A pull request closed, merged or not (`GitHubAlertFlow`). When an
        investigation opened it, the close lands on its problem and the
        problem moves: `verifying` on a merge, `waiting_human` when it was
        closed unmerged (`hub_fix.record_pr_closed`). Any other PR is nobody's
        fix and changes nothing. Then projects, so the task hears it now
        rather than at the next sweep.

        `pr` is `GitHubAlertFlow._pr_from_payload`: `url`, `merged`,
        `merged_at`, `closed_at`. Safe to retry: GitHub's timestamp is in the
        event's id."""
        if self.db_pool is None:
            return {"followed": 0, "problems": []}
        merged = bool(pr.get("merged"))
        rows = await hub_fix.record_pr_closed(
            self.db_pool,
            url=str(pr.get("url") or ""),
            merged=merged,
            at=str((pr.get("merged_at") if merged else pr.get("closed_at")) or ""),
        )
        for row in rows:
            try:
                await hub_project.project(self.db_pool, row["problem_id"])
            except Exception as exc:  # noqa: BLE001 — the sweep retries projection
                activity.logger.warning(
                    "follow_fix_pr_project_failed problem=%s err=%s",
                    row["problem_id"],
                    error_text(exc),
                )
        return {"followed": len(rows), "problems": rows}

    @activity.defn
    async def verify_fixes(
        self,
        window_hours: float = hub_fix.VERIFY_HOURS_DEFAULT,
        grace_hours: float = hub_fix.GRACE_HOURS_DEFAULT,
    ) -> dict:
        """Settle the `verifying` problems (`hub_fix.verify_fixes`): resolve
        one whose alert stayed clear for `window_hours` after its fix merged,
        reopen one it came back to. Run by `HubSweepFlow` before projection,
        which posts what this wrote in the same tick."""
        if self.db_pool is None:
            return {"resolved": 0, "reopened": 0, "problem_ids": []}
        rows = await hub_fix.verify_fixes(
            self.db_pool, window_hours=window_hours, grace_hours=grace_hours
        )
        return {
            "resolved": sum(1 for r in rows if r["action"] == "resolved"),
            "reopened": sum(1 for r in rows if r["action"] == "reopened"),
            "problem_ids": [r["problem_id"] for r in rows],
        }

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
                "record_plan_failed task_id=%s err=%s", task_id, error_text(exc)
            )
            return {"recorded": False, "steps": len(steps)}
        return {"recorded": True, "steps": len(steps), "problem_id": problem["id"]}

    @activity.defn
    async def build_digest(self, hours: float = 24.0) -> dict:
        """The day's problems, rendered for the briefing. One query over
        `problem_events` — there is no buffer to accumulate into and none to
        clear, so asking twice gives the same answer and a flow that died
        half-way through an investigation is still represented by what it
        actually recorded.

        Returns `{message, count}`: the shape the briefing already sends, so
        the delivery side is unchanged.
        """
        if self.db_pool is None:
            return {"message": "", "count": 0}
        out = await hub.digest(self.db_pool, hours=hours)
        counts = out["counts"]
        if not counts["total"]:
            return {"message": "", "count": 0}
        head = (
            f"<b>Problem digest</b> (last {hours:g}h)\n\n"
            f"{counts['total']} problems saw activity: {counts['new']} new, "
            f"{counts['open']} still open, {counts['resolved']} resolved, "
            f"{counts['investigated']} investigated. "
            f"{counts['occurrences']} occurrences in total."
        )
        if counts["suppressed"] or counts["muted"]:
            head += (
                f" Not raised: {counts['suppressed']} suppressed by a window, "
                f"{counts['muted']} muted."
            )
        lines = []
        for p in out["problems"][:_DIGEST_LIST_CAP]:
            mark = "🆕" if p["is_new"] else "•"
            subject = p["subject"] or p["subject_kind"] or "-"
            lines.append(
                f"{mark} {p['title'][:100]} — {subject} · {p['status']} · "
                f"{p['occurrences']}×"
            )
        more = len(out["problems"]) - len(lines)
        if more > 0:
            lines.append(f"… and {more} more")
        return {"message": head + "\n\n" + "\n".join(lines), "count": counts["total"]}

    @activity.defn
    async def close_resolved_problems(self, days: float = 7.0) -> dict:
        """Retire problems resolved longer than `days` ago. Closing frees the
        correlation key: the unique index covers OPEN keys, so a service that
        breaks again next month starts a fresh problem instead of reopening a
        month-old one. Run nightly by `CleanupFlow`."""
        if self.db_pool is None or float(days) < 0:
            return {"closed": 0, "problem_ids": []}
        ids = await hub.close_resolved(self.db_pool, days=days)
        return {"closed": len(ids), "problem_ids": ids}

    @activity.defn
    async def mute_problem(self, problem_id: str, hours: float, by: str = "gate2") -> dict:
        if self.db_pool is None or not problem_id:
            return {"muted_until": None}
        until = await hub.mute_problem(self.db_pool, problem_id, hours=hours, by=by)
        return {"muted_until": until.isoformat() if until else None}

    @activity.defn
    async def stale_stuck_problems(
        self, subjects: list[str], hours: float, classes: list[str] | None = None
    ) -> list[dict]:
        """Among `subjects` (services the heartbeat sees stuck right now), the
        ones whose open problem is older than `hours` and has had no
        investigation event in that long — due for a re-investigation.

        `classes` is what keeps the answer to the question the caller asked:
        the heartbeat means "this service is still down", not "anything ever
        recorded about this service"."""
        if self.db_pool is None or not subjects:
            return []
        rows = await hub.stale_open_problems(
            self.db_pool,
            [hub.slug(x) for x in subjects],
            hours=hours,
            classes=list(classes) if classes else None,
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
    async def promoted_investigations(self, problem_ids: list[str]) -> list[dict]:
        """The alert to investigate each just-promoted problem with (#630).

        A window held these problems back, so they got no investigation when
        they appeared, and nothing else will start one: alertmanager re-sends
        a firing alert under the same occurrence id, and the heartbeat emits
        only on a change. Only the producers that start an investigation for a
        new problem qualify (alertmanager and the heartbeat), and only a
        problem still `open` — one that resolved during the window needs
        nothing. The alert is rebuilt from the problem and its last
        occurrence, and carries `problem_id`, so the flow skips ingest."""
        if self.db_pool is None or not problem_ids:
            return []
        rows = await self.db_pool.fetch(
            "SELECT p.id::text AS id, p.title, p.severity, p.subject, p.subject_kind, "
            "       p.status, p.todoist_task_id, "
            "  (SELECT e.source FROM problem_events e WHERE e.problem_id = p.id "
            "     AND e.kind = 'occurrence' ORDER BY e.id LIMIT 1) AS source, "
            "  (SELECT e.payload FROM problem_events e WHERE e.problem_id = p.id "
            "     AND e.kind = 'occurrence' ORDER BY e.id DESC LIMIT 1) AS payload "
            "FROM problems p WHERE p.id = ANY($1::uuid[]) ORDER BY p.first_seen_at",
            list(problem_ids),
        )
        out: list[dict] = []
        for r in rows:
            if r["status"] != "open" or r["source"] not in _INVESTIGATED_ON_PROMOTE:
                continue
            payload = r["payload"] if isinstance(r["payload"], dict) else {}
            labels = payload.get("labels") if isinstance(payload.get("labels"), dict) else {}
            service = r["subject"] if r["subject_kind"] == "service" else ""
            out.append(
                {
                    "title": r["title"],
                    "fingerprint": str(payload.get("fingerprint") or ""),
                    "severity": r["severity"],
                    # The spelling the producers use; `hub.event_from_alert`
                    # maps it back to `heartbeat`.
                    "source": "aegis-heartbeat" if r["source"] == "heartbeat" else r["source"],
                    "service": service,
                    "description": str(payload.get("description") or ""),
                    "labels": labels,
                    "escalate": False,
                    "problem_id": r["id"],
                    "todoist_task_id": r["todoist_task_id"],
                }
            )
        return out

    @activity.defn
    async def retire_cards(self, inp: dict) -> dict:
        """Retire stale decision cards and finish the ones already retired
        (#629, `aegis.services.hub_cards`).

        `inp` may name a `problem_id` and a `reason`: the investigation flow
        passes `superseded` for the problem it is about to post a newer card
        for, with `exclude_run` set to its own run. Every card retired so far
        — here, or by a resolve inside the hub — is then finished: its Slack
        message edited to say why, and its waiting `InteractionFlow` signalled
        so the old run ends now. The sweep calls it with no problem, which
        finishes every retired card still waiting.

        Safe to retry: retiring only moves `pending` rows, and a finished card
        is not touched again."""
        if self.db_pool is None:
            return {"retired": 0, "finished": 0, "waiting": 0}
        problem_id = str(inp.get("problem_id") or "")
        reason = str(inp.get("reason") or "")
        retired: list[dict] = []
        if problem_id and reason:
            retired = await hub_cards.retire(
                self.db_pool,
                problem_id,
                reason=reason,
                exclude_run=str(inp.get("exclude_run") or ""),
            )
        rows = await hub_cards.unfinished(self.db_pool, problem_id=problem_id)
        finished = 0
        for row in rows:
            why = row["reason"] if row["reason"] in hub_cards.REASONS else hub_cards.SUPERSEDED
            edited = await self._edit_retired_card(row, why)
            signalled = await self._end_card_flow(row, why)
            if await hub_cards.finish(self.db_pool, row["id"], edited=edited, signalled=signalled):
                finished += 1
        if retired or rows:
            activity.logger.info(
                "hub_cards_retire problem=%s retired=%d finished=%d",
                problem_id or "*",
                len(retired),
                finished,
            )
        return {"retired": len(retired), "finished": finished, "waiting": len(rows) - finished}

    async def _edit_retired_card(self, row: dict, reason: str) -> bool:
        ref = row.get("delivery_ref")
        if isinstance(ref, str):
            try:
                ref = json.loads(ref)
            except ValueError:
                ref = None
        if self.delivery is None:
            return not (isinstance(ref, dict) and ref.get("adapter") == "slack")
        try:
            result = await self.delivery.edit_card(
                ref if isinstance(ref, dict) else None,
                hub_cards.edit_text(reason, str(row.get("prompt") or "")),
            )
        except Exception as exc:  # noqa: BLE001 — the next sweep tries again
            activity.logger.warning("hub_card_edit_failed id=%s err=%s", row["id"], error_text(exc))
            return False
        ok = isinstance(result, dict) and bool(result.get("ok"))
        if not ok:
            activity.logger.warning(
                "hub_card_edit_failed id=%s err=%s", row["id"], str((result or {}).get("error"))[:200]
            )
        return ok

    async def _end_card_flow(self, row: dict, reason: str) -> bool:
        """Signal the card's `InteractionFlow` with the answer that ends its
        run. A problem retired as resolved that has come back since is told
        `superseded` instead: `self_resolved` would make the old run record
        the problem resolved again while it is live."""
        if self.temporal_client is None:
            return False
        if reason == hub_cards.RESOLVED and row.get("problem_id"):
            try:
                p = await hub.get_problem(self.db_pool, row["problem_id"])
            except Exception:  # noqa: BLE001 — unknown reads as "still resolved"
                p = None
            if p is not None and p["status"] not in {"resolved", "closed"}:
                reason = hub_cards.SUPERSEDED
        try:
            handle = self.temporal_client.get_workflow_handle(row["flow_run_id"])
            await handle.signal("submit_response", hub_cards.answer(reason))
        except RPCError as exc:
            if exc.status == RPCStatusCode.NOT_FOUND:
                return True  # its run is already over: nothing waits on the card
            activity.logger.warning("hub_card_signal_failed id=%s err=%s", row["id"], error_text(exc))
            return False
        except Exception as exc:  # noqa: BLE001 — the next sweep tries again
            activity.logger.warning("hub_card_signal_failed id=%s err=%s", row["id"], error_text(exc))
            return False
        return True

    @activity.defn
    async def reconcile_completed_tasks(self) -> dict:
        """Resolve every live problem whose Todoist task a person completed,
        and reopen a task whose completion is the hub's own close from before
        the problem came back (`hub_project.reconcile_completed_tasks`). Run
        by `HubSweepFlow` before projection. Safe to retry: a problem already
        resolved, or a task already reopened, is not touched twice."""
        if self.db_pool is None:
            return {"resolved": 0, "problem_ids": [], "tasks_reopened": 0}
        rows = await hub_project.reconcile_completed_tasks(self.db_pool)
        resolved = [r["problem_id"] for r in rows if r["action"] == "resolved"]
        return {
            "resolved": len(resolved),
            "problem_ids": resolved,
            "tasks_reopened": sum(1 for r in rows if r["action"] == "task_reopened"),
        }

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

    # --- grouping: the same failure on many entities -------------------------

    @activity.defn
    async def find_group_candidates(
        self, min_members: int = 0, hours: float = 0.0
    ) -> list[dict]:
        """Clusters of live problems that share a class and a subject kind.

        A candidate, not a verdict — `judge_group` decides. A cluster with a
        standing verdict that has not grown since is dropped here rather than
        re-priced every sweep.
        """
        if self.db_pool is None:
            return []
        found = await hub_group.candidates(
            self.db_pool,
            min_members=int(min_members) or hub_group.MIN_MEMBERS,
            hours=float(hours) or hub_group.WINDOW_HOURS,
        )
        out: list[dict] = []
        for cluster in found:
            verdict = await hub_group.recent_verdict(
                self.db_pool, cluster["group_key"], cluster["member_count"]
            )
            if verdict is not None:
                continue
            out.append(
                {
                    "class": cluster["class"],
                    "subject_kind": cluster["subject_kind"],
                    "group_key": cluster["group_key"],
                    "member_count": cluster["member_count"],
                    "members": [
                        {
                            "id": m["id"],
                            "subject": m["subject"],
                            "title": m["title"],
                            "severity": m["severity"],
                            "occurrences": int(m["occurrences"] or 0),
                            "first_seen_at": m["first_seen_at"].isoformat(),
                        }
                        for m in cluster["members"]
                    ],
                }
            )
        activity.logger.info("hub_group_candidates found=%d", len(out))
        return out

    @activity.defn
    async def judge_group(self, candidate: dict) -> dict:
        """Ask the model whether these problems are one condition, and what to
        call it.

        NO_RETRY at the call site: this is a billed call, and a second opinion
        on the same cluster is worth less than the money it costs. A model that
        will not answer, or is not wired, declines — the problems stay separate,
        which is the safe direction. The verdict is recorded either way so the
        next sweep does not ask again until the cluster grows.
        """
        members = candidate.get("members") or []
        gkey = str(candidate.get("group_key") or "")
        no = {"group": False, "title": "", "reason": "", "group_key": gkey}
        if self.db_pool is None or not gkey or len(members) < 2:
            return no
        if not self.llm_client or not self.model:
            return {**no, "reason": "no model wired"}

        listed = "\n".join(
            f"- {m.get('subject')}: {str(m.get('title'))[:120]} "
            f"(seen {m.get('occurrences')}x)"
            for m in members[:_GROUP_PROMPT_CAP]
        )
        prompt = (
            "These open problems share a class and a kind of subject. Decide "
            "whether they are ONE condition affecting several things, or "
            "several unrelated problems that happen to look alike.\n\n"
            f"Class: {candidate.get('class')}\n"
            f"Kind of subject: {candidate.get('subject_kind')}\n"
            f"Count: {candidate.get('member_count')}\n"
            f"Members:\n{listed}\n\n"
            "Say yes only when one fix would clear all of them — a queue that "
            "stopped draining, a host that went down, one broken integration. "
            "Say no when each needs its own diagnosis, even if the wording "
            "matches.\n"
            'Return JSON only: {"same_condition": true|false, "title": '
            '"<short title for the shared problem, naming the count and what '
            'is affected>", "reason": "<one sentence>"}'
        )
        try:
            result = await self.llm_client.think(
                prompt,
                model=self.model,
                system_prompt=(
                    "You are a site reliability engineer deciding whether "
                    "several alerts are one incident."
                ),
                db_pool=self.db_pool,
                purpose="hub_group_judge",
                agent_id=await self._infra_agent() or None,
            )
        except Exception as exc:  # noqa: BLE001 — a judge that will not answer says no
            activity.logger.warning("hub_group_judge_failed error=%s", error_text(exc))
            return {**no, "reason": f"judge failed: {error_text(exc, 120)}"}

        from aegis.llm import parse_llm_json

        parsed = parse_llm_json(result.get("response") or "")
        if not isinstance(parsed, dict):
            activity.logger.warning("hub_group_judge_unparseable")
            return {**no, "reason": "unparseable verdict"}
        agreed = bool(parsed.get("same_condition"))
        title = str(parsed.get("title") or "").strip()[:200]
        reason = str(parsed.get("reason") or "").strip()[:300]
        await hub_group.record_verdict(
            self.db_pool,
            gkey,
            grouped=agreed,
            member_count=int(candidate.get("member_count") or len(members)),
            reason=reason,
        )
        activity.logger.info(
            "hub_group_judged key=%s group=%s reason=%s", gkey, agreed, reason[:120]
        )
        return {"group": agreed, "title": title, "reason": reason, "group_key": gkey}

    @activity.defn
    async def apply_group(self, candidate: dict, verdict: dict) -> dict:
        """Fold the cluster into one group problem, retire the tasks it
        swallowed, project the survivor and say so in the channel.

        NO_RETRY at the call site: it merges, closes tasks and posts. It is
        written to be safe to run again anyway — `hub_group.upgrade` folds into
        the group that already exists — but a silent second Slack card is not
        worth the retry.
        """
        if self.db_pool is None:
            return {"grouped": False, "reason": "no pool"}
        members = candidate.get("members") or []
        title = str(verdict.get("title") or "").strip()
        if not title:
            kind = candidate.get("subject_kind") or "subject"
            title = f"{len(members)} {kind}s: {candidate.get('class')}"
        try:
            result = await hub_group.upgrade(
                self.db_pool,
                klass=str(candidate.get("class") or ""),
                subject_kind=str(candidate.get("subject_kind") or ""),
                title=title,
                member_ids=[str(m.get("id")) for m in members if m.get("id")],
                by="hub-sweep",
            )
        except ValueError as exc:
            activity.logger.warning("hub_group_upgrade_refused error=%s", error_text(exc))
            return {"grouped": False, "reason": error_text(exc)}

        # Retire the tasks the folded problems owned: leaving them open is the
        # very thing grouping exists to stop.
        retired = 0
        for merged in result["merged"]:
            task_id = merged.get("task_id") or ""
            if not task_id:
                continue
            try:
                if await hub_project.retire_task(
                    self.db_pool,
                    task_id,
                    f"Folded into one problem: {title}. Work it there.",
                ):
                    retired += 1
            except Exception as exc:  # noqa: BLE001 — the group still stands
                activity.logger.warning(
                    "hub_group_retire_failed task_id=%s error=%s", task_id, error_text(exc)
                )
        with logged_failure("hub_group_project_failed", logger=activity.logger, field="error"):
            await hub_project.project(self.db_pool, result["problem_id"])

        subjects = [s for s in result["subjects"] if s]
        body = (
            f"{title}\n\n"
            f"{len(subjects)} problems of class `{result['class']}` were the same "
            "condition, so they are now one problem and one task:\n"
            + "\n".join(f"  {s}" for s in subjects[:12])
            + ("\n  …" if len(subjects) > 12 else "")
            + f"\n\n{retired} task(s) closed. A new one joins this problem instead "
            "of opening another.\n"
            + (f"Why: {verdict.get('reason')}" if verdict.get("reason") else "")
        )
        await safe_send_message(
            self.delivery,
            agent_id=await self._infra_agent(),
            message=f"[PROBLEM GROUPED] {title}\n\n{body}",
            log_event="hub_group_notify_failed",
        )
        return {
            "grouped": True,
            "problem_id": result["problem_id"],
            "group_key": result["group_key"],
            "title": title,
            "folded": len(result["merged"]),
            "tasks_retired": retired,
        }

    @activity.defn
    async def reconcile_alertmanager(self, url: str, min_uptime_seconds: int = 900) -> dict:
        """Resolve live alertmanager problems whose alerts it no longer lists.

        The alertmanager lane was the only producer on the hub with no
        reconciliation: it resolves a problem solely on the `resolved` webhook,
        and alertmanager keeps its alerts in memory, so a restart means that
        webhook is never sent and the problem plus its Todoist task are stranded
        for good (#551). Every other lane already recovers — the heartbeat
        re-checks, the watchdogs run `reconcile_findings`.

        **Everything here fails closed**, because the failure mode of getting
        this wrong is mass-resolving a live estate:

        * no URL configured → do nothing (a fork ships nobody's monitoring host);
        * the status or alerts read fails, times out, or answers non-200 → do
          nothing, because an unreachable monitoring stack must never read as
          "everything recovered";
        * **alertmanager itself started less than `min_uptime_seconds` ago → do
          nothing.** This is the guard the bug taught: a freshly restarted
          alertmanager holds an empty set until Prometheus re-sends, and
          reconciling against that would resolve every open problem at once.
          Prometheus re-sends on the order of a minute, so the default leaves a
          wide margin.

        A `suppressed` alert (silenced or inhibited) counts as ACTIVE: it is
        still firing, someone has merely asked not to be told.
        """
        target = (url or "").strip().rstrip("/")
        if not target:
            return {"skipped": "not_configured", "resolved": 0, "checked": 0}
        if self.db_pool is None:
            return {"skipped": "no_pool", "resolved": 0, "checked": 0}
        try:
            async with httpx.AsyncClient(timeout=_ALERTMANAGER_TIMEOUT_S) as client:
                status = await client.get(f"{target}/api/v2/status")
                status.raise_for_status()
                uptime_raw = str((status.json() or {}).get("uptime") or "")
                alerts = await client.get(f"{target}/api/v2/alerts")
                alerts.raise_for_status()
                payload = alerts.json()
        except Exception as exc:  # noqa: BLE001 — fail closed, never resolve on doubt
            activity.logger.warning(
                "hub_alertmanager_read_failed url=%s err=%s", target, error_text(exc)
            )
            return {"skipped": "unreachable", "resolved": 0, "checked": 0}

        now = datetime.now(UTC)
        uptime = _uptime_since(uptime_raw, now)
        if uptime is None:
            return {"skipped": "uptime_unreadable", "resolved": 0, "checked": 0}
        if uptime < timedelta(seconds=max(0, min_uptime_seconds)):
            # It has forgotten what it was holding and has not been told again.
            return {
                "skipped": "alertmanager_just_started",
                "uptime_seconds": int(uptime.total_seconds()),
                "resolved": 0,
                "checked": 0,
            }

        if not isinstance(payload, list):
            return {"skipped": "unexpected_payload", "resolved": 0, "checked": 0}
        active = {
            str(a.get("fingerprint") or "")
            for a in payload
            if isinstance(a, dict) and str((a.get("status") or {}).get("state") or "") != "unprocessed"
        }
        active.discard("")

        out = await hub_watch.reconcile_alertmanager(
            self.db_pool, active_fingerprints=active, now=now
        )
        return {
            "checked": out["checked"],
            "resolved": len(out["resolved"]),
            "problems": [r["problem_id"] for r in out["resolved"]],
            "active_alerts": len(active),
        }
