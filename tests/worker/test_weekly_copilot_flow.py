"""WeeklyReviewFlow copilot: frames a snapshot and spawns N decision cards."""
from __future__ import annotations

import uuid

import pytest
from aegis_worker.flows.interaction import InteractionFlow
from aegis_worker.flows.review import WeeklyReviewConfig, WeeklyReviewFlow
from temporalio import activity
from temporalio.client import Client
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker


@pytest.mark.asyncio
async def test_weekly_flow_frames_and_spawns_decisions():
    sent: list[str] = []
    spawned: list[dict] = []

    @activity.defn(name="gather_weekly_state")
    async def gather():
        return {"completed_7d_count": 2, "stalled_projects": [], "_top_n": 5}

    @activity.defn(name="frame_review")
    async def frame(snapshot):
        return {
            "narrative": "Weekly review: 2 decisions need you.",
            "decisions": [
                {"id": "aging_waiting:T1", "signal": "aging_waiting",
                 "task_id": "T1", "prompt": "chase X",
                 "options": {"nudge": "Nudge", "keep": "Keep"}},
                {"id": "slipping:T2", "signal": "slipping", "task_id": "T2",
                 "prompt": "file taxes",
                 "options": {"tomorrow": "Do tomorrow", "keep": "Keep"}},
            ],
        }

    @activity.defn(name="send_telegram")
    async def send_telegram(agent_id: str, message: str, chat_id: int = 0, keyboard=None):
        sent.append(message)
        return {"ok": True}

    @activity.defn(name="log_review_digest")
    async def log(kind, counts, preview, interaction_id):
        return 1

    @activity.defn(name="insert_interaction")
    async def insert(input):
        spawned.append({"origin": input["origin"], "metadata": input["metadata"]})
        return {"interaction_id": str(uuid.uuid4())}

    @activity.defn(name="send_interaction_card")
    async def card(*a, **kw):
        return {"ok": True, "message_id": 0}

    @activity.defn(name="update_interaction_message_id")
    async def upd(*a, **kw):
        return None

    @activity.defn(name="resolve_interaction")
    async def resolve(*a, **kw):
        return {"already_resolved": False}

    @activity.defn(name="apply_interaction_timeout")
    async def to(*a, **kw):
        return None

    @activity.defn(name="apply_review_decision")
    async def apply_dec(*a, **kw):
        return {"applied": True}

    async with await WorkflowEnvironment.start_time_skipping() as env:
        client: Client = env.client
        async with Worker(
            client,
            task_queue="aegis-weekly-copilot-test",
            workflows=[WeeklyReviewFlow, InteractionFlow],
            activities=[gather, frame, send_telegram, log, insert, card, upd,
                        resolve, to, apply_dec],
        ):
            result = await client.execute_workflow(
                WeeklyReviewFlow.run,
                WeeklyReviewConfig(),
                id=f"weekly-copilot-{uuid.uuid4()}",
                task_queue="aegis-weekly-copilot-test",
            )
            assert result["kind"] == "weekly"
            assert result["decisions"] == 2
            assert len(sent) == 1 and "decisions need you" in sent[0]
            assert len(spawned) == 2
            assert all(s["origin"] == "gtd_weekly_decision" for s in spawned)
            assert {s["metadata"]["task_id"] for s in spawned} == {"T1", "T2"}
