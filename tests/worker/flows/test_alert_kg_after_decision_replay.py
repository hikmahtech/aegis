"""Every Gate-2 outcome replays through the flow that recorded it (#502).

#502 put the verdict's knowledge-store write where the outcome is known
rather than before the card went out, so each answer writes at a different
point in the command sequence. An AlertInvestigationFlow run can wait 48
hours on its card, so some are always in flight when the worker is
redeployed and the worker replays their histories.

The scenarios walk every one of those points: no card; each answer that
carries on (ack, mute, Open PR with and without a PR); each answer that ends
the run (discard, run fix, the self-resolved race); a card nobody answered; a
Jira scoping run; and a card still open, plain and escalating, which is where
a run in flight actually is.
"""

from __future__ import annotations

import pytest
from aegis_worker.flows.alert_investigation import AlertInvestigationFlow

from tests.worker.flows import _alert_flow_harness as h
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
    # A newer card retired this one (#629).
    "superseded",
    "run_fix",
    "expired",
    "jira",
    "open_card",
    "open_escalating_card",
]


@pytest.mark.parametrize("scenario", SCENARIOS)
async def test_the_new_flow_replays_its_own_histories(scenario):
    alert, kwargs = _scenario(scenario)
    history = await _record(AlertInvestigationFlow, alert, **kwargs)
    await _replays(history)
