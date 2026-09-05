"""MoneyBriefFlow — the weekly money brief (spec §7.2).

Four steps: refresh the FX price file, build the brief from hledger plus the
journal index, render it, send it and file the Markdown copy in the books repo.

Only the FX refresh is allowed to fail quietly. Quotes come from a keyless
public provider — the least reliable thing in the lane — and a brief with last
week's rates is worth far more than no brief at all. (`build_money_brief` says
so out loud when a commodity has no rate, so a stale price file is reported,
not hidden.) Rendering is an activity, not workflow code, so the workflow stays
deterministic and the renderer stays a plain function.
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
class MoneyBriefConfig:
    agent_id: str = "maou"
    days: int = 7
    silent: bool = False


@workflow.defn(name="MoneyBriefFlow")
class MoneyBriefFlow:
    @workflow.run
    async def run(self, config: MoneyBriefConfig) -> dict:
        try:
            await workflow.execute_activity(
                "refresh_fx_prices", start_to_close_timeout=_FAST, retry_policy=NO_RETRY
            )
        except Exception as exc:  # noqa: BLE001 — prices are a nicety
            workflow.logger.warning("money_brief_fx_failed err=%s", str(exc)[:200])

        brief = await workflow.execute_activity(
            "build_money_brief", args=[config.days], start_to_close_timeout=_SLOW, retry_policy=FAST
        )
        rendered = await workflow.execute_activity(
            "render_money_brief", args=[brief], start_to_close_timeout=_FAST, retry_policy=NO_RETRY
        )

        # `sent` is delivery, not dispatch: the activity hands back what
        # `safe_send_message` observed, so a Slack outage reads as sent=False
        # in `workflow_runs` instead of a silent lie.
        sent = False
        if not config.silent:
            sent = bool(
                await workflow.execute_activity(
                    "notify_money_message",
                    args=[rendered["html"], "money_brief_notify_failed"],
                    start_to_close_timeout=_FAST,
                    retry_policy=NO_RETRY,
                )
            )

        await workflow.execute_activity(
            "write_money_report",
            args=[f"reports/weekly/{brief['as_of']}.md", rendered["markdown"]],
            start_to_close_timeout=_SLOW,
            retry_policy=NO_RETRY,
        )

        return {
            "as_of": brief["as_of"],
            "sent": sent,
            "dues": len(brief.get("dues") or []),
            "unknowns": len(brief.get("unknowns") or []),
            "books_ok": bool(brief.get("books_ok")),
        }
