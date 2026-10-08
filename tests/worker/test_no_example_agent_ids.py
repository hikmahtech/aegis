"""The worker finds no agent by an example id (#579).

The activity classes' owners come from the capability tags at boot.
"""

from __future__ import annotations

import inspect
from unittest.mock import AsyncMock

import pytest
from aegis_worker.activities.briefing import BriefingActivities
from aegis_worker.activities.clarify import ClarifyActivities
from aegis_worker.activities.flow_health import FlowHealthActivities
from aegis_worker.activities.gmail import GmailActivities
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


def _connector() -> AsyncMock:
    connector = AsyncMock()
    connector.commands = AsyncMock(
        return_value={"ok": True, "data": {"sync_status": {}, "temp_id_mapping": {}}}
    )
    return connector


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
