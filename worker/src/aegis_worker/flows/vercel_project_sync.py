"""VercelProjectSyncFlow — daily sweep over Vercel personal + team projects.

Mirrors WorkspaceRepoSyncFlow: pulls every project via Vercel REST, mints
`vercel-<name>` rows in `resources` (kind=`vercel_project`) and union-
merges tags / shallow-merges metadata.

When a Vercel project has a linked GitHub repo we capture it as
`metadata.github_repo`; the GIN expression index from migration 020
makes the reverse lookup (github → vercel) cheap so a GitHub alert can
surface its sibling Vercel deployment to the investigation prompt.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from aegis_worker.activities.inventory import (
        ListVercelProjectsInput,
        UpsertResourcesBatchInput,
    )
    from aegis_worker.shared.retry import RETRY_ONCE


_LIST_TIMEOUT = timedelta(seconds=120)
_UPSERT_TIMEOUT = timedelta(seconds=60)


@dataclass
class VercelProjectSyncInput:
    agent_id: str = "pandoras-actor"
    include_personal: bool = True
    team_ids: list[str] = field(default_factory=list)


@workflow.defn(name="VercelProjectSyncFlow")
class VercelProjectSyncFlow:
    @workflow.run
    async def run(self, input: VercelProjectSyncInput) -> dict:
        projects: list[dict] = await workflow.execute_activity(
            "list_vercel_projects",
            ListVercelProjectsInput(
                include_personal=input.include_personal,
                team_ids=list(input.team_ids),
            ),
            start_to_close_timeout=_LIST_TIMEOUT,
            retry_policy=RETRY_ONCE,
        )

        if not projects:
            return {"listed": 0, "inserted": 0, "updated": 0, "total": 0}

        items = [_project_to_resource_item(p) for p in projects]
        summary = await workflow.execute_activity(
            "upsert_resources_batch",
            UpsertResourcesBatchInput(items=items),
            start_to_close_timeout=_UPSERT_TIMEOUT,
            retry_policy=RETRY_ONCE,
        )

        return {"listed": len(projects), **summary}


def _project_to_resource_item(project: dict) -> dict:
    name = project.get("name", "")
    production_domain = project.get("production_domain", "")
    url = f"https://{production_domain}" if production_domain else ""
    scope = project.get("scope", "personal")
    team_id = project.get("team_id", "")
    tags = ["vercel", "deployment", "pandoras-actor"]
    tags.append(scope)
    if team_id:
        tags.append(team_id.lower())
    return {
        "kind": "vercel_project",
        "slug": f"vercel-{name}",
        "title": name,
        "url": url,
        "tags": tags,
        "metadata": {
            "project_id": project.get("id", ""),
            "team_id": team_id,
            "framework": project.get("framework", ""),
            "production_domain": production_domain,
            "github_repo": project.get("github_repo", ""),
        },
    }
