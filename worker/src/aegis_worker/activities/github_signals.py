"""GitHubSignalsActivities: the weekly rising-repos run (#677). The token is
read from the DB on every run, so an admin save applies without a restart."""

from __future__ import annotations

from typing import Any

from aegis.connectors.github import GitHubClient
from aegis.services import github_signals, integrations_config
from aegis.services.user_time import user_now
from temporalio import activity


class GitHubSignalsActivities:
    def __init__(self, *, db_pool: Any, settings: Any) -> None:
        self.db_pool = db_pool
        self.settings = settings

    @activity.defn
    async def github_rising_tick(self, config: dict) -> dict:
        token = await integrations_config.read_integration(self.db_pool, self.settings, "github_token")
        client = GitHubClient(token, db_pool=self.db_pool)
        try:
            return await github_signals.run_rising(
                self.db_pool,
                client,
                github_signals.RisingConfig.from_config(config),
                today=(await user_now(self.db_pool)).date(),
            )
        finally:
            await client.close()
