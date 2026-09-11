"""Verb resolution and task-context extraction."""

from __future__ import annotations

import pytest
import pytest_asyncio
from aegis.services.gtd_rules import SOURCE_TAGS
from aegis.services.hub_project import MONEY_SOURCE_TAG
from aegis.services.hub_project import SOURCE_TAG as HUB_SOURCE_TAG
from aegis_worker.activities.agent_task import (
    DEFAULT_VERBS,
    UNTAGGED,
    VERBS,
    VERBS_SETTING,
    AgentTaskActivities,
    extract_node_name,
    extract_service_name,
    merge_verbs,
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
        # #344: each of these is a request to the agent the task was given to,
        # so it goes to that agent's own chat path instead of parking unworked.
        ("#chat", ["@pandora"], "ask"),
        ("#research", ["@raphael"], "ask"),
        ("#calendar", ["@sebas"], "ask"),
        ("#manual", ["@sebas"], "ask"),
        # A hand-written task with an agent's label and no @code is the same
        # request `#manual` is.
        (None, ["@pandora"], "ask"),
        # Decided: nothing works these (Maou raises them, the user acts).
        ("#money", ["@maou"], "none"),
        # Nobody decided: a tag outside the table.
        ("#brand-new", ["@sebas"], "unknown"),
    ],
)
def test_resolve_verb(source_tag, labels, expected):
    assert resolve_verb({"source_tag": source_tag, "labels": labels}) == expected


def test_every_source_tag_has_a_decision():
    """The `_GTD_STATE_FOR` contract (#139) applied to verbs (#344): every tag
    AEGIS captures under maps to a verb or to an explicit None. Before, the
    table knew three of seven tags, so "nobody wired one up" and "deliberately
    nothing" were the same silent park. A tag added to `SOURCE_TAGS` (or a new
    hub tag) without a decision here fails this test."""
    tags = {*SOURCE_TAGS, HUB_SOURCE_TAG, MONEY_SOURCE_TAG, UNTAGGED}
    missing = tags - set(DEFAULT_VERBS)
    assert not missing, f"no verb decision for {sorted(missing)}"
    for tag, verb in DEFAULT_VERBS.items():
        assert verb is None or verb in VERBS, f"{tag} maps to unknown verb {verb!r}"


def test_merge_verbs_lets_a_setting_change_a_tag():
    merged = merge_verbs({"#calendar": None, "#chat": "infra"})
    assert merged["#calendar"] is None
    assert merged["#chat"] == "infra"
    # Everything the setting does not name keeps its default.
    assert merged["#alert"] == DEFAULT_VERBS["#alert"]


def test_merge_verbs_ignores_a_verb_the_lane_does_not_have():
    """Lenient on read, like every settings merge here: a typo must not turn a
    working tag into a parked one."""
    merged = merge_verbs({"#chat": "chta", "#manual": 3})
    assert merged["#chat"] == DEFAULT_VERBS["#chat"]
    assert merged["#manual"] == DEFAULT_VERBS["#manual"]


@pytest.mark.parametrize("value", [None, [], "ask", 7])
def test_merge_verbs_of_a_malformed_row_is_the_defaults(value):
    assert merge_verbs(value) == DEFAULT_VERBS


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("PROLONGED: clickhouse_clickhouse degraded for over 2 hours", "clickhouse_clickhouse"),
        ("PROLONGED: postiz_postiz-postgres degraded for over 2 hours", "postiz_postiz-postgres"),
        ("Service ollama_ollama has fewer tasks than desired", "ollama_ollama"),
        ("Loki is down", "loki"),
        ("PostgreSQL is down", "postgresql"),
        ("AttributeError: 'MongoRepository' object has no attribute 'db'", ""),
        # The heartbeat's own titles (flows/infra_heartbeat.py). A task that
        # predates the hub has no problem to read the subject from, and these
        # parked as "couldn't tell which service" in prod.
        ("Service portainer_agent down", "portainer_agent"),
        ("PROLONGED: miniflux_miniflux still down after 6h", "miniflux_miniflux"),
    ],
)
def test_extract_service_name(title, expected):
    assert extract_service_name(title) == expected


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Swarm node node-b down", "node-b"),
        ("Service portainer_agent down", ""),
        ("Something odd", ""),
    ],
)
def test_extract_node_name(title, expected):
    assert extract_node_name(title) == expected


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


async def test_load_task_context_reads_the_capture_source(db_pool, _ctx_seed):
    """The alert lane's own id is on the row, but only as `external_id`: the
    `fingerprint` this used to split out was the pre-hub identity, and nothing
    has looked an alert up by one since the hub replaced that lookup."""
    ctx = await AgentTaskActivities(db_pool=db_pool).load_task_context("ct-1")
    assert ctx["external_id"] == "alert-a2827e4213f4dae4"
    assert ctx["gmail_message_id"] == ""
    assert "fingerprint" not in ctx and "problem_id" not in ctx


async def test_load_task_context_gmail_message_id(db_pool, _ctx_seed):
    ctx = await AgentTaskActivities(db_pool=db_pool).load_task_context("ct-2")
    assert ctx["gmail_message_id"] == "19f761cbfd89d8c8"


async def test_load_task_context_missing_row_is_empty(db_pool, _ctx_seed):
    ctx = await AgentTaskActivities(db_pool=db_pool).load_task_context("ct-absent")
    assert ctx == {
        "external_id": "",
        "gmail_message_id": "",
        "subject": "",
        "subject_kind": "",
        "verb": "unknown",
    }


@pytest_asyncio.fixture(loop_scope="function")
async def _verb_seed(db_pool):
    await db_pool.execute("DELETE FROM todoist_tasks WHERE id LIKE 'cv-%'")
    await db_pool.execute("DELETE FROM settings WHERE key = $1", VERBS_SETTING)
    await db_pool.execute(
        "INSERT INTO todoist_tasks (id, content, labels, source_tag, assignee_label, is_completed) "
        "VALUES ('cv-1','prep for the review', ARRAY['#calendar','@sebas'], '#calendar', '@sebas', false),"
        "       ('cv-2','fix it', ARRAY['@pandora','@code'], NULL, '@pandora', false)"
    )
    yield
    await db_pool.execute("DELETE FROM todoist_tasks WHERE id LIKE 'cv-%'")
    await db_pool.execute("DELETE FROM settings WHERE key = $1", VERBS_SETTING)


async def test_load_task_context_resolves_the_verb_from_the_task_row(db_pool, _verb_seed):
    """The flow cannot read the database, so the verb — which a setting can
    change — comes back from this activity."""
    act = AgentTaskActivities(db_pool=db_pool)
    assert (await act.load_task_context("cv-1"))["verb"] == "ask"
    assert (await act.load_task_context("cv-2"))["verb"] == "coding"


async def test_load_task_context_verb_follows_the_setting(db_pool, _verb_seed):
    """A deployment that wants `#calendar` tasks left alone says so in the
    `agent_task_verbs` row; no code change."""
    await db_pool.execute(
        "INSERT INTO settings (key, value) VALUES ($1, $2)",
        VERBS_SETTING,
        {"#calendar": None},
    )
    ctx = await AgentTaskActivities(db_pool=db_pool).load_task_context("cv-1")
    assert ctx["verb"] == "none"


async def test_load_task_context_reads_the_hub_problem_behind_a_task(db_pool, _ctx_seed):
    """A task the problem hub projected carries its subject exactly, so the
    infra verb does not have to parse the title."""
    pid = await db_pool.fetchval(
        "INSERT INTO problems (correlation_key, class, subject, subject_kind, title, todoist_task_id) "
        "VALUES ('dockerservicedown:service:ct_svc', 'dockerservicedown', 'ct_svc', 'service', "
        "'t', 'ct-3') RETURNING id::text"
    )
    try:
        ctx = await AgentTaskActivities(db_pool=db_pool).load_task_context("ct-3")
        assert ctx["subject"] == "ct_svc" and ctx["subject_kind"] == "service"
        assert ctx["external_id"] == ""
        assert pid
    finally:
        await db_pool.execute("DELETE FROM problems WHERE id = $1::uuid", pid)
