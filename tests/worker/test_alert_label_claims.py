"""Issue #498: a Dagster pipeline failure is investigated in the repo whose code
failed, not in the infra repo.

"Dagster Pipeline Failure" is on the infra list, so every one of them went
straight to the infra repo — 11 of 11 in two weeks — while the bug was in the
pipeline repo. Nothing mapped a Dagster job or code location to its repo.

The mapping is generic and lives in the database: a repository resource claims
alerts by label (`resources.metadata.alert_labels`), and a claimed alert is
investigated in that repo even when its alertname is infra. Nothing about
Dagster is in code. These tests drive the real resolvers against real
`resources` and `settings` rows.
"""

from __future__ import annotations

import pytest_asyncio
from aegis.services import infra_alert_routing as iar
from aegis_worker.activities.alerts import (
    _PRE_498_INFRA_ALERTNAMES,
    AlertActivities,
    is_infra_alert,
)
from temporalio.testing import ActivityEnvironment

# A real "Dagster Pipeline Failure" as AlertInvestigationFlow received it in
# production (problem 586cbacb, 2026-09-10), trimmed and with the host name
# replaced. The Grafana rule names the job, the failed step and the error; it
# does NOT name the Dagster code location.
_DAGSTER_STEP_FAILURE = {
    "title": "Dagster Failed: crypto_ml_training [2026-09-01]",
    "source": "alertmanager",
    "service": "",
    "severity": "critical",
    "fingerprint": "45240766e2ed4198",
    "description": (
        "Asset/Job: crypto_ml_training\nPartition: 2026-09-01\nBackfill: -\n"
        "Job name: crypto_ml_training\nRun ID: 16b4476d-5803-4f03-827c-7845a1ff894f\n"
        "Failed Step: ml__crypto__training__crypto_ml_training\n"
        "Error Type: InvalidOperationError\n"
        "Error: polars.exceptions.InvalidOperationError: `is_infinite` operation not "
        "supported for dtype `bool`\n\n"
        "View run: https://dagster.example.com/runs/16b4476d-5803-4f03-827c-7845a1ff894f\n"
    ),
    "labels": {
        "run_id": "16b4476d-5803-4f03-827c-7845a1ff894f",
        "service": "dagster",
        "severity": "critical",
        "alertname": "Dagster Pipeline Failure",
        "backfill_id": "-",
        "error_class": "InvalidOperationError",
        "failed_step": "ml__crypto__training__crypto_ml_training",
        "asset_or_job": "crypto_ml_training",
        "error_message": (
            "polars.exceptions.InvalidOperationError: `is_infinite` operation not "
            "supported for dtype `bool`"
        ),
        "partition_key": "2026-09-01",
        "pipeline_name": "crypto_ml_training",
        "grafana_folder": "Infrastructure",
    },
}


def _dagster_alert(**labels) -> dict:
    return {
        **_DAGSTER_STEP_FAILURE,
        "labels": {**_DAGSTER_STEP_FAILURE["labels"], **labels},
    }


_PIPELINE_REPO = "acme/trading-pipeline"
_INFRA_REPO = "acme/infra-gitops"


async def _add_repo(db_pool, slug: str, github_repo: str, **meta) -> str:
    metadata = {"github_repo": github_repo, "path": f"code/{slug}", **meta}
    rid = await db_pool.fetchval(
        "INSERT INTO resources (kind, slug, title, metadata) "
        "VALUES ('repository', $1, $2, $3) RETURNING id",
        slug,
        github_repo,
        metadata,
    )
    return str(rid)


async def _set_routing(db_pool, value: dict | None) -> None:
    await db_pool.execute("DELETE FROM settings WHERE key = $1", iar.SETTINGS_KEY)
    if value is not None:
        await db_pool.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES ($1, $2, NOW())",
            iar.SETTINGS_KEY,
            value,
        )
    iar._cache.update(value=None, ts=0.0)


@pytest_asyncio.fixture(loop_scope="function")
async def repos(db_pool):
    """An infra repo (named by the settings row) and a pipeline repo that claims
    the `crypto_ml_training` job. Removed again afterwards."""
    await db_pool.execute("DELETE FROM resources WHERE slug LIKE 'test-claim-%'")
    ids = {
        "infra": await _add_repo(db_pool, "test-claim-infra", _INFRA_REPO),
        "pipeline": await _add_repo(
            db_pool,
            "test-claim-pipeline",
            _PIPELINE_REPO,
            coding_enabled="true",
            alert_labels={"pipeline_name": ["crypto_ml_training"]},
        ),
    }
    await _set_routing(
        db_pool, {"extra_alertnames": ["dagster pipeline failure"], "repo": _INFRA_REPO}
    )
    try:
        yield ids
    finally:
        await db_pool.execute("DELETE FROM resources WHERE slug LIKE 'test-claim-%'")
        await _set_routing(db_pool, None)


async def _resolve_infra(db_pool, alert: dict) -> dict:
    return await ActivityEnvironment().run(
        AlertActivities(db_pool=db_pool).resolve_infra_resource, alert
    )


# ── the infra list is configured, not hard-coded ──────────────────────────


def test_the_configured_list_decides_what_is_infra():
    alert = _dagster_alert()
    assert is_infra_alert(alert, "", ["Dagster Pipeline Failure"]) is True
    assert is_infra_alert(alert, "", sorted(iar.DEFAULT_INFRA_ALERTNAMES)) is False


def test_moving_names_to_the_db_loses_none_of_them():
    """The old built-in list = the generic defaults + what a deployment writes
    to its row. Writing back exactly the names that left the code restores
    the old classification, so a deploy that does it first changes nothing."""
    moved = _PRE_498_INFRA_ALERTNAMES - iar.DEFAULT_INFRA_ALERTNAMES
    assert iar.DEFAULT_INFRA_ALERTNAMES <= _PRE_498_INFRA_ALERTNAMES
    assert set(iar.merge({"extra_alertnames": sorted(moved)})["alertnames"]) == (
        _PRE_498_INFRA_ALERTNAMES
    )
    assert moved == {
        "dagster pipeline failure",
        "clickhousedown",
        "criticalendpointdown",
        "gpucriticaltemperature",
        "tempordown",
    }


def test_a_history_recorded_before_the_list_moved_replays_the_same_way():
    """No list = an AlertInvestigationFlow history from before #498, whose
    routing config carried no names. It must classify exactly as it did then,
    or replay schedules a different activity and the workflow wedges."""
    assert is_infra_alert(_dagster_alert()) is True
    assert is_infra_alert({"labels": {"alertname": "ClickHouseDown"}}) is True
    assert is_infra_alert({"labels": {"alertname": "HighMemoryUsage"}}) is False


async def test_routing_config_serves_the_effective_list_from_the_db(db_pool):
    await _set_routing(db_pool, {"extra_alertnames": ["Dagster Pipeline Failure"]})
    try:
        routing = await ActivityEnvironment().run(
            AlertActivities(db_pool=db_pool).get_alert_routing_config
        )
        assert "dagster pipeline failure" in routing["infra_alertnames"]
        assert "nodedown" in routing["infra_alertnames"]
    finally:
        await _set_routing(db_pool, None)


# ── a claim moves a Dagster failure to the repo whose code failed ─────────


async def test_a_claimed_dagster_failure_resolves_to_its_repo(db_pool, repos):
    result = await _resolve_infra(db_pool, _dagster_alert())

    assert result["source"] == "label_claim"
    assert result["github_repo"] == _PIPELINE_REPO
    assert result["resource_id"] == repos["pipeline"]
    assert result["resources"][0]["resource_path"] == "code/test-claim-pipeline"


async def test_an_unclaimed_dagster_failure_stays_on_the_infra_repo(db_pool, repos):
    alert = _dagster_alert(
        pipeline_name="equities_index_consolidation_pipeline",
        asset_or_job="equities_index_consolidation_pipeline",
    )
    result = await _resolve_infra(db_pool, alert)

    assert result["source"] == "infra"
    assert result["github_repo"] == _INFRA_REPO
    assert result["resource_id"] == repos["infra"]


async def test_a_code_location_claim_leaves_a_run_level_failure_on_infra(db_pool, repos):
    """The durable mapping, once the alert rule exports the code location only
    for a failed step. A run that died before any step (user code unreachable,
    run worker killed) carries '-' and stays an infra investigation."""
    await db_pool.execute(
        "UPDATE resources SET metadata = metadata || $2 WHERE id = $1::uuid",
        repos["pipeline"],
        {"alert_labels": {"code_location": ["trading-system"]}},
    )
    step_failure = _dagster_alert(code_location="trading-system", pipeline_name="__ASSET_JOB")
    run_failure = _dagster_alert(
        code_location="-", pipeline_name="__ASSET_JOB", failed_step="unknown"
    )

    assert (await _resolve_infra(db_pool, step_failure))["github_repo"] == _PIPELINE_REPO
    assert (await _resolve_infra(db_pool, run_failure))["source"] == "infra"


async def test_a_claim_only_counts_on_a_coding_enabled_repo(db_pool, repos):
    await db_pool.execute(
        "UPDATE resources SET metadata = metadata - 'coding_enabled' WHERE id = $1::uuid",
        repos["pipeline"],
    )
    assert (await _resolve_infra(db_pool, _dagster_alert()))["source"] == "infra"


async def test_two_repos_claiming_one_alert_is_ambiguous_and_claims_nothing(db_pool, repos):
    await _add_repo(
        db_pool,
        "test-claim-other",
        "acme/other-pipeline",
        coding_enabled="true",
        alert_labels={"pipeline_name": ["crypto_ml_training"]},
    )
    assert (await _resolve_infra(db_pool, _dagster_alert()))["source"] == "infra"


# ── the infra repo comes from the settings row too ────────────────────────


async def test_with_no_infra_repo_configured_an_infra_alert_resolves_to_nothing(db_pool, repos):
    await _set_routing(db_pool, {"extra_alertnames": ["dagster pipeline failure"]})
    alert = {"source": "alertmanager", "labels": {"alertname": "NodeDown"}}

    result = await _resolve_infra(db_pool, alert)

    assert result["source"] == "none"
    assert result["resources"] == []


async def test_the_infra_repo_is_matched_case_insensitively(db_pool, repos):
    await _set_routing(db_pool, {"repo": _INFRA_REPO.upper()})
    alert = {"source": "alertmanager", "labels": {"alertname": "NodeDown"}}

    result = await _resolve_infra(db_pool, alert)

    assert result["source"] == "infra"
    assert result["resource_id"] == repos["infra"]


# ── a claim is honoured on the non-infra path as well ─────────────────────


async def test_a_claim_wins_on_the_normal_resolution_ladder(db_pool, repos):
    """An operator who takes Dagster off the infra list keeps the mapping: the
    ladder checks claims first, before any guess."""

    class _NoLLM:
        async def think(self, *a, **kw):  # pragma: no cover — must not be reached
            raise AssertionError("a claimed alert must not reach the LLM tier")

    act = AlertActivities(db_pool=db_pool, llm_client=_NoLLM())
    result = await ActivityEnvironment().run(act.resolve_alert_resource, _dagster_alert())

    assert result["source"] == "label_claim"
    assert result["github_repo"] == _PIPELINE_REPO
