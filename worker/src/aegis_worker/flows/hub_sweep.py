"""HubSweepFlow — the problem hub's housekeeping tick.

Four things, in order:

1. Open every `suppressed` problem whose deploy or maintenance window has
   passed without a `resolved` event. The heartbeat only emits on transitions,
   so a service that broke during a deploy and stayed broken would otherwise
   surface only at the 24h re-investigation.
2. Read completed tasks back: a live problem whose Todoist task a person
   completed resolves, and a task the hub closed before the problem came
   back is reopened. Nothing else reads a completion back, so without this
   the problem stayed live for good while its task sat closed (#473).
3. Project: bring each problem's Todoist task up to date with its events, and
   create the task for anything promoted a moment ago.
4. Group: when several live problems share a class and a kind of subject, ask
   the model whether they are one condition, and fold them into a single
   problem when they are. Six posts wedged in one Postiz queue were six
   problems and six tasks; grouped, they are one task, and the seventh stuck
   post joins it instead of opening another.

The grouping step costs a model call, so it only runs when a cluster is both
big enough and has not already been judged (`hub_group.recent_verdict`). Most
ticks make no call at all.
"""

from __future__ import annotations

from dataclasses import dataclass

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from aegis_worker.activities.hub import HubActivities
    from aegis_worker.shared.retry import (
        FAST,
        NO_RETRY,
        TIMEOUT_FAST,
        TIMEOUT_LLM,
        TIMEOUT_LONG,
    )

# At most this many clusters are judged in one tick: grouping is not urgent,
# and a sweep that runs every five minutes has no reason to spend four model
# calls at once.
_MAX_JUDGED_PER_TICK = 2
# The patch id for step 2. The sweep runs every five minutes, so a worker
# deployed mid-run replays a history that has no such activity in it.
PATCH_COMPLETED_TASKS = "hub-sweep-completed-tasks"


@dataclass
class HubSweepConfig:
    agent_id: str = "pandoras-actor"
    # Both 0 mean "the service defaults" (3 members, seen in the last 72h).
    group_min_members: int = 0
    group_window_hours: float = 0.0


@workflow.defn
class HubSweepFlow:
    @workflow.run
    async def run(self, config: HubSweepConfig) -> dict:
        promoted = await workflow.execute_activity_method(
            HubActivities.promote_expired_suppressions,
            start_to_close_timeout=TIMEOUT_FAST,
            retry_policy=FAST,
        )
        # Then read completions back: a task a person ticked off resolves its
        # problem, before projection, so the resolve reaches the task in this
        # tick. FAST retries are safe — nothing is touched twice.
        completed: dict = {}
        if workflow.patched(PATCH_COMPLETED_TASKS):
            completed = await workflow.execute_activity_method(
                HubActivities.reconcile_completed_tasks,
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=FAST,
            )
        # Then project: a problem promoted a moment ago gets its task in the
        # same tick, and any comment a producer's inline projection could not
        # post is retried here.
        projected = await workflow.execute_activity_method(
            HubActivities.project_pending,
            # LONG, not STANDARD: a sweep can project up to 50 problems, each
            # one or more Todoist calls with a 10s connector timeout, so a slow
            # Todoist used to time the activity out — and with NO_RETRY that
            # failed the whole sweep every five minutes.
            start_to_close_timeout=TIMEOUT_LONG,
            retry_policy=NO_RETRY,
        )
        # Then group. Candidates are cheap (one query); the judge is a billed
        # call, so it runs only on a cluster nothing has ruled on yet.
        candidates = await workflow.execute_activity_method(
            HubActivities.find_group_candidates,
            args=[config.group_min_members, config.group_window_hours],
            start_to_close_timeout=TIMEOUT_FAST,
            retry_policy=FAST,
        )
        grouped: list[dict] = []
        for candidate in candidates[:_MAX_JUDGED_PER_TICK]:
            verdict = await workflow.execute_activity_method(
                HubActivities.judge_group,
                args=[candidate],
                start_to_close_timeout=TIMEOUT_LLM,
                # NO_RETRY: billed, and a second opinion on the same cluster
                # is worth less than what it costs. A failed judge leaves the
                # problems separate, which is the safe direction.
                retry_policy=NO_RETRY,
            )
            if not verdict.get("group"):
                continue
            result = await workflow.execute_activity_method(
                HubActivities.apply_group,
                args=[candidate, verdict],
                # NO_RETRY: it merges problems, closes tasks and posts a card.
                start_to_close_timeout=TIMEOUT_LONG,
                retry_policy=NO_RETRY,
            )
            if result.get("grouped"):
                grouped.append(result)

        return {
            "promoted": int(promoted.get("promoted") or 0),
            "task_completed": int(completed.get("resolved") or 0),
            "task_reopened": int(completed.get("tasks_reopened") or 0),
            "projected": int(projected.get("projected") or 0),
            "created": int(projected.get("created") or 0),
            "errors": int(projected.get("errors") or 0),
            "group_candidates": len(candidates),
            "grouped": len(grouped),
            "folded": sum(int(g.get("folded") or 0) for g in grouped),
        }
