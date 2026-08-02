"""SocialPublishFlow orchestration — stubbed activities, time-skipping env.

The interesting invariants: a due task produces exactly one interaction card
(deterministic child id dedupes overlapping ticks), approval reaches the
apply_social_approval hook with the task metadata, and an empty find result
does nothing.
"""

from __future__ import annotations

from uuid import uuid4

import pytest_asyncio
from aegis_worker.activities.interactions import (
    ApplyTimeoutInput,
    InsertInteractionInput,
    InsertInteractionResult,
    ResolveInteractionInput,
)
from aegis_worker.flows.interaction import InteractionFlow
from aegis_worker.flows.social_publish import SocialPublishConfig, SocialPublishFlow
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

_TASK = {
    "task_id": "42",
    "text": "hello world",
    "link": "https://example.com",
    "platforms": ["x"],
    "post_at": "2026-07-10T09:30:00+00:00",
}


def _make_stubs(
    due: list[dict],
    *,
    sync_fails: bool = False,
    retire_fails: bool = False,
    complete_result: dict | None = None,
):
    """Stub activity set + call recorder for one worker instance."""
    calls: dict[str, list] = {
        "hook": [],
        "drain": [],
        "complete": [],
        "card_kind": [],
        "sync": [],
        "retire": [],
        "order": [],
    }

    @activity.defn(name="sync_postiz_channels")
    async def sync_postiz_channels(min_interval_minutes: int = 60) -> dict:
        calls["sync"].append(min_interval_minutes)
        calls["order"].append("sync")
        if sync_fails:
            raise RuntimeError("postiz unreachable")
        return {"synced": 3, "skipped_disabled": 1, "status": "ok"}

    @activity.defn(name="retire_unpublishable_tasks")
    async def retire_unpublishable_tasks(limit: int = 20) -> dict:
        calls["retire"].append(limit)
        calls["order"].append("retire")
        if retire_fails:
            raise RuntimeError("todoist projection mid-sync")
        return {"retired": 2}

    @activity.defn(name="find_due_posts")
    async def find_due_posts(lookahead_minutes: int = 10, default_post_hour: int = 9):
        calls["order"].append("find_due")
        return due

    @activity.defn(name="drain_social_outbox")
    async def drain_social_outbox() -> dict:
        calls["drain"].append(True)
        return {"posted": 0, "failed": 0}

    @activity.defn(name="complete_posted_tasks")
    async def complete_posted_tasks() -> dict:
        calls["complete"].append(True)
        return complete_result if complete_result is not None else {"completed": 0}

    @activity.defn(name="apply_social_approval")
    async def apply_social_approval(interaction_id: str, response: dict, metadata: dict) -> dict:
        calls["hook"].append((interaction_id, response, metadata))
        return {"applied": response.get("value", "none")}

    @activity.defn(name="insert_interaction")
    async def insert_interaction(inp: InsertInteractionInput) -> InsertInteractionResult:
        calls["card_kind"].append(inp.kind)
        return InsertInteractionResult(interaction_id=f"ia-{uuid4().hex[:8]}")

    @activity.defn(name="send_interaction_card")
    async def send_interaction_card(
        interaction_id: str, agent_id: str, kind: str, prompt: str, options, allow_hint=False
    ) -> dict:
        return {"ok": True}

    @activity.defn(name="resolve_interaction")
    async def resolve_interaction(inp: ResolveInteractionInput) -> None:
        return None

    @activity.defn(name="apply_interaction_timeout")
    async def apply_interaction_timeout(inp: ApplyTimeoutInput) -> None:
        return None

    stubs = [
        sync_postiz_channels,
        retire_unpublishable_tasks,
        find_due_posts,
        drain_social_outbox,
        complete_posted_tasks,
        apply_social_approval,
        insert_interaction,
        send_interaction_card,
        resolve_interaction,
        apply_interaction_timeout,
    ]
    return stubs, calls


@pytest_asyncio.fixture(loop_scope="function")
async def temporal_env():
    async with await WorkflowEnvironment.start_time_skipping() as env:
        yield env


async def test_due_task_cards_then_approval_reaches_hook(temporal_env):
    stubs, calls = _make_stubs(due=[_TASK])
    tq = f"test-{uuid4().hex[:8]}"
    async with Worker(
        temporal_env.client,
        task_queue=tq,
        workflows=[SocialPublishFlow, InteractionFlow],
        activities=stubs,
    ):
        # Freeze auto time-skipping: otherwise awaiting the parent's result can
        # fast-forward past the abandoned card's 24h timeout, archiving it
        # before we get to signal.
        with temporal_env.auto_time_skipping_disabled():
            result = await temporal_env.client.execute_workflow(
                SocialPublishFlow.run,
                SocialPublishConfig(agent_id="sebas"),
                id=f"social-publish-{uuid4()}",
                task_queue=tq,
            )
            assert result["due"] == 1
            assert result["carded"] == 1
            assert calls["drain"] and calls["complete"]
            # The card kind must stay in the closed set the Slack renderer and
            # admin panel draw buttons for — "decision" regressed to a
            # button-less card once already.
            assert calls["card_kind"] == ["choice"]

            # Approve on the abandoned card → post_resolve hook fires with metadata.
            child = temporal_env.client.get_workflow_handle("social-approve-42")
            await child.signal(InteractionFlow.submit_response, {"value": "approve"})
            await child.result()
        assert len(calls["hook"]) == 1
        _, response, metadata = calls["hook"][0]
        assert response == {"value": "approve"}
        assert metadata["task_id"] == "42"
        assert metadata["platforms"] == ["x"]
        assert metadata["post_at"] == "2026-07-10T09:30:00+00:00"


async def test_open_card_is_not_duplicated_by_next_tick(temporal_env):
    stubs, calls = _make_stubs(due=[_TASK])
    tq = f"test-{uuid4().hex[:8]}"
    async with Worker(
        temporal_env.client,
        task_queue=tq,
        workflows=[SocialPublishFlow, InteractionFlow],
        activities=stubs,
    ):
        # Without this, the env can skip past the first card's 24h timeout
        # between the two ticks — the card archives, the second tick re-cards
        # legitimately, and the assertion below flakes.
        with temporal_env.auto_time_skipping_disabled():
            first = await temporal_env.client.execute_workflow(
                SocialPublishFlow.run,
                SocialPublishConfig(agent_id="sebas"),
                id=f"social-publish-{uuid4()}",
                task_queue=tq,
            )
            assert first["carded"] == 1
            # Same task still due, card still open → second tick must not re-card.
            second = await temporal_env.client.execute_workflow(
                SocialPublishFlow.run,
                SocialPublishConfig(agent_id="sebas"),
                id=f"social-publish-{uuid4()}",
                task_queue=tq,
            )
            assert second["carded"] == 0
            child = temporal_env.client.get_workflow_handle("social-approve-42")
            await child.signal(InteractionFlow.submit_response, {"value": "skip"})
            await child.result()
        assert calls["hook"][0][1] == {"value": "skip"}


async def test_no_due_posts_does_nothing(temporal_env):
    stubs, calls = _make_stubs(due=[])
    tq = f"test-{uuid4().hex[:8]}"
    async with Worker(
        temporal_env.client,
        task_queue=tq,
        workflows=[SocialPublishFlow, InteractionFlow],
        activities=stubs,
    ):
        result = await temporal_env.client.execute_workflow(
            SocialPublishFlow.run,
            SocialPublishConfig(agent_id="sebas"),
            id=f"social-publish-{uuid4()}",
            task_queue=tq,
        )
        assert result == {
            "due": 0,
            "carded": 0,
            "drain_posted": 0,
            "drain_failed": 0,
            "completed": 0,
            "blocked": 0,
            "channel_sync": "ok",
            "channels_synced": 3,
            "retired": 2,
        }
        assert not calls["hook"]


# --- #182 / #183: the housekeeping steps in front of the publish loop -------


async def test_channel_sync_and_retirement_run_before_cards_are_dealt(temporal_env):
    """#182: the Postiz mirror is refreshed on the schedule, not by a button.
    #183: unpublishable tasks are retired BEFORE find_due_posts, so the tick
    that ends them cannot also card them."""
    stubs, calls = _make_stubs(due=[])
    tq = f"test-{uuid4().hex[:8]}"
    async with Worker(
        temporal_env.client,
        task_queue=tq,
        workflows=[SocialPublishFlow, InteractionFlow],
        activities=stubs,
    ):
        result = await temporal_env.client.execute_workflow(
            SocialPublishFlow.run,
            SocialPublishConfig(agent_id="sebas", channel_sync_minutes=15, max_retire=7),
            id=f"social-publish-{uuid4()}",
            task_queue=tq,
        )

    assert calls["order"] == ["sync", "retire", "find_due"]
    assert calls["sync"] == [15], "channel_sync_minutes must reach the activity"
    assert calls["retire"] == [7], "max_retire must reach the activity"
    assert result["channel_sync"] == "ok"
    assert result["channels_synced"] == 3
    assert result["retired"] == 2


async def test_a_postiz_outage_does_not_stop_the_publish_tick(temporal_env):
    """The mirror refresh is housekeeping: an unreachable Postiz must not stop
    an already-approved post from draining, nor fail the run."""
    stubs, calls = _make_stubs(due=[], sync_fails=True, retire_fails=True)
    tq = f"test-{uuid4().hex[:8]}"
    async with Worker(
        temporal_env.client,
        task_queue=tq,
        workflows=[SocialPublishFlow, InteractionFlow],
        activities=stubs,
    ):
        result = await temporal_env.client.execute_workflow(
            SocialPublishFlow.run,
            SocialPublishConfig(agent_id="sebas"),
            id=f"social-publish-{uuid4()}",
            task_queue=tq,
        )

    assert result["channel_sync"] == "sync_failed"
    assert result["channels_synced"] == 0
    assert result["retired"] == 0
    # The publish half still ran end to end.
    assert calls["order"][-1] == "find_due"
    assert calls["drain"] and calls["complete"]


async def test_blocked_tasks_reach_the_run_summary(temporal_env):
    """#135: `completed: 0` alone cannot distinguish "nothing to close" from
    "everything is stuck" — 6,743 consecutive zero runs were read as the second
    when they were the first. `blocked` is the key that separates them, so it
    has to survive the trip from the activity into result_summary."""
    stubs, _ = _make_stubs(due=[], complete_result={"completed": 0, "blocked": 4})
    tq = f"test-{uuid4().hex[:8]}"
    async with Worker(
        temporal_env.client,
        task_queue=tq,
        workflows=[SocialPublishFlow, InteractionFlow],
        activities=stubs,
    ):
        result = await temporal_env.client.execute_workflow(
            SocialPublishFlow.run,
            SocialPublishConfig(agent_id="sebas"),
            id=f"social-publish-{uuid4()}",
            task_queue=tq,
        )
    assert result["blocked"] == 4
    assert result["completed"] == 0
