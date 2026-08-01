"""The last_contact_with_person chat tool, against a real Postgres fixture."""

from __future__ import annotations

import datetime as dt

import pytest
import pytest_asyncio
from aegis.services.chat import TOOL_EXECUTORS, ToolContext, _get_agent_tools

PREFIX = "zzc3chat-"


@pytest_asyncio.fixture(loop_scope="function")
async def seeded(db_pool):
    await db_pool.execute("DELETE FROM life.people WHERE name LIKE $1", f"{PREFIX}%")
    await db_pool.execute(
        "INSERT INTO life.people "
        "(name, aliases, relationship, key_dates, notes, last_contact) "
        "VALUES ($1, $2, 'brother', $3, 'allergic to peanuts', $4)",
        f"{PREFIX}Imran",
        # Stored lowercased on write by services.people.normalize_aliases —
        # written pre-normalised here so the row matches production shape.
        ["moni", "imran@example.test"],
        {"birthday": "1990-03-04"},
        dt.datetime.now(dt.UTC) - dt.timedelta(days=20),
    )
    yield db_pool
    await db_pool.execute("DELETE FROM life.people WHERE name LIKE $1", f"{PREFIX}%")


async def test_last_contact_by_exact_name(seeded):
    executor = TOOL_EXECUTORS["last_contact_with_person"]
    out = await executor(seeded, {"name": f"{PREFIX}Imran"}, ToolContext(agent_id="sebas"))
    assert f"{PREFIX}Imran (brother)" in out
    assert "20 days ago" in out
    assert "birthday: 1990-03-04" in out
    assert "allergic to peanuts" in out


async def test_last_contact_by_mixed_case_alias(seeded):
    """Aliases are stored lowercased, so the probe must be lowercased too —
    'MoNi' has to find the row a plain `aliases @> ARRAY['MoNi']` would miss."""
    executor = TOOL_EXECUTORS["last_contact_with_person"]
    out = await executor(seeded, {"name": "MoNi"}, ToolContext(agent_id="sebas"))
    assert f"{PREFIX}Imran (brother)" in out
    assert "20 days ago" in out


async def test_last_contact_by_email_alias(seeded):
    executor = TOOL_EXECUTORS["last_contact_with_person"]
    out = await executor(
        seeded, {"name": "Imran@Example.Test"}, ToolContext(agent_id="sebas")
    )
    assert f"{PREFIX}Imran" in out


async def test_last_contact_unknown_person_says_so(seeded):
    executor = TOOL_EXECUTORS["last_contact_with_person"]
    out = await executor(
        seeded, {"name": f"{PREFIX}Nobody"}, ToolContext(agent_id="sebas")
    )
    assert "No one called" in out
    assert f"{PREFIX}Nobody" in out


async def test_last_contact_never_recorded(seeded):
    await seeded.execute(
        "INSERT INTO life.people (name, last_contact) VALUES ($1, NULL)",
        f"{PREFIX}Newcomer",
    )
    executor = TOOL_EXECUTORS["last_contact_with_person"]
    out = await executor(
        seeded, {"name": f"{PREFIX}Newcomer"}, ToolContext(agent_id="sebas")
    )
    assert "no contact recorded yet" in out


@pytest.mark.asyncio
async def test_last_contact_refuses_empty_name():
    executor = TOOL_EXECUTORS["last_contact_with_person"]
    assert await executor(None, {"name": "  "}, ToolContext(agent_id="sebas")) == (
        "Refused: empty name"
    )


def test_tool_is_granted_to_sebas_but_not_to_ungranted_agents():
    """Narrow, opt-in capability: it must NOT leak in via the fallback set."""
    names = {t["function"]["name"] for t in _get_agent_tools("sebas")}
    assert "last_contact_with_person" in names

    for agent_id in ("raphael", "some-unconfigured-agent"):
        other = {t["function"]["name"] for t in _get_agent_tools(agent_id)}
        assert "last_contact_with_person" not in other, agent_id


def test_tool_appears_when_granted_through_agent_metadata():
    """agents.metadata.tool_set (admin Behavior tab) is the runtime override."""
    names = {
        t["function"]["name"]
        for t in _get_agent_tools(
            "raphael", {"tool_set": ["last_contact_with_person"]}
        )
    }
    assert names == {"last_contact_with_person"}
