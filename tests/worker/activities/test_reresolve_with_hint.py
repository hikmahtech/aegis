import pytest_asyncio
from aegis_worker.activities.alerts import AlertActivities
from temporalio.testing import ActivityEnvironment


@pytest_asyncio.fixture(loop_scope="function")
async def _repos(db_pool):
    await db_pool.execute("DELETE FROM resources WHERE id IN ('11111111-1111-1111-1111-111111111111')")
    await db_pool.execute(
        "INSERT INTO resources (id, slug, title, kind, metadata) VALUES "
        "('11111111-1111-1111-1111-111111111111', 'repo-aegis-test', 'aegis', 'repository', "
        "'{\"path\": \"aegis\", \"github_repo\": \"example/aegis\"}'::jsonb)"
    )
    yield
    await db_pool.execute("DELETE FROM resources WHERE id = '11111111-1111-1111-1111-111111111111'")


async def test_hint_injects_unconfigured_repo(db_pool, _repos):
    acts = AlertActivities(db_pool=db_pool)
    env = ActivityEnvironment()
    out = await env.run(acts.reresolve_with_hint, {"title": "x", "description": ""},
                        "acme/brand-new-repo")
    assert out["confident"] is False
    gh = [c["github_repo"] for c in out["candidates"]]
    assert "acme/brand-new-repo" in gh   # surfaced despite not being configured
    synth = next(c for c in out["candidates"] if c["github_repo"] == "acme/brand-new-repo")
    assert "from hint" in synth["label"]


async def test_keyword_hint_reranks_configured(db_pool, _repos):
    acts = AlertActivities(db_pool=db_pool)
    env = ActivityEnvironment()
    out = await env.run(acts.reresolve_with_hint, {"title": "crash", "description": ""}, "aegis worker")
    assert out["confident"] is False
    assert any(c["github_repo"] == "example/aegis" for c in out["candidates"])
