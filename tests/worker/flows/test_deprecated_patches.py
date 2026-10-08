"""A run that RECORDED a patch marker still replays after the branch went.

Retiring a `workflow.patched(X)` has two failure modes and only one of them is
the obvious one. The obvious one — a history with NO marker replaying against
code that lost the old branch — is why the markers existed. The other is the
reason `workflow.deprecate_patch` exists: a worker whose code no longer
mentions the id AT ALL rejects a history that HAS the marker, with

    [TMPRL1100] Non-deprecated patch marker encountered ...
    there is no corresponding change command

and every run started under today's production worker carries these markers.
An `AlertInvestigationFlow` can sit 48 hours on its Gate-2 card, `HubSweepFlow`
runs every five minutes and `InfraHeartbeatFlow` every two, so a rollout always
lands on some.

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
    _project,
    _promote,
    _reconcile,
    _run,
    _verify,
    _verify_args,
)

with workflow.unsafe.imports_passed_through():
    from aegis_worker.flows.hub_sweep import (
        PATCH_ALERTMANAGER_RECONCILE,
        PATCH_COMPLETED_TASKS,
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
