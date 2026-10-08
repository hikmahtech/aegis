"""Worker-side activities over the problem hub (`aegis.services.hub`).

Thin by design: the logic lives in core's `services/hub.py` and
`services/hub_project.py`, which both packages import, so a workflow reaches
the hub through these and never carries SQL of its own.

The alert seam (`ingest_alert`) and the investigation's activities left with
the infra lane, which moved to the DevOps vertical (a2-devops). Four sweep
activities stay one release as no-ops so a sweep recorded before that change
replays (`hub_sweep.PATCH_DROP_INFRA_STEPS`).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import asyncpg
from aegis.errors import error_text, logged_failure
from aegis.services import hub, hub_group, hub_project, hub_watch
from temporalio import activity

from aegis_worker.activities.delivery import safe_send_message

# The briefing message names this many problems; the rest are a count.
_DIGEST_LIST_CAP = 12
# How many members a grouping judge is shown. Enough to see a pattern; a
# hundred stuck posts do not read differently from twelve.
_GROUP_PROMPT_CAP = 12


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
        # Unused since the infra lane left; kept so the worker's constructor
        # call is unchanged. Remove with the legacy sweep stubs.
        self.temporal_client = temporal_client

    async def _owner_agent(self) -> str:
        """The generalist (`gtd` holder), who judges and announces a group —
        never an example id (#579). "" when nobody holds the tag or there is
        no pool."""
        if self.db_pool is None:
            return ""
        from aegis.agent_tags import GENERALIST_TAG
        from aegis.services.agents import resolve_tag

        try:
            return await resolve_tag(self.db_pool, GENERALIST_TAG) or ""
        except Exception as exc:  # noqa: BLE001 — an owner lookup never breaks the sweep
            activity.logger.warning("hub_owner_agent_lookup_failed error=%s", error_text(exc))
            return ""

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
                "resolved": [],
            }
        return await hub_watch.reconcile_findings(
            self.db_pool,
            source=str(inp.get("source") or "manual"),
            subject_kind=str(inp.get("subject_kind") or "service"),
            classes=[str(c) for c in (inp.get("classes") or [])],
            findings=findings,
        )

    # --- legacy sweep stubs (one release; see the module docstring) ---------

    @activity.defn
    async def promote_expired_suppressions(self) -> dict:
        """Retired with the deploy/outage windows. No-op stub for a sweep
        recorded before `PATCH_DROP_INFRA_STEPS`."""
        return {"promoted": 0, "problem_ids": []}

    @activity.defn
    async def promoted_investigations(self, problem_ids: list[str]) -> list[dict]:
        """Retired with the investigations. No-op stub, as above."""
        return []

    @activity.defn
    async def reconcile_alertmanager(self, url: str, min_uptime_seconds: int = 900) -> dict:
        """Retired with the alertmanager intake. No-op stub, as above."""
        return {"resolved": 0, "checked": 0, "skipped": "retired"}

    @activity.defn
    async def retire_cards(self, inp: dict) -> dict:
        """Retired with the investigations' decision cards. No-op stub, as
        above."""
        return {"retired": 0, "finished": 0}

    @activity.defn
    async def verify_fixes(self, window_hours: float = 0.0, grace_hours: float = 0.0) -> dict:
        """Retired: v1 no longer follows fix PRs (the GitHub intake moved to the
        v2 Development vertical), so there is nothing to verify. Kept as a no-op
        for one release: a `HubSweepFlow` in flight across the deploy still
        schedules it (`hub_sweep.PATCH_DROP_FIX_VERIFICATION`). Remove it with
        that patch."""
        return {"resolved": 0, "reopened": 0, "problem_ids": []}

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
        if counts["muted"]:
            head += f" Not raised: {counts['muted']} muted."
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
                agent_id=await self._owner_agent() or None,
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
            agent_id=await self._owner_agent(),
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
