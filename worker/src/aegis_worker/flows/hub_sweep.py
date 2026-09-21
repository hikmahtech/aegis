"""HubSweepFlow — the problem hub's housekeeping tick.

Five things, in order:

1. Open every `suppressed` problem whose deploy, maintenance or outage
   window has passed without a `resolved` event. The heartbeat only emits on
   transitions, so a service that broke during a deploy and stayed broken
   would otherwise surface only at the 24h re-investigation. One raised by
   alertmanager or the heartbeat also gets the investigation the window held
   back (#630): a cluster outage that ends leaves one card for each thing
   still broken, instead of forty for everything that went down with it.
2. Read completed tasks back: a live problem whose Todoist task a person
   completed resolves, and a task the hub closed before the problem came
   back is reopened. Nothing else reads a completion back, so without this
   the problem stayed live for good while its task sat closed (#473).
3. Settle merged fixes (#502): a `verifying` problem — an investigation's fix
   PR merged — resolves once its alert has stayed clear for
   `fix_verify_hours`, and goes back to `open` when the alert comes back
   later than `fix_grace_hours` after the merge (`hub_fix.verify_fixes`).
4. Retire decision cards (#629): a resolve anywhere in the hub already moved
   its problem's pending cards out of `pending`; here each one's Slack message
   is edited to say so and its waiting run is ended.
   Then project: bring each problem's Todoist task up to date with its
   events, and create the task for anything promoted a moment ago.
5. Group: when several live problems share a class and a kind of subject, ask
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
    from aegis.errors import error_text, logged_failure

    from aegis_worker.activities.hub import HubActivities
    from aegis_worker.flows.alert_investigation import AlertInvestigationFlow
    from aegis_worker.shared.retry import (
        FAST,
        NO_RETRY,
        TIMEOUT_FAST,
        TIMEOUT_LLM,
        TIMEOUT_LONG,
        TIMEOUT_STANDARD,
    )

# At most this many clusters are judged in one tick: grouping is not urgent,
# and a sweep that runs every five minutes has no reason to spend four model
# calls at once.
_MAX_JUDGED_PER_TICK = 2

# Retired `workflow.patched` ids. The old branches are gone; the markers
# stay one release longer as `workflow.deprecate_patch`, because a run that
# RECORDED one is wedged by a worker whose code no longer mentions it at all
# ("[TMPRL1100] Non-deprecated patch marker encountered"). Drop the calls and
# these ids in the release after next — see #614.
# The sweep runs every five minutes, so a worker deployed mid-run always has
# some in flight.
PATCH_COMPLETED_TASKS = "hub-sweep-completed-tasks"
PATCH_FIX_VERIFICATION = "hub-sweep-fix-verification"
PATCH_ALERTMANAGER_RECONCILE = "hub-sweep-alertmanager-reconcile"

# Live patches. A sweep in flight across the deploy has none of these steps in
# its history, so `patched` answers False on its replay and it finishes as
# recorded; the next tick takes them.
# #630: a promoted problem gets the investigation its window held back.
PATCH_PROMOTE_INVESTIGATES = "hub-sweep-promote-investigates"
# #629: finish retired decision cards (edit the message, end the waiting run).
PATCH_RETIRE_CARDS = "hub-sweep-retire-cards"

@dataclass
class HubSweepConfig:
    agent_id: str = "pandoras-actor"
    # Both 0 mean "the service defaults" (3 members, seen in the last 72h).
    group_min_members: int = 0
    group_window_hours: float = 0.0
    # Step 3: how long a merged fix's alert must stay clear before the problem
    # resolves, and how long after the merge an occurrence is still put down
    # to the old code (`fix_verify_hours` / `fix_grace_hours` on the row).
    fix_verify_hours: float = 24.0
    fix_grace_hours: float = 1.0
    # Alertmanager's base URL, for resolving problems whose alert it no longer
    # lists (#551). The INTERNAL address — the public host is behind an identity
    # proxy — and empty, the default, disables the step: a fork ships nobody's
    # monitoring host. `alertmanager_min_uptime_seconds` is the guard that
    # matters: a freshly restarted alertmanager holds nothing until Prometheus
    # re-sends, and reconciling against that empty set would resolve the estate.
    alertmanager_url: str = ""
    alertmanager_min_uptime_seconds: int = 900


@workflow.defn
class HubSweepFlow:
    async def _investigate_promoted(self, problem_ids: list[str]) -> int:
        """Start an investigation for each promoted problem that should have
        one. Returns how many started."""
        alerts: list[dict] = []
        with logged_failure("hub_sweep_promoted_lookup_failed", logger=workflow.logger):
            alerts = await workflow.execute_activity_method(
                HubActivities.promoted_investigations,
                args=[problem_ids],
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=FAST,
            )
        started = 0
        stamp = workflow.now().strftime("%Y%m%d%H%M%S")
        for alert in alerts:
            # `investigate-<problem>-…`, the shape every hub investigation has,
            # so clarify and the card retirement both recognise it.
            child_id = f"investigate-{alert['problem_id']}-pr{stamp}"
            try:
                await workflow.start_child_workflow(
                    AlertInvestigationFlow.run,
                    alert,
                    id=child_id,
                    parent_close_policy=workflow.ParentClosePolicy.ABANDON,
                )
                started += 1
            except Exception as exc:  # noqa: BLE001 — already started is benign
                workflow.logger.warning(
                    "hub_sweep_promote_spawn_skipped id=%s err=%s", child_id, error_text(exc)
                )
        return started

    @workflow.run
    async def run(self, config: HubSweepConfig) -> dict:
        promoted = await workflow.execute_activity_method(
            HubActivities.promote_expired_suppressions,
            start_to_close_timeout=TIMEOUT_FAST,
            retry_policy=FAST,
        )
        # A promoted problem never had its investigation: the window held it
        # back, and nothing else will start one (#630). Started ABANDONED, as
        # the heartbeat starts its own: an investigation waits on a person and
        # must outlive this tick. A failure here is logged, never raised —
        # the problem is open and gets its task in this tick either way.
        investigated = 0
        if workflow.patched(PATCH_PROMOTE_INVESTIGATES) and promoted.get("problem_ids"):
            investigated = await self._investigate_promoted(list(promoted["problem_ids"]))
        # Then read completions back: a task a person ticked off resolves its
        # problem, before projection, so the resolve reaches the task in this
        # tick. FAST retries are safe — nothing is touched twice.
        # deprecate_patch: remove after the next release, see #614
        workflow.deprecate_patch(PATCH_COMPLETED_TASKS)
        completed = await workflow.execute_activity_method(
            HubActivities.reconcile_completed_tasks,
            start_to_close_timeout=TIMEOUT_FAST,
            retry_policy=FAST,
        )
        # Then settle merged fixes, also before projection, so the resolve or
        # the "it came back" reaches the task in this tick. A failure here is
        # logged, not raised: projection matters more than a verdict that
        # the next tick can reach just as well.
        verified: dict = {}
        # deprecate_patch: remove after the next release, see #614
        workflow.deprecate_patch(PATCH_FIX_VERIFICATION)
        with logged_failure("hub_sweep_verify_fixes_failed", logger=workflow.logger):
            verified = await workflow.execute_activity_method(
                HubActivities.verify_fixes,
                args=[config.fix_verify_hours, config.fix_grace_hours],
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=FAST,
            )
        # Then ask alertmanager what it is still holding, and resolve the live
        # problems it no longer lists (#551). Alertmanager keeps its alerts in
        # memory, so a restart loses every `resolved` webhook it owed — and that
        # lane is the only one on the hub with no other way back, so a lost
        # webhook stranded a problem and its Todoist task for good. Before
        # projection, so a resolve reaches the task in this tick.
        #
        # A failure is logged, never raised: the activity already fails closed
        # on an unreachable or freshly-restarted alertmanager, and projection
        # matters more than a reconciliation the next tick can do just as well.
        reconciled: dict = {}
        # deprecate_patch: remove after the next release, see #614
        workflow.deprecate_patch(PATCH_ALERTMANAGER_RECONCILE)
        if config.alertmanager_url:
            with logged_failure("hub_sweep_alertmanager_reconcile_failed", logger=workflow.logger):
                reconciled = await workflow.execute_activity_method(
                    HubActivities.reconcile_alertmanager,
                    args=[config.alertmanager_url, config.alertmanager_min_uptime_seconds],
                    start_to_close_timeout=TIMEOUT_STANDARD,
                    retry_policy=FAST,
                )
        # Then finish the decision cards a resolve retired (#629) — this tick's
        # included, which is why it comes after every step that resolves. The
        # hub already refuses their buttons; this edits each Slack message to
        # say why and ends the run still waiting on it.
        cards: dict = {}
        if workflow.patched(PATCH_RETIRE_CARDS):
            with logged_failure("hub_sweep_retire_cards_failed", logger=workflow.logger):
                cards = await workflow.execute_activity_method(
                    HubActivities.retire_cards,
                    args=[{}],
                    start_to_close_timeout=TIMEOUT_STANDARD,
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
            "promoted_investigated": investigated,
            "cards_finished": int(cards.get("finished") or 0),
            "task_completed": int(completed.get("resolved") or 0),
            "task_reopened": int(completed.get("tasks_reopened") or 0),
            "fix_resolved": int(verified.get("resolved") or 0),
            "fix_reopened": int(verified.get("reopened") or 0),
            "alertmanager_resolved": int(reconciled.get("resolved") or 0),
            "alertmanager_skipped": str(reconciled.get("skipped") or ""),
            # -1 = the step did not run at all (no URL, or the patch is off in a
            # replayed history). 0 or more = it ran and this many problems were
            # in scope. Without the sentinel, "resolved 0, skipped nothing"
            # reads identically whether it found nothing or never happened —
            # which is the same trap as a canary whose pass looks like no probe.
            "alertmanager_checked": int(reconciled.get("checked", -1)),
            "projected": int(projected.get("projected") or 0),
            "created": int(projected.get("created") or 0),
            "errors": int(projected.get("errors") or 0),
            "group_candidates": len(candidates),
            "grouped": len(grouped),
            "folded": sum(int(g.get("folded") or 0) for g in grouped),
        }
