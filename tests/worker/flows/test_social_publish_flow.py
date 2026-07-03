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
}


def _make_stubs(due: list[dict]):
    """Stub activity set + call recorder for one worker instance."""
    calls: dict[str, list] = {"hook": [], "drain": [], "complete": [], "card_kind": []}

    @activity.defn(name="find_due_posts")
    async def find_due_posts(lookahead_minutes: int = 10, default_post_hour: int = 9):
        return due

    @activity.defn(name="drain_social_outbox")
    async def drain_social_outbox() -> dict:
        calls["drain"].append(True)
        return {"posted": 0, "failed": 0}

    @activity.defn(name="complete_posted_tasks")
    async def complete_posted_tasks() -> dict:
        calls["complete"].append(True)
        return {"completed": 0}

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
            "posted": 0,
            "post_failed": 0,
            "completed": 0,
        }
        assert not calls["hook"]
