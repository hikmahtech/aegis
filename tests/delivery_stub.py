"""One stand-in for `DeliveryActivities` as `safe_send_message` uses it.

`send_message` mirrors the real signature exactly, and the four attributes
`safe_send_message` reads off the object — `channel`, `db_pool`,
`budget_enabled`, `daily_budget` — are all here, as CLASS attributes so a
`hasattr(FakeDelivery, ...)` pin sees them and as constructor arguments so a
test can pick the path it wants.

The defaults are "push, skip the budget": channel `slack` (anything else makes
`safe_send_message` no-op) and no pool (a pool is what turns the
notification-budget gate on). Three test files pin this class against the real
one, so it cannot drift into testing nothing.
"""

from __future__ import annotations

from typing import Any


class FakeDelivery:
    channel = "slack"
    db_pool = None
    budget_enabled = False
    daily_budget = 8

    def __init__(
        self,
        channel: str = "slack",
        *,
        db_pool: Any = None,
        budget_enabled: bool = False,
        daily_budget: int = 8,
    ):
        self.channel = channel
        self.db_pool = db_pool
        self.budget_enabled = budget_enabled
        self.daily_budget = daily_budget
        self.sent: list[str] = []

    async def send_message(
        self,
        agent_id: str,
        message: str,
        chat_id: int = 0,
        thread_ref: dict | None = None,
        thread_overflow: bool = False,
    ) -> dict:
        self.sent.append(message)
        return {"ok": True}
