"""DailyReviewFlow + WeeklyReviewFlow orchestration tests."""

from __future__ import annotations

import uuid

import pytest
from aegis_worker.activities.review import format_key_dates, format_meeting_week
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

    @activity.defn(name="send_message")
    async def send_message(agent_id: str, message: str, chat_id: int = 0, keyboard=None):
        sent_messages.append(message)
        return {"ok": True}

    @activity.defn(name="log_review_digest")
    async def log_review_digest(kind: str, counts: dict, preview: str, interaction_id):
        log_calls.append(
            {"kind": kind, "counts": counts, "preview": preview, "interaction_id": interaction_id}
        )
        return 42

    @activity.defn(name="insert_interaction")
    async def insert_interaction(input):
        return {"interaction_id": "11111111-1111-1111-1111-111111111111"}

    @activity.defn(name="send_interaction_card")
    async def send_card(*a, **kw):
        return {"ok": True, "message_id": 0}

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
            gather_daily,
            gather_weekly,
            send_message,
            log_review_digest,
            insert_interaction,
            send_card,
            resolve,
            timeout,
            apply_ack,
            gather_today_focus,
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
            # The chat channel got the daily preview and today's focus shortlist
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

    @activity.defn(name="send_message")
    async def send_message(agent_id: str, message: str, chat_id: int = 0, keyboard=None):
        sent.append(message)
        return {"ok": True}

    @activity.defn(name="log_review_digest")
    async def log_review_digest(kind: str, counts: dict, preview: str, interaction_id):
        logs.append(
            {"kind": kind, "counts": counts, "preview": preview, "interaction_id": interaction_id}
        )
        return 42

    @activity.defn(name="insert_interaction")
    async def insert_interaction(input):
        return {"interaction_id": "11111111-1111-1111-1111-111111111111"}

    @activity.defn(name="send_interaction_card")
    async def send_card(*a, **kw):
        return {"ok": True, "message_id": 0}

    @activity.defn(name="resolve_interaction")
    async def resolve(*a, **kw):
        return {"already_resolved": False}

    @activity.defn(name="apply_interaction_timeout")
    async def timeout(*a, **kw):
        return None

    @activity.defn(name="apply_review_decision")
    async def apply_dec(*a, **kw):
        return {"applied": True}

    @activity.defn(name="check_upcoming_key_dates")
    async def check_key_dates():
        return []

    async with await WorkflowEnvironment.start_time_skipping() as env:
        client = env.client
        async with Worker(
            client,
            task_queue="aegis-review-weekly-test",
            workflows=[WeeklyReviewFlow, InteractionFlow],
            activities=[
                gather_weekly_state,
                frame_review,
                send_message,
                log_review_digest,
                insert_interaction,
                send_card,
                resolve,
                timeout,
                apply_dec,
                check_key_dates,
                stub_gather_meeting_week,
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
            # Nothing upcoming ⇒ no people block bolted onto the narrative.
            assert "Coming up" not in sent[0]


@pytest.mark.asyncio
async def test_daily_review_flow_continues_when_delivery_fails() -> None:
    """Delivery error shouldn't abort the flow — interaction + log still
    happen so the audit row is preserved."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        client = env.client
        sent = []
        log_calls = []

        @activity.defn(name="gather_daily_digest")
        async def gather():
            return _stub_digest_daily()

        @activity.defn(name="send_message")
        async def send_message(*a, **kw):
            sent.append(a)
            raise RuntimeError("simulated delivery outage")

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
                gather,
                send_message,
                log,
                insert,
                card,
                resolve,
                to,
                ack,
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


# ── Issue #36: review flows send as their own config.agent_id, not "sebas" ──


@pytest.mark.asyncio
async def test_daily_review_addresses_config_agent_id() -> None:
    """DailyReviewFlow delivers as its owning agent (config.agent_id), so a
    renamed GTD owner no longer sends everything as the literal 'sebas'."""
    sent_agents: list[str] = []

    @activity.defn(name="gather_daily_digest")
    async def gather_daily():
        return _stub_digest_daily()

    @activity.defn(name="send_message")
    async def send_message(agent_id: str, message: str, chat_id: int = 0, keyboard=None):
        sent_agents.append(agent_id)
        return {"ok": True}

    @activity.defn(name="log_review_digest")
    async def log_review_digest(kind, counts, preview, interaction_id):
        return 42

    @activity.defn(name="insert_interaction")
    async def insert_interaction(input):
        return {"interaction_id": "11111111-1111-1111-1111-111111111111"}

    @activity.defn(name="send_interaction_card")
    async def send_card(interaction_id, agent_id, kind, prompt, options, allow_hint=False):
        sent_agents.append(agent_id)
        return {"ok": True, "message_id": 0}

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

    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue="aegis-review-agentid-test",
            workflows=[DailyReviewFlow, InteractionFlow],
            activities=[
                gather_daily,
                send_message,
                log_review_digest,
                insert_interaction,
                send_card,
                resolve,
                timeout,
                apply_ack,
                gather_today_focus,
            ],
        ),
    ):
        await env.client.execute_workflow(
            DailyReviewFlow.run,
            DailyReviewConfig(agent_id="custom-gtd"),
            id=f"daily-review-{uuid.uuid4()}",
            task_queue="aegis-review-agentid-test",
        )

    assert sent_agents, "expected at least one agent-addressed action"
    assert all(a == "custom-gtd" for a in sent_agents)
    assert "sebas" not in sent_agents


# ── C3: people radar — life.people key dates ride out with the weekly review ──


_meeting_week_payload: dict = {}


@activity.defn(name="gather_meeting_week")
async def stub_gather_meeting_week() -> dict:
    return dict(_meeting_week_payload)


def _weekly_stubs(sent: list[str], key_dates: list[dict] | None, *, fail: bool = False):
    """Weekly-review activity stubs with a configurable check_upcoming_key_dates."""

    @activity.defn(name="gather_weekly_state")
    async def gather_weekly_state():
        return _stub_digest_weekly()

    @activity.defn(name="frame_review")
    async def frame_review(snapshot):
        # Production path: the LLM narrative REPLACES format_weekly_preview,
        # so the people block has to survive a framed narrative.
        return {"narrative": "Weekly review — LLM framing.", "decisions": []}

    @activity.defn(name="send_message")
    async def send_message(agent_id: str, message: str, chat_id: int = 0, keyboard=None):
        sent.append(message)
        return {"ok": True}

    @activity.defn(name="log_review_digest")
    async def log_review_digest(kind, counts, preview, interaction_id):
        return 42

    @activity.defn(name="check_upcoming_key_dates")
    async def check_upcoming_key_dates():
        if fail:
            raise RuntimeError("simulated life.people outage")
        return key_dates or []

    @activity.defn(name="apply_review_decision")
    async def apply_dec(*a, **kw):
        return {"applied": True}

    return [
        gather_weekly_state,
        frame_review,
        send_message,
        log_review_digest,
        check_upcoming_key_dates,
        stub_gather_meeting_week,
        apply_dec,
    ]


@pytest.mark.asyncio
async def test_weekly_review_appends_upcoming_key_dates() -> None:
    sent: list[str] = []
    hits = [
        {
            "name": "Amma",
            "relationship": "mother",
            "label": "birthday",
            "date": "2026-08-04",
            "days_until": 3,
            "years": 60,
        }
    ]
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue="aegis-review-keydates-test",
            workflows=[WeeklyReviewFlow, InteractionFlow],
            activities=_weekly_stubs(sent, hits),
        ),
    ):
        await env.client.execute_workflow(
            WeeklyReviewFlow.run,
            WeeklyReviewConfig(),
            id=f"weekly-keydates-{uuid.uuid4()}",
            task_queue="aegis-review-keydates-test",
        )
    assert len(sent) == 1
    # The framed narrative is preserved AND carries the people block.
    assert "Weekly review — LLM framing." in sent[0]
    assert "Coming up" in sent[0]
    assert "Amma (mother): birthday in 3d — turning 60" in sent[0]


@pytest.mark.asyncio
async def test_weekly_review_appends_meeting_block_when_present() -> None:
    _meeting_week_payload.clear()
    _meeting_week_payload.update(
        {"meetings": [{"title": "Standup", "talk_share_pct": 9.0, "contributions": ["c"],
                       "problems_raised": [], "commitments": [], "verbosity_note": ""}],
         "talk_share_avg": 9.0, "talk_share_prev": None,
         "words_per_turn_avg": None, "words_per_turn_prev": None, "missing_doc_by_account": {}}
    )
    sent: list[str] = []
    try:
        async with (
            await WorkflowEnvironment.start_time_skipping() as env,
            Worker(
                env.client,
                task_queue="aegis-review-meetingweek-test",
                workflows=[WeeklyReviewFlow, InteractionFlow],
                activities=_weekly_stubs(sent, None),
            ),
        ):
            await env.client.execute_workflow(
                WeeklyReviewFlow.run,
                WeeklyReviewConfig(),
                id=f"weekly-meetingweek-{uuid.uuid4()}",
                task_queue="aegis-review-meetingweek-test",
            )
        assert len(sent) == 1
        sent_text = sent[0]
        # One message: the framed narrative still carries the meetings block.
        assert "Weekly review — LLM framing." in sent_text
        assert "🎙 <b>Meetings this week</b> (1)" in sent_text
        assert "• Standup — you spoke 9% · c" in sent_text
    finally:
        _meeting_week_payload.clear()


@pytest.mark.asyncio
async def test_weekly_review_ships_when_key_dates_lookup_fails() -> None:
    """A broken/absent people registry must not cost the user their review."""
    sent: list[str] = []
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue="aegis-review-keydates-fail",
            workflows=[WeeklyReviewFlow, InteractionFlow],
            activities=_weekly_stubs(sent, None, fail=True),
        ),
    ):
        result = await env.client.execute_workflow(
            WeeklyReviewFlow.run,
            WeeklyReviewConfig(),
            id=f"weekly-keydates-fail-{uuid.uuid4()}",
            task_queue="aegis-review-keydates-fail",
        )
    assert result["kind"] == "weekly"
    assert len(sent) == 1
    assert "Weekly review — LLM framing." in sent[0]
    assert "Coming up" not in sent[0]


@pytest.mark.asyncio
async def test_weekly_review_ships_when_the_meeting_formatter_raises() -> None:
    """The gather was guarded but the FORMAT call sat outside the try, so a
    malformed payload failed the flow before send_message ever ran. `meetings`
    as a dict is that payload: truthy, so the block is built, and `meetings[:8]`
    raises TypeError."""
    _meeting_week_payload.clear()
    _meeting_week_payload.update({"meetings": {"not": "a list"}, "missing_doc_by_account": {}})
    sent: list[str] = []
    try:
        async with (
            await WorkflowEnvironment.start_time_skipping() as env,
            Worker(
                env.client,
                task_queue="aegis-review-meetingfmt-fail",
                workflows=[WeeklyReviewFlow, InteractionFlow],
                activities=_weekly_stubs(sent, None),
            ),
        ):
            result = await env.client.execute_workflow(
                WeeklyReviewFlow.run,
                WeeklyReviewConfig(),
                id=f"weekly-meetingfmt-fail-{uuid.uuid4()}",
                task_queue="aegis-review-meetingfmt-fail",
            )
        assert result["kind"] == "weekly"
        assert len(sent) == 1
        assert "Weekly review — LLM framing." in sent[0]
        assert "Meetings this week" not in sent[0]
    finally:
        _meeting_week_payload.clear()


def test_format_meeting_week_renders_block_and_is_empty_without_meetings():
    assert format_meeting_week({}) == ""
    assert format_meeting_week({"meetings": [], "missing_doc_by_account": {}}) == ""
    data = {
        "meetings": [
            {"title": "New Pipeline Standup", "talk_share_pct": 6.4,
             "contributions": ["proposed the pull-based batch pattern"],
             "problems_raised": ["parity script is slow"],
             "commitments": ["move reference collections to Postgres"],
             "verbosity_note": "Your longest turn ran 240 words; the decision landed in the first 40."},
            {"title": "1:1", "talk_share_pct": None, "contributions": [], "problems_raised": [],
             "commitments": [], "verbosity_note": ""},
        ],
        "talk_share_avg": 11.2, "talk_share_prev": 14.0,
        "words_per_turn_avg": 38.0, "words_per_turn_prev": None,
        "missing_doc_by_account": {"arshad-stpd": {"no_drive_scope": 2}},
    }
    out = format_meeting_week(data)
    assert out.startswith("🎙 <b>Meetings this week</b> (2)")
    assert "• New Pipeline Standup — you spoke 6% · proposed the pull-based batch pattern" in out
    assert "• 1:1 — no transcript" in out
    assert "Commitments: move reference collections to Postgres" in out
    assert "Problems you raised: parity script is slow" in out
    assert "Talk share 11% (last week 14%) · 38 words per turn" in out
    assert "last week" in out.split("Talk share")[1].split("\n")[0]
    assert "On brevity: Your longest turn ran 240 words" in out
    assert "⚠ 2 meetings stored without their doc — re-authorise Drive for arshad-stpd" in out
    # Only the warning when there were no reviews at all this week. A status
    # that is not a missing Drive grant must NOT advise re-authorising Drive.
    only_warn = format_meeting_week({"meetings": [], "missing_doc_by_account": {"a": {"no_link": 1}}})
    assert only_warn.startswith("🎙 <b>Meetings this week</b> (0)")
    assert "⚠ 1 meeting stored without their doc (no_link)" in only_warn
    assert "re-authorise" not in only_warn
    # Both kinds on one account: the Drive line first, then the rest.
    both = format_meeting_week(
        {"meetings": [], "missing_doc_by_account": {"b": {"no_drive_scope": 1, "fetch_failed": 2}}}
    )
    scope_line = "  ⚠ 1 meeting stored without their doc — re-authorise Drive for b"
    other_line = "  ⚠ 2 meetings stored without their doc (fetch_failed)"
    assert scope_line in both and other_line in both
    assert both.index(scope_line) < both.index(other_line)
    # A stale flat-shape row (pre-fix payload, in-flight deploy) must not crash.
    legacy = format_meeting_week({"meetings": [], "missing_doc_by_account": {"old": 3}})
    assert "⚠ 3 meetings stored without their doc (unknown)" in legacy


def test_format_meeting_week_escapes_every_value_it_interpolates():
    """The weekly block is parsed as HTML downstream (comms `html_to_mrkdwn` is
    an HTMLParser), so an `<a href>` the review model emits would arrive as a
    real clickable Slack link and a `<` in a doc title would be swallowed.
    Every interpolated value is escaped; the block's own tags are not."""
    evil = '<b>bold</b> <a href="http://x">link</a>'
    out = format_meeting_week(
        {
            "meetings": [
                {
                    "title": evil,
                    "talk_share_pct": 9.0,
                    "contributions": [evil],
                    "problems_raised": [evil],
                    "commitments": [evil],
                    "verbosity_note": evil,
                }
            ],
            "missing_doc_by_account": {evil: {"no_drive_scope": 1, "<i>odd</i>": 2}},
        }
    )
    assert "<b>bold</b>" not in out
    assert "<a href=" not in out
    assert "<i>odd</i>" not in out
    assert "&lt;b&gt;bold&lt;/b&gt;" in out
    assert "&lt;a href=&quot;http://x&quot;&gt;link&lt;/a&gt;" in out
    # …and the formatter's own markup is untouched.
    assert out.startswith("🎙 <b>Meetings this week</b> (1)")


def test_format_key_dates_escapes_the_people_values():
    """Same gap, same delivered message: life.people values are free text too."""
    out = format_key_dates(
        [
            {
                "name": "<script>alert(1)</script>",
                "relationship": "<b>mother</b>",
                "label": "<i>birthday</i>",
                "date": "2026-08-04",
                "days_until": 3,
                "years": 60,
            }
        ]
    )
    assert "<script>" not in out
    assert "<b>mother</b>" not in out
    assert "<i>birthday</i>" not in out
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in out
    assert out.startswith("🎂 <b>Coming up</b>")
