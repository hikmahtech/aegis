"""Runs in flight across the #502 deploy replay through the new flow.

#502 moved the verdict's knowledge-store write from Step 7b — before the
Gate-2 card — to where the outcome is known, behind the
`kg-verdict-after-decision` patch. An AlertInvestigationFlow run can wait 48
hours on its card, so some are always in flight when the worker is
redeployed, and the worker replays each one's history through the new code.

Each history below is recorded by the flow as it was before this change
(`_alert_investigation_pre502.py`, a frozen copy) and replayed through the
current flow. Between them they walk every place the patch changed the
command sequence: no card; each answer that carries on (ack, mute, Open PR
with and without a PR); each answer that ends the run (discard, run fix, the
self-resolved race); a card nobody answered; a Jira scoping run; and a card
still open, plain and escalating, which is where a run in flight actually is.
Remove the guard and every one fails but the Jira run: with no card and no
no-card marker between the old write and the new one, its write lands on the
same command either way.
"""

from __future__ import annotations

import pytest
from aegis_worker.flows.alert_investigation import AlertInvestigationFlow

from tests.worker.flows import _alert_flow_harness as h
from tests.worker.flows._alert_investigation_pre502 import AlertInvestigationFlowPre502
from tests.worker.flows.test_alert_investigation_replay import OpenCard, _record, _replays

pytestmark = pytest.mark.asyncio


def _scenario(name: str) -> tuple[dict, dict]:
    """Set the stubs up for `name`; returns (alert, record kwargs)."""
    h.reset()
    kwargs: dict = {}
    alert = h.app_alert()
    if name == "no_card":
        pass
    elif name == "jira":
        h.fix_branch()
        alert = h.app_alert(source="todoist-jira")
    elif name == "run_fix":
        h.S.answer = {"value": "run_fix"}
        h.S.run_investigation = {
            **h.S.run_investigation,
            "output": "Stuck.\n\nPROPOSED_COMMANDS:\n- docker service update --force shop_web\n",
        }
        h.S.verdict = {**h.S.verdict, "status": "actionable"}
        alert = h.service_down_alert()
    else:
        h.fix_branch()
        if name == "open_card":
            kwargs["card_cls"] = OpenCard
        elif name == "open_escalating_card":
            kwargs["card_cls"] = OpenCard
            alert = h.app_alert(escalate=True)
        elif name == "expired":
            h.S.card_status = "archived"
        elif name == "open_all_prs_none_opened":
            h.S.answer = {"value": "open_all_prs"}
            h.S.pr_url = ""
        else:
            h.S.answer = {"value": name}
    return alert, kwargs


SCENARIOS = [
    "no_card",
    "ack",
    "mute_24h",
    "open_all_prs",
    "open_all_prs_none_opened",
    "discard",
    "self_resolved",
    "run_fix",
    "expired",
    "jira",
    "open_card",
    "open_escalating_card",
]


@pytest.mark.parametrize("scenario", SCENARIOS)
async def test_a_run_recorded_before_502_replays_through_the_new_flow(scenario):
    alert, kwargs = _scenario(scenario)
    history = await _record(AlertInvestigationFlowPre502, alert, **kwargs)
    # The old flow stored every verdict before its card, untagged.
    assert [k["outcome"] for k in h.S.kg] == [""]
    await _replays(history)


@pytest.mark.parametrize("scenario", SCENARIOS)
async def test_the_new_flow_replays_its_own_histories(scenario):
    alert, kwargs = _scenario(scenario)
    history = await _record(AlertInvestigationFlow, alert, **kwargs)
    await _replays(history)
