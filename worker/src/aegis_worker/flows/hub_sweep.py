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
    from aegis_worker.shared.retry import FAST, NO_RETRY, TIMEOUT_FAST, TIMEOUT_STANDARD


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
        # Then project: a problem promoted a moment ago gets its task in the
        # same tick, and any comment a producer's inline projection could not
        # post is retried here.
        projected = await workflow.execute_activity_method(
            HubActivities.project_pending,
            start_to_close_timeout=TIMEOUT_STANDARD,
            retry_policy=NO_RETRY,
        )
        return {
            "promoted": int(promoted.get("promoted") or 0),
            "projected": int(projected.get("projected") or 0),
            "created": int(projected.get("created") or 0),
            "errors": int(projected.get("errors") or 0),
        }
