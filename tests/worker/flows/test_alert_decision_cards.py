"""A Gate-2 card only when there is a decision to make (#500).

In the two weeks before this change Pandora posted 47 verdict cards, and 27 of
the 38 answered were a bare `ack`: the card offered nothing a person could
approve. A card now goes out only when the card itself can do something:

* a fix branch to open as a PR,
* proposed commands to run,
* an escalating alert, which nags until someone acks it,
* a restart that did not stick (#501, see test_alert_restart_once_flow.py).

Anything else is told, not asked: the verdict goes on the task and on the
problem's timeline, and the usual chat ping follows, which is exactly what an
`ack` used to lead to.
"""

from __future__ import annotations

import pytest
from aegis_worker.flows.alert_investigation import AlertInvestigationFlow, gate2_needs_decision

from tests.worker.flows._alert_flow_harness import (
    S,
    app_alert,
    reset,
    run_flow,
    service_down_alert,
    steps,
)


@pytest.mark.parametrize(
    ("verdict_status", "final_status"),
    [("not_actionable", "not_actionable"), ("inconclusive", "inconclusive"), ("actionable", "logged")],
)
async def test_a_verdict_with_nothing_to_decide_sends_no_card(verdict_status, final_status):
    """No fix branch, no commands, not escalating: no card, whatever the
    verdict says. An `actionable` verdict with no branch is still work for a
    person, but nothing a card can approve, so it lives on the task."""
    reset()
    S.verdict = {**S.verdict, "status": verdict_status}

    result = await run_flow(AlertInvestigationFlow, app_alert())

    assert S.cards == []
    assert result["status"] == final_status
    assert result["decision_card"] is False
    # The timeline has the investigation and the verdict, and no card.
    assert steps(S.records) == ["investigating", "final"]
    assert S.records[-1]["status"] == "waiting_human"
    assert S.records[-1]["payload"]["decision_card"] is False
    # The verdict is on the task, with a word on why nothing was asked.
    final_note = S.notes[-1][1]
    assert "no card" in final_note.lower()
    assert "Problems page" in final_note
    # And chat still hears the verdict, as a ping rather than a question.
    assert S.messages and "Full verdict on Todoist" in S.messages[-1]


async def test_a_fix_branch_still_gets_a_card():
    reset()
    S.run_investigation = {**S.run_investigation, "branches": {"shop": "aegis-fix/1"}}
    S.verdict = {**S.verdict, "status": "actionable"}

    result = await run_flow(AlertInvestigationFlow, app_alert())

    assert len(S.cards) == 1
    assert "open_all_prs" in S.cards[0].options
    assert result["decision_card"] is True
    assert "gate2" in steps(S.records)


async def test_proposed_commands_still_get_a_card():
    """An infra investigation that ends in a PROPOSED_COMMANDS footer offers
    Run fix, which only the card can approve."""
    reset()
    S.run_investigation = {
        **S.run_investigation,
        "output": "Memory is exhausted.\n\nPROPOSED_COMMANDS:\n- docker service update --force shop_web\n",
    }
    S.verdict = {**S.verdict, "status": "inconclusive"}
    alert = app_alert(
        title="Host out of memory",
        source="alertmanager",
        labels={"alertname": "HostOutOfMemory"},
    )

    result = await run_flow(AlertInvestigationFlow, alert)

    assert len(S.cards) == 1
    assert "run_fix" in S.cards[0].options
    assert result["decision_card"] is True


async def test_an_escalating_alert_with_nothing_to_decide_still_gets_a_card():
    """A node that is down nags until someone acks it, so its card stays."""
    reset()
    S.verdict = {**S.verdict, "status": "inconclusive"}
    alert = service_down_alert(
        title="Swarm node wow down",
        labels={"alertname": "NodeDown"},
        escalate=True,
        service="",
    )

    result = await run_flow(AlertInvestigationFlow, alert)

    assert len(S.cards) == 1
    assert S.cards[0].metadata and "escalation" in S.cards[0].metadata
    assert result["decision_card"] is True


def test_the_rule_names_every_reason_for_a_card():
    """The rule on its own, so the 14-day replay in the PR can use it too."""
    assert gate2_needs_decision(branches={}, proposed_cmds=[], escalate=False, restart_repeat=False) is False
    assert gate2_needs_decision(branches={"r": "b"}, proposed_cmds=[], escalate=False, restart_repeat=False)
    assert gate2_needs_decision(branches={}, proposed_cmds=["x"], escalate=False, restart_repeat=False)
    assert gate2_needs_decision(branches={}, proposed_cmds=[], escalate=True, restart_repeat=False)
    assert gate2_needs_decision(branches={}, proposed_cmds=[], escalate=False, restart_repeat=True)
