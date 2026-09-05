"""MonthCloseFlow — the previous calendar month's close (spec §7.3).

Build the income statement, balance sheet and index counts for the month that
just ended, render them, send the message and file the Markdown copy under
`reports/monthly/`. The month is chosen by `build_month_close`, not here, so a
manual re-run on any day of the month closes the same month a scheduled one
would. There is no FX step: the weekly brief already refreshes the price file,
and a month close is worth running with the rates it has.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from aegis_worker.shared.retry import FAST, NO_RETRY

_FAST = timedelta(seconds=60)
_SLOW = timedelta(seconds=180)


@dataclass
class MonthCloseConfig:
    agent_id: str = "maou"
    silent: bool = False


@workflow.defn(name="MonthCloseFlow")
class MonthCloseFlow:
    @workflow.run
    async def run(self, config: MonthCloseConfig) -> dict:
        close = await workflow.execute_activity(
            "build_month_close", start_to_close_timeout=_SLOW, retry_policy=FAST
        )
        rendered = await workflow.execute_activity(
            "render_month_close", args=[close], start_to_close_timeout=_FAST, retry_policy=NO_RETRY
        )

        sent = False
        if not config.silent:
            await workflow.execute_activity(
                "notify_money_message",
                args=[rendered["html"], "month_close_notify_failed"],
                start_to_close_timeout=_FAST,
                retry_policy=NO_RETRY,
            )
            sent = True

        await workflow.execute_activity(
            "write_money_report",
            args=[f"reports/monthly/{close['month']}.md", rendered["markdown"]],
            start_to_close_timeout=_SLOW,
            retry_policy=NO_RETRY,
        )

        return {"month": close["month"], "sent": sent, "books_ok": bool(close.get("books_ok"))}
