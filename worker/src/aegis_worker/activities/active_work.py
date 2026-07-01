"""Active-work guard activity — gather the read-only signals and assess."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from temporalio import activity

from aegis_worker.activework import github, guard, todoist


@dataclass
class ActiveWorkActivities:
    db_pool: Any = None
    remote_script: Any = None
    lookback_hours: int = 48

    @activity.defn
    async def check_active_work(self, alert: dict, repo: str) -> dict:
        """Gather the active-work signals (each degrades to empty) and assess."""
        now = datetime.now(UTC)
        since_iso = (now - timedelta(hours=self.lookback_hours)).isoformat()
        today_iso = now.date().isoformat()

        # Each helper catches its own exceptions and degrades to [] internally.
        prs = await github.open_prs(self.remote_script, repo)
        pushes = await github.recent_pushes(self.remote_script, repo, since_iso)
        tasks = await todoist.due_today_or_overdue(self.db_pool, today_iso)

        return guard.assess(
            repo=repo,
            service=alert.get("service", ""),
            prs=prs,
            pushes=pushes,
            tasks=tasks,
        )
