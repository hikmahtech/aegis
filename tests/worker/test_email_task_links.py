"""Tests for email → existing-task links (settings.email_task_links).

The rule that matters most is the negative one: Jira sends the SAME subject for
every event on an issue, so a rule that matched on subject alone would close a
ticket because somebody left a comment on it.
"""

from __future__ import annotations

import pytest
from aegis.services.email_task_links import (
    match_link,
    merge,
    task_key_pattern,
)
from aegis_worker.activities.capture import CaptureActivities
from temporalio.testing import ActivityEnvironment

JIRA_DONE = {
    "key": "jira-done",
    "subject_re": r"\((APP-\d+)\)",
    "body_re": r"changed the status.*to\s*'?Done|resolved this issue",
    "action": "complete",
}

# Jira's subject line is identical for a resolution and a comment.
SUBJECT = "[JIRA] (APP-1234) Login page throws on submit"
BODY_RESOLVED = "Alex Doe changed the status of APP-1234 to Done"
BODY_COMMENTED = "Alex Doe commented on APP-1234:\n> any update on this?"


class _RecordingConnector:
    """Captures submitted commands; reports success for all of them."""

    def __init__(self):
        self.submitted: list[dict] = []

    async def commands(self, cmds: list[dict]) -> dict:
        self.submitted.extend(cmds)
        return {
            "ok": True,
            "data": {"sync_status": {c["uuid"]: "ok" for c in cmds}, "temp_id_mapping": {}},
            "error": None,
            "retryable": False,
        }


def _types(connector: _RecordingConnector) -> list[str]:
    return [c["type"] for c in connector.submitted]


# ---------------------------------------------------------------- pure matching


def test_body_discriminator_separates_resolved_from_commented():
    assert match_link([JIRA_DONE], SUBJECT, BODY_RESOLVED) == {
        "key": "jira-done",
        "action": "complete",
        "task_key": "APP-1234",
    }
    # Same subject, different event — must NOT fire.
    assert match_link([JIRA_DONE], SUBJECT, BODY_COMMENTED) is None


def test_no_subject_match_is_no_link():
    assert match_link([JIRA_DONE], "Your Amazon order has shipped", BODY_RESOLVED) is None


def test_first_rule_wins():
    unblock = {**JIRA_DONE, "key": "first", "body_re": None, "action": "unblock"}
    hit = match_link([unblock, JIRA_DONE], SUBJECT, BODY_RESOLVED)
    assert hit["key"] == "first" and hit["action"] == "unblock"


def test_merge_drops_malformed_rules_without_raising():
    kept = merge(
        [
            JIRA_DONE,
            {"key": "bad-action", "subject_re": "x", "action": "delete_everything"},
            {"key": "bad-regex", "subject_re": "([unclosed", "action": "complete"},
            {"key": "", "subject_re": "x", "action": "complete"},
            {**JIRA_DONE, "key": "jira-done"},  # duplicate key
            "not-a-dict",
        ]
    )
    assert [r["key"] for r in kept] == ["jira-done"]
    assert merge(None) == [] and merge({"nope": 1}) == []


@pytest.mark.asyncio
async def test_task_key_pattern_is_word_bounded_in_postgres(db_pool):
    """APP-12 must not match APP-123 — checked in the engine that runs it."""
    async with db_pool.acquire() as conn:
        pat = task_key_pattern("APP-12")
        assert await conn.fetchval("SELECT $1 ~ $2", "APP-12: Fix login", pat) is True
        assert await conn.fetchval("SELECT $1 ~ $2", "Fix login (APP-12)", pat) is True
        assert await conn.fetchval("SELECT $1 ~ $2", "APP-123: Fix login", pat) is False
        # A key carrying regex metacharacters stays literal, not a wildcard.
        dotted = task_key_pattern("A.C-1")
        assert await conn.fetchval("SELECT $1 ~ $2", "AXC-1: nope", dotted) is False


# ------------------------------------------------------------------- activity


async def _seed(db_pool, task_id: str, content: str, labels: list[str], completed: bool = False):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO settings (key, value) VALUES ('email_task_links', $1) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            [JIRA_DONE],
        )
        # Tests share Jira keys, so clear every candidate — otherwise an earlier
        # test's still-open row is what the lookup finds.
        await conn.execute("DELETE FROM todoist_tasks WHERE content LIKE 'APP-%'")
        await conn.execute(
            "INSERT INTO todoist_tasks (id, content, labels, is_completed) VALUES ($1,$2,$3,$4)",
            task_id,
            content,
            labels,
            completed,
        )


@pytest.mark.asyncio
async def test_complete_closes_the_open_task(db_pool):
    await _seed(db_pool, "etl-1", "APP-1234: Login page throws on submit", ["@next"])
    connector = _RecordingConnector()
    acts = CaptureActivities(db_pool=db_pool, connector=connector)
    result = await ActivityEnvironment().run(
        acts.link_email_to_task,
        {"id": "m1", "subject": SUBJECT, "permalink": "https://mail/x"},
        BODY_RESOLVED,
    )
    assert result["applied"] is True
    assert result["task_id"] == "etl-1"
    assert result["action"] == "complete"
    # A note is always left first — the audit trail for a task closed by a robot.
    assert _types(connector) == ["note_add", "item_complete"]
    assert connector.submitted[0]["args"]["item_id"] == "etl-1"
    assert connector.submitted[1]["args"]["id"] == "etl-1"


@pytest.mark.asyncio
async def test_commented_email_touches_nothing(db_pool):
    await _seed(db_pool, "etl-2", "APP-1234: Login page throws on submit", ["@next"])
    connector = _RecordingConnector()
    acts = CaptureActivities(db_pool=db_pool, connector=connector)
    result = await ActivityEnvironment().run(
        acts.link_email_to_task,
        {"id": "m2", "subject": SUBJECT},
        BODY_COMMENTED,
    )
    assert result == {"applied": False, "reason": "no_match"}
    assert connector.submitted == []


@pytest.mark.asyncio
async def test_already_completed_task_is_not_touched_again(db_pool):
    """What makes a backfill re-runnable: nothing open, nothing to do."""
    await _seed(db_pool, "etl-3", "APP-1234: done already", ["@next"], completed=True)
    connector = _RecordingConnector()
    acts = CaptureActivities(db_pool=db_pool, connector=connector)
    result = await ActivityEnvironment().run(
        acts.link_email_to_task,
        {"id": "m3", "subject": SUBJECT},
        BODY_RESOLVED,
    )
    assert result["applied"] is False and result["reason"] == "no_open_task"
    assert connector.submitted == []


@pytest.mark.asyncio
async def test_unblock_swaps_waiting_for_next(db_pool):
    await _seed(db_pool, "etl-4", "APP-99: chase vendor", ["@waiting", "@email"])
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO settings (key, value) VALUES ('email_task_links', $1) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            [{**JIRA_DONE, "body_re": None, "action": "unblock"}],
        )
    connector = _RecordingConnector()
    acts = CaptureActivities(db_pool=db_pool, connector=connector)
    result = await ActivityEnvironment().run(
        acts.link_email_to_task,
        {"id": "m4", "subject": "[JIRA] (APP-99) chase vendor"},
        "anything",
    )
    assert result["applied"] is True
    assert _types(connector) == ["note_add", "item_update"]
    labels = connector.submitted[1]["args"]["labels"]
    assert "@waiting" not in labels
    assert "@next" in labels and "@email" in labels


@pytest.mark.asyncio
async def test_no_rules_configured_is_a_noop(db_pool):
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM settings WHERE key = 'email_task_links'")
    connector = _RecordingConnector()
    acts = CaptureActivities(db_pool=db_pool, connector=connector)
    result = await ActivityEnvironment().run(
        acts.link_email_to_task, {"id": "m5", "subject": SUBJECT}, BODY_RESOLVED
    )
    assert result == {"applied": False, "reason": "no_rules"}
    assert connector.submitted == []
