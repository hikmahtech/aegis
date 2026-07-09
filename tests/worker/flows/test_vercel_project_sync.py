"""VercelProjectSyncFlow — daily sweep over Vercel personal + team projects."""

from __future__ import annotations

import pytest
from temporalio import activity, workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

with workflow.unsafe.imports_passed_through():
    from aegis_worker.activities.inventory import (
        ListVercelProjectsInput,
        UpsertResourcesBatchInput,
    )
    from aegis_worker.flows.vercel_project_sync import (
        VercelProjectSyncFlow,
        VercelProjectSyncInput,
    )


_list_calls: list[dict] = []
_upsert_calls: list[list[dict]] = []


@activity.defn(name="list_vercel_projects")
async def stub_list(input: ListVercelProjectsInput):
    _list_calls.append(
        {
            "include_personal": input.include_personal,
            "team_ids": list(input.team_ids),
        }
    )
    return [
        {
            "id": "prj_abc",
            "name": "example-site",
            "framework": "nextjs",
            "github_repo": "example/example-site",
            "production_domain": "example.com",
            "scope": "team",
            "team_id": "team_example",
        },
        {
            "id": "prj_def",
            "name": "acme-marketing",
            "framework": "astro",
            "github_repo": "",
            "production_domain": "acme-marketing.vercel.app",
            "scope": "personal",
            "team_id": "",
        },
    ]


@activity.defn(name="upsert_resources_batch")
async def stub_upsert(input: UpsertResourcesBatchInput):
    _upsert_calls.append(list(input.items))
    return {"inserted": len(input.items), "updated": 0, "total": len(input.items)}


@pytest.mark.asyncio
async def test_vercel_project_sync_builds_correct_items():
    _list_calls.clear()
    _upsert_calls.clear()
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue="tq",
            workflows=[VercelProjectSyncFlow],
            activities=[stub_list, stub_upsert],
        ),
    ):
        result = await env.client.execute_workflow(
            VercelProjectSyncFlow.run,
            VercelProjectSyncInput(
                agent_id="pandoras-actor",
                include_personal=True,
                team_ids=["team_example"],
            ),
            id="vc-sync-1",
            task_queue="tq",
        )

    assert _list_calls == [{"include_personal": True, "team_ids": ["team_example"]}]
    items = _upsert_calls[0]
    assert len(items) == 2

    example_site = next(i for i in items if i["slug"] == "vercel-example-site")
    assert example_site["kind"] == "vercel_project"
    assert example_site["title"] == "example-site"
    assert example_site["url"] == "https://example.com"
    assert "vercel" in example_site["tags"]
    assert "deployment" in example_site["tags"]
    assert "pandoras-actor" in example_site["tags"]
    assert "team_example" in example_site["tags"]
    assert example_site["metadata"] == {
        "project_id": "prj_abc",
        "team_id": "team_example",
        "framework": "nextjs",
        "production_domain": "example.com",
        "github_repo": "example/example-site",
    }

    acme = next(i for i in items if i["slug"] == "vercel-acme-marketing")
    assert acme["url"] == "https://acme-marketing.vercel.app"
    assert acme["metadata"]["github_repo"] == ""
    assert "personal" in acme["tags"]

    assert result == {"listed": 2, "inserted": 2, "updated": 0, "total": 2}


@pytest.mark.asyncio
async def test_vercel_project_sync_no_production_domain_uses_empty_url():
    _list_calls.clear()
    _upsert_calls.clear()

    @activity.defn(name="list_vercel_projects")
    async def list_no_domain(input: ListVercelProjectsInput):
        return [
            {
                "id": "prj_x",
                "name": "no-domain",
                "framework": "",
                "github_repo": "",
                "production_domain": "",
                "scope": "personal",
                "team_id": "",
            }
        ]

    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue="tq",
            workflows=[VercelProjectSyncFlow],
            activities=[list_no_domain, stub_upsert],
        ),
    ):
        await env.client.execute_workflow(
            VercelProjectSyncFlow.run,
            VercelProjectSyncInput(agent_id="pandoras-actor"),
            id="vc-sync-2",
            task_queue="tq",
        )

    items = _upsert_calls[0]
    assert items[0]["url"] == ""
