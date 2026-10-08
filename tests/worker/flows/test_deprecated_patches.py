"""A run that RECORDED a patch marker still replays after the branch went.

Retiring a `workflow.patched(X)` has two failure modes and only one of them is
the obvious one. The obvious one — a history with NO marker replaying against
code that lost the old branch — is why the markers existed. The other is the
reason `workflow.deprecate_patch` exists: a worker whose code no longer
mentions the id AT ALL rejects a history that HAS the marker, with

    [TMPRL1100] Non-deprecated patch marker encountered ...
    there is no corresponding change command

and every run started under today's production worker carries these markers.
`HubSweepFlow` runs every five minutes, so a rollout always lands on some.

So this records a history WITH the markers in it and replays it through the
flow that now only calls `deprecate_patch`. It is falsifiable in the way that
matters: delete a `deprecate_patch` line from `hub_sweep.py` and this fails
with exactly the error above.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from temporalio import workflow
from temporalio.worker import Replayer

from tests.worker.flows.test_hub_sweep import (
    _apply,
    _calls,
    _finder,
    _judge,
    _NoInvestigation,
    _one_promoted_investigation,
    _project,
    _promote,
    _reconcile,
    _reconcile_am,
    _retire_cards,
    _run,
    _verify,
    _verify_args,
)

with workflow.unsafe.imports_passed_through():
    from aegis_worker.flows.hub_sweep import (
        PATCH_ALERTMANAGER_RECONCILE,
        PATCH_COMPLETED_TASKS,
        PATCH_DROP_FIX_VERIFICATION,
        PATCH_FIX_VERIFICATION,
        PATCH_PROMOTE_INVESTIGATES,
        PATCH_RETIRE_CARDS,
        HubSweepConfig,
        HubSweepFlow,
    )

pytestmark = pytest.mark.asyncio

_SHORT = timedelta(seconds=30)
# What the Python SDK calls a patch marker in workflow history.
_PATCH_MARKER = "core_patch"


@workflow.defn(name="HubSweepFlow")
class _SweepWithPatches:
    """The sweep as the deployed worker runs it: the three steps behind their
    `workflow.patched` guards, which is what puts the markers in the history."""

    @workflow.run
    async def run(self, config: HubSweepConfig) -> dict:
        await workflow.execute_activity(
            "promote_expired_suppressions", start_to_close_timeout=_SHORT
        )
        if workflow.patched(PATCH_COMPLETED_TASKS):
            await workflow.execute_activity(
                "reconcile_completed_tasks", start_to_close_timeout=_SHORT
            )
        if workflow.patched(PATCH_FIX_VERIFICATION):
            await workflow.execute_activity(
                "verify_fixes", args=[24.0, 1.0], start_to_close_timeout=_SHORT
            )
        # No alertmanager_url on the default config, so the old code recorded
        # this marker and then did nothing — which is the shape that matters:
        # the marker is in the history with no activity behind it.
        if workflow.patched(PATCH_ALERTMANAGER_RECONCILE) and config.alertmanager_url:
            pass
        await workflow.execute_activity("project_pending", start_to_close_timeout=_SHORT)
        await workflow.execute_activity(
            "find_group_candidates", args=[0, 0.0], start_to_close_timeout=_SHORT
        )
        return {}


def _marker_names(history) -> list[str]:
    return [
        e.marker_recorded_event_attributes.marker_name
        for e in history.events
        if e.HasField("marker_recorded_event_attributes")
    ]


async def test_a_history_carrying_the_markers_replays_on_the_marker_free_flow():
    _, history = await _run(
        [_promote, _reconcile, _verify, _project, _finder([])],
        workflows=(_SweepWithPatches,),
        flow=_SweepWithPatches,
    )
    # The premise, checked: the recorded run really did write three patch
    # markers. Without this the replay below could pass for the wrong reason —
    # and the marker name is the SDK's (`core_patch` on temporalio 1.x), not
    # one this repo chooses, so read it rather than assume it.
    assert _marker_names(history).count(_PATCH_MARKER) == 3, _marker_names(history)
    await Replayer(workflows=[HubSweepFlow]).replay_workflow(history)


async def test_the_flow_still_replays_its_own_history():
    """`deprecate_patch` records its own marker on a fresh run, so the flow has
    to accept that too — or the deploy after this one wedges everything this
    one started."""
    _, history = await _run(
        [_promote, _reconcile, _verify, _project, _finder([]), _judge(True), _apply]
    )
    await Replayer(workflows=[HubSweepFlow]).replay_workflow(history)


@workflow.defn(name="HubSweepFlow")
class _SweepBeforeTheFixStepWent:
    """The sweep as the worker before `PATCH_DROP_FIX_VERIFICATION` ran it:
    `verify_fixes` unconditionally, between the completed-task read-back and the
    card retirement. Every run in flight at that deploy has this history."""

    @workflow.run
    async def run(self, config: HubSweepConfig) -> dict:
        promoted = await workflow.execute_activity(
            "promote_expired_suppressions", start_to_close_timeout=_SHORT
        )
        if workflow.patched(PATCH_PROMOTE_INVESTIGATES) and promoted.get("problem_ids"):
            await workflow.execute_activity(
                "promoted_investigations",
                args=[list(promoted["problem_ids"])],
                start_to_close_timeout=_SHORT,
            )
        workflow.deprecate_patch(PATCH_COMPLETED_TASKS)
        await workflow.execute_activity(
            "reconcile_completed_tasks", start_to_close_timeout=_SHORT
        )
        workflow.deprecate_patch(PATCH_FIX_VERIFICATION)
        await workflow.execute_activity(
            "verify_fixes", args=[24.0, 1.0], start_to_close_timeout=_SHORT
        )
        workflow.deprecate_patch(PATCH_ALERTMANAGER_RECONCILE)
        if workflow.patched(PATCH_RETIRE_CARDS):
            await workflow.execute_activity("retire_cards", args=[{}], start_to_close_timeout=_SHORT)
        await workflow.execute_activity("project_pending", start_to_close_timeout=_SHORT)
        await workflow.execute_activity(
            "find_group_candidates", args=[0, 0.0], start_to_close_timeout=_SHORT
        )
        return {}


async def test_a_sweep_that_ran_verify_fixes_replays_after_the_step_went():
    """The fix verification left the sweep behind `workflow.patched`. A history
    with `verify_fixes` in it must still replay: falsifiable by deleting the
    `if not workflow.patched(PATCH_DROP_FIX_VERIFICATION)` branch, which fails
    this with a nondeterminism error (the history schedules an activity the
    code no longer does)."""
    _calls.clear()
    _verify_args.clear()
    _, history = await _run(
        [_promote, _reconcile, _verify, _project, _finder([])],
        workflows=(_SweepBeforeTheFixStepWent,),
        flow=_SweepBeforeTheFixStepWent,
    )
    assert "verify" in _calls  # the premise: the old shape really ran it
    await Replayer(workflows=[HubSweepFlow]).replay_workflow(history)


@workflow.defn(name="HubSweepFlow")
class _SweepBeforeTheInfraStepsWent:
    """The sweep as the worker before `PATCH_DROP_INFRA_STEPS` ran it (a0b2226):
    window promotion and the investigations it starts, the completed-task
    read-back, the fix-verification patch, the alertmanager reconcile, the card
    retirement, then projection and grouping. Every sweep in flight at the
    PR 3 deploy has this history."""

    @workflow.run
    async def run(self, config: HubSweepConfig) -> dict:
        promoted = await workflow.execute_activity(
            "promote_expired_suppressions", start_to_close_timeout=_SHORT
        )
        if workflow.patched(PATCH_PROMOTE_INVESTIGATES) and promoted.get("problem_ids"):
            alerts = await workflow.execute_activity(
                "promoted_investigations",
                args=[list(promoted["problem_ids"])],
                start_to_close_timeout=_SHORT,
            )
            stamp = workflow.now().strftime("%Y%m%d%H%M%S")
            for alert in alerts:
                await workflow.start_child_workflow(
                    "AlertInvestigationFlow",
                    alert,
                    id=f"investigate-{alert['problem_id']}-pr{stamp}",
                    parent_close_policy=workflow.ParentClosePolicy.ABANDON,
                )
        workflow.deprecate_patch(PATCH_COMPLETED_TASKS)
        await workflow.execute_activity(
            "reconcile_completed_tasks", start_to_close_timeout=_SHORT
        )
        workflow.deprecate_patch(PATCH_FIX_VERIFICATION)
        if not workflow.patched(PATCH_DROP_FIX_VERIFICATION):
            await workflow.execute_activity(
                "verify_fixes", args=[24.0, 1.0], start_to_close_timeout=_SHORT
            )
        workflow.deprecate_patch(PATCH_ALERTMANAGER_RECONCILE)
        if config.alertmanager_url:
            await workflow.execute_activity(
                "reconcile_alertmanager",
                args=[config.alertmanager_url, config.alertmanager_min_uptime_seconds],
                start_to_close_timeout=_SHORT,
            )
        if workflow.patched(PATCH_RETIRE_CARDS):
            await workflow.execute_activity("retire_cards", args=[{}], start_to_close_timeout=_SHORT)
        await workflow.execute_activity("project_pending", start_to_close_timeout=_SHORT)
        await workflow.execute_activity(
            "find_group_candidates",
            args=[config.group_min_members, config.group_window_hours],
            start_to_close_timeout=_SHORT,
        )
        return {}


async def test_a_sweep_that_ran_the_infra_steps_replays_after_they_went():
    """The infra steps left the sweep behind `PATCH_DROP_INFRA_STEPS`. A history
    with every one of them in it — a promotion that started an investigation,
    an alertmanager reconcile and a card retirement — must still replay.
    Falsifiable: make `run` call `_run_current` unconditionally and this fails
    with a nondeterminism error (the history schedules activities and a child
    the new shape never does)."""
    _calls.clear()
    _, history = await _run(
        [_promote, _one_promoted_investigation, _reconcile, _verify, _reconcile_am,
         _retire_cards, _project, _finder([])],
        workflows=(_SweepBeforeTheInfraStepsWent, _NoInvestigation),
        flow=_SweepBeforeTheInfraStepsWent,
        config=HubSweepConfig(agent_id="sebas", alertmanager_url="http://alertmanager:9093"),
    )
    # The premise: the old shape really ran every infra step.
    assert "promote" in _calls
    assert "promoted_investigations" in _calls
    assert "retire_cards" in _calls
    assert any(c.startswith("reconcile_alertmanager") for c in _calls)
    started = [
        e.start_child_workflow_execution_initiated_event_attributes.workflow_type.name
        for e in history.events
        if e.HasField("start_child_workflow_execution_initiated_event_attributes")
    ]
    assert started == ["AlertInvestigationFlow"]
    await Replayer(workflows=[HubSweepFlow]).replay_workflow(history)


async def test_a_new_sweep_records_the_infra_drop_marker():
    """A fresh tick takes the new shape and says so in its history, so the
    deploy after this one can tell the two apart."""
    _calls.clear()
    _, history = await _run([_promote, _reconcile, _verify, _project, _finder([])])
    assert _calls == ["reconcile", "project", "find"]
    assert _PATCH_MARKER in _marker_names(history)
    await Replayer(workflows=[HubSweepFlow]).replay_workflow(history)

