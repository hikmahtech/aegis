"""Interaction persistence activities.

Used by InteractionFlow and by the /api/interactions/{id}/resolve endpoint
(indirectly: the endpoint updates the DB row itself and signals the flow,
but the flow's signal handler also calls resolve_interaction so the
resolve-endpoint and signal-handler paths produce the same terminal state).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

import asyncpg
import structlog
from temporalio import activity

logger = structlog.get_logger()


@dataclass
class InsertInteractionInput:
    flow_run_id: str
    agent_id: str
    kind: str
    origin: str
    prompt: str
    options: dict[str, Any] | None
    timeout_policy: str
    timeout_at: datetime | None
    telegram_message_id: int | None = None
    # Phase 4: carries source/flavor/task_id/decision so post_resolve activity
    # can dispatch. Persisted into interactions.metadata (migration 013).
    metadata: dict[str, Any] | None = None


@dataclass
class InsertInteractionResult:
    interaction_id: str


@dataclass
class ResolveInteractionInput:
    interaction_id: str
    response: dict[str, Any]


@dataclass
class ResolveInteractionResult:
    already_resolved: bool


@dataclass
class ApplyTimeoutInput:
    interaction_id: str
    policy: str  # archive | hold (auto_reject/auto_approve removed 2026-05-28)


class InteractionActivities:
    """Owns the interactions table."""

    def __init__(self, db_pool: asyncpg.Pool):
        self._pool = db_pool

    @activity.defn(name="insert_interaction")
    async def insert_interaction(self, input: InsertInteractionInput) -> InsertInteractionResult:
        # Note: `kind='approval'` is a legacy value still rendered by
        # delivery.send_interaction_card (two-button approve/reject card) but
        # no current producer creates rows with this kind. All live callers
        # use `choice`, `ack`, `input`, or `draft_review`. The 103
        # `kind='approval'` rows that exist in prod (as of 2026-05-28) are
        # historical (from the removed v2 EmailTriageWorkflow); do not
        # introduce new ones — spawn an InteractionFlow with the appropriate
        # modern kind instead.
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO interactions
                    (flow_run_id, agent_id, kind, origin, prompt, options,
                     status, timeout_at, timeout_policy, telegram_message_id,
                     metadata)
                VALUES ($1, $2, $3, $4, $5, $6, 'pending', $7, $8, $9, $10)
                RETURNING id
                """,
                input.flow_run_id,
                input.agent_id,
                input.kind,
                input.origin,
                input.prompt,
                input.options,
                input.timeout_at,
                input.timeout_policy,
                input.telegram_message_id,
                input.metadata or {},
            )
        return InsertInteractionResult(interaction_id=str(row["id"]))

    @activity.defn(name="resolve_interaction")
    async def resolve_interaction(self, input: ResolveInteractionInput) -> ResolveInteractionResult:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE interactions
                SET status = 'resolved',
                    response = $2,
                    resolved_at = now()
                WHERE id = $1 AND status = 'pending'
                RETURNING id
                """,
                UUID(input.interaction_id),
                input.response,
            )
        already_resolved = row is None
        if already_resolved:
            logger.info(
                "interaction_resolve_idempotent_noop",
                interaction_id=input.interaction_id,
            )
        return ResolveInteractionResult(already_resolved=already_resolved)

    @activity.defn(name="apply_interaction_timeout")
    async def apply_interaction_timeout(self, input: ApplyTimeoutInput) -> None:
        if input.policy == "hold":
            return

        if input.policy != "archive":
            # `auto_reject` / `auto_approve` were removed 2026-05-28 (no
            # producers in 30 days). Only `archive` and `hold` remain.
            raise ValueError(f"unknown timeout policy: {input.policy}")

        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE interactions "
                "SET status = 'archived', resolved_at = now() "
                "WHERE id = $1 AND status = 'pending'",
                UUID(input.interaction_id),
            )

    @activity.defn(name="update_interaction_message_id")
    async def update_interaction_message_id(
        self, interaction_id: str, telegram_message_id: int
    ) -> None:
        """Best-effort write that bridges send_interaction_card → bot
        edit-in-place when the user clicks a callback. Status-guarded with
        `AND status='pending'` so a fast resolve-then-card-late race can't
        clobber the resolved row's `telegram_message_id`. Caller retries up
        to 2 attempts (`_BEST_EFFORT_RETRY` in flows/interaction.py); on
        persistent failure the flow logs and continues — the interaction is
        still functional, just without the edit-in-place hook."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE interactions SET telegram_message_id = $2 "
                "WHERE id = $1 AND status = 'pending'",
                UUID(interaction_id),
                telegram_message_id,
            )

    @activity.defn(name="update_interaction_delivery_ref")
    async def update_interaction_delivery_ref(
        self, interaction_id: str, delivery_ref: dict
    ) -> None:
        """Persist the channel-neutral delivery reference (e.g. Slack
        `{adapter, channel, ts}`) returned by `send_interaction_card`. Slack
        cards have no numeric `telegram_message_id`, so this is the only record
        that the card was delivered — the DeliveryWatchdog treats a row with
        BOTH `telegram_message_id` and `delivery_ref` NULL as undelivered.
        Status-guarded with `AND status='pending'` (same fast-resolve / late-card
        race guard as `update_interaction_message_id`). Best-effort: the caller
        retries twice, then logs and continues."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE interactions SET delivery_ref = $2 "
                "WHERE id = $1 AND status = 'pending'",
                UUID(interaction_id),
                delivery_ref,
            )
