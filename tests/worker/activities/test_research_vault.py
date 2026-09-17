"""The research lane and the vault (#514): an answer is also kept as a note,
and the gather step puts the user's own notes first."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from aegis_worker.activities.research import ResearchActivities
from temporalio.testing import ActivityEnvironment

from tests.notes_vault import make_vault, needs_git, remote_file


@pytest.fixture
def vault(tmp_path):
    return make_vault(tmp_path)


def _kc():
    kc = AsyncMock()
    kc.ingest_content = AsyncMock(return_value={"status": "ok"})

    async def search(query, limit=5, source_type=None, **_):
        if source_type == "note":
            return [{"title": "rag", "url": "vault://raphael/topics/rag.md", "content": "My RAG note."}]
        if source_type == "book":
            return []
        return [
            {"title": "Other", "url": "aegis://x", "summary": "stored"},
            {"title": "rag", "url": "vault://raphael/topics/rag.md", "summary": "dup"},
        ]

    kc.search = AsyncMock(side_effect=search)
    return kc


@needs_git
async def test_an_answer_is_also_kept_in_the_vault_once(vault):
    acts = ResearchActivities(knowledge_connector=_kc(), settings=vault["settings"])
    sources = [{"n": 1, "kind": "web", "title": "A", "url": "https://a.example/1"}]
    first = await ActivityEnvironment().run(acts.research_save, "What is RAG?", "RAG [1].", sources)
    second = await ActivityEnvironment().run(acts.research_save, "what is rag", "RAG [1].", sources)
    assert first["saved"] is True and first["vault"]["status"] == "written"
    path = first["vault"]["path"]
    assert path.startswith("raphael/questions/what-is-rag-")
    assert second["vault"] == {"status": "exists", "path": path}
    text = remote_file(vault, path)
    assert text.startswith("# What is RAG?\n") and text.count("RAG [1].") == 1


@needs_git
async def test_an_answer_follows_the_vault_layout_and_is_signed_by_its_agent(vault, db_pool):
    """#567: a non-default `questions_dir` and `date_heading_format` reach
    research answers, and the commit is the owning agent's, under its name."""
    from aegis.services import vault_layout as vl

    from tests.notes_vault import git

    await db_pool.execute("DELETE FROM settings WHERE key = $1", vl.SETTINGS_KEY)
    vl.invalidate_cache()
    try:
        await vl.save_layout(
            db_pool, {"questions_dir": "raphael/answers", "date_heading_format": "DD/MM/YYYY"}
        )
        acts = ResearchActivities(
            knowledge_connector=_kc(), settings=vault["settings"], db_pool=db_pool,
            agent_id="raphael",
        )
        out = await ActivityEnvironment().run(
            acts.research_save, "What is RAG?", "RAG [1].", [{"n": 1, "kind": "web", "title": "A"}]
        )
    finally:
        await db_pool.execute("DELETE FROM settings WHERE key = $1", vl.SETTINGS_KEY)
        vl.invalidate_cache()
    path = out["vault"]["path"]
    assert path.startswith("raphael/answers/what-is-rag-")
    text = remote_file(vault, path)
    assert "\n## " in text and "/" in text.split("\n## ", 1)[1].split("\n", 1)[0], "a DD/MM/YYYY heading"
    name = await db_pool.fetchval("SELECT name FROM agents WHERE id = 'raphael'")
    log = git("log", "-1", "--format=%an <%ae>%n%s", "main", cwd=vault["remote"])
    assert log == f"{name} <raphael@aegis.local>\nraphael: research answer\n"


async def test_no_vault_means_the_save_is_unchanged():
    out = await ActivityEnvironment().run(
        ResearchActivities(knowledge_connector=_kc()).research_save, "q", "a", []
    )
    assert out == {"saved": True}


@needs_git
async def test_gather_puts_the_users_notes_first(vault, monkeypatch):
    from aegis.services import research as rs

    monkeypatch.setattr(rs, "looks_academic", lambda *a, **k: False)
    acts = ResearchActivities(knowledge_connector=_kc(), settings=vault["settings"])
    out = await ActivityEnvironment().run(acts.research_gather, {"question": "rag"})
    assert out["kg"][0] == {
        "title": "rag", "url": "vault://raphael/topics/rag.md", "summary": "My RAG note.",
    }
    assert [k["url"] for k in out["kg"]].count("vault://raphael/topics/rag.md") == 1


async def test_gather_skips_the_notes_search_without_a_vault(monkeypatch):
    from aegis.services import research as rs

    monkeypatch.setattr(rs, "looks_academic", lambda *a, **k: False)
    kc = _kc()
    await ActivityEnvironment().run(ResearchActivities(knowledge_connector=kc).research_gather,
                                    {"question": "rag"})
    assert all(c.kwargs.get("source_type") != "note" for c in kc.search.await_args_list)
