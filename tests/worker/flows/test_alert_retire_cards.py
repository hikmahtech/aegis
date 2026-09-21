"""AlertInvestigationFlow and stale decision cards (#629).

Before its Gate-2 card goes out, a run retires the problem's older pending
cards, naming itself so it never retires its own. A card retired that way is
answered `superseded`, and the run waiting on it ends there without acting.

Runs wait up to 48 hours on a card, so some are always in flight across a
deploy. The last two tests replay histories through the new flow: one
recorded before this change (no retire call, no patch marker) and one after.
"""

from __future__ import annotations

import pytest
from aegis_worker.flows.alert_investigation import (
    _PATCH_RETIRE_OLD_CARDS,
    AlertInvestigationFlow,
)
from temporalio import workflow

from tests.worker.flows import _alert_flow_harness as h
from tests.worker.flows.test_alert_investigation_replay import OpenCard, _record, _replays

pytestmark = pytest.mark.asyncio


async def test_a_new_card_retires_the_older_ones_first():
    h.reset()
    h.fix_branch()
    result = await h.run_flow(AlertInvestigationFlow, h.app_alert())

    assert result["decision_card"] is True
    assert len(h.S.retires) == 1
    call = h.S.retires[0]
    assert call["problem_id"] == "prob-1"
    assert call["reason"] == "superseded"
    # It names the run posting the card, so that card is never retired.
    assert call["exclude_run"] and call["exclude_run"].startswith("alert-")
    # Before the card went out, and before the run recorded it on the problem:
    # the problem's timeline is how a card is found, so the new card's own
    # `gate2` event must not exist yet.
    assert call["cards_before"] == 0
    assert "gate2" not in h.steps(h.S.records[: call["records_before"]])


async def test_a_verdict_with_no_card_retires_nothing():
    h.reset()
    result = await h.run_flow(AlertInvestigationFlow, h.app_alert())
    assert result["decision_card"] is False
    assert h.S.retires == []


async def test_a_superseded_card_ends_its_run_without_acting():
    """What the old run does when a newer one retired its card: nothing. No
    task comment, no chat ping, no mute, no status on the problem, and no
    verdict stored — the newer run owns the problem now."""
    h.reset()
    h.fix_branch()
    h.S.answer = {"value": "superseded", "note": "auto-closed: newer card"}
    result = await h.run_flow(AlertInvestigationFlow, h.app_alert())

    assert result["status"] == "gate2_superseded"
    assert len(h.S.cards) == 1
    # The last thing on the problem is the card going out.
    assert h.steps(h.S.records)[-1] == "gate2"
    assert h.S.kg == []
    assert h.S.staged == []
    # The start-of-investigation note went out before the card; nothing after.
    assert not any("Acknowledged" in n or "Muted" in n for _, n in h.S.notes)
    assert h.S.messages == []


async def test_a_card_retired_as_resolved_still_takes_the_self_resolved_branch():
    """A resolve retires the card with `self_resolved`, the value the
    escalating race has always sent, so the existing branch handles it."""
    h.reset()
    h.fix_branch()
    h.S.answer = {"value": "self_resolved", "note": "auto-closed: problem resolved"}
    result = await h.run_flow(AlertInvestigationFlow, h.app_alert())
    assert result["status"] == "self_resolved_during_gate"
    assert h.S.staged == []


async def test_a_run_waiting_on_its_card_from_before_the_change_replays(monkeypatch):
    """The in-flight case. The history is recorded with the patch forced off,
    which is exactly what the code before #629 wrote: no marker and no retire
    call before the card. The new flow must replay it, not wedge it."""
    h.reset()
    h.fix_branch()
    real = workflow.patched

    def before_629(patch_id: str) -> bool:
        return False if patch_id == _PATCH_RETIRE_OLD_CARDS else real(patch_id)

    monkeypatch.setattr(workflow, "patched", before_629)
    history = await _record(AlertInvestigationFlow, h.app_alert(), card_cls=OpenCard)
    monkeypatch.undo()

    # The premise, checked: this really is an old history.
    assert h.S.retires == []
    names = [
        e.activity_task_scheduled_event_attributes.activity_type.name
        for e in history.events
        if e.HasField("activity_task_scheduled_event_attributes")
    ]
    assert "retire_cards" not in names
    await _replays(history)


async def test_a_run_waiting_on_its_card_after_the_change_replays():
    h.reset()
    h.fix_branch()
    history = await _record(AlertInvestigationFlow, h.app_alert(), card_cls=OpenCard)
    assert len(h.S.retires) == 1
    await _replays(history)
