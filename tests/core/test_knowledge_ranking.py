"""Knowledge ranking (#579): the agent's domain boost counts, and the ranking
knobs are the `knowledge_ranking` settings row — lenient on read, strict on
write, edited at GET/PUT /api/admin/knowledge/ranking."""

from __future__ import annotations

import math
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from aegis.api.app import create_app
from aegis.api.deps import get_settings
from aegis.config import Settings
from aegis.db import run_migrations
from aegis.services import knowledge_ranking as kr
from aegis.services.chat import _apply_knowledge_decay, _gather_knowledge_context
from aegis.services.source_types import DEFAULT_DECAY_DAYS
from httpx import ASGITransport, AsyncClient


def _hit(name: str, similarity: float, source_type: str) -> dict:
    return {
        "title": name,
        "similarity": similarity,
        "source_type": source_type,
        "summary": name,
        "url": f"aegis://{name}",
        "content_id": name,
    }


# --- the boost counts --------------------------------------------------------


async def test_a_domain_doc_under_the_threshold_on_similarity_alone_gets_in():
    """0.4 similarity misses a 0.5 threshold; the agent's own domain adds 0.2
    and makes it. Before #579, decay restarted from raw similarity, so the
    boost never reached the threshold and this document was dropped."""
    kc = AsyncMock()
    kc.search.return_value = [_hit("mine", 0.4, "email"), _hit("other", 0.4, "article")]
    context, injected = await _gather_knowledge_context(
        kc, "q", knowledge_domains=["email"], score_threshold=0.5
    )
    assert context is not None
    assert [i["content_id"] for i in injected] == ["mine"]


async def test_a_domain_doc_outranks_an_equal_non_domain_doc():
    kc = AsyncMock()
    kc.search.return_value = [_hit("other", 0.7, "article"), _hit("mine", 0.7, "email")]
    _, injected = await _gather_knowledge_context(
        kc, "q", knowledge_domains=["email"], score_threshold=0.5
    )
    assert [i["content_id"] for i in injected] == ["mine", "other"]


async def test_the_injection_log_still_reports_the_boosted_score():
    kc = AsyncMock()
    kc.search.return_value = [_hit("mine", 0.7, "email")]
    _, injected = await _gather_knowledge_context(kc, "q", knowledge_domains=["email"])
    assert injected[0]["score"] == pytest.approx(0.9)


async def test_the_row_sets_the_boost_and_a_types_weight():
    """No domain boost, and articles weighted double: the article (0.3 x 2)
    passes and the agent's own email (0.4, unboosted) does not."""
    ranking = kr.Ranking.from_config(
        kr.merge({"domain_boost": 0.0, "source_types": {"article": {"rank_boost": 2.0}}})
    )
    kc = AsyncMock()
    kc.search.return_value = [_hit("mine", 0.4, "email"), _hit("other", 0.3, "article")]
    _, injected = await _gather_knowledge_context(
        kc, "q", knowledge_domains=["email"], score_threshold=0.5, ranking=ranking
    )
    assert [i["content_id"] for i in injected] == ["other"]


def test_decay_starts_from_the_boosted_score():
    items = [{"similarity": 0.4, "_score": 0.6, "source_type": "chat", "days_since_referenced": 15}]
    _apply_knowledge_decay(items)
    assert items[0]["effective_score"] == pytest.approx(0.6 * (1 - 15 / 30))


def test_decay_without_a_boost_still_starts_from_similarity():
    items = [{"similarity": 0.8, "source_type": "chat"}]
    _apply_knowledge_decay(items)
    assert items[0]["effective_score"] == pytest.approx(0.8)


# --- the effective ranking ---------------------------------------------------


def test_no_row_ranks_exactly_as_the_registry():
    r = kr.DEFAULT_RANKING
    assert r.domain_boost == 0.2
    assert r.rank_boost("note") == 1.25
    assert r.rank_boost("email") == 1.0
    assert r.rank_boost("no-such-type") == 1.0
    assert r.decay_days("chat") == 30
    assert r.decay_days("email") == DEFAULT_DECAY_DAYS
    assert kr.Ranking.from_config(kr.merge(None)) == kr.DEFAULT_RANKING


def test_a_partial_override_keeps_the_registry_for_what_it_leaves_out():
    r = kr.Ranking.from_config(
        kr.merge({"source_types": {"note": {"decay_days": 30}, "meeting": {"decay_days": None}}})
    )
    assert r.decay_days("note") == 30
    assert r.rank_boost("note") == 1.25  # not named, so the registry's
    assert r.decay_days("meeting") == DEFAULT_DECAY_DAYS  # null = the default window
    assert r.decay_days("chat") == 30  # a type the row does not name


def test_a_type_the_registry_does_not_know_can_be_named():
    r = kr.Ranking.from_config(kr.merge({"source_types": {"sentry": {"rank_boost": 0.5}}}))
    assert r.rank_boost("sentry") == 0.5


async def test_get_ranking_with_no_pool_is_the_defaults():
    kr.ROW.clear_cache()
    try:
        assert await kr.get_ranking(None) == kr.DEFAULT_RANKING
    finally:
        kr.ROW.clear_cache()


# --- lenient merge, strict validate -------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        None,
        "x",
        [],
        {"domain_boost": "big"},
        {"domain_boost": -1},
        {"domain_boost": math.nan},
        {"domain_boost": math.inf},
        {"domain_boost": True},
        {"source_types": "x"},
    ],
)
def test_merge_never_raises_and_falls_back(bad):
    assert kr.merge(bad) == {"domain_boost": 0.2, "source_types": {}}


def test_merge_drops_bad_entries_and_keeps_good_ones():
    out = kr.merge(
        {
            "source_types": {
                "Bad Name": {"rank_boost": 2},
                "email": {"rank_boost": -1, "decay_days": 30},
                "note": {"rank_boost": math.nan},
                "pdf": {"decay_days": 0},
                "article": {"rank_boost": 1.5, "decay_days": True},
                "chat": "fast",
            }
        }
    )
    assert out["source_types"] == {"email": {"decay_days": 30}, "article": {"rank_boost": 1.5}}


@pytest.mark.parametrize(
    "body, needle",
    [
        ("x", "object"),
        ({"domain_boost": -0.1}, "domain_boost"),
        ({"domain_boost": math.nan}, "domain_boost"),
        ({"domain_boost": 1.5}, "domain_boost"),
        ({"domain_boost": "0.2"}, "domain_boost"),
        ({"source_types": []}, "source_types"),
        ({"source_types": {"Bad Name": {}}}, "Bad Name"),
        ({"source_types": {"email": 3}}, "email"),
        ({"source_types": {"email": {"weight": 2}}}, "weight"),
        ({"source_types": {"email": {"rank_boost": -1}}}, "rank_boost"),
        ({"source_types": {"email": {"rank_boost": math.inf}}}, "rank_boost"),
        ({"source_types": {"email": {"rank_boost": 11}}}, "rank_boost"),
        ({"source_types": {"email": {"decay_days": 0}}}, "decay_days"),
        ({"source_types": {"email": {"decay_days": -3}}}, "decay_days"),
        ({"source_types": {"email": {"decay_days": 1.5}}}, "decay_days"),
        ({"source_types": {"email": {"decay_days": "30"}}}, "decay_days"),
    ],
)
def test_validate_refuses(body, needle):
    with pytest.raises(ValueError) as exc:
        kr.validate(body)
    assert needle in str(exc.value)


def test_validate_normalises_and_accepts_a_null_decay():
    assert kr.validate(
        {"source_types": {"email": {"decay_days": None, "rank_boost": 2}, "sentry": {}}}
    ) == {"domain_boost": 0.2, "source_types": {"email": {"rank_boost": 2.0, "decay_days": None}}}


# --- the admin route ------------------------------------------------------------

_SETTINGS = {
    "database_url": "postgresql://test:test@localhost:5432/test",
    "litellm_url": "https://litellm.example.com/v1",
    "temporal_ui_url": "https://temporal.example.com",
    "admin_username": "admin",
    "admin_password": "admin",
}
AUTH = ("admin", "admin")
URL = "/api/admin/knowledge/ranking"


@pytest_asyncio.fixture(loop_scope="function")
async def ranking_pool(db_pool):
    await run_migrations(db_pool)
    await db_pool.execute("DELETE FROM settings WHERE key = 'knowledge_ranking'")
    kr.ROW.clear_cache()
    yield db_pool
    await db_pool.execute("DELETE FROM settings WHERE key = 'knowledge_ranking'")
    await db_pool.execute("DELETE FROM audit_log WHERE action = 'knowledge_ranking_saved'")
    kr.ROW.clear_cache()


@pytest_asyncio.fixture(loop_scope="function")
async def app_client(ranking_pool):
    settings = Settings(**_SETTINGS)
    app = create_app(run_lifespan=False)
    app.state.db_pool = ranking_pool
    app.state.llm = AsyncMock()
    app.dependency_overrides[get_settings] = lambda: settings
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def test_route_requires_auth(app_client):
    assert (await app_client.get(URL)).status_code == 401


async def test_get_with_no_row_is_the_registry(app_client):
    body = (await app_client.get(URL, auth=AUTH)).json()
    assert body["domain_boost"] == 0.2
    assert body["source_types"] == {}
    assert body["defaults"] == {"domain_boost": 0.2, "decay_days": DEFAULT_DECAY_DAYS}
    assert body["registry"]["note"]["rank_boost"] == 1.25
    assert body["registry"]["chat"]["decay_days"] == 30
    assert body["registry"]["email"]["decay_days"] is None


async def test_put_persists_audits_and_the_next_turn_reads_it(app_client, ranking_pool):
    r = await app_client.put(
        URL, auth=AUTH, json={"domain_boost": 0.3, "source_types": {"email": {"decay_days": 30}}}
    )
    assert r.status_code == 200
    assert r.json()["source_types"] == {"email": {"decay_days": 30}}
    ranking = await kr.get_ranking(ranking_pool)
    assert ranking.domain_boost == 0.3
    assert ranking.decay_days("email") == 30
    assert await ranking_pool.fetchval(
        "SELECT count(*) FROM audit_log WHERE action = 'knowledge_ranking_saved'"
    ) == 1


async def test_put_400s_and_writes_nothing(app_client, ranking_pool):
    r = await app_client.put(URL, auth=AUTH, json={"source_types": {"email": {"rank_boost": -1}}})
    assert r.status_code == 400
    assert "rank_boost" in r.json()["detail"]
    assert await ranking_pool.fetchval(
        "SELECT count(*) FROM settings WHERE key = 'knowledge_ranking'"
    ) == 0


async def test_a_malformed_row_still_ranks(ranking_pool):
    await ranking_pool.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES ('knowledge_ranking', $1, NOW())",
        {"domain_boost": "lots", "source_types": {"email": {"decay_days": -5}, "pdf": {"rank_boost": 0.5}}},
    )
    kr.ROW.clear_cache()
    ranking = await kr.get_ranking(ranking_pool)
    assert ranking.domain_boost == 0.2
    assert ranking.decay_days("email") == DEFAULT_DECAY_DAYS
    assert ranking.rank_boost("pdf") == 0.5
