"""A Gate-2 card only when there is a decision to make (#500).

In the two weeks before this change Pandora posted 47 verdict cards, and 27 of
the 38 answered were a bare `ack`: the card offered nothing a person could
approve. A card now goes out only when the card itself can do something:

* a fix branch to open as a PR,
* proposed commands to run, on an `actionable` verdict only (#518),
* an escalating alert, which nags until someone acks it,
* a restart that did not stick (#501, see test_alert_restart_once_flow.py).

Anything else is told, not asked: the verdict goes on the task and on the
problem's timeline, and the usual chat ping follows, which is exactly what an
`ack` used to lead to. Commands proposed on an `inconclusive` or "no action
needed" verdict go on the task comment, for a person to run by hand: 22 such
cards in those two weeks drew 17 bare acks and one Run fix.
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


_PROPOSED = (
    "Memory is exhausted.\n\nPROPOSED_COMMANDS:\n"
    "- docker service update --force shop_web\n"
    "- docker service ps shop_web\n"
)


def _memory_alert() -> dict:
    """Infra (HostOutOfMemory is on the default list) and never restarted, so
    the investigation is asked for a PROPOSED_COMMANDS footer and nothing
    else gets in the way."""
    return app_alert(
        title="Host out of memory",
        source="alertmanager",
        labels={"alertname": "HostOutOfMemory"},
    )


async def test_proposed_commands_on_an_actionable_verdict_get_a_card():
    """The investigation established a cause and says what fixes it: Run fix,
    which only the card can approve."""
    reset()
    S.run_investigation = {**S.run_investigation, "output": _PROPOSED}
    S.verdict = {**S.verdict, "status": "actionable"}

    result = await run_flow(AlertInvestigationFlow, _memory_alert())

    assert len(S.cards) == 1
    assert "run_fix" in S.cards[0].options
    assert result["decision_card"] is True


@pytest.mark.parametrize("verdict_status", ["inconclusive", "not_actionable"])
async def test_proposed_commands_on_a_verdict_that_is_not_actionable_go_to_the_task(
    verdict_status,
):
    """No cause established, or nothing to do: the commands are a guess or a
    contradiction, and in two weeks such cards drew 17 bare acks and one Run
    fix. No card; the commands go on the task, not run, for a person to run
    by hand."""
    reset()
    S.run_investigation = {**S.run_investigation, "output": _PROPOSED}
    S.verdict = {**S.verdict, "status": verdict_status}

    result = await run_flow(AlertInvestigationFlow, _memory_alert())

    assert S.cards == []
    assert result["decision_card"] is False
    assert "gate2" not in steps(S.records)
    final_note = S.notes[-1][1]
    assert "no card" in final_note.lower()
    assert "not run" in final_note
    assert "  - docker service update --force shop_web" in final_note
    assert "  - docker service ps shop_web" in final_note


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


def _needs(verdict_status="inconclusive", **kw) -> bool:
    args = {"branches": {}, "proposed_cmds": [], "escalate": False, "restart_repeat": False}
    return gate2_needs_decision(**{**args, **kw}, verdict_status=verdict_status)


@pytest.mark.parametrize("verdict_status", ["actionable", "inconclusive", "not_actionable"])
def test_the_rule_names_every_reason_for_a_card(verdict_status):
    """The rule on its own, so the 14-day replay in the PR can use it too.
    A branch, an escalation and a restart that did not stick earn a card
    whatever the verdict; commands only on an actionable one."""
    assert _needs(verdict_status) is False
    assert _needs(verdict_status, branches={"r": "b"})
    assert _needs(verdict_status, escalate=True)
    assert _needs(verdict_status, restart_repeat=True)
    assert _needs(verdict_status, proposed_cmds=["x"]) is (verdict_status == "actionable")
