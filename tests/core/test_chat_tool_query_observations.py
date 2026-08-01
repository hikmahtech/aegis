"""The query_observations chat tool, against a real Postgres fixture."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from aegis.services.chat import TOOL_EXECUTORS, ToolContext, _get_agent_tools
from aegis.services.observations import record_observation

PREFIX = "zzc4chat-"
METRIC = f"{PREFIX}weight_kg"
SOURCE = f"{PREFIX}scale"

NOW = datetime.now(UTC)


@pytest_asyncio.fixture(loop_scope="function")
async def seeded(db_pool):
    await db_pool.execute("DELETE FROM life.observations WHERE metric LIKE $1", f"{PREFIX}%")
    # Current 10-day window: avg 71.0. Previous 10-day window: avg 74.0.
    for days_ago, value in ((1, 70.5), (3, 71.5), (12, 74.5), (15, 73.5)):
        await record_observation(
            db_pool, SOURCE, METRIC, value, observed_at=NOW - timedelta(days=days_ago)
        )
    yield db_pool
    await db_pool.execute("DELETE FROM life.observations WHERE metric LIKE $1", f"{PREFIX}%")


async def test_summary_reports_stats_and_a_downward_trend(seeded):
    executor = TOOL_EXECUTORS["query_observations"]
    out = await executor(
        seeded,
        {"metric": METRIC, "window_days": 10},
        ToolContext(agent_id="sebas"),
    )
    assert f"{METRIC} — 2 observation(s) in the last 10 days" in out
    assert "latest 70.50" in out
    assert (NOW - timedelta(days=1)).date().isoformat() in out
    assert "min 70.50 / max 71.50 / avg 71.00" in out
    # 71.0 now vs 74.0 in the preceding 10-day window.
    assert "trend: down 3.00 vs the previous 10 days (avg 74.00)" in out


async def test_metric_lookup_is_case_insensitive(seeded):
    """The LLM will send the metric however the user said it."""
    executor = TOOL_EXECUTORS["query_observations"]
    out = await executor(
        seeded, {"metric": METRIC.upper(), "window_days": 10}, ToolContext(agent_id="sebas")
    )
    assert "avg 71.00" in out


async def test_window_without_earlier_data_says_so_instead_of_inventing_a_trend(seeded):
    executor = TOOL_EXECUTORS["query_observations"]
    out = await executor(
        seeded, {"metric": METRIC, "window_days": 60}, ToolContext(agent_id="sebas")
    )
    assert "4 observation(s) in the last 60 days" in out
    assert "trend: no data for the previous 60 days to compare against" in out


async def test_unknown_metric_gets_a_graceful_no_data_answer(seeded):
    executor = TOOL_EXECUTORS["query_observations"]
    out = await executor(
        seeded, {"metric": f"{PREFIX}never_recorded"}, ToolContext(agent_id="sebas")
    )
    assert f"No '{PREFIX}never_recorded' observations in the last 30 days" in out
    # Must not pretend there are stats.
    assert "avg" not in out


async def test_metadata_only_metric_does_not_format_none_as_a_number(seeded):
    """Rows with a NULL value (location pings) have no avg — formatting one
    with :.2f would raise TypeError and surface as a tool error."""
    await record_observation(
        seeded, SOURCE, f"{PREFIX}location", None, observed_at=NOW, metadata={"lat": 1.0}
    )
    executor = TOOL_EXECUTORS["query_observations"]
    out = await executor(
        seeded, {"metric": f"{PREFIX}location"}, ToolContext(agent_id="sebas")
    )
    assert "1 observation(s)" in out
    assert "metadata only" in out


@pytest.mark.asyncio
async def test_refuses_empty_metric():
    executor = TOOL_EXECUTORS["query_observations"]
    assert await executor(None, {"metric": "  "}, ToolContext(agent_id="sebas")) == (
        "Refused: empty metric"
    )


def test_tool_is_granted_to_sebas_but_not_to_ungranted_agents():
    """Narrow, opt-in capability: it must NOT leak in via the fallback set."""
    names = {t["function"]["name"] for t in _get_agent_tools("sebas")}
    assert "query_observations" in names

    for agent_id in ("raphael", "some-unconfigured-agent"):
        other = {t["function"]["name"] for t in _get_agent_tools(agent_id)}
        assert "query_observations" not in other, agent_id


def test_tool_appears_when_granted_through_agent_metadata():
    """agents.metadata.tool_set (admin Behavior tab) is the runtime override —
    on an existing deployment that DB value, not AGENT_TOOL_SETS, decides."""
    names = {
        t["function"]["name"]
        for t in _get_agent_tools("raphael", {"tool_set": ["query_observations"]})
    }
    assert names == {"query_observations"}
