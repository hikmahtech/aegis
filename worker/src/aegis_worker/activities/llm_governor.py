"""LLM spend-governor activities — rolling-24h token budget enforcement.

`check_llm_budget` is the whole governor: it compares the last 24h of
`llm_calls` tokens against `settings.llm_governor.daily_token_budget` and
flips `settings.llm_kill_switch` accordingly. `LLMClient` reads that switch
before every generation call (see `aegis.services.llm_governor`).

Two rules keep this from fighting a human operator or spamming the channel:

* **Only auto-clear our own switch.** A manual kill (`set_by != "governor"`)
  is left alone when usage drops back under budget — a human freeze ends
  when a human ends it.
* **Alert on transitions only.** `breached` is True on the tick that *trips*
  the switch, not on every tick of a sustained breach — otherwise a 15-min
  schedule would post a Slack card four times an hour until the window rolls.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog
from temporalio import activity

logger = structlog.get_logger()


@dataclass
class LLMGovernorActivities:
    db_pool: Any

    @activity.defn
    async def check_llm_budget(self) -> dict:
        """Compare rolling-24h token usage to the budget; set/clear the switch.

        Returns `{breached, cleared, already_active, tokens, budget, message}`.
        `breached` / `cleared` are edge-triggered — the flow alerts only when
        one of them is True, so a sustained breach is announced once.
        """
        from aegis.observability import log_audit
        from aegis.services.llm_governor import (
            get_governor_config,
            get_kill_switch,
            llm_tokens_last_24h,
            set_kill_switch,
        )

        quiet = {
            "breached": False,
            "cleared": False,
            "already_active": False,
            "tokens": 0,
            "budget": 0,
            "message": "",
        }

        config = await get_governor_config(self.db_pool)
        budget = int(config.get("daily_token_budget") or 0)
        model_filter = config.get("model_filter") or ""
        if budget <= 0:
            # Governor disabled — the default for any deployment that never
            # configures it. Touch nothing (not even to clear an existing
            # switch: disabling the budget is not "approve all spend").
            activity.logger.debug("llm_governor_disabled")
            return quiet

        tokens = await llm_tokens_last_24h(self.db_pool, model_filter)
        switch = await get_kill_switch(self.db_pool, use_cache=False)
        scope = model_filter or "all models"
        result = {**quiet, "tokens": tokens, "budget": budget}

        if tokens > budget:
            if switch.get("active"):
                # Already frozen (by us or by a human) — stay quiet.
                return {**result, "already_active": True}
            reason = f"rolling-24h tokens {tokens:,} exceeded budget {budget:,} ({scope})"
            await set_kill_switch(self.db_pool, active=True, reason=reason, set_by="governor")
            await log_audit(
                self.db_pool,
                actor="worker:llm_governor",
                action="llm_kill_switch_set",
                target_type="settings",
                target_id="llm_kill_switch",
                details={"tokens": tokens, "budget": budget, "model_filter": model_filter},
            )
            logger.warning("llm_budget_breached", tokens=tokens, budget=budget)
            return {
                **result,
                "breached": True,
                "message": (
                    f"🛑 LLM kill switch ENGAGED — {reason}. New generation calls are "
                    f"refused (embeddings still work). Clear it on the admin Settings "
                    f"page: llm_kill_switch.active = false."
                ),
            }

        # Under budget: auto-clear ONLY a switch the governor set itself.
        if switch.get("active") and switch.get("set_by") == "governor":
            await set_kill_switch(self.db_pool, active=False, reason="", set_by="governor")
            await log_audit(
                self.db_pool,
                actor="worker:llm_governor",
                action="llm_kill_switch_cleared",
                target_type="settings",
                target_id="llm_kill_switch",
                details={"tokens": tokens, "budget": budget},
            )
            logger.info("llm_budget_recovered", tokens=tokens, budget=budget)
            return {
                **result,
                "cleared": True,
                "message": (
                    f"✅ LLM kill switch cleared — rolling-24h tokens {tokens:,} back "
                    f"under budget {budget:,} ({scope}). Generation calls resume."
                ),
            }

        return result
