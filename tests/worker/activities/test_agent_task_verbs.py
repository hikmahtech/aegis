"""Verb resolution and task-context extraction."""

from __future__ import annotations

import pytest
import pytest_asyncio
from aegis_worker.activities.agent_task import (
    AgentTaskActivities,
    extract_service_name,
    resolve_verb,
)


@pytest.mark.parametrize(
    ("source_tag", "labels", "expected"),
    [
        ("#alert", ["@pandora"], "infra"),
        ("#receipt", ["@maou"], "finance"),
        ("#email", ["@sebas"], "email"),
        (None, ["@pandora", "@code"], "coding"),
        # source_tag wins over a stray @code label. Clarify put @code on a real
        # #email task in prod; running a coding agent on an email is nonsense.
        ("#email", ["@sebas", "@code"], "email"),
        (None, ["@pandora"], "unknown"),
        ("#chat", ["@pandora"], "unknown"),
    ],
)
def test_resolve_verb(source_tag, labels, expected):
    assert resolve_verb({"source_tag": source_tag, "labels": labels}) == expected


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("PROLONGED: clickhouse_clickhouse degraded for over 2 hours", "clickhouse_clickhouse"),
        ("PROLONGED: postiz_postiz-postgres degraded for over 2 hours", "postiz_postiz-postgres"),
        ("Service ollama_ollama has fewer tasks than desired", "ollama_ollama"),
        ("Loki is down", "loki"),
        ("PostgreSQL is down", "postgresql"),
        ("AttributeError: 'MongoRepository' object has no attribute 'db'", ""),
    ],
)
def test_extract_service_name(title, expected):
    assert extract_service_name(title) == expected


@pytest_asyncio.fixture(loop_scope="function")
async def _ctx_seed(db_pool):
    await db_pool.execute("DELETE FROM todoist_capture_idempotency WHERE todoist_task_ref LIKE 'ct-%'")
    await db_pool.execute(
        """
        INSERT INTO todoist_capture_idempotency (source_tag, external_id, todoist_task_ref)
        VALUES ('#alert','alert-a2827e4213f4dae4','ct-1'),
               ('#email','gmail-19f761cbfd89d8c8','ct-2')
        """
    )
    yield
    await db_pool.execute("DELETE FROM todoist_capture_idempotency WHERE todoist_task_ref LIKE 'ct-%'")


async def test_load_task_context_alert_fingerprint(db_pool, _ctx_seed):
    ctx = await AgentTaskActivities(db_pool=db_pool).load_task_context("ct-1")
    assert ctx["fingerprint"] == "a2827e4213f4dae4"
    assert ctx["gmail_message_id"] == ""


async def test_load_task_context_gmail_message_id(db_pool, _ctx_seed):
    ctx = await AgentTaskActivities(db_pool=db_pool).load_task_context("ct-2")
    assert ctx["gmail_message_id"] == "19f761cbfd89d8c8"
    assert ctx["fingerprint"] == ""


async def test_load_task_context_missing_row_is_empty(db_pool, _ctx_seed):
    ctx = await AgentTaskActivities(db_pool=db_pool).load_task_context("ct-absent")
    assert ctx == {"external_id": "", "fingerprint": "", "gmail_message_id": ""}
