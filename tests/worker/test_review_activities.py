"""ReviewActivities — Phase 5 daily + weekly digest gathering + ack."""

from __future__ import annotations

import pytest
import pytest_asyncio
from aegis.db import run_migrations
from aegis_worker.activities.review import (
    ReviewActivities,
    format_daily_preview,
    format_weekly_preview,
)


@pytest_asyncio.fixture(loop_scope="function")
async def _review_seeded(db_pool):
    """Seed managed projects + a few tasks for digest tests.

    Lays down:
      - P_INBOX_R (inbox), with 2 unclassified source-tagged tasks
      - P_NEXT_R (next), with active tasks incl. a stale project/*-labelled one
      - 2 resting tasks carrying the @someday label (Someday/Later is a
        label now, not a managed project — Todoist restructure, 2026-07)
    """
    await run_migrations(db_pool)
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO settings (key, value) VALUES "
            "('todoist_managed_project_ids', $1) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            {
                "inbox": "P_INBOX_R",
                "next": "P_NEXT_R",
                "someday": "P_SOMEDAY_R",
            },
        )
        # Clean any prior state
        await conn.execute(
            "DELETE FROM todoist_notes WHERE item_id IN "
            "(SELECT id FROM todoist_tasks WHERE project_id LIKE 'P_%_R')"
        )
        await conn.execute("DELETE FROM todoist_tasks WHERE project_id LIKE 'P_%_R'")
        # Stale @waiting rows left behind by other test files would compete
        # for the LIMIT-5 waiting_stale_top slots — clear them.
        await conn.execute(
            "DELETE FROM todoist_tasks WHERE '@waiting' = ANY(labels) "
            "AND updated_at < now() - interval '7 days'"
        )
        await conn.execute(
            "DELETE FROM todoist_projects WHERE id LIKE 'P_%_R'"
        )
        for pid in ("P_INBOX_R", "P_NEXT_R", "P_SOMEDAY_R"):
            await conn.execute(
                "INSERT INTO todoist_projects (id, name, is_managed, raw) "
                "VALUES ($1, $1, true, '{}'::jsonb) "
                "ON CONFLICT (id) DO UPDATE SET is_managed=true",
                pid,
            )
        # 2 unclassified Inbox tasks (source_tag set, last_clarified_at NULL)
        await conn.execute(
            "INSERT INTO todoist_tasks "
            "(id, project_id, content, labels, source_tag, is_completed, raw) "
            "VALUES "
            "('T_INB_R1','P_INBOX_R','Newsletter from FT',ARRAY['#research'],'#research',false,'{}'::jsonb), "
            "('T_INB_R2','P_INBOX_R','Reply to vendor',ARRAY['#email'],'#email',false,'{}'::jsonb)"
        )
        # 1 task due today, @me. Seed via SQL CURRENT_DATE (UTC-relative)
        # rather than Python's dt.date.today() (local-tz-relative) so the
        # match against the activity's `due_date <= CURRENT_DATE` is
        # deterministic regardless of test-machine timezone offset.
        await conn.execute(
            "INSERT INTO todoist_tasks "
            "(id, project_id, content, labels, assignee_label, due_date, is_completed, raw) "
            "VALUES ('T_TODAY_R','P_NEXT_R','Pay Axis Bank',"
            "ARRAY['@me','@phone'],'@me',CURRENT_DATE,false,'{}'::jsonb)"
        )
        # 1 @waiting task stale (updated > 3 days ago). The @waiting label is
        # the sole signal that this task is waiting-for.
        await conn.execute(
            "INSERT INTO todoist_tasks "
            "(id, project_id, content, labels, assignee_label, is_completed, "
            " updated_at, raw) "
            "VALUES ('T_WAIT_R','P_NEXT_R','Chase invoice','{@waiting,@me}',"
            "'@me',false,now() - interval '5 days','{}'::jsonb)"
        )
        # 1 stale next action carrying a project/* work-stream label (>14d)
        await conn.execute(
            "INSERT INTO todoist_tasks "
            "(id, project_id, content, labels, assignee_label, is_completed, "
            " updated_at, raw) "
            "VALUES ('T_STALE_R','P_NEXT_R','Stale next action',"
            "'{@me,project/bcp}','@me',"
            "false, now() - interval '20 days','{}'::jsonb)"
        )
        # 2 resting tasks carrying the @someday label (Todoist restructure,
        # 2026-07: Someday/Later is a label now, not a managed project).
        await conn.execute(
            "INSERT INTO todoist_tasks "
            "(id, project_id, content, labels, is_completed, raw) VALUES "
            "('T_SOM_R1','P_SOMEDAY_R','Learn the violin','{@me,@someday}',false,'{}'::jsonb), "
            "('T_SOM_R2','P_SOMEDAY_R','Read Sapiens','{@me,@someday}',false,'{}'::jsonb)"
        )
        # 1 task completed within last 7d
        await conn.execute(
            "INSERT INTO todoist_tasks "
            "(id, project_id, content, labels, assignee_label, is_completed, "
            " completed_at, raw) "
            "VALUES ('T_DONE_R','P_NEXT_R','Done this week','{@me}','@me',true,"
            "now() - interval '2 days','{}'::jsonb)"
        )
        # 1 applied clarify in last 24h
        await conn.execute(
            "INSERT INTO gtd_clarify_log "
            "(todoist_task_id, pass, source_tag, classification, confidence, "
            " llm_model, applied) "
            "VALUES ('T_APPLIED_R', 1, '#email', 'next_action', 0.9, "
            "'qwen3:14b', true)"
        )
        # 1 @waiting task stale >7d with a delegate/* label — weekly per-item
        # nudge material.
        await conn.execute(
            "INSERT INTO todoist_tasks "
            "(id, project_id, content, labels, assignee_label, is_completed, "
            " updated_at, raw) "
            "VALUES ('T_WAIT7_R','P_NEXT_R','Await design review',"
            "'{@waiting,delegate/john}','@me',false,"
            "now() - interval '10 days','{}'::jsonb)"
        )
        # 1 permanently failed outbox command (recent) — lost-write signal.
        await conn.execute(
            "DELETE FROM todoist_outbox WHERE temp_id = 'rv-failed-1'"
        )
        await conn.execute(
            "INSERT INTO todoist_outbox (temp_id, command, status, attempt_count) "
            "VALUES ('rv-failed-1', '{\"type\": \"item_add\"}'::jsonb, 'failed', 5)"
        )


@pytest.mark.asyncio
async def test_gather_daily_digest_counts_waiting_by_label(db_pool, _review_seeded) -> None:
    """@waiting tasks in regular projects count toward waiting_stale_count."""
    acts = ReviewActivities(db_pool=db_pool)
    digest = await acts.gather_daily_digest()
    # The seeded inbox has 2 unclassified tasks
    assert digest["inbox_count"] >= 2
    # At least 1 due-today task (we just inserted T_TODAY_R)
    assert digest["due_today_count"] >= 1
    # T_WAIT_R lives in P_PRJ_R (not a waiting-for project) but has @waiting
    # label and is 5 days stale — must be counted by the label-based query.
    assert digest["waiting_stale_count"] >= 1
    # Pending clarify count includes our 2 unclassified Inbox tasks
    assert digest["pending_clarify_count"] >= 2
    # Applied last 24h includes our row
    assert digest["applied_24h_count"] >= 1
    # Preview formatting should compose without raising
    body = format_daily_preview(digest)
    assert "Daily review" in body
    assert "Inbox" in body


@pytest.mark.asyncio
async def test_gather_daily_digest_empty_when_no_managed_settings(db_pool) -> None:
    await run_migrations(db_pool)
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM settings WHERE key='todoist_managed_project_ids'"
        )
    acts = ReviewActivities(db_pool=db_pool)
    digest = await acts.gather_daily_digest()
    assert digest["inbox_count"] == 0
    assert digest["due_today_count"] == 0


@pytest.mark.asyncio
async def test_daily_digest_counts_waiting_by_label(db_pool) -> None:
    """A @waiting task in a regular project counts toward waiting_stale_count."""
    await run_migrations(db_pool)
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO settings (key, value) VALUES "
            "('todoist_managed_project_ids', $1) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            {"inbox": "P_INBOX", "projects": "P_PRJ", "single_actions": "P_SA"},
        )
        # Insert the project first (FK constraint on todoist_tasks.project_id)
        await conn.execute(
            "INSERT INTO todoist_projects (id, name, is_managed, raw) "
            "VALUES ('P_BCP', 'BCP Project', false, '{}'::jsonb) "
            "ON CONFLICT (id) DO NOTHING"
        )
        # The task lives in a regular project (P_BCP) and is marked @waiting
        # via the labels array — exactly the state-as-label model.
        await conn.execute(
            "INSERT INTO todoist_tasks (id, content, labels, project_id, "
            "is_completed, updated_at, raw) VALUES "
            "('T_WLBL', 'awaiting legal', ARRAY['@waiting','@me'], 'P_BCP', "
            "false, now() - interval '5 days', '{}'::jsonb) "
            "ON CONFLICT (id) DO UPDATE SET labels = EXCLUDED.labels, "
            "updated_at = EXCLUDED.updated_at"
        )
    acts = ReviewActivities(db_pool=db_pool)
    out = await acts.gather_daily_digest()
    assert out["waiting_stale_count"] >= 1


@pytest.mark.asyncio
async def test_weekly_digest_counts_waiting_by_label(db_pool) -> None:
    """A @waiting task in a regular project counts toward waiting_stale_7d_count."""
    await run_migrations(db_pool)
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO settings (key, value) VALUES "
            "('todoist_managed_project_ids', $1) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            {"inbox": "P_INBOX2", "projects": "P_PRJ2"},
        )
        # Insert the project first (FK constraint on todoist_tasks.project_id)
        await conn.execute(
            "INSERT INTO todoist_projects (id, name, is_managed, raw) "
            "VALUES ('P_OTHER', 'Other Project', false, '{}'::jsonb) "
            "ON CONFLICT (id) DO NOTHING"
        )
        # Task in a regular project with @waiting label, stale > 7 days
        await conn.execute(
            "INSERT INTO todoist_tasks (id, content, labels, project_id, "
            "is_completed, updated_at, raw) VALUES "
            "('T_WLBL7', 'waiting on supplier', ARRAY['@waiting'], 'P_OTHER', "
            "false, now() - interval '10 days', '{}'::jsonb) "
            "ON CONFLICT (id) DO UPDATE SET labels = EXCLUDED.labels, "
            "updated_at = EXCLUDED.updated_at"
        )
    acts = ReviewActivities(db_pool=db_pool)
    out = await acts.gather_weekly_digest()
    assert out["waiting_stale_7d_count"] >= 1


@pytest.mark.asyncio
async def test_gather_weekly_digest_counts(db_pool, _review_seeded) -> None:
    """stale_next_actions_count now requires the task to live in a LEAF
    work-stream project (a real Todoist project with parent_id IS NOT NULL,
    nested under an AREA project) — project/* labels (e.g. T_STALE_R's
    'project/bcp', planted by _review_seeded) are retired and no longer
    drive staleness. Plant a nested AREA + work-stream project pair here so
    the metric has something to actually count under the current model.
    """
    area_id = "P_AREA_R"
    ws_id = "P_WS_R"
    stale_task_id = "T_STALE_WS_R"
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM todoist_tasks WHERE id = $1", stale_task_id)
        await conn.execute(
            "DELETE FROM todoist_projects WHERE id IN ($1,$2)", ws_id, area_id
        )
        # AREA project (parent_id IS NULL) — not itself a work-stream.
        await conn.execute(
            "INSERT INTO todoist_projects (id, name, parent_id, is_managed, is_archived, raw) "
            "VALUES ($1, 'Area R', NULL, false, false, '{}'::jsonb) "
            "ON CONFLICT (id) DO NOTHING",
            area_id,
        )
        # Leaf work-stream project nested under the area.
        await conn.execute(
            "INSERT INTO todoist_projects (id, name, parent_id, is_managed, is_archived, raw) "
            "VALUES ($1, 'Work-stream R', $2, false, false, '{}'::jsonb) "
            "ON CONFLICT (id) DO NOTHING",
            ws_id, area_id,
        )
        # Stale (>14d untouched) open task living in the leaf work-stream.
        await conn.execute(
            "INSERT INTO todoist_tasks "
            "(id, project_id, content, labels, is_completed, updated_at, raw) "
            "VALUES ($1, $2, 'Stale work-stream action', '{@me}', false, "
            "now() - interval '20 days', '{}'::jsonb)",
            stale_task_id, ws_id,
        )
    try:
        acts = ReviewActivities(db_pool=db_pool)
        digest = await acts.gather_weekly_digest()
        # T_STALE_WS_R lives in a leaf work-stream project (parent_id set)
        # and is 20d old → counts as a stale next action.
        assert digest["stale_next_actions_count"] >= 1
        # T_SOM_R1 + T_SOM_R2 carry the @someday label. someday_count is now a
        # global count (state-as-label model, not scoped to a managed project),
        # so other test files' @someday-labelled rows can add to it — use >=.
        assert digest["someday_count"] >= 2
        # T_WAIT_R has @waiting label but is only 5d old → does NOT cross 7d threshold
        assert digest["waiting_stale_7d_count"] >= 0
        # T_DONE_R completed 2d ago
        assert digest["completed_7d_count"] >= 1
        body = format_weekly_preview(digest)
        assert "Weekly review" in body
        assert "Someday / Later:" in body
    finally:
        async with db_pool.acquire() as conn:
            await conn.execute("DELETE FROM todoist_tasks WHERE id = $1", stale_task_id)
            await conn.execute(
                "DELETE FROM todoist_projects WHERE id IN ($1,$2)", ws_id, area_id
            )


@pytest.mark.asyncio
async def test_daily_digest_surfaces_failed_outbox(db_pool, _review_seeded) -> None:
    """A todoist_outbox row in status='failed' (lost write) must show in the
    daily digest — previously nothing read failed rows at all."""
    acts = ReviewActivities(db_pool=db_pool)
    digest = await acts.gather_daily_digest()
    assert digest["outbox_failed_7d_count"] >= 1
    body = format_daily_preview(digest)
    assert "Todoist write failures" in body


@pytest.mark.asyncio
async def test_daily_preview_omits_outbox_line_when_clean() -> None:
    body = format_daily_preview({"inbox_count": 0, "outbox_failed_7d_count": 0})
    assert "Todoist write failures" not in body


@pytest.mark.asyncio
async def test_weekly_digest_lists_stale_waiting_items(db_pool, _review_seeded) -> None:
    """The weekly review names the actual stale @waiting tasks (with their
    delegate/* labels), not just a count."""
    acts = ReviewActivities(db_pool=db_pool)
    digest = await acts.gather_weekly_digest()
    top = digest["waiting_stale_top"]
    match = [i for i in top if i["content"] == "Await design review"]
    assert match, f"T_WAIT7_R missing from waiting_stale_top: {top}"
    assert match[0]["days"] >= 7
    assert "delegate/john" in match[0]["delegates"]
    body = format_weekly_preview(digest)
    assert "Await design review" in body
    assert "delegate/john" in body


@pytest.mark.asyncio
async def test_log_review_digest_inserts_row(db_pool, _review_seeded) -> None:
    acts = ReviewActivities(db_pool=db_pool)
    digest = {"inbox_count": 3, "due_today_count": 2}
    rid = await acts.log_review_digest(
        kind="daily",
        counts=digest,
        preview="hello",
        interaction_id="iw-x",
    )
    assert rid > 0
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT review_kind, counts, preview, interaction_id, acknowledged "
            "FROM review_digest_log WHERE id=$1",
            rid,
        )
    assert row["review_kind"] == "daily"
    assert row["counts"] == digest
    assert row["preview"] == "hello"
    assert row["interaction_id"] == "iw-x"
    assert row["acknowledged"] is False


async def _seed_interaction(db_pool, flow_run_id: str) -> str:
    """Insert an interactions row and return its UUID (as string)."""
    async with db_pool.acquire() as conn:
        return str(await conn.fetchval(
            "INSERT INTO interactions "
            "(flow_run_id, agent_id, kind, origin, prompt) "
            "VALUES ($1, 'sebas', 'choice', 'gtd_daily_review', 'p') "
            "RETURNING id",
            flow_run_id,
        ))


@pytest.mark.asyncio
async def test_apply_review_acknowledgement_updates_row(db_pool, _review_seeded) -> None:
    acts = ReviewActivities(db_pool=db_pool)
    # Production stores review_digest_log.interaction_id = InteractionFlow's
    # workflow_id (flow_run_id), but apply_review_acknowledgement is called
    # with the interactions.id UUID. The fix bridges them via subquery.
    flow_run_id = "gtd-review-daily-test-ack"
    uid = await _seed_interaction(db_pool, flow_run_id)
    rid = await acts.log_review_digest(
        kind="daily",
        counts={"inbox_count": 1},
        preview="x",
        interaction_id=flow_run_id,
    )
    out = await acts.apply_review_acknowledgement(
        interaction_id=uid,
        response={"value": "reviewed"},
        metadata={"source": "gtd_review", "kind": "daily"},
    )
    assert out["acknowledged"] is True
    assert out["kind"] == "daily"
    assert out["choice"] == "reviewed"
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT acknowledged, user_choice, acknowledged_at FROM review_digest_log "
            "WHERE id=$1",
            rid,
        )
    assert row["acknowledged"] is True
    assert row["user_choice"] == "reviewed"
    assert row["acknowledged_at"] is not None


@pytest.mark.asyncio
async def test_apply_review_acknowledgement_no_matching_row(db_pool) -> None:
    await run_migrations(db_pool)
    acts = ReviewActivities(db_pool=db_pool)
    # Unknown UUID — no interactions row, subquery yields NULL, UPDATE no-ops.
    out = await acts.apply_review_acknowledgement(
        interaction_id="00000000-0000-0000-0000-000000000000",
        response={"value": "reviewed"},
        metadata={"kind": "daily"},
    )
    # Activity still returns success (idempotent — the matching row simply
    # doesn't exist), but no row was updated.
    assert out["acknowledged"] is True


def test_format_daily_preview_compact_when_empty() -> None:
    """Even with all-zero digest, the formatter produces a clear 'all clear'
    body — important so the user gets confirmation the flow ran."""
    body = format_daily_preview(
        {
            "inbox_count": 0,
            "inbox_top3": [],
            "due_today_count": 0,
            "due_today_top3": [],
            "waiting_stale_count": 0,
            "pending_clarify_count": 0,
            "applied_24h_count": 0,
        }
    )
    assert "Daily review" in body
    assert "clear" in body.lower() or "nothing" in body.lower()


def test_format_weekly_preview_truncates_long_input() -> None:
    body = format_weekly_preview(
        {
            "stale_next_actions_count": 999,
            "stale_next_actions_top3": ["x" * 200] * 3,
            "someday_count": 5,
            "waiting_stale_7d_count": 2,
            "inbox_unclarified_7d_count": 4,
            "completed_7d_count": 7,
        }
    )
    assert len(body) <= 3600


# --- Phase 5 polish: snooze ---


@pytest.mark.asyncio
async def test_apply_review_acknowledgement_snoozes_on_need_time(
    db_pool, _review_seeded, monkeypatch
) -> None:
    """choice='need_time' on a daily review starts a DailyReviewFlow with
    start_delay=1h via the Temporal client."""
    from unittest.mock import AsyncMock, MagicMock

    # Capture the workflow start call
    started: dict = {}

    class _StubClient:
        @staticmethod
        async def connect(host):
            client = MagicMock()
            async def _start_workflow(*args, **kwargs):
                started["args"] = args
                started["kwargs"] = kwargs
            client.start_workflow = AsyncMock(side_effect=_start_workflow)
            return client

    monkeypatch.setattr(
        "temporalio.client.Client", _StubClient
    )

    acts = ReviewActivities(
        db_pool=db_pool, temporal_host="aegis_temporal:7233"
    )
    # Seed paired interaction + digest row so the UPDATE actually finds it
    flow_run_id = "gtd-review-daily-test-snooze"
    uid = await _seed_interaction(db_pool, flow_run_id)
    await acts.log_review_digest(
        kind="daily", counts={}, preview="x", interaction_id=flow_run_id,
    )
    out = await acts.apply_review_acknowledgement(
        interaction_id=uid,
        response={"value": "need_time"},
        metadata={"source": "gtd_review", "kind": "daily"},
    )
    assert out["acknowledged"] is True
    assert out["snoozed"] is True
    # start_workflow was called with DailyReviewFlow + start_delay=1h
    assert started["args"][0] == "DailyReviewFlow"
    kw = started["kwargs"]
    assert kw.get("task_queue") == "aegis-main"
    from datetime import timedelta as _td
    assert kw.get("start_delay") == _td(hours=1)
    assert kw.get("id", "").startswith("daily-review-snooze-")


@pytest.mark.asyncio
async def test_apply_review_acknowledgement_no_snooze_when_no_temporal_host(
    db_pool, _review_seeded
) -> None:
    """Without temporal_host set, snooze gracefully no-ops; acknowledgement
    still records."""
    acts = ReviewActivities(db_pool=db_pool, temporal_host=None)
    flow_run_id = "gtd-review-daily-test-noth"
    uid = await _seed_interaction(db_pool, flow_run_id)
    await acts.log_review_digest(
        kind="daily", counts={}, preview="x", interaction_id=flow_run_id,
    )
    out = await acts.apply_review_acknowledgement(
        interaction_id=uid,
        response={"value": "need_time"},
        metadata={"kind": "daily"},
    )
    assert out["acknowledged"] is True
    assert out["snoozed"] is False


@pytest.mark.asyncio
async def test_apply_review_acknowledgement_reviewed_choice_no_snooze(
    db_pool, _review_seeded
) -> None:
    """choice='reviewed' should NOT trigger snooze even with temporal_host."""
    acts = ReviewActivities(
        db_pool=db_pool, temporal_host="aegis_temporal:7233"
    )
    flow_run_id = "gtd-review-daily-test-rev"
    uid = await _seed_interaction(db_pool, flow_run_id)
    await acts.log_review_digest(
        kind="daily", counts={}, preview="x", interaction_id=flow_run_id,
    )
    out = await acts.apply_review_acknowledgement(
        interaction_id=uid,
        response={"value": "reviewed"},
        metadata={"kind": "daily"},
    )
    assert out["acknowledged"] is True
    assert out["snoozed"] is False


# --- Bug-fix tests: daily mislabel + weekly never-clarified backlog ---


@pytest.mark.asyncio
async def test_daily_preview_labels_inbox_as_open_not_unclassified(db_pool) -> None:
    """Inbox line must say 'open', not 'unclassified'. The phantom 'unclassified'
    label is the prod bug: inbox_count = ALL open Inbox tasks, but it was
    labelled as though they all need clarification."""
    await run_migrations(db_pool)
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO settings (key, value) VALUES "
            "('todoist_managed_project_ids', $1) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            {"inbox": "P_INBOX_OPEN", "next": "P_NEXT_OPEN", "someday": "P_SOM_OPEN"},
        )
        for pid in ("P_INBOX_OPEN", "P_NEXT_OPEN", "P_SOM_OPEN"):
            await conn.execute(
                "INSERT INTO todoist_projects (id, name, is_managed, raw) "
                "VALUES ($1, $1, true, '{}'::jsonb) ON CONFLICT (id) DO NOTHING",
                pid,
            )
        await conn.execute("DELETE FROM todoist_tasks WHERE project_id LIKE 'P_%_OPEN'")
        # Seed clarified Inbox tasks (last_clarified_at IS NOT NULL)
        await conn.execute(
            "INSERT INTO todoist_tasks "
            "(id, project_id, content, source_tag, last_clarified_at, is_completed, raw) "
            "VALUES ('T_CLR1','P_INBOX_OPEN','Clarified task','#email',"
            "now() - interval '1 day',false,'{}'::jsonb)"
        )
        await conn.execute(
            "INSERT INTO todoist_tasks "
            "(id, project_id, content, source_tag, last_clarified_at, is_completed, raw) "
            "VALUES ('T_CLR2','P_INBOX_OPEN','Also clarified','#email',"
            "now() - interval '2 days',false,'{}'::jsonb)"
        )
    acts = ReviewActivities(db_pool=db_pool)
    digest = await acts.gather_daily_digest()
    # inbox_count counts ALL open tasks, clarified or not
    assert digest["inbox_count"] >= 2
    # pending_clarify_count is 0 — both tasks are clarified
    assert digest["pending_clarify_count"] == 0
    body = format_daily_preview(digest)
    # Must NOT say "unclassified"
    assert "unclassified" not in body
    # Must say "open"
    assert "open" in body
    # The needs-clarify line must be ABSENT (no unclarified tasks)
    assert "Needs clarify" not in body


@pytest.mark.asyncio
async def test_daily_preview_shows_needs_clarify_when_pending(db_pool) -> None:
    """When there are unclarified Inbox tasks (source_tag set, last_clarified_at
    NULL), a conditional 'Needs clarify: N' line appears in the card."""
    await run_migrations(db_pool)
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO settings (key, value) VALUES "
            "('todoist_managed_project_ids', $1) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            {"inbox": "P_INBOX_NC", "next": "P_NEXT_NC", "someday": "P_SOM_NC"},
        )
        for pid in ("P_INBOX_NC", "P_NEXT_NC", "P_SOM_NC"):
            await conn.execute(
                "INSERT INTO todoist_projects (id, name, is_managed, raw) "
                "VALUES ($1, $1, true, '{}'::jsonb) ON CONFLICT (id) DO NOTHING",
                pid,
            )
        await conn.execute("DELETE FROM todoist_tasks WHERE project_id LIKE 'P_%_NC'")
        # One clarified task
        await conn.execute(
            "INSERT INTO todoist_tasks "
            "(id, project_id, content, source_tag, last_clarified_at, is_completed, raw) "
            "VALUES ('T_CLR_NC','P_INBOX_NC','Clarified','#email',"
            "now(),false,'{}'::jsonb)"
        )
        # One unclarified task (source_tag set, last_clarified_at NULL)
        await conn.execute(
            "INSERT INTO todoist_tasks "
            "(id, project_id, content, source_tag, is_completed, raw) "
            "VALUES ('T_UNCLR_NC','P_INBOX_NC','Unclarified','#email',"
            "false,'{}'::jsonb)"
        )
    acts = ReviewActivities(db_pool=db_pool)
    digest = await acts.gather_daily_digest()
    assert digest["inbox_count"] >= 2
    assert digest["pending_clarify_count"] >= 1
    body = format_daily_preview(digest)
    assert "unclassified" not in body
    # inbox line says "open"
    assert "open" in body
    # Needs-clarify line IS shown
    assert "Needs clarify: 1" in body


@pytest.mark.asyncio
async def test_weekly_digest_surfaces_never_clarified_across_all_projects(
    db_pool,
) -> None:
    """The never-clarified metric must count open tasks with last_clarified_at IS NULL
    across inbox + next + someday, and list the oldest by raw->>'added_at'."""
    await run_migrations(db_pool)
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO settings (key, value) VALUES "
            "('todoist_managed_project_ids', $1) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            {"inbox": "P_INBOX_W", "next": "P_NEXT_W", "someday": "P_SOM_W"},
        )
        for pid in ("P_INBOX_W", "P_NEXT_W", "P_SOM_W"):
            await conn.execute(
                "INSERT INTO todoist_projects (id, name, is_managed, raw) "
                "VALUES ($1, $1, true, '{}'::jsonb) ON CONFLICT (id) DO NOTHING",
                pid,
            )
        await conn.execute("DELETE FROM todoist_tasks WHERE project_id LIKE 'P_%_W'")
        # Older task in Next (should appear first in oldest5)
        await conn.execute(
            "INSERT INTO todoist_tasks "
            "(id, project_id, content, is_completed, raw) "
            "VALUES ('T_NC_NEXT','P_NEXT_W','Pay Grantha 2L',false,"
            "'{\"added_at\": \"2026-05-19T10:00:00Z\"}'::jsonb)"
        )
        # Newer task in Someday
        await conn.execute(
            "INSERT INTO todoist_tasks "
            "(id, project_id, content, is_completed, raw) "
            "VALUES ('T_NC_SOM','P_SOM_W','Learn guitar',false,"
            "'{\"added_at\": \"2026-06-01T10:00:00Z\"}'::jsonb)"
        )
        # Clarified task in Inbox — must NOT be counted
        await conn.execute(
            "INSERT INTO todoist_tasks "
            "(id, project_id, content, last_clarified_at, is_completed, raw) "
            "VALUES ('T_CLR_INBOX_W','P_INBOX_W','Already clarified',"
            "now(),false,'{\"added_at\": \"2026-06-10T10:00:00Z\"}'::jsonb)"
        )
        # Completed task with NULL last_clarified_at — must NOT be counted
        await conn.execute(
            "INSERT INTO todoist_tasks "
            "(id, project_id, content, is_completed, completed_at, raw) "
            "VALUES ('T_DONE_W','P_NEXT_W','Done task',true,now(),"
            "'{\"added_at\": \"2026-05-01T10:00:00Z\"}'::jsonb)"
        )
    acts = ReviewActivities(db_pool=db_pool)
    digest = await acts.gather_weekly_digest()
    # 2 open + unclarified tasks (T_NC_NEXT + T_NC_SOM); clarified + completed excluded
    assert digest["never_clarified_count"] >= 2
    oldest5 = digest["never_clarified_oldest5"]
    assert len(oldest5) >= 2
    # Oldest (May 19) should be first
    assert oldest5[0]["content"] == "Pay Grantha 2L"
    assert oldest5[0]["age_days"] >= 20
    # Second is June 1
    assert oldest5[1]["content"] == "Learn guitar"
    body = format_weekly_preview(digest)
    assert "Never-clarified" in body
    assert "Pay Grantha 2L" in body


def test_weekly_preview_omits_never_clarified_when_zero() -> None:
    """When never_clarified_count is 0, the section must be absent (no noise)."""
    body = format_weekly_preview({
        "stale_next_actions_count": 0,
        "stale_next_actions_top3": [],
        "someday_count": 0,
        "waiting_stale_7d_count": 0,
        "waiting_stale_top": [],
        "inbox_unclarified_7d_count": 0,
        "completed_7d_count": 0,
        "never_clarified_count": 0,
        "never_clarified_oldest5": [],
    })
    assert "Never-clarified" not in body
