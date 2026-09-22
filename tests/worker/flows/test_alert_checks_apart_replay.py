"""A Run fix card posted before #641 still replays after it.

Before #641 every proposed command was a "fix". A run whose card went out
then offered Run fix for a read-only `service ps`, which the new split would
call a check and give no card at all, so the new code would issue different
commands. `gate2-checks-apart-from-fixes` is what keeps the old run on its
old path: this records such a run with the patch answering False (no marker
in the history, as on the deployed worker) and replays it through the flow
as it is now. Remove the `workflow.patched` guard and this fails.
"""

from __future__ import annotations

import pytest
from temporalio import workflow

from tests.worker.flows import _alert_flow_harness as h
from tests.worker.flows.test_alert_investigation_replay import _record, _replays

with workflow.unsafe.imports_passed_through():
    from aegis_worker.flows import alert_investigation as ai
    from aegis_worker.flows.alert_investigation import AlertInvestigationFlow

pytestmark = pytest.mark.asyncio


def _old_run() -> dict:
    h.reset()
    h.S.answer = {"value": "run_fix"}
    h.S.run_investigation = {
        **h.S.run_investigation,
        "output": "Stuck.\n\nPROPOSED_COMMANDS:\n- docker service ps shop_web\n",
    }
    h.S.verdict = {**h.S.verdict, "status": "actionable"}
    return h.app_alert(
        title="Host out of memory",
        source="alertmanager",
        labels={"alertname": "HostOutOfMemory"},
    )


async def test_a_run_fix_card_from_before_the_split_replays(monkeypatch):
    alert = _old_run()
    real_patched = workflow.patched

    def _before_641(patch_id: str) -> bool:
        if patch_id == ai._PATCH_CHECKS_APART:
            return False
        return real_patched(patch_id)

    monkeypatch.setattr(workflow, "patched", _before_641)
    history = await _record(AlertInvestigationFlow, alert)
    monkeypatch.undo()

    # The old path really was taken: Run fix on a card for a read-only command.
    assert h.S.cards and "run_fix" in h.S.cards[0].options
    assert "run_checks" not in h.S.cards[0].options

    await _replays(history)


async def test_the_same_run_today_gets_no_card():
    """The other half: with the split, that command is a check, so the
    verdict earns no card. This is the difference the patch guards."""
    alert = _old_run()
    history = await _record(AlertInvestigationFlow, alert)
    assert h.S.cards == []
    await _replays(history)
