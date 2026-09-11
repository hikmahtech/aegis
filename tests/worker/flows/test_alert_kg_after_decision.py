"""The verdict reaches the knowledge store after the operator decides, tagged
with the decision, and a fix PR leaves its problem in `fixing` (#502).

Before this, Step 7b stored the verdict before the Gate-2 card went out, so a
verdict the operator discarded was recalled next time exactly like one they
acted on — and the discard branch's comment said the opposite. And the flow's
last step moved a problem whose PR had just opened from `fixing` back to
`waiting_human`, so nothing about it said a fix was on its way.

Each test runs the current flow over the shared stubs and reads what it
stored: one write per run, after the card (when there is one), with the
outcome the run ended on.
"""

from __future__ import annotations

import pytest
from aegis_worker.flows.alert_investigation import AlertInvestigationFlow

from tests.worker.flows import _alert_flow_harness as h

pytestmark = pytest.mark.asyncio


def _stored() -> list[tuple[str, int]]:
    """(outcome, cards that had gone out) for every store write."""
    return [(k["outcome"], k["cards_before"]) for k in h.S.kg]


async def test_a_verdict_with_nothing_to_decide_is_stored_as_no_card():
    h.reset()
    result = await h.run_flow(AlertInvestigationFlow, h.app_alert())
    assert result["decision_card"] is False
    assert _stored() == [("no_card", 0)]


@pytest.mark.parametrize(
    ("answer", "outcome"),
    [("ack", "acknowledged"), ("mute_24h", "muted"), ("discard", "discarded")],
)
async def test_the_answer_on_the_card_is_the_outcome(answer, outcome):
    h.reset(answer={"value": answer})
    h.fix_branch()
    await h.run_flow(AlertInvestigationFlow, h.app_alert())
    assert len(h.S.cards) == 1
    # Stored once, and only after the card was answered.
    assert _stored() == [(outcome, 1)]


async def test_an_opened_fix_pr_is_stored_as_approved_and_the_problem_stays_fixing():
    h.reset(answer={"value": "open_all_prs"})
    h.fix_branch()
    result = await h.run_flow(AlertInvestigationFlow, h.app_alert())
    assert _stored() == [("opened_pr", 1)]
    by_step = {r["external_id"].rsplit(":", 1)[-1]: r for r in h.S.records}
    assert by_step["prs_opened"]["payload"] == {"pr_urls": [h.S.pr_url]}
    assert by_step["prs_opened"]["status"] == "fixing"
    # The last step no longer hands a problem with an open PR back to a human.
    assert by_step["final"]["status"] == "fixing"
    assert result["status"] == "logged"
    note = next(text for _, text in h.S.notes if h.S.pr_url in text)
    assert "when it merges" in note


async def test_an_approved_pr_that_could_not_open_is_stored_as_pr_failed():
    h.reset(answer={"value": "open_all_prs"}, pr_url="")
    h.fix_branch()
    await h.run_flow(AlertInvestigationFlow, h.app_alert())
    assert _stored() == [("pr_failed", 1)]
    final = next(r for r in h.S.records if r["external_id"].endswith(":final"))
    assert final["status"] == "waiting_human"


async def test_a_card_nobody_answered_is_stored_as_expired():
    h.reset(card_status="archived")
    h.fix_branch()
    result = await h.run_flow(AlertInvestigationFlow, h.app_alert())
    assert result["status"] == "gate2_archived"
    assert _stored() == [("expired", 1)]


async def test_an_alert_that_cleared_during_the_card_is_stored_as_self_resolved():
    h.reset(answer={"value": "self_resolved"})
    h.fix_branch()
    result = await h.run_flow(AlertInvestigationFlow, h.app_alert())
    assert result["status"] == "self_resolved_during_gate"
    assert _stored() == [("self_resolved", 1)]


async def test_run_fix_is_stored_as_approved():
    h.reset(answer={"value": "run_fix"})
    h.S.run_investigation = {
        **h.S.run_investigation,
        "output": "Scheduler stuck.\n\nPROPOSED_COMMANDS:\n- docker service update --force shop_web\n",
    }
    h.S.verdict = {**h.S.verdict, "status": "actionable"}
    result = await h.run_flow(AlertInvestigationFlow, h.service_down_alert())
    assert result["status"] == "remediation_refused"
    assert "run_fix" in h.S.cards[0].options
    assert _stored() == [("run_fix", 1)]


async def test_a_jira_scoping_run_has_no_card_and_is_stored_as_such():
    h.reset()
    h.fix_branch()
    await h.run_flow(AlertInvestigationFlow, h.app_alert(source="todoist-jira"))
    assert h.S.cards == []
    assert _stored() == [("no_card", 0)]
