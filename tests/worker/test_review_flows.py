"""DailyReviewFlow + WeeklyReviewFlow orchestration tests."""

from __future__ import annotations

import uuid

import pytest
from aegis_worker.flows.interaction import InteractionFlow
from aegis_worker.flows.review import (
    DailyReviewConfig,
    DailyReviewFlow,
    WeeklyReviewConfig,
    WeeklyReviewFlow,
)
from temporalio import activity
from temporalio.client import Client
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker


def _stub_digest_daily() -> dict:
    return {
        "inbox_count": 3,
        "inbox_top3": ["a", "b", "c"],
        "due_today_count": 1,
        "due_today_top3": [{"content": "Pay", "due_date": "2026-05-19"}],
        "waiting_stale_count": 2,
        "pending_clarify_count": 1,
        "applied_24h_count": 4,
    }


def _stub_digest_weekly() -> dict:
    return {
        "stale_next_actions_count": 5,
        "stale_next_actions_top3": ["x", "y", "z"],
        "someday_count": 2,
        "waiting_stale_7d_count": 1,
        "inbox_unclarified_7d_count": 0,
        "completed_7d_count": 8,
    }


def _build_stubs(digest: dict, kind: str):
    """Return a dict of named @activity.defn stubs for a review flow test."""
    sent_messages: list[str] = []
    log_calls: list[dict] = []

    @activity.defn(name="gather_daily_digest")
    async def gather_daily():
        return digest

    @activity.defn(name="gather_weekly_digest")
    async def gather_weekly():
        return digest

    @activity.defn(name="send_telegram")
    async def send_telegram(
        agent_id: str, message: str, chat_id: int = 0, keyboard=None
    ):
        sent_messages.append(message)
        return {"ok": True}

    @activity.defn(name="log_review_digest")
    async def log_review_digest(
        kind: str, counts: dict, preview: str, interaction_id
    ):
        log_calls.append(
            {"kind": kind, "counts": counts, "preview": preview,
             "interaction_id": interaction_id}
        )
        return 42

    @activity.defn(name="insert_interaction")
    async def insert_interaction(input):
        return {"interaction_id": "11111111-1111-1111-1111-111111111111"}

    @activity.defn(name="send_interaction_card")
    async def send_card(*a, **kw):
        return {"ok": True, "message_id": 0}

    @activity.defn(name="update_interaction_message_id")
    async def update_msg(*a, **kw):
        return None

    @activity.defn(name="resolve_interaction")
    async def resolve(*a, **kw):
        return {"already_resolved": False}

    @activity.defn(name="apply_interaction_timeout")
    async def timeout(*a, **kw):
        return None

    @activity.defn(name="apply_review_acknowledgement")
    async def apply_ack(*a, **kw):
        return {"acknowledged": True}

    @activity.defn(name="gather_today_focus")
    async def gather_today_focus():
        return [{"task_id": "X", "content": "do x", "due_date": None}]

    return (
        [
            gather_daily, gather_weekly, send_telegram, log_review_digest,
            insert_interaction, send_card, update_msg, resolve, timeout,
            apply_ack, gather_today_focus,
        ],
        sent_messages,
        log_calls,
    )


@pytest.mark.asyncio
async def test_daily_review_flow_sends_digest_and_logs() -> None:
    async with await WorkflowEnvironment.start_time_skipping() as env:
        client: Client = env.client
        activities, sent, logs = _build_stubs(_stub_digest_daily(), "daily")
        async with Worker(
            client,
            task_queue="aegis-review-daily-test",
            workflows=[DailyReviewFlow, InteractionFlow],
            activities=activities,
        ):
            result = await client.execute_workflow(
                DailyReviewFlow.run,
                DailyReviewConfig(),
                id=f"daily-review-{uuid.uuid4()}",
                task_queue="aegis-review-daily-test",
            )
            assert result["kind"] == "daily"
            # Counts threaded through unchanged
            assert result["counts"]["inbox_count"] == 3
            # Telegram got the daily preview and today's focus shortlist
            assert len(sent) == 2
            assert "Daily review" in sent[0]
            assert "Today's focus" in sent[1]
            # log_review_digest was called with kind='daily'
            assert len(logs) == 1
            assert logs[0]["kind"] == "daily"
            # interaction_id is the child workflow id (or None if spawn failed,
            # but we expect success in the test worker)
            assert logs[0]["interaction_id"] is not None


@pytest.mark.asyncio
async def test_weekly_review_flow_sends_digest_and_logs() -> None:
    sent: list[str] = []
    logs: list[dict] = []

    @activity.defn(name="gather_weekly_state")
    async def gather_weekly_state():
        return _stub_digest_weekly()

    @activity.defn(name="frame_review")
    async def frame_review(snapshot):
        return {"narrative": "Weekly review ...", "decisions": []}

    @activity.defn(name="send_telegram")
    async def send_telegram(agent_id: str, message: str, chat_id: int = 0, keyboard=None):
        sent.append(message)
        return {"ok": True}

    @activity.defn(name="log_review_digest")
    async def log_review_digest(kind: str, counts: dict, preview: str, interaction_id):
        logs.append({"kind": kind, "counts": counts, "preview": preview,
                     "interaction_id": interaction_id})
        return 42

    @activity.defn(name="insert_interaction")
    async def insert_interaction(input):
        return {"interaction_id": "11111111-1111-1111-1111-111111111111"}

    @activity.defn(name="send_interaction_card")
    async def send_card(*a, **kw):
        return {"ok": True, "message_id": 0}

    @activity.defn(name="update_interaction_message_id")
    async def update_msg(*a, **kw):
        return None

    @activity.defn(name="resolve_interaction")
    async def resolve(*a, **kw):
        return {"already_resolved": False}

    @activity.defn(name="apply_interaction_timeout")
    async def timeout(*a, **kw):
        return None

    @activity.defn(name="apply_review_decision")
    async def apply_dec(*a, **kw):
        return {"applied": True}

    async with await WorkflowEnvironment.start_time_skipping() as env:
        client = env.client
        async with Worker(
            client,
            task_queue="aegis-review-weekly-test",
            workflows=[WeeklyReviewFlow, InteractionFlow],
            activities=[
                gather_weekly_state, frame_review, send_telegram, log_review_digest,
                insert_interaction, send_card, update_msg, resolve, timeout, apply_dec,
            ],
        ):
            result = await client.execute_workflow(
                WeeklyReviewFlow.run,
                WeeklyReviewConfig(),
                id=f"weekly-review-{uuid.uuid4()}",
                task_queue="aegis-review-weekly-test",
            )
            assert result["kind"] == "weekly"
            assert result["counts"]["stale_next_actions_count"] == 5
            assert len(sent) == 1
            assert "Weekly review" in sent[0]
            assert logs[0]["kind"] == "weekly"
            assert result["decisions"] == 0


@pytest.mark.asyncio
async def test_daily_review_flow_continues_when_telegram_fails() -> None:
    """Telegram error shouldn't abort the flow — interaction + log still
    happen so the audit row is preserved."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        client = env.client
        sent = []
        log_calls = []

        @activity.defn(name="gather_daily_digest")
        async def gather():
            return _stub_digest_daily()

        @activity.defn(name="send_telegram")
        async def send_telegram(*a, **kw):
            sent.append(a)
            raise RuntimeError("simulated telegram outage")

        @activity.defn(name="log_review_digest")
        async def log(kind, counts, preview, interaction_id):
            log_calls.append({"kind": kind, "interaction_id": interaction_id})
            return 1

        @activity.defn(name="insert_interaction")
        async def insert(*a, **kw):
            return {"interaction_id": "22222222-2222-2222-2222-222222222222"}

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

        @activity.defn(name="apply_review_acknowledgement")
        async def ack(*a, **kw):
            return {}

        @activity.defn(name="gather_today_focus")
        async def gather_focus():
            return [{"task_id": "X", "content": "do x", "due_date": None}]

        async with Worker(
            client,
            task_queue="aegis-review-tg-fail",
            workflows=[DailyReviewFlow, InteractionFlow],
            activities=[
                gather, send_telegram, log, insert, card, upd, resolve, to, ack,
                gather_focus,
            ],
        ):
            result = await client.execute_workflow(
                DailyReviewFlow.run,
                DailyReviewConfig(),
                id=f"daily-tgfail-{uuid.uuid4()}",
                task_queue="aegis-review-tg-fail",
            )
            assert result["kind"] == "daily"
            assert len(log_calls) == 1
            assert log_calls[0]["kind"] == "daily"
