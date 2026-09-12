"""The task a settle window held back still gets the investigation's findings.

#537 stopped a young alert from earning a Todoist task until it outlived its
class's window. That left a hole this pins shut: the flow reads
`todoist_task_id` once, at step 0, and both producers now hand it None for
exactly the alerts the window defers. The task IS created a few seconds later
— moving the problem to `investigating` is what lifts the deferral — but the
flow was still holding None, and `_safe_post_note` returns silently on an
empty id. So the start comment, the restart evidence, the verdict and the
transcript were all dropped, and the chore the human acts on carried the
occurrence text and nothing the investigation found.

The fix is that `record_investigation` reports the task its own projection
minted, and the flow adopts it. Falsifiable two ways: stop returning `task_id`
from the activity, or drop the adoption in the flow, and the verdict comment
below lands on nothing.
"""

from __future__ import annotations

from aegis_worker.flows.alert_investigation import AlertInvestigationFlow
from temporalio import activity

from tests.worker.flows import _alert_flow_harness as h

MINTED = "task-minted-by-the-projection"


@activity.defn(name="problem_status")
async def _status_without_a_task(problem_id: str) -> dict:
    """The hub during the settle window: a live problem, no task yet."""
    return {
        "found": True,
        "status": "open",
        "resolved": False,
        "occurrences": 1,
        "todoist_task_id": None,
    }


@activity.defn(name="record_investigation")
async def _record_that_mints(inp: dict) -> dict:
    """The real activity projects, and that projection is what creates the task
    once the status moves off `open`. Only that first move mints one."""
    h.S.records.append(inp)
    minted = MINTED if inp.get("status") == "investigating" else ""
    return {"recorded": True, "status_changed": True, "task_id": minted}


def _stubs() -> list:
    swapped = {h.stub_problem_status, h.stub_record_investigation}
    return [s for s in h.STUBS if s not in swapped] + [
        _status_without_a_task,
        _record_that_mints,
    ]


async def test_a_deferred_task_still_hears_the_verdict():
    h.reset()

    result = await h.run_flow(
        AlertInvestigationFlow,
        # What the heartbeat hands over while the window defers: a problem, no
        # task. `problem_id` is set, so the flow takes its `problem_status`
        # branch and still finds nothing.
        h.service_down_alert(todoist_task_id=None),
        activities=_stubs(),
    )

    assert h.S.notes, "the flow posted nothing at all"
    # Every comment went to the task the projection minted, not to "".
    assert {task_id for task_id, _ in h.S.notes} == {MINTED}
    # And the one that matters is there: what the investigation concluded.
    assert any("upstream API was down" in body for _, body in h.S.notes)
    assert result.get("todoist_task_id") == MINTED


async def test_a_task_that_already_existed_is_not_replaced():
    """The ordinary case — a class with no window, or a problem past it — hands
    the flow a real id at step 0, and that id stays the one it uses."""
    h.reset()

    await h.run_flow(
        AlertInvestigationFlow,
        h.service_down_alert(todoist_task_id="task-from-step-zero"),
        activities=_stubs(),
    )

    assert {task_id for task_id, _ in h.S.notes} == {"task-from-step-zero"}
