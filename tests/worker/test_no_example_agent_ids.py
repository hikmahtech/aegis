"""The worker finds no agent by an example id (#579).

The activity classes' owners come from the capability tags at boot, and
clarify's @pandora comment channel goes to whoever answers to @pandora.
"""

from __future__ import annotations

import inspect
from unittest.mock import AsyncMock

import pytest
from aegis_worker.activities import clarify as clarify_mod
from aegis_worker.activities.briefing import BriefingActivities
from aegis_worker.activities.clarify import ClarifyActivities
from aegis_worker.activities.flow_health import FlowHealthActivities
from aegis_worker.activities.gmail import GmailActivities
from aegis_worker.activities.homelab import HomelabActivities
from aegis_worker.activities.meeting import MeetingActivities
from aegis_worker.activities.money import MoneyActivities
from aegis_worker.activities.review import ReviewActivities
from aegis_worker.activities.social import SocialActivities


@pytest.mark.parametrize(
    "cls",
    [
        BriefingActivities,
        GmailActivities,
        MeetingActivities,
        ReviewActivities,
        HomelabActivities,
        MoneyActivities,
    ],
)
def test_an_activity_class_names_no_example_owner(cls):
    """`__main__` passes the tag holder; the default is nobody, not an example id."""
    assert inspect.signature(cls).parameters["agent_id"].default == ""


@pytest.mark.parametrize(
    "method", [FlowHealthActivities.report_flow_health, SocialActivities.report_stuck_posts]
)
def test_a_report_activity_names_no_example_owner(method):
    assert inspect.signature(method).parameters["agent_id"].default == ""


# --- clarify: the @pandora comment channel ----------------------------------------


def _connector() -> AsyncMock:
    connector = AsyncMock()
    connector.commands = AsyncMock(
        return_value={"ok": True, "data": {"sync_status": {}, "temp_id_mapping": {}}}
    )
    return connector


def _task() -> dict:
    return {
        "id": "task-p",
        "content": "Maintenance window prep",
        "description": "",
        "labels": ["@pandora", "#manual"],
        "source_tag": "#manual",
        "latest_user_note": "Comment from user.",
        "last_note_at": None,
    }


_DECISION = {
    "classification": "pandora_chat_followup",
    "confidence": 1.0,
    "assignee": "@pandora",
    "contexts": ["@deep"],
    "reason": "user comment on @pandora non-APP task",
    "llm_model": "rules",
}


def _registry(monkeypatch, reg: dict) -> None:
    async def _get(pool):
        return reg

    monkeypatch.setattr(clarify_mod, "get_agent_registry", _get)


async def test_pandora_followup_goes_to_whoever_answers_to_at_pandora(db_pool, monkeypatch):
    _registry(
        monkeypatch,
        {
            "jeeves": {"aliases": ["@jeeves"], "caps": {"gtd"}},
            "ops-bot": {"aliases": ["@pandora"], "caps": set()},
        },
    )
    acts = ClarifyActivities(db_pool=db_pool, todoist_connector=_connector())
    outcome = await acts.apply_outcome(_task(), dict(_DECISION))
    assert outcome["interaction_payload"]["target_agent"] == "ops-bot"


async def test_pandora_followup_falls_back_to_the_infra_holder(db_pool, monkeypatch):
    _registry(
        monkeypatch,
        {
            "jeeves": {"aliases": ["@jeeves"], "caps": {"gtd"}},
            "ops-bot": {"aliases": ["@ops"], "caps": {"infra"}},
        },
    )
    acts = ClarifyActivities(db_pool=db_pool, todoist_connector=_connector())
    outcome = await acts.apply_outcome(_task(), dict(_DECISION))
    assert outcome["interaction_payload"]["target_agent"] == "ops-bot"


async def test_pandora_followup_with_nobody_to_reply_is_left_for_later(db_pool, monkeypatch):
    """No agent answers to @pandora and none holds `infra`: nothing is spawned
    and nothing written, so the task stays unclarified until one is set up."""
    _registry(monkeypatch, {"jeeves": {"aliases": ["@jeeves"], "caps": {"gtd"}}})
    todoist = _connector()
    acts = ClarifyActivities(db_pool=db_pool, todoist_connector=todoist)
    outcome = await acts.apply_outcome(_task(), dict(_DECISION))
    assert outcome["applied"] is False
    assert not outcome.get("interaction_spawned")
    todoist.commands.assert_not_awaited()


async def test_the_library_notices_speak_as_the_research_holder(db_pool):
    """The seeded research holder speaks; with no pool, nobody (comms' default)."""
    acts = ClarifyActivities(db_pool=db_pool, todoist_connector=_connector())
    holder = await db_pool.fetchval(
        "SELECT id FROM agents WHERE active AND capabilities @> '[\"research\"]'::jsonb "
        "ORDER BY id LIMIT 1"
    )
    assert await acts._agent_holding("research") == (holder or "")
    assert await ClarifyActivities(db_pool=None, todoist_connector=_connector())._agent_holding(
        "research"
    ) == ""
