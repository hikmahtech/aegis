"""BriefingActivities.feed_review_line — the monthly "drop it?" line (#511)."""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from aegis_worker.activities.briefing import BriefingActivities
from aegis_worker.flows.daily_briefing import DailyBriefingConfig, DailyBriefingFlow
from temporalio import activity
from temporalio.testing import ActivityEnvironment, WorkflowEnvironment
from temporalio.worker import Worker

pytestmark = pytest.mark.asyncio

_PREFIX = "https://zzfeedreview.test/"


@pytest_asyncio.fixture(loop_scope="function")
async def pool(db_pool):
    await db_pool.execute("DELETE FROM channels WHERE identifier LIKE $1", _PREFIX + "%")
    yield db_pool
    await db_pool.execute("DELETE FROM channels WHERE identifier LIKE $1", _PREFIX + "%")


async def test_it_names_a_feed_with_90_days_of_history_and_no_use(pool):
    cid = await pool.fetchval(
        "INSERT INTO channels (kind, identifier, config) VALUES ('rss', $1, $2) RETURNING id::text",
        _PREFIX + "old",
        {"label": "Zz press releases"},
    )
    await pool.execute(
        "INSERT INTO feed_entries (channel_id, external_id, content_id, mode, seen_at) "
        "VALUES ($1::uuid, 'e1', 'zzreview-c1', 'full', now() - interval '120 days')",
        cid,
    )
    line = await ActivityEnvironment().run(BriefingActivities(db_pool=pool).feed_review_line)
    assert "Zz press releases" in line
    assert "unsubscribe" in line


async def test_no_pool_means_no_line():
    line = await ActivityEnvironment().run(BriefingActivities(db_pool=None).feed_review_line)
    assert line == ""


# --------------------------------------------------------------------------
# DailyBriefingFlow sends it to the research agent, on its day, and only there.
# --------------------------------------------------------------------------

_RESOLVE = {"finance": "maou", "infra": "pandoras-actor", "research": "raphael"}


async def _run_briefing(agent_id: str) -> list[tuple[str, str]]:
    from tests.worker.test_briefing_flow import _stubs

    sent: list = []

    @activity.defn(name="feed_review_line")
    async def review() -> str:
        return "Feeds no prompt used in 90 days: Zz feed."

    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue="brf-feed-review",
            workflows=[DailyBriefingFlow],
            activities=[*_stubs(sent, [], resolve_map=_RESOLVE), review],
        ),
    ):
        await env.client.execute_workflow(
            DailyBriefingFlow.run,
            DailyBriefingConfig(agent_id=agent_id, feed_review_day=0),
            id=f"brf-feed-{uuid.uuid4()}",
            task_queue="brf-feed-review",
        )
    return sent


async def test_the_research_agent_gets_the_feed_review():
    sent = await _run_briefing("raphael")
    assert any(a == "raphael" and "Feeds no prompt used" in m for a, m in sent)


async def test_another_agent_does_not():
    sent = await _run_briefing("sebas")
    assert not any("Feeds no prompt used" in m for _, m in sent)
