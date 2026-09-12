"""ResearchActivities fixes from the programme's validation (#509, #511, #513):
the retrieval log the feed stats read, an honest `saved`, and the problem
behind a topic task."""

from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aegis.services import notes, research_topics
from aegis_worker.activities.research import ResearchActivities
from temporalio.testing import ActivityEnvironment


async def test_gather_logs_what_it_retrieved_so_feed_use_counts_research(db_pool):
    cid = f"c-{uuid.uuid4().hex[:10]}"
    kc = AsyncMock()
    kc.search = AsyncMock(
        return_value=[{"content_id": cid, "title": "T", "url": "https://x.example/a", "summary": "s"}]
    )
    sc = AsyncMock()
    sc.search = AsyncMock(return_value=[])
    act = ResearchActivities(knowledge_connector=kc, search_connector=sc, db_pool=db_pool)
    try:
        await ActivityEnvironment().run(
            act.research_gather, {"question": "what do the feeds say", "depth": "quick"}
        )
        rows = await db_pool.fetch(
            "SELECT source, agent_id, content_ids FROM knowledge_injection_log "
            "WHERE $1 = ANY(content_ids)",
            cid,
        )
    finally:
        await db_pool.execute("DELETE FROM knowledge_injection_log WHERE $1 = ANY(content_ids)", cid)
    assert len(rows) == 1
    assert rows[0]["source"] == "research"
    assert rows[0]["agent_id"] == "raphael"
    assert list(rows[0]["content_ids"]) == [cid], "the same document is logged once per run"


async def test_a_vault_failure_of_any_kind_does_not_unsay_a_saved_answer(monkeypatch):
    kc = AsyncMock()
    kc.ingest_content = AsyncMock(return_value={"status": "ok"})
    monkeypatch.setattr(
        notes,
        "config_from_settings",
        lambda _s: notes.NotesConfig(
            path=Path("/nonexistent/vault"), repo_url="git@example:v.git", deploy_key=Path("/k")
        ),
    )

    async def disk_full(*_a, **_kw):
        raise RuntimeError("disk full")

    monkeypatch.setattr(notes, "write", disk_full)
    act = ResearchActivities(knowledge_connector=kc, settings=SimpleNamespace())
    out = await ActivityEnvironment().run(act.research_save, "a question?", "an answer", [])
    assert out["saved"] is True
    assert out["vault"]["status"] == "error"


async def test_the_problem_behind_a_topic_task_says_it_is_a_topic(db_pool):
    await db_pool.execute("DELETE FROM settings WHERE key = $1", research_topics.TOPICS_SETTING)
    name = f"Topic {uuid.uuid4().hex[:6]}"
    task_id = f"tt-{uuid.uuid4().hex[:8]}"
    try:
        pid = (await research_topics.track(db_pool, name, ["alpha"]))["problem_id"]
        await research_topics.attach_items(
            db_pool,
            [{"title": "alpha news today", "url": f"https://n.example/{uuid.uuid4().hex[:6]}"}],
            origin="test",
            project=False,
        )
        await db_pool.execute(
            "INSERT INTO todoist_tasks (id, content, labels, source_tag, assignee_label, is_completed) "
            "VALUES ($1, $2, ARRAY['#research'], '#research', '@raphael', false)",
            task_id,
            f"{name}: new items worth a look",
        )
        await db_pool.execute(
            "UPDATE problems SET todoist_task_id = $2 WHERE id = $1::uuid", pid, task_id
        )
        out = await ActivityEnvironment().run(
            ResearchActivities(db_pool=db_pool).research_task_problem, task_id
        )
    finally:
        await db_pool.execute("DELETE FROM settings WHERE key = $1", research_topics.TOPICS_SETTING)
        await db_pool.execute("DELETE FROM todoist_tasks WHERE id = $1", task_id)
    assert out["problem_id"] == pid
    assert out["class"] == "topic" and out["topic"] == name
    assert [i["title"] for i in out["items"]] == ["alpha news today"]
