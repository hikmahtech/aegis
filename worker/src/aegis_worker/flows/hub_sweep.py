"""HubSweepFlow — the problem hub's housekeeping tick.

Today it does one thing: open every `suppressed` problem whose deploy or
maintenance window has passed without a `resolved` event. The heartbeat only
emits on transitions, so a service that broke during a deploy and stayed broken
would otherwise surface only at the 24h re-investigation. Later PRs add the
projection sweep (re-render any problem whose events outran its Todoist task)
and the close sweep here.
"""

from __future__ import annotations

from dataclasses import dataclass

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from aegis_worker.activities.hub import HubActivities
    from aegis_worker.shared.retry import FAST, TIMEOUT_FAST


@dataclass
class HubSweepConfig:
    agent_id: str = "pandoras-actor"


@workflow.defn
class HubSweepFlow:
    @workflow.run
    async def run(self, config: HubSweepConfig) -> dict:
        promoted = await workflow.execute_activity_method(
            HubActivities.promote_expired_suppressions,
            start_to_close_timeout=TIMEOUT_FAST,
            retry_policy=FAST,
        )
        return {"promoted": int(promoted.get("promoted") or 0)}
