"""FlowHealthActivities — the #226 watchdog over AEGIS's own scheduled flows.

Every row this file writes is prefixed `zzwd-` (workflow_type, workflow_id,
activities.slug) and every audit row it writes carries actor
`flow-health-watchdog`, so nothing here can perturb another test file sharing
the same xdist-worker database — and `_prep` cleans exactly that prefix, never
a whole table.
"""

from __future__ import annotations

import datetime as dt
import inspect

import pytest
import structlog
from aegis.db import run_migrations
from aegis_worker.activities.delivery import DeliveryActivities
from aegis_worker.activities.flow_health import (
    ALERT_ACTION,
    LLM_SUBJECT_PREFIX,
    MUTE_PREFIX,
    RECOVERY_ACTION,
    FlowHealthActivities,
    cron_interval_minutes,
)
from temporalio.testing import ActivityEnvironment

UTC = dt.UTC
TYPE_A = "zzwd-type-a"
TYPE_B = "zzwd-type-b"
SLUG_A = "zzwd-sched-a"
PURPOSE_A = "zzwd-purpose-a"
PURPOSE_B = "zzwd-purpose-b"


async def _prep(db_pool):
    await run_migrations(db_pool)
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM workflow_runs WHERE workflow_type LIKE 'zzwd-%'")
        await conn.execute("DELETE FROM workflow_runs WHERE workflow_id LIKE 'scheduled-zzwd-%'")
        await conn.execute("DELETE FROM activities WHERE slug LIKE 'zzwd-%'")
        await conn.execute("DELETE FROM audit_log WHERE actor = 'flow-health-watchdog'")
        await conn.execute("DELETE FROM alert_mutes WHERE mute_key LIKE 'flow-health:zzwd-%'")
        await conn.execute("DELETE FROM llm_calls WHERE purpose LIKE 'zzwd-%'")


async def _run_row(
    db_pool,
    workflow_type: str,
    status: str,
    minutes_ago: float,
    *,
    workflow_id: str | None = None,
    reason: str = "boom",
):
    started = dt.datetime.now(UTC) - dt.timedelta(minutes=minutes_ago)
    run_id = f"zzwd-run-{workflow_type}-{status}-{minutes_ago}"
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO workflow_runs (run_id, workflow_id, workflow_type, status, "
            "started_at, completed_at, result_summary) "
            "VALUES ($1,$2,$3,$4,$5,$5,$6)",
            run_id,
            workflow_id or f"zzwd-wf-{run_id}",
            workflow_type,
            status,
            started,
            {"reason": reason},
        )


async def _activity_row(db_pool, slug: str, workflow_type: str, cron: str):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO activities (slug, workflow_type, agent_id, schedule_cron, active) "
            "VALUES ($1,$2,'pandoras-actor',$3,TRUE)",
            slug,
            workflow_type,
            cron,
        )


async def _llm_row(
    db_pool,
    purpose: str,
    status: str,
    minutes_ago: float,
    *,
    model: str = "kimi-k2.5",
    error: str | None = "truncated: empty content",
):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO llm_calls (model, purpose, status, error, input_tokens, "
            "output_tokens, latency_ms, created_at) "
            "VALUES ($1,$2,$3,$4,50,4096,900,now() - make_interval(mins => $5))",
            model,
            purpose,
            status,
            error,
            int(minutes_ago),
        )


class FakeDelivery:
    """Stand-in for DeliveryActivities as `safe_send_message` uses it.

    `test_fake_delivery_matches_the_real_class` pins this against the real
    class so the fake cannot drift into testing nothing.
    """

    channel = "slack"
    db_pool = None  # skips the notification-budget path in safe_send_message

    def __init__(self):
        self.sent: list[str] = []

    async def send_message(self, *, agent_id: str, message: str, chat_id: int = 0) -> dict:
        self.sent.append(message)
        return {"ok": True}


def _acts(db_pool, delivery=None):
    return FlowHealthActivities(db_pool=db_pool, delivery=delivery)


def test_fake_delivery_matches_the_real_class():
    """The fake must expose what safe_send_message actually reads off the real
    DeliveryActivities: a `channel` attribute, a `db_pool` attribute and a
    keyword-only send_message(agent_id, message, chat_id)."""
    real = inspect.signature(DeliveryActivities.send_message).parameters
    fake = inspect.signature(FakeDelivery.send_message).parameters
    for name in ("agent_id", "message", "chat_id"):
        assert name in real, f"DeliveryActivities.send_message lost {name}"
        assert name in fake, f"FakeDelivery.send_message lost {name}"
    fields = set(DeliveryActivities.__dataclass_fields__)
    assert {"channel", "db_pool"} <= fields
    assert hasattr(FakeDelivery, "channel") and hasattr(FakeDelivery, "db_pool")


# ---------------------------------------------------------------------------
# cron → cadence
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cron", "expected"),
    [
        ("*/5 * * * *", 5),
        ("*/2 * * * *", 2),
        ("7,37 * * * *", 30),  # this flow's own slot
        ("0 * * * *", 60),
        ("50 */6 * * *", 360),
        ("15 */4 * * *", 240),
        ("0 4 * * *", 1440),
        ("30 3 * * 0", 10080),  # weekly
        ("0 9 * * 1-5", 4320),  # Fri 09:00 -> Mon 09:00 is the LONGEST gap
        ("0 10 1 * *", 44640),  # day-of-month => treated as monthly
        ("20 21 28-31 * *", 44640),
    ],
)
def test_cron_interval_minutes(cron, expected):
    assert cron_interval_minutes(cron) == expected


@pytest.mark.parametrize(
    "cron",
    ["", "garbage", "0 4 * *", "0 4 * * MON", "*/0 * * * *", "0 4 * * * *", "99 * * * *"],
)
def test_cron_interval_none_when_not_understood(cron):
    """None, never a guess: a wrong cadence either alerts forever or never."""
    assert cron_interval_minutes(cron) is None


def test_every_seeded_cron_is_understood():
    """The seed file is the real input to the stale detector — if a cron there
    is unparseable that schedule is silently unwatched."""
    import pathlib

    import yaml

    root = pathlib.Path(__file__).resolve().parents[3]
    rows = yaml.safe_load((root / "config/seed/activities.yaml").read_text())["activities"]
    assert rows, "seed file parsed empty — this test would be vacuous"
    unparsed = [r["slug"] for r in rows if cron_interval_minutes(r["schedule_cron"]) is None]
    assert unparsed == []


# ---------------------------------------------------------------------------
# detector 1: N consecutive failures
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("threshold", [2, 3])
async def test_n_minus_one_failures_are_silent_and_n_alert(db_pool, threshold):
    """The core rule. Written with a literal `threshold` passed in, never the
    production default, so the assertion cannot be satisfied by the constant
    under test."""
    await _prep(db_pool)
    env = ActivityEnvironment()
    act = _acts(db_pool)

    for i in range(threshold - 1):
        await _run_row(db_pool, TYPE_A, "failed", minutes_ago=60 - i)
    found = await env.run(act.find_failing_flows, threshold, 24)
    assert TYPE_A not in {f["subject"] for f in found}, (
        f"{threshold - 1} failures must not alert at threshold {threshold}"
    )

    await _run_row(db_pool, TYPE_A, "failed", minutes_ago=1)
    found = await env.run(act.find_failing_flows, threshold, 24)
    subjects = {f["subject"] for f in found}
    assert TYPE_A in subjects, f"{threshold} consecutive failures must alert"
    row = next(f for f in found if f["subject"] == TYPE_A)
    assert row["kind"] == "failing"
    assert row["consecutive"] == threshold
    assert row["reason"] == "boom"


@pytest.mark.asyncio
async def test_a_recent_success_clears_older_failures(db_pool):
    """Ordering, not counting: two failures followed by a success is healthy.
    A query that ignored started_at ordering would report this type."""
    await _prep(db_pool)
    env = ActivityEnvironment()
    act = _acts(db_pool)
    await _run_row(db_pool, TYPE_A, "failed", minutes_ago=30)
    await _run_row(db_pool, TYPE_A, "failed", minutes_ago=20)
    assert TYPE_A in {f["subject"] for f in await env.run(act.find_failing_flows, 2, 24)}

    await _run_row(db_pool, TYPE_A, "completed", minutes_ago=1)
    assert TYPE_A not in {f["subject"] for f in await env.run(act.find_failing_flows, 2, 24)}


@pytest.mark.asyncio
async def test_the_resolved_2026_08_02_incident_does_not_re_alert(db_pool):
    """The exact shape sitting in prod `workflow_runs` right now: the six
    consecutive TodoistSyncFlow failures of 12:15-12:40 UTC, followed by the
    12:45 and 12:50 successes that ended the incident. Those rows are permanent
    history. The watchdog's FIRST EVER run must be silent about them — a page
    for an outage that ended hours ago is the fastest way to teach someone to
    ignore this alert.

    End-to-end, not just the detector: zero findings AND zero cards.
    """
    await _prep(db_pool)
    env = ActivityEnvironment()
    delivery = FakeDelivery()
    act = _acts(db_pool, delivery)

    # 12:15..12:40 failed (6 runs, 5 min apart), then 12:45 + 12:50 completed.
    # Expressed as minutes-ago so the rows stay inside the 24h window.
    for i, ago in enumerate([50, 45, 40, 35, 30, 25]):
        await _run_row(
            db_pool, TYPE_A, "failed", ago,
            reason=f"todoist_sync_failed at step=apply_sync_diff #{i}",
        )
    for ago in (20, 15):
        await _run_row(db_pool, TYPE_A, "completed", ago, reason="ok")

    found = await env.run(act.find_failing_flows, 2, 24)
    assert TYPE_A not in {f["subject"] for f in found}, (
        "six historical failures already followed by two successes must not alert"
    )

    report = await env.run(act.report_flow_health, found, "pandoras-actor", 12, 168)
    assert report["alerted"] == 0
    assert delivery.sent == [], "a resolved incident produced a card"

    # ...and the detector is not simply blind: break it again right now and the
    # very next sweep does alert.
    await _run_row(db_pool, TYPE_A, "failed", 2)
    await _run_row(db_pool, TYPE_A, "failed", 1)
    assert TYPE_A in {f["subject"] for f in await env.run(act.find_failing_flows, 2, 24)}


@pytest.mark.asyncio
async def test_first_run_sees_only_the_bounded_window_of_history(db_pool):
    """The watchdog's first run meets a `workflow_runs` table full of history
    that predates it. No first-run guard is needed: recency ordering plus the
    `lookback_hours` bound mean only currently-broken flows can match. A flow
    that broke and stayed broken 3 days ago, then went quiet, is invisible to
    this detector (it belongs to the stale one)."""
    await _prep(db_pool)
    env = ActivityEnvironment()
    act = _acts(db_pool)
    for ago in (60 * 72, 60 * 71):  # 3 days ago, never ran since
        await _run_row(db_pool, TYPE_A, "failed", ago)
    for ago in (30, 20):  # broken now
        await _run_row(db_pool, TYPE_B, "failed", ago)
    subjects = {f["subject"] for f in await env.run(act.find_failing_flows, 2, 24)}
    assert TYPE_A not in subjects, "3-day-old history resurfaced on the first run"
    assert TYPE_B in subjects, "a currently-wedged flow must still alert on run 1"


@pytest.mark.asyncio
async def test_failures_outside_the_lookback_window_are_not_reported(db_pool):
    """A type that failed twice and then stopped running belongs to the stale
    detector, not this one — otherwise it would re-alert forever."""
    await _prep(db_pool)
    env = ActivityEnvironment()
    act = _acts(db_pool)
    await _run_row(db_pool, TYPE_A, "failed", minutes_ago=60 * 40)
    await _run_row(db_pool, TYPE_A, "failed", minutes_ago=60 * 39)
    assert TYPE_A in {f["subject"] for f in await env.run(act.find_failing_flows, 2, 24 * 7)}
    assert TYPE_A not in {f["subject"] for f in await env.run(act.find_failing_flows, 2, 24)}


@pytest.mark.asyncio
async def test_failures_of_different_types_do_not_combine(db_pool):
    """One failure each of two types is not "two consecutive failures"."""
    await _prep(db_pool)
    env = ActivityEnvironment()
    act = _acts(db_pool)
    await _run_row(db_pool, TYPE_A, "failed", minutes_ago=10)
    await _run_row(db_pool, TYPE_B, "failed", minutes_ago=5)
    subjects = {f["subject"] for f in await env.run(act.find_failing_flows, 2, 24)}
    assert TYPE_A not in subjects and TYPE_B not in subjects


@pytest.mark.asyncio
async def test_empty_window_returns_empty_and_no_pool_degrades(db_pool):
    """Degradation: nothing to read is [], not an exception. Non-vacuous —
    the same seeded data DOES produce a finding at a 24h lookback."""
    await _prep(db_pool)
    env = ActivityEnvironment()
    act = _acts(db_pool)
    await _run_row(db_pool, TYPE_A, "failed", minutes_ago=30)
    await _run_row(db_pool, TYPE_A, "failed", minutes_ago=20)
    assert await env.run(act.find_failing_flows, 2, 24) != []
    # lookback 0 => `started_at > now()` matches no row that exists anywhere.
    assert await env.run(act.find_failing_flows, 2, 0) == []
    assert await env.run(_acts(None).find_failing_flows, 2, 24) == []


# ---------------------------------------------------------------------------
# detector 2: schedules that stopped succeeding
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_schedule_detected_only_past_its_threshold(db_pool):
    """A */5 schedule: threshold is the 60-min floor (3 x 5 min is smaller),
    so 30 min idle is healthy and 3h idle is stale."""
    await _prep(db_pool)
    env = ActivityEnvironment()
    act = _acts(db_pool)
    await _activity_row(db_pool, SLUG_A, TYPE_A, "*/5 * * * *")

    await _run_row(
        db_pool, TYPE_A, "completed", 30, workflow_id=f"scheduled-{SLUG_A}--vabc-2026"
    )
    assert SLUG_A not in {f["subject"] for f in await env.run(act.find_stale_flows, 3.0, 60)}

    await _run_row(
        db_pool, TYPE_A, "completed", 180, workflow_id=f"scheduled-{SLUG_A}--vabc-2025"
    )
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM workflow_runs WHERE workflow_id = $1",
            f"scheduled-{SLUG_A}--vabc-2026",
        )
    stale = [f for f in await env.run(act.find_stale_flows, 3.0, 60) if f["subject"] == SLUG_A]
    assert len(stale) == 1
    assert stale[0]["kind"] == "stale"
    assert stale[0]["threshold_minutes"] == 60
    assert 175 <= stale[0]["idle_minutes"] <= 185


@pytest.mark.asyncio
async def test_a_failed_run_does_not_count_as_a_success(db_pool):
    """`stale` is measured from the last SUCCESS — a schedule that fires and
    fails every time is still stale (and is caught twice, once by each rule)."""
    await _prep(db_pool)
    env = ActivityEnvironment()
    act = _acts(db_pool)
    await _activity_row(db_pool, SLUG_A, TYPE_A, "0 * * * *")
    await _run_row(
        db_pool, TYPE_A, "completed", 60 * 24, workflow_id=f"scheduled-{SLUG_A}--v1-old"
    )
    await _run_row(db_pool, TYPE_A, "failed", 1, workflow_id=f"scheduled-{SLUG_A}--v1-new")
    assert SLUG_A in {f["subject"] for f in await env.run(act.find_stale_flows, 3.0, 60)}


@pytest.mark.asyncio
async def test_never_succeeded_schedules_are_not_stale(db_pool):
    """A never-run schedule is unconfigured or feature-flag-gated, not a
    regression — a fresh install must not scream on first boot."""
    await _prep(db_pool)
    env = ActivityEnvironment()
    act = _acts(db_pool)
    await _activity_row(db_pool, SLUG_A, TYPE_A, "*/5 * * * *")
    assert SLUG_A not in {f["subject"] for f in await env.run(act.find_stale_flows, 3.0, 60)}


@pytest.mark.asyncio
async def test_another_slugs_success_does_not_count(db_pool):
    """Two schedules share one workflow_type (DayLog daily/weekly/monthly,
    IntelligenceScan x3). Success is matched on the scheduled workflow_id
    prefix, so the healthy sibling must not mask the dead one."""
    await _prep(db_pool)
    env = ActivityEnvironment()
    act = _acts(db_pool)
    await _activity_row(db_pool, SLUG_A, TYPE_A, "*/5 * * * *")
    await _activity_row(db_pool, "zzwd-sched-sibling", TYPE_A, "*/5 * * * *")
    await _run_row(db_pool, TYPE_A, "completed", 300, workflow_id=f"scheduled-{SLUG_A}--v1-x")
    await _run_row(
        db_pool, TYPE_A, "completed", 1, workflow_id="scheduled-zzwd-sched-sibling--v1-y"
    )
    subjects = {f["subject"] for f in await env.run(act.find_stale_flows, 3.0, 60)}
    assert SLUG_A in subjects, "the sibling's fresh success leaked across slugs"
    assert "zzwd-sched-sibling" not in subjects


@pytest.mark.asyncio
async def test_inactive_schedules_are_ignored(db_pool):
    await _prep(db_pool)
    env = ActivityEnvironment()
    act = _acts(db_pool)
    await _activity_row(db_pool, SLUG_A, TYPE_A, "*/5 * * * *")
    await _run_row(db_pool, TYPE_A, "completed", 300, workflow_id=f"scheduled-{SLUG_A}--v1-x")
    assert SLUG_A in {f["subject"] for f in await env.run(act.find_stale_flows, 3.0, 60)}
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE activities SET active = FALSE WHERE slug = $1", SLUG_A)
    assert SLUG_A not in {f["subject"] for f in await env.run(act.find_stale_flows, 3.0, 60)}


@pytest.mark.asyncio
async def test_unparseable_cron_is_skipped_and_logged(db_pool):
    """Degradation: one bad cron must not crash the sweep or invent a cadence,
    and it must leave a trace. structlog bypasses stdlib logging, so caplog
    would see nothing — capture_logs is the only thing that works here."""
    await _prep(db_pool)
    env = ActivityEnvironment()
    act = _acts(db_pool)
    await _activity_row(db_pool, SLUG_A, TYPE_A, "every other tuesday")
    await _activity_row(db_pool, "zzwd-sched-ok", TYPE_B, "*/5 * * * *")
    await _run_row(db_pool, TYPE_A, "completed", 60 * 24 * 90, workflow_id=f"scheduled-{SLUG_A}--v1")
    await _run_row(
        db_pool, TYPE_B, "completed", 60 * 24 * 90, workflow_id="scheduled-zzwd-sched-ok--v1"
    )

    with structlog.testing.capture_logs() as logs:
        stale = await env.run(act.find_stale_flows, 3.0, 60)

    subjects = {f["subject"] for f in stale}
    assert "zzwd-sched-ok" in subjects, "the parseable sibling must still be swept"
    assert SLUG_A not in subjects, "an unparseable cron must not produce a finding"
    warned = [
        entry
        for entry in logs
        if entry.get("event") == "flow_health_cron_unparseable" and entry.get("slug") == SLUG_A
    ]
    assert len(warned) == 1, f"expected one unparseable-cron warning, got {logs}"


@pytest.mark.asyncio
async def test_stale_no_pool_degrades(db_pool):
    env = ActivityEnvironment()
    assert await env.run(_acts(None).find_stale_flows, 3.0, 60) == []


# ---------------------------------------------------------------------------
# detector 3: LLM purposes that call and never succeed (#321)
# ---------------------------------------------------------------------------
#
# The workflow-level detectors cannot see this at all. Almost every LLM caller
# in AEGIS catches its own failure and degrades to non-LLM output, so the flow
# COMPLETES, the schedule stays fresh, and the thinking part of it quietly
# produces nothing — six days of it in #255.


@pytest.mark.asyncio
async def test_a_purpose_that_never_succeeds_is_reported(db_pool):
    """The #255 shape exactly: briefing_frame returned empty content on 100% of
    calls for six days while the briefing went out every morning."""
    await _prep(db_pool)
    await _llm_row(db_pool, PURPOSE_A, "error", 30)
    await _llm_row(db_pool, PURPOSE_A, "error", 20)
    await _llm_row(db_pool, PURPOSE_A, "error", 10)

    found = await ActivityEnvironment().run(_acts(db_pool).find_dead_llm_purposes, 2, 24)

    hit = next(f for f in found if f["purpose"] == PURPOSE_A)
    assert hit["kind"] == "llm_dead"
    assert hit["calls"] == 3
    assert hit["errors"] == 3
    assert hit["model"] == "kimi-k2.5"
    assert hit["window_hours"] == 24


@pytest.mark.asyncio
async def test_the_subject_is_namespaced_against_flow_names(db_pool):
    """Dedup, mutes and recovery all key on the bare subject string, which a
    workflow_type and an activities slug already share. A purpose called
    `todoist_sync` must not be able to mute TodoistSyncFlow."""
    await _prep(db_pool)
    await _llm_row(db_pool, PURPOSE_A, "error", 10)
    await _llm_row(db_pool, PURPOSE_A, "timeout", 5)

    found = await ActivityEnvironment().run(_acts(db_pool).find_dead_llm_purposes, 2, 24)

    hit = next(f for f in found if f["purpose"] == PURPOSE_A)
    assert hit["subject"] == LLM_SUBJECT_PREFIX + PURPOSE_A
    assert hit["timeouts"] == 1


@pytest.mark.asyncio
async def test_one_success_clears_the_purpose(db_pool):
    """THE false-positive guard. A purpose that works at all is not dead, and a
    watchdog that cried on partial failure would be muted within a week."""
    await _prep(db_pool)
    await _llm_row(db_pool, PURPOSE_A, "error", 30)
    await _llm_row(db_pool, PURPOSE_A, "error", 20)
    await _llm_row(db_pool, PURPOSE_A, "success", 10, error=None)

    found = await ActivityEnvironment().run(_acts(db_pool).find_dead_llm_purposes, 2, 24)

    assert PURPOSE_A not in {f["purpose"] for f in found}


@pytest.mark.asyncio
async def test_a_single_failed_call_is_below_the_threshold(db_pool):
    """One failure is a blip — same reasoning as `consecutive_failures=2`. It
    also keeps a one-shot purpose (a chat tool nobody ran twice) off the card."""
    await _prep(db_pool)
    await _llm_row(db_pool, PURPOSE_A, "error", 10)

    found = await ActivityEnvironment().run(_acts(db_pool).find_dead_llm_purposes, 2, 24)

    assert PURPOSE_A not in {f["purpose"] for f in found}


@pytest.mark.asyncio
async def test_calls_outside_the_window_are_ignored(db_pool):
    """A purpose that broke, was fixed and is no longer called must stop
    alerting on its own rather than needing a mute."""
    await _prep(db_pool)
    await _llm_row(db_pool, PURPOSE_A, "error", 60 * 30)
    await _llm_row(db_pool, PURPOSE_A, "error", 60 * 26)

    found = await ActivityEnvironment().run(_acts(db_pool).find_dead_llm_purposes, 2, 24)

    assert PURPOSE_A not in {f["purpose"] for f in found}


@pytest.mark.asyncio
async def test_a_purpose_that_only_ever_clips_is_reported(db_pool):
    """`clipped` is a response cut mid-write (#255), not a success. Every call
    clipping means every call returns truncated JSON — the same silent
    uselessness, and it must not hide behind a status that is not 'error'."""
    await _prep(db_pool)
    await _llm_row(db_pool, PURPOSE_A, "clipped", 20, error="clipped: cut mid-response")
    await _llm_row(db_pool, PURPOSE_A, "clipped", 10, error="clipped: cut mid-response")

    found = await ActivityEnvironment().run(_acts(db_pool).find_dead_llm_purposes, 2, 24)

    hit = next(f for f in found if f["purpose"] == PURPOSE_A)
    assert hit["clipped"] == 2


@pytest.mark.asyncio
async def test_purposes_do_not_combine(db_pool):
    """Per purpose, not per fleet: the cause is usually one budget, one prompt
    or one model behind one tier. Two failures spread over two purposes is two
    blips, not one dead purpose."""
    await _prep(db_pool)
    await _llm_row(db_pool, PURPOSE_A, "error", 20)
    await _llm_row(db_pool, PURPOSE_B, "error", 10)

    found = await ActivityEnvironment().run(_acts(db_pool).find_dead_llm_purposes, 2, 24)

    assert {PURPOSE_A, PURPOSE_B} & {f["purpose"] for f in found} == set()


@pytest.mark.asyncio
async def test_the_last_error_is_carried_for_diagnosis(db_pool):
    """The card has to say WHY, or the operator's first move is the same SQL
    query every time."""
    await _prep(db_pool)
    await _llm_row(db_pool, PURPOSE_A, "error", 20, error="truncated: old one")
    await _llm_row(db_pool, PURPOSE_A, "error", 5, error="truncated: the newest one")

    found = await ActivityEnvironment().run(_acts(db_pool).find_dead_llm_purposes, 2, 24)

    assert next(f for f in found if f["purpose"] == PURPOSE_A)["reason"] == (
        "truncated: the newest one"
    )


@pytest.mark.asyncio
async def test_dead_llm_no_pool_degrades(db_pool):
    assert await ActivityEnvironment().run(_acts(None).find_dead_llm_purposes, 2, 24) == []


@pytest.mark.asyncio
async def test_a_dead_purpose_alerts_through_the_existing_plumbing(db_pool):
    """No new notification path: a dead purpose rides the same card, the same
    audit-log dedup and the same mute key as a failing flow. The card names the
    purpose and hands over the query that applies to IT, not the workflow_runs
    one."""
    await _prep(db_pool)
    await _llm_row(db_pool, PURPOSE_A, "error", 20)
    await _llm_row(db_pool, PURPOSE_A, "error", 10)
    env = ActivityEnvironment()
    delivery = FakeDelivery()
    act = _acts(db_pool, delivery)

    findings = await env.run(act.find_dead_llm_purposes, 2, 24)
    findings = [f for f in findings if f["purpose"] == PURPOSE_A]
    first = await env.run(act.report_flow_health, findings, "pandoras-actor", 12, 168)
    second = await env.run(act.report_flow_health, findings, "pandoras-actor", 12, 168)

    assert (first["alerted"], second["alerted"]) == (1, 0), "a wedged purpose alerts once"
    assert second["deduped"] == 1
    assert len(delivery.sent) == 1
    card = delivery.sent[0]
    assert PURPOSE_A in card
    assert "ZERO successful" in card
    assert "FROM llm_calls WHERE purpose" in card


# ---------------------------------------------------------------------------
# alerting: dedup, recovery, mutes
# ---------------------------------------------------------------------------


def _finding(subject: str = TYPE_A) -> dict:
    return {
        "kind": "failing",
        "subject": subject,
        "workflow_type": subject,
        "consecutive": 2,
        "last_run_at": "2026-08-02T12:40:00+00:00",
        "reason": "todoist_sync_failed at step=apply_sync_diff",
    }


@pytest.mark.asyncio
async def test_a_wedged_flow_produces_exactly_one_alert(db_pool):
    """THE dedup property. A 5-minute flow wedged for a day would run this
    watchdog 48 times; the operator must get one card, not 48."""
    await _prep(db_pool)
    env = ActivityEnvironment()
    delivery = FakeDelivery()
    act = _acts(db_pool, delivery)

    results = [
        await env.run(act.report_flow_health, [_finding()], "pandoras-actor", 12, 168)
        for _ in range(48)
    ]
    assert sum(r["alerted"] for r in results) == 1
    assert sum(r["deduped"] for r in results) == 47
    assert len(delivery.sent) == 1, f"sent {len(delivery.sent)} cards, expected exactly 1"
    assert TYPE_A in delivery.sent[0]

    async with db_pool.acquire() as conn:
        n = await conn.fetchval(
            "SELECT count(*) FROM audit_log WHERE action = $1 AND target_id = $2",
            ALERT_ACTION,
            TYPE_A,
        )
    assert n == 1


@pytest.mark.asyncio
async def test_dedup_expires_after_the_window(db_pool):
    """Proves the dedup is time-bounded, not permanent — a fault still open
    after `dedup_hours` re-alerts once."""
    await _prep(db_pool)
    env = ActivityEnvironment()
    delivery = FakeDelivery()
    act = _acts(db_pool, delivery)
    await env.run(act.report_flow_health, [_finding()], "pandoras-actor", 12, 168)
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE audit_log SET created_at = now() - interval '13 hours' "
            "WHERE actor = 'flow-health-watchdog'"
        )
    r = await env.run(act.report_flow_health, [_finding()], "pandoras-actor", 12, 168)
    assert r["alerted"] == 1
    assert len(delivery.sent) == 2


@pytest.mark.asyncio
async def test_recovery_is_announced_and_re_arms_the_alert(db_pool):
    """Recovery is observable two ways at once: the operator gets a [FLOW OK]
    card, and the audit row it writes is what re-arms dedup — so the same flow
    breaking again alerts again instead of being suppressed."""
    await _prep(db_pool)
    env = ActivityEnvironment()
    delivery = FakeDelivery()
    act = _acts(db_pool, delivery)

    assert (await env.run(act.report_flow_health, [_finding()], "a", 12, 168))["alerted"] == 1

    recovered = await env.run(act.report_flow_health, [], "a", 12, 168)
    assert recovered["recovered"] == 1
    assert len(delivery.sent) == 2
    assert "FLOW OK" in delivery.sent[1] and TYPE_A in delivery.sent[1]
    async with db_pool.acquire() as conn:
        n = await conn.fetchval(
            "SELECT count(*) FROM audit_log WHERE action = $1 AND target_id = $2",
            RECOVERY_ACTION,
            TYPE_A,
        )
    assert n == 1

    # Re-armed: the SAME subject, still inside the 12h dedup window, alerts.
    again = await env.run(act.report_flow_health, [_finding()], "a", 12, 168)
    assert again["alerted"] == 1
    assert len(delivery.sent) == 3

    # ...and does not re-announce a recovery that was already announced.
    quiet = await env.run(act.report_flow_health, [_finding()], "a", 12, 168)
    assert quiet == {"alerted": 0, "deduped": 1, "muted": 0, "recovered": 0}


@pytest.mark.asyncio
async def test_no_recovery_notice_without_a_prior_alert(db_pool):
    """A healthy system is silent: zero findings and no open alert is nothing
    at all, not a stream of 'recovered' cards."""
    await _prep(db_pool)
    env = ActivityEnvironment()
    delivery = FakeDelivery()
    act = _acts(db_pool, delivery)
    r = await env.run(act.report_flow_health, [], "a", 12, 168)
    assert r == {"alerted": 0, "deduped": 0, "muted": 0, "recovered": 0}
    assert delivery.sent == []


@pytest.mark.asyncio
async def test_an_active_mute_silences_a_subject(db_pool):
    await _prep(db_pool)
    env = ActivityEnvironment()
    delivery = FakeDelivery()
    act = _acts(db_pool, delivery)
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO alert_mutes (mute_key, muted_until) VALUES ($1, now() + interval '1 day')",
            MUTE_PREFIX + TYPE_A,
        )
    r = await env.run(act.report_flow_health, [_finding()], "a", 12, 168)
    assert r["muted"] == 1
    assert r["alerted"] == 0
    assert delivery.sent == []


@pytest.mark.asyncio
async def test_an_expired_mute_does_not_silence(db_pool):
    """Guard-independence: the mute lookup and the dedup lookup must each be
    provably load-bearing. This one isolates `muted_until > now()`."""
    await _prep(db_pool)
    env = ActivityEnvironment()
    delivery = FakeDelivery()
    act = _acts(db_pool, delivery)
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO alert_mutes (mute_key, muted_until) VALUES ($1, now() - interval '1 hour')",
            MUTE_PREFIX + TYPE_A,
        )
    r = await env.run(act.report_flow_health, [_finding()], "a", 12, 168)
    assert r["muted"] == 0
    assert r["alerted"] == 1
    assert len(delivery.sent) == 1


@pytest.mark.asyncio
async def test_one_card_carries_every_fresh_subject(db_pool):
    """Two broken flows is one card with both, not two cards."""
    await _prep(db_pool)
    env = ActivityEnvironment()
    delivery = FakeDelivery()
    act = _acts(db_pool, delivery)
    r = await env.run(
        act.report_flow_health, [_finding(TYPE_A), _finding(TYPE_B)], "a", 12, 168
    )
    assert r["alerted"] == 2
    assert len(delivery.sent) == 1
    assert TYPE_A in delivery.sent[0] and TYPE_B in delivery.sent[0]


@pytest.mark.asyncio
async def test_report_without_pool_degrades(db_pool):
    env = ActivityEnvironment()
    r = await env.run(_acts(None).report_flow_health, [_finding()], "a", 12, 168)
    assert r == {"alerted": 0, "deduped": 0, "muted": 0, "recovered": 0}
