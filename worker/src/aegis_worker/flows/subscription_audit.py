"""SubscriptionAuditFlow - monthly burn digest for Maou."""

from __future__ import annotations

from dataclasses import dataclass

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from aegis_worker.activities.money import MoneyActivities
    from aegis_worker.shared.retry import (
        FAST,
        NO_RETRY,
        TIMEOUT_FAST,
        TIMEOUT_STANDARD,
    )


@dataclass
class SubscriptionAuditConfig:
    agent_id: str = "maou"
    silent: bool = False


@workflow.defn
class SubscriptionAuditFlow:
    @workflow.run
    async def run(self, config: SubscriptionAuditConfig) -> dict:
        digest: dict = {}
        try:
            digest = await workflow.execute_activity_method(
                MoneyActivities.build_subscription_digest,
                start_to_close_timeout=TIMEOUT_STANDARD,
                retry_policy=FAST,
            )
            if not config.silent:
                try:
                    await workflow.execute_activity_method(
                        MoneyActivities.notify_subscription_digest,
                        args=[digest],
                        start_to_close_timeout=TIMEOUT_FAST,
                        retry_policy=NO_RETRY,
                    )
                except Exception:
                    pass
        except Exception as exc:
            workflow.logger.error("subscription_audit_failed error=%s", str(exc)[:200])
            raise
        return digest
