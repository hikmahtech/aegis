"""One automatic restart per problem per window (#501), over stub activities.

On 2026-09-11 two koyra services were force-restarted, came back down ten
minutes later, and were force-restarted again, while the restart's own
evidence said the cause was scheduling, which a restart cannot fix. Now the
first restart is still automatic, and a problem that comes back inside the
window is not restarted again: its task and timeline get both attempts, the
investigation is told what already happened, and one card goes out whatever
the verdict says.

The lookup itself (`recent_auto_restart` against the real hub) is pinned in
tests/worker/test_alert_restart_once.py, and the two together end to end in
test_alert_restart_once_hub.py.
"""

from __future__ import annotations

from aegis_worker.flows.alert_investigation import AlertInvestigationFlow
from temporalio.exceptions import ApplicationError

from tests.worker.flows._alert_flow_harness import (
    S,
    reset,
    run_flow,
    service_down_alert,
    steps,
)

_THEN = [
    {
        "task_id": "t-old",
        "node": "noon",
        "current_state": "Rejected 11 minutes ago",
        "desired_state": "Shutdown",
        "error": "no suitable node (insufficient resources on 3 nodes)",
    }
]
_NOW = [
    {
        "task_id": "t-new",
        "node": "",
        "current_state": "Pending 2 minutes ago",
        "desired_state": "Running",
        "error": "no suitable node (scheduling constraints not satisfied on 9 nodes)",
    },
    *_THEN,
]


def _repeat() -> dict:
    return {
        "repeat": True,
        "window_minutes": 60,
        "service": "shop_web",
        "restarted_at": "2026-09-11T12:31:15+00:00",
        "minutes_ago": 10.2,
        "command": "docker service update --force shop_web",
        "recovered": True,
        "diagnostics_then": _THEN,
        "diagnostics_now": _NOW,
        "new_tasks": _NOW[:1],
    }


async def test_a_restart_that_did_not_stick_is_not_restarted_again():
    reset(restart_history=_repeat())
    S.verdict = {**S.verdict, "status": "inconclusive"}

    result = await run_flow(AlertInvestigationFlow, service_down_alert())

    # Asked about this problem, and did not restart it.
    assert [pid for pid, _ in S.restart_checks] == ["prob-1"]
    assert S.restarts == []
    # The second attempt is on the timeline, carrying the first one.
    assert steps(S.records) == ["restart_repeat", "investigating", "gate2", "final"]
    repeat = S.records[0]
    assert repeat["status"] == ""
    assert repeat["payload"]["restart_repeat"]["restarted_at"] == "2026-09-11T12:31:15+00:00"
    assert repeat["payload"]["restart_repeat"]["diagnostics_then"] == _THEN
    # ...and on the task, with the evidence a person needs.
    note = S.notes[0][1]
    assert "came back" in note and "not restarting it again" in note
    assert "insufficient resources on 3 nodes" in note
    assert "scheduling constraints not satisfied" in note
    # The investigation is told a plain restart has already failed.
    context = S.investigations[0][2]
    assert "force-restarted" in context and "insufficient resources on 3 nodes" in context
    # One card, though the verdict alone would not have earned one.
    assert len(S.cards) == 1
    prompt = S.cards[0].prompt
    assert "did not stick" in prompt
    assert "insufficient resources on 3 nodes" in prompt
    assert "scheduling constraints not satisfied" in prompt
    assert result["decision_card"] is True
    assert result["restart_repeat"] is True


async def test_the_first_restart_is_still_automatic_and_keeps_its_evidence():
    reset()
    S.remediation = {
        "attempted": True,
        "recovered": True,
        "service": "shop_web",
        "command": "docker service update --force shop_web",
        "output": "",
        "reason": "recovered",
        "diagnostics": _THEN,
    }

    result = await run_flow(AlertInvestigationFlow, service_down_alert())

    assert result["status"] == "auto_remediated"
    assert len(S.restarts) == 1
    assert steps(S.records) == ["auto_remediated"]
    remediation = S.records[0]["payload"]["remediation"]
    assert remediation == {
        "service": "shop_web",
        "command": "docker service update --force shop_web",
        "recovered": True,
        "reason": "recovered",
        "diagnostics": _THEN,
    }
    assert S.cards == []


async def test_a_restart_that_did_not_recover_is_on_the_timeline_too():
    """It counts as the attempt: the next return inside the window is not
    restarted either."""
    reset()
    S.remediation = {
        "attempted": True,
        "recovered": False,
        "service": "shop_web",
        "command": "docker service update --force shop_web",
        "output": "",
        "reason": "restart_issued_not_converged",
        "diagnostics": _THEN,
    }

    await run_flow(AlertInvestigationFlow, service_down_alert())

    assert steps(S.records) == ["auto_restart_unrecovered", "investigating", "final"]
    first = S.records[0]
    assert first["status"] == ""
    assert first["payload"]["remediation"]["recovered"] is False
    assert first["payload"]["remediation"]["diagnostics"] == _THEN


async def test_a_lookup_that_fails_does_not_block_the_restart():
    """Not knowing is not a reason to leave a service down."""
    reset(restart_history=ApplicationError("hub unreachable", non_retryable=True))
    S.remediation = {
        "attempted": True,
        "recovered": True,
        "service": "shop_web",
        "command": "docker service update --force shop_web",
        "output": "",
        "reason": "recovered",
        "diagnostics": [],
    }

    result = await run_flow(AlertInvestigationFlow, service_down_alert())

    assert result["status"] == "auto_remediated"
    assert len(S.restarts) == 1


async def test_an_alert_the_restart_does_not_cover_is_never_looked_up():
    """NodeDown is infra but never restarted, so there is nothing to ask."""
    reset()
    alert = service_down_alert(
        title="Swarm node wow down",
        labels={"alertname": "NodeDown"},
        escalate=True,
        service="",
    )

    await run_flow(AlertInvestigationFlow, alert)

    assert S.restart_checks == []
