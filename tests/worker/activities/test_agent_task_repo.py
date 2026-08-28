"""resolve_task_repo — Todoist project name is the strongest repo signal."""

from __future__ import annotations

import pytest_asyncio
from aegis_worker.activities.agent_task import (
    _TIER2_CONFIDENCE_THRESHOLD,
    AgentTaskActivities,
)


@pytest_asyncio.fixture(loop_scope="function")
async def _seed(db_pool):
    await db_pool.execute("DELETE FROM todoist_projects WHERE id LIKE 'pr-%'")
    await db_pool.execute("DELETE FROM resources WHERE slug LIKE 'test-repo-%'")
    await db_pool.execute(
        "INSERT INTO todoist_projects (id, name, is_managed, is_archived, order_idx) "
        "VALUES ('pr-bcp','BCP',false,false,1), ('pr-unknown','Nowhere',false,false,2)"
    )
    # `path` must be present AND nested, so a wrong JSONB key is catchable.
    await db_pool.execute(
        "INSERT INTO resources (slug, kind, title, metadata) VALUES "
        "('test-repo-bcp','repository','Stockopedia/bcp',"
        " '{\"github_repo\": \"Stockopedia/bcp\", \"path\": \"stockopedia/bcp\"}'::jsonb)"
    )
    # Tier 1's mapping is DB config, not a Python constant (issue #345), so the
    # fixture has to seed it — which also means these tests now exercise the
    # real settings read rather than a hardcoded dict.
    await db_pool.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES ($1, $2, NOW()) "
        "ON CONFLICT (key) DO UPDATE SET value = $2",
        "project_repo_map",
        {"bcp": "Stockopedia/bcp"},
    )
    yield
    await db_pool.execute("DELETE FROM todoist_projects WHERE id LIKE 'pr-%'")
    await db_pool.execute("DELETE FROM resources WHERE slug LIKE 'test-repo-%'")
    await db_pool.execute("DELETE FROM settings WHERE key = 'project_repo_map'")


async def test_project_name_resolves_to_repo(db_pool, _seed):
    act = AgentTaskActivities(db_pool=db_pool)
    result = await act.resolve_task_repo(
        {"id": "x", "content": "Fix the exporter", "project_id": "pr-bcp"}
    )
    assert result["github_repo"] == "Stockopedia/bcp"
    assert result["source"] == "project_map"
    # Load-bearing: proves the JSONB key is right. Without this the wrong key
    # ships green, flattening every nested checkout.
    assert result["repo_path"] == "stockopedia/bcp"


async def test_unmapped_project_returns_no_repo_never_a_guess(db_pool, _seed):
    act = AgentTaskActivities(db_pool=db_pool)
    result = await act.resolve_task_repo(
        {"id": "x", "content": "Fix something", "project_id": "pr-unknown"}
    )
    assert result["github_repo"] == ""
    assert result["source"] == "none"


async def test_missing_project_id_returns_no_repo(db_pool, _seed):
    act = AgentTaskActivities(db_pool=db_pool)
    result = await act.resolve_task_repo({"id": "x", "content": "Fix", "project_id": None})
    assert result["github_repo"] == ""


# --- Issue #158: tier 2 (title/description match) and tier 3 (candidates for
# the flow's Gate-0 confirm card) -----------------------------------------
#
# `alert_act` is a plain late-wired field (mirrors gmail_activities); a fake
# with a controllable resolve_alert_resource stands in for the real
# AlertActivities instance so these tests exercise resolve_task_repo's OWN
# integration logic (synthetic-alert shape, threshold, candidate mapping),
# not resolve_alert_resource's internals — those are already covered by
# tests/worker/test_alert_resource_resolution.py.


class _FakeAlertAct:
    def __init__(self, result: dict):
        self._result = result
        self.calls: list[dict] = []

    async def resolve_alert_resource(self, alert: dict) -> dict:
        self.calls.append(alert)
        return self._result


class _RaisingAlertAct:
    async def resolve_alert_resource(self, alert: dict) -> dict:
        raise RuntimeError("boom")


async def test_tier2_confident_match_resolves_repo_and_synthesizes_alert_shape(db_pool):
    """A confident tier-2 pick resolves the task exactly like tier 1, and the
    synthetic alert passed to resolve_alert_resource has the shape the issue
    specifies: title=content, description=description, fingerprint=task:<id>,
    service='' (so the deterministic service/sentry_project tiers correctly
    sit out inside resolve_alert_resource)."""
    fake = _FakeAlertAct(
        {
            "github_repo": "hikmahtech/em-credibility-monitor",
            "resource_path": "hikmah/quantamental-data-platform",
            "confidence": 0.92,
            "source": "llm",
            "resources": [
                {
                    "resource_title": "em-credibility-monitor",
                    "github_repo": "hikmahtech/em-credibility-monitor",
                    "resource_path": "hikmah/quantamental-data-platform",
                    "confidence": 0.92,
                }
            ],
        }
    )
    act = AgentTaskActivities(db_pool=db_pool, alert_act=fake)
    result = await act.resolve_task_repo(
        {
            "id": "t-tier2-1",
            "content": "Fix the credibility job",
            "description": "duplicate rows",
            "project_id": None,
        }
    )
    assert result["github_repo"] == "hikmahtech/em-credibility-monitor"
    assert result["repo_path"] == "hikmah/quantamental-data-platform"
    assert result["source"] == "title_match"
    assert result["candidates"] == []
    assert fake.calls == [
        {
            "title": "Fix the credibility job",
            "description": "duplicate rows",
            "fingerprint": "task:t-tier2-1",
            "service": "",
        }
    ]


async def test_tier2_low_confidence_surfaces_candidates_not_a_guess(db_pool):
    """Below the confidence bar: the repo stays unresolved (never guessed) but
    the resources are surfaced as `candidates` in the shape
    _build_repo_confirm_prompt expects, for the flow's tier-3 Gate-0 card."""
    fake = _FakeAlertAct(
        {
            "github_repo": "",
            "confidence": 0.3,
            "source": "llm_unconfirmed",
            "resources": [
                {
                    "resource_title": "aegis-core",
                    "github_repo": "hikmahtech/aegis",
                    "resource_path": "aegis",
                    "confidence": 0.3,
                },
                {
                    "resource_title": "bcp",
                    "github_repo": "Stockopedia/bcp",
                    "resource_path": "stockopedia/bcp",
                    "confidence": 0.2,
                },
            ],
        }
    )
    act = AgentTaskActivities(db_pool=db_pool, alert_act=fake)
    result = await act.resolve_task_repo(
        {"id": "t-tier2-2", "content": "something ambiguous", "project_id": None}
    )
    assert result["github_repo"] == ""
    assert result["source"] == "none"
    assert result["candidates"] == [
        {
            "resource_title": "aegis-core",
            "github_repo": "hikmahtech/aegis",
            "resource_path": "aegis",
            "score": 0.3,
        },
        {
            "resource_title": "bcp",
            "github_repo": "Stockopedia/bcp",
            "resource_path": "stockopedia/bcp",
            "score": 0.2,
        },
    ]


async def test_tier2_confidence_just_below_threshold_is_unconfirmed(db_pool):
    fake = _FakeAlertAct(
        {
            "github_repo": "hikmahtech/aegis",
            "resource_path": "aegis",
            "confidence": _TIER2_CONFIDENCE_THRESHOLD - 0.01,
            "source": "llm",
            "resources": [
                {
                    "resource_title": "aegis-core",
                    "github_repo": "hikmahtech/aegis",
                    "resource_path": "aegis",
                    "confidence": _TIER2_CONFIDENCE_THRESHOLD - 0.01,
                }
            ],
        }
    )
    act = AgentTaskActivities(db_pool=db_pool, alert_act=fake)
    result = await act.resolve_task_repo(
        {"id": "t-tier2-3", "content": "x", "project_id": None}
    )
    assert result["github_repo"] == ""
    assert len(result["candidates"]) == 1


async def test_tier2_confidence_exactly_at_threshold_resolves(db_pool):
    fake = _FakeAlertAct(
        {
            "github_repo": "hikmahtech/aegis",
            "resource_path": "aegis",
            "confidence": _TIER2_CONFIDENCE_THRESHOLD,
            "source": "llm",
            "resources": [
                {
                    "resource_title": "aegis-core",
                    "github_repo": "hikmahtech/aegis",
                    "resource_path": "aegis",
                    "confidence": _TIER2_CONFIDENCE_THRESHOLD,
                }
            ],
        }
    )
    act = AgentTaskActivities(db_pool=db_pool, alert_act=fake)
    result = await act.resolve_task_repo(
        {"id": "t-tier2-4", "content": "x", "project_id": None}
    )
    assert result["github_repo"] == "hikmahtech/aegis"
    assert result["source"] == "title_match"


async def test_tier2_no_match_returns_empty_like_tier1_miss(db_pool):
    """resolve_alert_resource's own null_result (no candidates at all) must
    still hard-park, exactly like an unmapped project with no title signal."""
    fake = _FakeAlertAct(
        {
            "resource_id": None,
            "resource_title": None,
            "resource_path": None,
            "github_repo": "",
            "confidence": 0.0,
            "source": "none",
            "resources": [],
        }
    )
    act = AgentTaskActivities(db_pool=db_pool, alert_act=fake)
    result = await act.resolve_task_repo(
        {"id": "t-tier2-5", "content": "nothing matches", "project_id": None}
    )
    assert result["github_repo"] == ""
    assert result["source"] == "none"
    assert result["candidates"] == []


async def test_tier2_skipped_without_alert_act_wired(db_pool):
    """alert_act=None (the dataclass default) — tier 2/3 are a no-op and
    resolve_task_repo behaves exactly as tier-1-only."""
    act = AgentTaskActivities(db_pool=db_pool)
    result = await act.resolve_task_repo(
        {"id": "t-tier2-6", "content": "Fix the credibility job", "project_id": None}
    )
    assert result["github_repo"] == ""
    assert result["source"] == "none"
    assert result["candidates"] == []


async def test_tier2_exception_is_caught_and_never_guesses(db_pool):
    act = AgentTaskActivities(db_pool=db_pool, alert_act=_RaisingAlertAct())
    result = await act.resolve_task_repo(
        {"id": "t-tier2-7", "content": "x", "project_id": None}
    )
    assert result["github_repo"] == ""
    assert result["source"] == "none"
    assert result["candidates"] == []


async def test_tier1_hit_skips_tier2_entirely(db_pool, _seed):
    """When the project map already resolves the repo, tier 2 is never
    consulted — the strongest signal wins without spending an LLM call."""
    fake = _FakeAlertAct({"github_repo": "should-not-be-used", "confidence": 1.0,
                          "source": "llm", "resources": []})
    act = AgentTaskActivities(db_pool=db_pool, alert_act=fake)
    result = await act.resolve_task_repo(
        {"id": "t-tier1-skip", "content": "Fix the exporter", "project_id": "pr-bcp"}
    )
    assert result["github_repo"] == "Stockopedia/bcp"
    assert result["source"] == "project_map"
    assert fake.calls == []
