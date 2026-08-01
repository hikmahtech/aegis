"""ExpiryRadarFlow — daily sweep over `life.expiring_items` (C6).

The life-document counterpart of `CertRadarFlow`: instead of probing TLS
certificates it walks the registry from C5 and raises one interaction card per
newly-crossed `lead_days` threshold.

Correctness lives in `ExpiringItemsActivities.claim_due_alerts`, which claims
each alert through the unique index on
`life.expiring_item_alerts (item_id, threshold_days, expires_on)`. This flow
only ever spawns cards for alerts that activity already claimed, so re-running
it — same day, twice at once, or after a Temporal replay — cannot produce a
second card for the same threshold.

Cards are `ack` kind: one "✓ Acknowledge" button, which the Slack renderer
emits with no extra plumbing (`input`/`draft_review` cards render buttonless
unless `options.aegis_ui_url` is threaded through, and there is nothing to
edit in the admin panel here beyond the item itself). Children are spawned
ABANDONED so the daily run closes immediately rather than living for the
card's week-long timeout.
"""

from __future__ import annotations

from dataclasses import dataclass

from temporalio import workflow
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    from aegis_worker.activities.expiring_items import ExpiringItemsActivities
    from aegis_worker.flows.interaction import InteractionFlow, InteractionFlowInput
    from aegis_worker.shared.retry import NO_RETRY, TIMEOUT_FAST

# A week: long enough that a 30-day warning stays actionable in the inbox,
# short enough that the next threshold's card supersedes it rather than
# stacking. On timeout the interaction is archived, not auto-answered.
_CARD_TIMEOUT_SECONDS = 604800


@dataclass
class ExpiryRadarConfig:
    agent_id: str = "sebas"
    # How far ahead `due_within` looks. Generous on purpose: the real filter is
    # each item's own `lead_days`, so the window only has to exceed the largest
    # lead anyone configures. A year and a bit covers "warn me 12 months before
    # my passport expires".
    lookahead_days: int = 400
    # Hard cap on cards per run — see claim_due_alerts. Unclaimed surplus
    # simply alerts tomorrow.
    max_cards: int = 5


async def _spawn_expiry_card(agent_id: str, alert: dict) -> bool:
    """Spawn one abandoned ack card. True on successful spawn.

    The child id is the dedup key itself, so Temporal's workflow-id uniqueness
    is a second, independent guard against a duplicate card.
    """
    child_id = f"expiry-{alert['item_id']}-{alert['threshold_days']}-{alert['expires_on']}"
    try:
        await workflow.start_child_workflow(
            InteractionFlow.run,
            InteractionFlowInput(
                agent_id=agent_id,
                kind="ack",
                origin="expiry_radar",
                prompt=str(alert.get("prompt") or "")[:600],
                metadata={
                    "source": "expiry_radar",
                    "item_id": alert["item_id"],
                    "threshold_days": alert["threshold_days"],
                    "expires_on": alert["expires_on"],
                },
                timeout_seconds=_CARD_TIMEOUT_SECONDS,
                timeout_policy="archive",
            ),
            id=child_id,
            parent_close_policy=workflow.ParentClosePolicy.ABANDON,
        )
        return True
    except Exception as exc:  # noqa: BLE001 — one bad card must not kill the sweep
        workflow.logger.warning(
            "expiry_card_spawn_failed item=%s err=%s", alert.get("item_id"), str(exc)[:200]
        )
        return False


@workflow.defn(name="ExpiryRadarFlow")
class ExpiryRadarFlow:
    @workflow.run
    async def run(self, config: ExpiryRadarConfig) -> dict:
        step = "claim_due_alerts"
        try:
            # NO_RETRY: claiming is a write. A retry would skip the alerts the
            # failed attempt already claimed, so an extra attempt cannot
            # recover them — better to surface one failed run than to burn
            # thresholds quietly across three of them.
            alerts = await workflow.execute_activity_method(
                ExpiringItemsActivities.claim_due_alerts,
                args=[config.lookahead_days, config.max_cards],
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=NO_RETRY,
            )
            step = "spawn_cards"
            sent = 0
            for alert in alerts:
                if await _spawn_expiry_card(config.agent_id, alert):
                    sent += 1
            step = "record_expiry_cards"
            await workflow.execute_activity_method(
                ExpiringItemsActivities.record_expiry_cards,
                args=[config.agent_id, sent, len(alerts) - sent],
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=NO_RETRY,
            )
        except ApplicationError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ApplicationError(
                f"expiry_radar_failed at step={step}: {exc!r}", non_retryable=True
            ) from exc
        return {"claimed": len(alerts), "cards": sent}
