"""Tests for AlertActivities.resolve_alert_resource."""

import json
from unittest.mock import AsyncMock

from aegis_worker.activities.alerts import AlertActivities
from temporalio.testing import ActivityEnvironment

# `service` here is deliberately a value that does NOT match any
# SAMPLE_RESOURCES path/github_repo basename, so these tests exercise the
# KG + LLM tiers. The deterministic service→resource tier is covered
# separately in test_resolve_resource_service_match_deterministic.
SAMPLE_ALERT = {
    "title": "NullPointerException in UserService",
    "source": "sentry",
    "fingerprint": "fp-abc123",
    "service": "billing-service",
    "description": "NullPointerException at UserService.java:42",
}

SAMPLE_RESOURCES = [
    {
        "id": "res-001",
        "title": "aegis-core",
        "kind": "repository",
        "url": "https://github.com/org/aegis",
        "metadata": {"path": "aegis", "github_repo": "youruser/aegis", "coding_enabled": True},
    },
    {
        "id": "res-002",
        "title": "knowledge-service",
        "kind": "connector",
        "url": "http://knowledge:8000",
        "metadata": {},
    },
    {
        "id": "res-003",
        "title": "Homelab GitOps — infrastructure Ansible + Swarm config",
        "kind": "repository",
        "url": "https://github.com/example/infra-gitops",
        "metadata": {"path": "infra-gitops", "github_repo": "example/infra-gitops", "coding_enabled": True},
    },
]


async def test_resolve_resource_service_match_deterministic():
    """A `service` that exactly matches a resource path/github_repo basename
    resolves deterministically — before, and without, the unreliable gemma
    LLM tier.

    Regression: a sentry alert with service="bcp" was silently dropped by the
    2B LLM matcher, so the flow skipped kimi and fell to the LLM-only
    investigate() path (root-caused 2026-05-30 on sentry:7510268390).
    """
    mock_db = AsyncMock()
    mock_db.fetch.return_value = SAMPLE_RESOURCES

    mock_kg = AsyncMock()
    mock_kg.search.return_value = []  # no KG cache hit

    mock_llm = AsyncMock()  # must never be consulted

    alert = {**SAMPLE_ALERT, "service": "aegis"}
    act = AlertActivities(db_pool=mock_db, llm_client=mock_llm, knowledge_connector=mock_kg)
    env = ActivityEnvironment()
    result = await env.run(act.resolve_alert_resource, alert)

    assert result["source"] == "service_match"
    assert result["resource_id"] == "res-001"
    assert result["resource_path"] == "aegis"
    assert result["github_repo"] == "youruser/aegis"
    assert result["confidence"] == 1.0
    assert len(result["resources"]) == 1
    assert result["resources"][0]["resource_id"] == "res-001"
    # The deterministic tier means the flaky LLM matcher is never reached.
    mock_llm.think.assert_not_called()


async def test_resolve_resource_via_llm():
    """LLM maps a sentry issue to the correct resource when KG has no cache."""
    mock_db = AsyncMock()
    mock_db.fetch.return_value = SAMPLE_RESOURCES

    mock_kg = AsyncMock()
    mock_kg.search.return_value = []  # No KG cache hit
    mock_kg.ingest_claims = AsyncMock(return_value={"triples_created": 1})

    mock_llm = AsyncMock()
    mock_llm.think.return_value = {
        "response": json.dumps(
            {
                "resources": [
                    {"resource_id": "res-001", "resource_title": "aegis-core", "confidence": 0.85}
                ]
            }
        ),
        "model": "gemma4:e2b",
        "prompt_tokens": 150,
        "completion_tokens": 30,
    }

    act = AlertActivities(db_pool=mock_db, llm_client=mock_llm, knowledge_connector=mock_kg)
    env = ActivityEnvironment()
    result = await env.run(act.resolve_alert_resource, SAMPLE_ALERT)

    assert result["resource_id"] == "res-001"
    assert result["resource_title"] == "aegis-core"
    assert result["resource_path"] == "aegis"
    assert result["github_repo"] == "youruser/aegis"
    assert result["confidence"] >= 0.5
    assert result["source"] == "llm"
    # New: resources list
    assert len(result["resources"]) >= 1
    assert result["resources"][0]["resource_id"] == "res-001"
    assert result["resources"][0]["github_repo"] == "youruser/aegis"
    mock_llm.think.assert_called_once()


async def test_resolve_resource_kg_cache_hit():
    """KG cached mapping skips LLM entirely. The KG only proves
    alert→resource_id — path/github_repo come from the CURRENT resources
    row, never from the (possibly stale) cached KG metadata."""
    mock_db = AsyncMock()
    mock_db.fetch.return_value = SAMPLE_RESOURCES
    mock_db.fetchrow.return_value = {
        "id": "res-001",
        "title": "aegis-core",
        "metadata": {"path": "personal/aegis", "github_repo": "youruser/aegis", "coding_enabled": True},
    }

    mock_kg = AsyncMock()
    mock_kg.search.return_value = [
        {
            "content": "relates_to resource:res-001",
            "score": 0.9,
            "metadata": {
                "resource_id": "res-001",
                "resource_title": "aegis-core",
                # Stale flat path cached before the workspace restructure —
                # must NOT be returned.
                "resource_path": "aegis",
            },
        }
    ]
    mock_kg.ingest_claims = AsyncMock()

    mock_llm = AsyncMock()

    act = AlertActivities(db_pool=mock_db, llm_client=mock_llm, knowledge_connector=mock_kg)
    env = ActivityEnvironment()
    result = await env.run(act.resolve_alert_resource, SAMPLE_ALERT)

    assert result["resource_id"] == "res-001"
    assert result["source"] == "knowledge"
    assert result["resource_path"] == "personal/aegis"  # fresh, from the DB row
    assert result["github_repo"] == "youruser/aegis"
    assert len(result["resources"]) >= 1
    assert result["resources"][0]["resource_id"] == "res-001"
    # LLM should NOT be called when KG has a confident hit
    mock_llm.think.assert_not_called()


async def test_resolve_resource_kg_hit_on_pruned_resource_falls_through():
    """A KG hit pointing at a resource that no longer exists (pruned by
    WorkspaceRepoSyncFlow) must fall through to the live tiers."""
    mock_db = AsyncMock()
    mock_db.fetch.return_value = SAMPLE_RESOURCES
    mock_db.fetchrow.return_value = None  # row deleted

    mock_kg = AsyncMock()
    mock_kg.search.return_value = [
        {
            "content": "relates_to resource:res-gone",
            "score": 0.95,
            "metadata": {"resource_id": "res-gone", "resource_path": "gone-repo"},
        }
    ]
    mock_kg.ingest_claims = AsyncMock()

    mock_llm = AsyncMock()
    mock_llm.think.return_value = {
        "response": json.dumps({"resources": []}),
        "model": "gemma4:e2b",
        "prompt_tokens": 1,
        "completion_tokens": 1,
    }

    act = AlertActivities(db_pool=mock_db, llm_client=mock_llm, knowledge_connector=mock_kg)
    env = ActivityEnvironment()
    result = await env.run(act.resolve_alert_resource, SAMPLE_ALERT)

    assert result["source"] != "knowledge"
    mock_llm.think.assert_called_once()


async def test_resolve_resource_no_match():
    """LLM can't match alert to any resource — returns null result."""
    mock_db = AsyncMock()
    mock_db.fetch.return_value = SAMPLE_RESOURCES

    mock_kg = AsyncMock()
    mock_kg.search.return_value = []

    mock_llm = AsyncMock()
    mock_llm.think.return_value = {
        "response": json.dumps({"resources": []}),
        "model": "gemma4:e2b",
        "prompt_tokens": 100,
        "completion_tokens": 20,
    }

    act = AlertActivities(db_pool=mock_db, llm_client=mock_llm, knowledge_connector=mock_kg)
    env = ActivityEnvironment()
    result = await env.run(act.resolve_alert_resource, SAMPLE_ALERT)

    assert result["resource_id"] is None
    assert result["resource_title"] is None


async def test_resolve_resource_no_db_pool():
    """Without db_pool, returns null result immediately."""
    act = AlertActivities(db_pool=None, llm_client=AsyncMock(), knowledge_connector=AsyncMock())
    env = ActivityEnvironment()
    result = await env.run(act.resolve_alert_resource, SAMPLE_ALERT)

    assert result["resource_id"] is None


async def test_resolve_resource_no_llm():
    """Without LLM client, returns null result after KG miss."""
    mock_db = AsyncMock()
    mock_db.fetch.return_value = SAMPLE_RESOURCES

    mock_kg = AsyncMock()
    mock_kg.search.return_value = []

    act = AlertActivities(db_pool=mock_db, llm_client=None, knowledge_connector=mock_kg)
    env = ActivityEnvironment()
    result = await env.run(act.resolve_alert_resource, SAMPLE_ALERT)

    assert result["resource_id"] is None


async def test_resolve_resource_kg_low_confidence_falls_through():
    """KG match below 0.7 threshold falls through to LLM."""
    mock_db = AsyncMock()
    mock_db.fetch.return_value = SAMPLE_RESOURCES

    mock_kg = AsyncMock()
    mock_kg.search.return_value = [
        {
            "content": "relates_to resource:res-001",
            "score": 0.5,  # Below threshold
            "metadata": {"resource_id": "res-001"},
        }
    ]
    mock_kg.ingest_claims = AsyncMock()

    mock_llm = AsyncMock()
    mock_llm.think.return_value = {
        "response": json.dumps(
            {
                "resources": [
                    {
                        "resource_id": "res-002",
                        "resource_title": "knowledge-service",
                        "confidence": 0.8,
                    }
                ]
            }
        ),
        "model": "gemma4:e2b",
        "prompt_tokens": 100,
        "completion_tokens": 30,
    }

    act = AlertActivities(db_pool=mock_db, llm_client=mock_llm, knowledge_connector=mock_kg)
    env = ActivityEnvironment()
    result = await env.run(act.resolve_alert_resource, SAMPLE_ALERT)

    mock_llm.think.assert_called_once()
    assert result["source"] == "llm"


async def test_resolve_resource_connector_expands_to_homelab():
    """When a connector resource is matched, infra-gitops is added via expansion rule."""
    mock_db = AsyncMock()
    mock_db.fetch.return_value = SAMPLE_RESOURCES  # includes res-003 = infra-gitops

    mock_kg = AsyncMock()
    mock_kg.search.return_value = []
    mock_kg.ingest_claims = AsyncMock(return_value={"triples_created": 1})

    mock_llm = AsyncMock()
    # LLM returns the connector (knowledge-service) as the only match
    mock_llm.think.return_value = {
        "response": json.dumps(
            {
                "resources": [
                    {
                        "resource_id": "res-002",
                        "resource_title": "knowledge-service",
                        "confidence": 0.8,
                    }
                ]
            }
        ),
        "model": "gemma4:e2b",
        "prompt_tokens": 100,
        "completion_tokens": 20,
    }

    act = AlertActivities(db_pool=mock_db, llm_client=mock_llm, knowledge_connector=mock_kg)
    env = ActivityEnvironment()
    result = await env.run(act.resolve_alert_resource, SAMPLE_ALERT)

    # Primary resource is the connector
    assert result["resource_id"] == "res-002"
    # Expansion added infra-gitops as second resource
    resource_ids = [r["resource_id"] for r in result["resources"]]
    assert "res-003" in resource_ids, f"infra-gitops not expanded in: {resource_ids}"
    homelab = next(r for r in result["resources"] if r["resource_id"] == "res-003")
    assert homelab["resource_path"] == "infra-gitops"
    assert homelab["github_repo"] == "example/infra-gitops"


async def test_resolve_resource_subthreshold_returns_unconfirmed_candidates():
    """Jira-class regression: `service` is the generic org ("acme"), so no
    repo service-matches and the LLM only returns sub-0.5 picks. Previously this
    returned null → the flow's `resources_list` was empty → Gate-0 skipped →
    kimi_attempted=False → silent degrade to the LLM-only investigate(). Now the
    resolver surfaces the top sub-threshold picks as source="llm_unconfirmed" so
    the flow runs Gate-0 (content-confirm or ask the user) and kimi can run.
    """
    mock_db = AsyncMock()
    mock_db.fetch.return_value = SAMPLE_RESOURCES
    mock_kg = AsyncMock()
    mock_kg.search.return_value = []
    mock_kg.ingest_claims = AsyncMock()
    mock_llm = AsyncMock()
    mock_llm.think.return_value = {
        "response": json.dumps(
            {
                "resources": [
                    {
                        "resource_id": "res-003",
                        "resource_title": "Homelab GitOps",
                        "confidence": 0.2,
                    },
                    {"resource_id": "res-001", "resource_title": "aegis-core", "confidence": 0.4},
                ]
            }
        ),
        "model": "gpt-oss:20b",
        "prompt_tokens": 100,
        "completion_tokens": 30,
    }

    act = AlertActivities(
        db_pool=mock_db,
        llm_client=mock_llm,
        knowledge_connector=mock_kg,
        model_balanced="gpt-oss:20b",
    )
    env = ActivityEnvironment()
    alert = {**SAMPLE_ALERT, "service": "acme", "source": "todoist-jira"}
    result = await env.run(act.resolve_alert_resource, alert)

    assert result["source"] == "llm_unconfirmed"
    # Highest sub-threshold pick is primary.
    assert result["resource_id"] == "res-001"
    ids = {r["resource_id"] for r in result["resources"]}
    assert {"res-001", "res-003"} <= ids
    # An unconfirmed guess must NOT be cached as a confident KG resolution.
    mock_kg.ingest_claims.assert_not_called()


async def test_resolve_resource_uses_balanced_model_not_gemma():
    """The Tier-2 resolver must run on the capable balanced model (gpt-oss),
    not model_light (gemma4:e2b) which whiffed 1-of-N repo picks and stranded
    every Jira investigation on the LLM-only fallback."""
    mock_db = AsyncMock()
    mock_db.fetch.return_value = SAMPLE_RESOURCES
    mock_kg = AsyncMock()
    mock_kg.search.return_value = []
    mock_kg.ingest_claims = AsyncMock()
    mock_llm = AsyncMock()
    mock_llm.think.return_value = {
        "response": json.dumps(
            {
                "resources": [
                    {"resource_id": "res-001", "resource_title": "aegis-core", "confidence": 0.85}
                ]
            }
        ),
        "model": "gpt-oss:20b",
        "prompt_tokens": 1,
        "completion_tokens": 1,
    }

    act = AlertActivities(
        db_pool=mock_db,
        llm_client=mock_llm,
        knowledge_connector=mock_kg,
        model_balanced="gpt-oss:20b",
    )
    env = ActivityEnvironment()
    await env.run(act.resolve_alert_resource, SAMPLE_ALERT)

    assert mock_llm.think.call_args.kwargs["model"] == "gpt-oss:20b"


async def test_investigate_fallback_uses_balanced_model_not_gemma():
    """The LLM-only fallback investigate() must run on the balanced model
    (gpt-oss), not model_light (gemma4:e2b) — gemma emitted confident-sounding
    but ungrounded speculation that looked like a real verdict."""
    mock_db = AsyncMock()
    mock_llm = AsyncMock()
    mock_llm.think.return_value = {
        "response": "1. root cause\n2. yes\n3. fix\n4. yes",
        "model": "gpt-oss:20b",
        "prompt_tokens": 1,
        "completion_tokens": 1,
    }
    act = AlertActivities(
        db_pool=mock_db,
        llm_client=mock_llm,
        knowledge_connector=AsyncMock(),
        model_balanced="gpt-oss:20b",
    )
    env = ActivityEnvironment()
    await env.run(act.investigate, {"title": "x", "source": "todoist-jira"})

    assert mock_llm.think.call_args.kwargs["model"] == "gpt-oss:20b"


async def test_kg_hit_on_disabled_resource_falls_through():
    """A cached alert→resource link whose resource is NOT coding_enabled must
    be ignored (allow-list) — it falls through to the live tiers instead of
    firing a coding run on a disabled repo."""
    mock_db = AsyncMock()
    mock_db.fetch.return_value = SAMPLE_RESOURCES
    mock_db.fetchrow.return_value = {
        "id": "res-001",
        "title": "aegis-core",
        "metadata": {"path": "aegis", "github_repo": "youruser/aegis"},  # no coding_enabled
    }
    mock_kg = AsyncMock()
    mock_kg.search.return_value = [
        {"content": "relates_to resource:res-001", "score": 0.95,
         "metadata": {"resource_id": "res-001"}},
    ]
    mock_kg.ingest_claims = AsyncMock()
    mock_llm = AsyncMock()
    mock_llm.think.return_value = {
        "response": json.dumps({"resources": []}),
        "model": "gpt-oss:20b", "prompt_tokens": 1, "completion_tokens": 1,
    }
    act = AlertActivities(db_pool=mock_db, llm_client=mock_llm, knowledge_connector=mock_kg,
                          model_balanced="gpt-oss:20b")
    result = await ActivityEnvironment().run(act.resolve_alert_resource, SAMPLE_ALERT)
    assert result["source"] != "knowledge"  # disabled repo not served from cache
    mock_llm.think.assert_called_once()


async def test_sentry_project_deterministic_match_carries_routing():
    """A resource pinning metadata.sentry_project matches a Sentry issue's
    project slug deterministically (before the LLM), and the match carries the
    resource-scoped engine + claude_account routing."""
    rows = [
        {"id": "res-x", "title": "My App", "kind": "repository",
         "url": "https://github.com/acme/app",
         "metadata": {"path": "acme/app", "github_repo": "acme/app",
                      "coding_enabled": True, "sentry_project": "my-app",
                      "engine": "claude", "claude_account": "work"}},
    ]
    mock_db = AsyncMock()
    mock_db.fetch.return_value = rows
    mock_kg = AsyncMock()
    mock_kg.search.return_value = []
    mock_llm = AsyncMock()  # must not be consulted
    alert = {"title": "err", "source": "sentry", "fingerprint": "fp-s", "service": "my-app"}
    act = AlertActivities(db_pool=mock_db, llm_client=mock_llm, knowledge_connector=mock_kg)
    result = await ActivityEnvironment().run(act.resolve_alert_resource, alert)
    assert result["source"] == "sentry_project"
    assert result["resource_id"] == "res-x"
    assert result["resources"][0]["engine"] == "claude"
    assert result["resources"][0]["claude_account"] == "work"
    mock_llm.think.assert_not_called()


async def test_allow_list_excludes_non_coding_enabled_repos(db_pool):
    """End-to-end SQL: only kind='repository' AND coding_enabled='true' resources
    are candidates. A service that matches a NON-enabled repo resolves to none."""
    await db_pool.execute("DELETE FROM resources WHERE slug LIKE 'test-al-%'")
    await db_pool.execute(
        "INSERT INTO resources (kind, slug, title, url, tags, metadata) VALUES "
        "('repository','test-al-on','On repo','', ARRAY[]::text[], $1::jsonb),"
        "('repository','test-al-off','Off repo','', ARRAY[]::text[], $2::jsonb)",
        # Pass dicts, not json.dumps strings — the pool's jsonb codec already
        # applies json.dumps, so pre-stringifying double-encodes and stores a
        # JSONB string primitive that never matches metadata->>'coding_enabled'.
        {"path": "onrepo", "github_repo": "o/onrepo", "coding_enabled": True},
        {"path": "offrepo", "github_repo": "o/offrepo", "coding_enabled": False},
    )
    mock_kg = AsyncMock()
    mock_kg.search.return_value = []
    mock_llm = AsyncMock()
    mock_llm.think.return_value = {
        "response": json.dumps({"resources": []}),
        "model": "m", "prompt_tokens": 0, "completion_tokens": 0,
    }
    act = AlertActivities(db_pool=db_pool, llm_client=mock_llm, knowledge_connector=mock_kg,
                          model_balanced="m")
    env = ActivityEnvironment()
    try:
        # service matches the DISABLED repo basename → excluded → no resolution
        off = await env.run(act.resolve_alert_resource,
                            {"title": "x", "source": "sentry", "fingerprint": "f1",
                             "service": "offrepo", "description": ""})
        assert off["resource_id"] is None
        # service matches the ENABLED repo → deterministic service_match
        on = await env.run(act.resolve_alert_resource,
                           {"title": "x", "source": "sentry", "fingerprint": "f2",
                            "service": "onrepo", "description": ""})
        assert on["source"] == "service_match" and on["github_repo"] == "o/onrepo"
    finally:
        await db_pool.execute("DELETE FROM resources WHERE slug LIKE 'test-al-%'")


async def test_resolve_resource_llm_returns_multiple():
    """LLM returns 2 resources — both appear in result['resources'], primary is first."""
    mock_db = AsyncMock()
    mock_db.fetch.return_value = SAMPLE_RESOURCES

    mock_kg = AsyncMock()
    mock_kg.search.return_value = []
    mock_kg.ingest_claims = AsyncMock(return_value={"triples_created": 2})

    mock_llm = AsyncMock()
    mock_llm.think.return_value = {
        "response": json.dumps(
            {
                "resources": [
                    {"resource_id": "res-001", "resource_title": "aegis-core", "confidence": 0.9},
                    {
                        "resource_id": "res-003",
                        "resource_title": "Homelab GitOps",
                        "confidence": 0.7,
                    },
                ]
            }
        ),
        "model": "gemma4:e2b",
        "prompt_tokens": 150,
        "completion_tokens": 40,
    }

    act = AlertActivities(db_pool=mock_db, llm_client=mock_llm, knowledge_connector=mock_kg)
    env = ActivityEnvironment()
    result = await env.run(act.resolve_alert_resource, SAMPLE_ALERT)

    assert result["resource_id"] == "res-001"  # primary
    assert len(result["resources"]) == 2
    assert result["resources"][0]["resource_id"] == "res-001"
    assert result["resources"][1]["resource_id"] == "res-003"
    # No connector in list → no infra-gitops expansion (already present)
    assert len([r for r in result["resources"] if r["resource_id"] == "res-003"]) == 1
