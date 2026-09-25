"""GithubRisingFlow: one weekly run of the rising-repos search (#677). All in
the `github_rising_tick` activity; a repo already filed is a no-op."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from aegis_worker.shared.retry import FAST


@dataclass
class GithubRisingConfig:
    agent_id: str = "raphael"
    # The `github-rising-weekly` activities row's config, as written there.
    rising: dict = field(default_factory=dict)


@workflow.defn(name="GithubRisingFlow")
class GithubRisingFlow:
    @workflow.run
    async def run(self, config: GithubRisingConfig) -> dict:
        return await workflow.execute_activity(
            "github_rising_tick",
            config.rising,
            start_to_close_timeout=timedelta(minutes=3),
            retry_policy=FAST,
        )
