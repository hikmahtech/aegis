"""#513 — a tracked topic is one hub problem that Raphael owns.

Real test database; the Todoist Sync API is replaced by a recorder, as in
test_hub_project.py.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio
from aegis.connectors.todoist import TodoistConnector
from aegis.services import hub_project, research_topics
from aegis.services.hub import Event, digest, get_problem, ingest_event
from aegis.services.hub_group import candidates
from aegis.services.hub_project import project, project_pending, reconcile_completed_tasks
from aegis.services.hub_watch import reconcile_findings

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 9, 12, 9, 0, tzinfo=UTC)


@pytest.fixture
def todoist(monkeypatch):
    state = {"batches": []}

    async def fake_commands(self, commands):
        state["batches"].append(commands)
        mapping = {c["temp_id"]: f"T{uuid.uuid4().hex[:10]}" for c in commands if "temp_id" in c}
        return {
            "ok": True,
            "data": {"sync_status": {c["uuid"]: "ok" for c in commands}, "temp_id_mapping": mapping},
        }

    async def fake_key(pool, settings):
        return "test-key"

    async def _close(self):
        return None

    monkeypatch.setattr(TodoistConnector, "commands", fake_commands)
    monkeypatch.setattr(TodoistConnector, "close", _close)
    monkeypatch.setattr("aegis.services.hub_project.resolve_todoist_api_key", fake_key)
    monkeypatch.setattr("aegis.services.tools.gtd.resolve_todoist_api_key", fake_key)
    monkeypatch.setattr("aegis.config.Settings", lambda: SimpleNamespace(secret_key="x"))
    return state


@pytest_asyncio.fixture(loop_scope="function")
async def world(db_pool):
    await db_pool.execute(
        "INSERT INTO settings (key, value) VALUES ('todoist_managed_project_ids', $1) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        {"inbox": "P_INBOX"},
    )
    await db_pool.execute(
        "INSERT INTO todoist_projects (id, name, is_managed, raw) "
        "VALUES ('P_INBOX','Inbox',true,'{}'::jsonb) ON CONFLICT (id) DO NOTHING"
    )
    await db_pool.execute("DELETE FROM settings WHERE key = $1", research_topics.TOPICS_SETTING)
    yield db_pool
    await db_pool.execute("DELETE FROM settings WHERE key = $1", research_topics.TOPICS_SETTING)


def _name() -> str:
    return f"Topic {uuid.uuid4().hex[:6]}"


def _item(n: int, term: str, **kw) -> dict:
    return {
        "title": kw.get("title", f"Story {n} about {term}"),
        "url": kw.get("url", f"https://news.example/{uuid.uuid4().hex[:8]}/{n}"),
        "summary": kw.get("summary", ""),
    }


def _item_adds(todoist) -> list[dict]:
    return [c for batch in todoist["batches"] for c in batch if c["type"] == "item_add"]


# --- pure ----------------------------------------------------------------------


def test_parse_topics_is_lenient_and_dedupes_by_slug():
    topics = research_topics.parse_topics(
        {
            "topics": [
                {"name": "Bitcoin ETFs", "queries": ["bitcoin etf", " "], "priority": "high"},
                {"name": "bitcoin etfs", "queries": ["dupe"]},
                {"name": "", "queries": ["x"]},
                "junk",
                {"name": "Rust", "queries": "not a list", "priority": "urgent"},
            ]
        }
    )
    assert [(t.name, t.queries, t.priority) for t in topics] == [
        ("Bitcoin ETFs", ("bitcoin etf",), "high"),
        ("Rust", (), "medium"),
    ]
    assert topics[1].terms == ("Rust",)
    assert research_topics.parse_topics("not json") == []


def test_match_topics_is_whole_word_and_any_case():
    rust = research_topics.Topic("Rust", ("rust",))
    ai = research_topics.Topic("AI", ("ai",))
    assert research_topics.match_topics([rust, ai], "Why RUST matters") == [rust]
    # Inside a word is not a match: "trust" is not about Rust, "said" is not AI.
    assert research_topics.match_topics([rust, ai], "In trust we said") == []


# --- track / untrack -----------------------------------------------------------


async def test_track_writes_the_registry_and_opens_a_round_raphael_owns(world):
    name = _name()
    out = await research_topics.track(world, name, ["alpha", "beta"], "high", now=NOW)
    assert out["status"] == "added" and out["task_after_items"] == 2
    row = await world.fetchval(
        "SELECT value FROM settings WHERE key = $1", research_topics.TOPICS_SETTING
    )
    assert row["topics"][-1] == {"name": name, "queries": ["alpha", "beta"], "priority": "high"}
    problem = await get_problem(world, out["problem_id"])
    assert problem["class"] == "topic" and problem["subject_kind"] == "topic"
    assert problem["metadata"]["topic"] == name
    owner = await hub_project._owner(world, out["problem_id"])
    assert (owner.source_tag, owner.fallback_label) == ("#research", "@raphael")

    again = await research_topics.track(world, name.upper(), ["gamma"], now=NOW)
    assert again["status"] == "updated" and again["problem_id"] == out["problem_id"]
    assert again["total_topics"] == 1


async def test_track_refuses_an_empty_topic(world):
    with pytest.raises(ValueError, match="topic_name and queries are required"):
        await research_topics.track(world, "x", [])


async def test_untrack_drops_the_topic_and_closes_its_round(world):
    name = _name()
    pid = (await research_topics.track(world, name, ["alpha"], now=NOW))["problem_id"]
    out = await research_topics.untrack(world, name, now=NOW)
    assert out["status"] == "removed" and out["round_closed"] is True
    assert (await get_problem(world, pid))["closed_at"] is not None
    assert await research_topics.load_topics(world) == []
    assert (await research_topics.untrack(world, name))["status"] == "not_found"


# --- attach, attention, projection ---------------------------------------------


async def test_items_attach_once_and_a_round_below_its_threshold_raises_no_task(world, todoist):
    name = _name()
    pid = (await research_topics.track(world, name, ["alpha"], now=NOW))["problem_id"]
    item = _item(1, "alpha")
    out = await research_topics.attach_items(
        world, [item, _item(2, "nothing here")], origin="intel:hn", now=NOW
    )
    assert (out["matched"], out["attached"], out["tasks"]) == (1, 1, 0)
    # The same article again, from another path, attaches nothing.
    again = await research_topics.attach_items(world, [item], origin="rss", now=NOW)
    assert again["attached"] == 0

    assert (await project(world, pid, now=NOW))["skipped"] == "below_attention"
    # `project_pending` sweeps every problem in the shared test database, so
    # the check is about THIS round: never selected, and no task of its own.
    assert pid not in {r["problem_id"] for r in await project_pending(world, now=NOW)}
    assert (await get_problem(world, pid))["todoist_task_id"] is None
    assert not [c for c in _item_adds(todoist) if "#research" in c["args"].get("labels", [])]


async def test_a_round_that_crosses_its_threshold_raises_one_research_task(world, todoist):
    name = _name()
    pid = (await research_topics.track(world, name, ["alpha"], "medium", now=NOW))["problem_id"]
    items = [_item(n, "alpha") for n in range(3)]
    out = await research_topics.attach_items(world, items, origin="intel:news", now=NOW)
    assert out["tasks"] == 1
    adds = _item_adds(todoist)
    assert len(adds) == 1
    args = adds[0]["args"]
    assert args["labels"][:1] == ["#research"] and "@raphael" in args["labels"]
    assert "@next" in args["labels"]
    # The description lists the round's articles, not the latest one alone.
    for it in items:
        assert it["url"] in args["description"]
    problem = await get_problem(world, pid)
    assert problem["todoist_task_id"] and problem["metadata"]["attention"] is True

    # More items become one collapsed comment, in the topic's own words.
    await research_topics.attach_items(
        world, [_item(9, "alpha")], origin="rss", now=NOW + timedelta(hours=1)
    )
    await project(world, pid, now=NOW + timedelta(hours=1))
    notes = [c for batch in todoist["batches"] for c in batch if c["type"] == "note_add"]
    assert any("new item" in n["args"]["content"] for n in notes)


async def test_completing_a_topic_task_closes_the_round_and_the_next_item_starts_fresh(
    world, todoist
):
    name = _name()
    pid = (await research_topics.track(world, name, ["alpha"], "high", now=NOW))["problem_id"]
    await research_topics.attach_items(
        world, [_item(n, "alpha") for n in range(2)], origin="intel:hn", now=NOW
    )
    task_id = (await get_problem(world, pid))["todoist_task_id"]
    await world.execute(
        "INSERT INTO todoist_tasks (id, project_id, content, labels, assignee_label, source_tag, "
        "is_completed, completed_at, raw) VALUES ($1,'P_INBOX','t',ARRAY['#research','@raphael'],"
        "'@raphael','#research',true,$2,'{}'::jsonb)",
        task_id,
        NOW + timedelta(hours=2),
    )
    done = await reconcile_completed_tasks(world, now=NOW + timedelta(hours=3))
    assert {d["problem_id"] for d in done} >= {pid}
    assert (await get_problem(world, pid))["closed_at"] is not None

    # The next article opens a NEW round with no task, instead of reopening
    # the task the user just ticked off.
    await research_topics.attach_items(
        world, [_item(5, "alpha")], origin="rss", now=NOW + timedelta(hours=4)
    )
    fresh = await research_topics.live_problem(world, research_topics.Topic(name, ("alpha",)))
    assert fresh is not None and fresh["id"] != pid and fresh["todoist_task_id"] is None


# --- owners, groups, digest ----------------------------------------------------


async def test_a_feed_finding_is_raphaels_and_tagged_for_the_user(world, todoist):
    feed = f"https://feeds.example/{uuid.uuid4().hex[:8]}"
    out = await reconcile_findings(
        world,
        source="feeds",
        subject_kind="feed",
        classes=["feed_failing"],
        findings=[{"klass": "feed_failing", "subject": feed, "title": "RSS feed failing"}],
        now=NOW,
    )
    pid = out["fresh"][0]["problem_id"]
    adds = _item_adds(todoist)
    assert len(adds) == 1
    labels = adds[0]["args"]["labels"]
    assert labels[0] == "#feeds" and "@raphael" in labels and "@next" in labels
    assert (await hub_project._owner(world, pid)).source_tag == "#feeds"


async def test_a_research_task_problem_is_a_question_raphael_owns(world):
    task_id = f"T{uuid.uuid4().hex[:10]}"
    await world.execute(
        "INSERT INTO todoist_tasks (id, project_id, content, labels, assignee_label, source_tag, "
        "is_completed, raw) VALUES ($1,'P_INBOX','Why is the sky blue?',"
        "ARRAY['#research','@raphael'],'@raphael','#research',false,'{}'::jsonb)",
        task_id,
    )
    problem = await hub_project.ensure_problem_for_task(world, task_id, source="research")
    assert problem["class"] == "question" and problem["subject_kind"] == "task"
    assert (await hub_project._owner(world, problem["id"])).source_tag == "#research"


async def test_topics_are_never_offered_as_a_group_and_stay_out_of_the_digest(world):
    for _ in range(3):
        await research_topics.track(world, _name(), ["alpha"], now=NOW)
    clusters = await candidates(world, now=NOW)
    assert not [c for c in clusters if c["class"] == "topic"]
    out = await digest(world, hours=48, now=NOW + timedelta(minutes=1))
    assert not [p for p in out["problems"] if p["class"] == "topic"]


async def test_an_ordinary_problem_still_reaches_the_digest(world):
    subject = f"svc_{uuid.uuid4().hex[:8]}"
    await ingest_event(
        world,
        Event(
            source="heartbeat",
            external_id=f"{subject}@1",
            kind="occurrence",
            title=f"Service {subject} down",
            klass="DockerServiceDown",
            subject=subject,
            occurred_at=NOW,
        ),
        now=NOW,
    )
    out = await digest(world, hours=1, now=NOW + timedelta(minutes=1))
    assert subject in {p["subject"] for p in out["problems"]}
