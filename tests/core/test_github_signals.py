"""#677 — GitHub signals: weekly rising repos as area items, and issue search."""

from __future__ import annotations

import json
import uuid
from datetime import date

import pytest
import pytest_asyncio
import respx
from aegis.connectors.github import API, GitHubClient, GitHubError
from aegis.services import github_signals as gs
from aegis.services import research_topics
from aegis.services.tools.base import ToolContext
from aegis.services.tools.research import _exec_github_issues

TODAY = date(2026, 9, 26)


def _repo(name="acme/fastq", stars=420, desc="A fast queue", **kw):
    return {
        "full_name": name, "html_url": f"https://github.com/{name}", "description": desc,
        "stargazers_count": stars, "language": "Rust", "created_at": "2026-09-10T00:00:00Z", **kw,
    }


def test_config_is_lenient_and_the_query_is_github_syntax():
    cfg = gs.RisingConfig.from_config({"topics": ["LLM", " ", "duckdb"], "min_stars": "x", "window_days": 14})
    assert (cfg.topics, cfg.min_stars, cfg.window_days) == (("llm", "duckdb"), 100, 14)
    assert gs.RisingConfig.from_config(None) == gs.RisingConfig()
    assert gs.rising_query("duckdb", cfg, TODAY) == (
        "topic:duckdb created:>=2026-09-12 stars:>=100 archived:false"
    )


def test_a_repo_becomes_an_item_keyed_on_its_url():
    item = gs.repo_item(_repo(), "duckdb")
    assert item == {
        "title": "acme/fastq: A fast queue (★420, new)",
        "url": "https://github.com/acme/fastq",
        "summary": "#duckdb · Rust · created 2026-09-10",
    }
    assert gs.repo_item({"full_name": "x/y"}, "t") is None


def test_issue_queries_narrow_by_focus():
    assert gs.issues_query("duckdb/duckdb", "bug") == "repo:duckdb/duckdb is:issue label:bug"
    assert gs.issues_query("a/b", "any", "parquet s3") == "repo:a/b is:issue parquet s3"
    assert "outage" in gs.issues_query("a/b", "production")


@pytest.mark.asyncio
async def test_the_client_sends_the_token_only_when_set_and_names_a_rate_limit():
    with respx.mock(base_url=API) as mock:
        route = mock.get("/search/repositories").respond(200, json={"items": [_repo()]})
        with_token = GitHubClient("tok")
        assert (await with_token.search_repos("q"))[0]["full_name"] == "acme/fastq"
        assert route.calls[0].request.headers["Authorization"] == "Bearer tok"
        await with_token.close()

        anon = GitHubClient()
        await anon.search_repos("q")
        assert "Authorization" not in route.calls[1].request.headers
        mock.get("/search/issues").respond(403, headers={"x-ratelimit-remaining": "0"})
        with pytest.raises(GitHubError, match="rate limited"):
            await anon.search_issues("q")
        await anon.close()


@pytest.mark.asyncio
async def test_the_tool_refuses_a_bad_repo_and_returns_ranked_issues():
    ctx = ToolContext(agent_id="raphael")
    bad = json.loads(await _exec_github_issues(None, {"repo": "duckdb"}, ctx))
    assert "owner/name" in bad["error"]
    issue = {
        "title": "OOM reading parquet", "html_url": "https://github.com/duckdb/duckdb/issues/1",
        "state": "open", "comments": 12, "reactions": {"total_count": 40},
        "labels": [{"name": "bug"}], "created_at": "2026-08-01T00:00:00Z", "body": "x" * 500,
    }
    with respx.mock(base_url=API) as mock:
        route = mock.get("/search/issues").respond(200, json={"items": [issue]})
        out = json.loads(await _exec_github_issues(
            None, {"repo": "duckdb/duckdb", "focus": "performance", "limit": 5}, ctx
        ))
    assert "repo:duckdb/duckdb is:issue (performance" in route.calls[0].request.url.params["q"]
    assert route.calls[0].request.url.params["sort"] == "reactions"
    (row,) = out["issues"]
    assert (row["reactions"], row["labels"], len(row["excerpt"])) == (40, ["bug"], 300)


# --- the weekly run, on a real database ----------------------------------------


class _Client:
    def __init__(self, hits):
        self.hits, self.queries = hits, []

    async def search_repos(self, q, per_page=10):
        self.queries.append(q)
        topic = q.split()[0].removeprefix("topic:")
        if topic == "broken":
            raise GitHubError("/search/repositories: HTTP 500")
        return self.hits.get(topic, [])


@pytest_asyncio.fixture(loop_scope="function")
async def pool(db_pool):
    yield db_pool
    await db_pool.execute("DELETE FROM settings WHERE key = $1", research_topics.TOPICS_SETTING)


@pytest.mark.asyncio
async def test_a_weekly_run_files_each_repo_once_and_survives_a_failing_topic(pool):
    topic = f"GitHub rising {uuid.uuid4().hex[:6]}"
    await research_topics.track(pool, topic, [topic])
    shared = _repo("acme/both")
    client = _Client({"llm": [_repo("acme/one"), shared], "duckdb": [shared]})
    cfg = gs.RisingConfig(topics=("llm", "broken", "duckdb"), topic=topic, per_topic=2)

    first = await gs.run_rising(pool, client, cfg, today=TODAY)
    assert (first["items"], first["attached"], first["failed_topics"]) == (2, 2, ["broken"])
    again = await gs.run_rising(pool, client, cfg, today=TODAY)
    assert again["attached"] == 0
    assert await gs.run_rising(pool, client, gs.RisingConfig(), today=TODAY) == {"skipped": "no_topics"}
