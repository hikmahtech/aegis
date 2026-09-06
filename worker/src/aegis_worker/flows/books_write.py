"""BooksWriteFlow — one durable books write, started by a chat tool.

`ledger_post` / `ledger_reclassify` / `ledger_add_rule` validate the call, start
this workflow under an id derived from the write's own content, and wait a few
seconds for it. Issue #388: a books write is flock → clone-or-pull → mutate →
`hledger check --strict` → commit → push, whose budgets add up to 540s against
the 600s the whole chat turn gets — and `asyncio.wait_for` cannot cancel the
thread the write runs in, so keeping it inside the turn could only ever
misreport a slow write, never stop one.

Two things this flow owes the user:

* **One write per content hash.** The workflow id IS that hash, so a retried
  chat turn attaches to this run rather than starting a second one, and
  `books.py`'s own idempotency covers a call genuinely repeated later.
* **The outcome is reported even when nobody is waiting any more.** Past
  `reply_after_seconds` the tool has already answered "still running", so the
  flow delivers the result to the agent's channel itself — through
  `send_message`, the same activity a chat reply is delivered by. That is a
  deferred half of an answer the user asked for, not a proactive FYI, which is
  why it does not go through the notification budget.

Activities are named as strings, like `MoneyProcessFlow`'s: core starts this
flow, and the money lane's activities are already addressed that way.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from html import escape

from temporalio import workflow
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    from aegis.services.ledger_write import BOOKS_WRITE_TIMEOUT_S

    from aegis_worker.shared.retry import RETRY_ONCE, TIMEOUT_FAST

# The writer's own worst case (`books.py`: clone 180 + pull 120 + strict check
# 60 + commit 60 + push 120). Spending it here costs a durable workflow its
# time and nothing else — which is the whole point of the move.
_WRITE_TIMEOUT = timedelta(seconds=BOOKS_WRITE_TIMEOUT_S)

# One retry, not three. The write is idempotent, so a retry is free insurance
# against a worker restart; more than one only multiplies a 540s budget by the
# number of attempts when something is genuinely stuck.
_WRITE_RETRY = RETRY_ONCE

# Slack under the wait, to decide whether the tool is still listening. The
# result can land a moment before the tool's deadline and still not reach it,
# so the band errs towards telling the user twice rather than never — a
# duplicate is noise, a miss is a write nobody ever hears about.
_REPORT_MARGIN_S = 2.0


@dataclass
class BooksWriteInput:
    agent_id: str
    op: str = ""
    payload: dict = field(default_factory=dict)
    # How long the tool waited before it told the user the write was still
    # running. At or past it, nobody is reading the return value any more.
    reply_after_seconds: int = 20


@workflow.defn(name="BooksWriteFlow")
class BooksWriteFlow:
    @workflow.run
    async def run(self, inp: BooksWriteInput) -> dict:
        try:
            result = await workflow.execute_activity(
                "books_write",
                args=[inp.op, inp.payload],
                start_to_close_timeout=_WRITE_TIMEOUT,
                retry_policy=_WRITE_RETRY,
            )
        except Exception as exc:
            # Reported before it is raised: the user who was told "still
            # running" learns it failed, instead of waiting on a workflow whose
            # only record of itself is a `workflow_runs` row.
            message = f"error: the books {inp.op} write failed: {str(exc)[:200]}"
            await self._report_if_late(inp, message)
            raise ApplicationError(
                f"books_write_failed at step=write op={inp.op}: {exc!r}", non_retryable=True
            ) from exc

        message = str(result.get("message") or "") if isinstance(result, dict) else ""
        ok = bool(result.get("ok")) if isinstance(result, dict) else False
        notified = await self._report_if_late(inp, message)
        return {
            "status": "ok" if ok else "refused",
            "op": inp.op,
            "message": message,
            "elapsed_s": int(self._elapsed()),
            "notified": notified,
        }

    def _elapsed(self) -> float:
        """Seconds since the workflow was STARTED, not since this worker picked
        it up. The difference is the queue wait, and that is exactly the delay
        that makes an otherwise fast write arrive after the tool gave up —
        measuring from the first line here would call such a write fast and say
        nothing to a user who was told it was still running."""
        return (workflow.now() - workflow.info().start_time).total_seconds()

    async def _report_if_late(self, inp: BooksWriteInput, message: str) -> bool:
        """Deliver the outcome when the tool has stopped waiting for it.

        Returns whether it actually landed, so `notified: false` in the run
        record means the user did NOT hear about a write they were told was
        still running. Never raises: the write is already done either way, and
        failing the workflow over a dead comms server would misreport the
        ledger.
        """
        elapsed = self._elapsed()
        if elapsed + _REPORT_MARGIN_S < inp.reply_after_seconds:
            return False
        text = f"<b>Books write</b> ({escape(inp.op)}, took {int(elapsed)}s): {escape(message)}"
        try:
            res = await workflow.execute_activity(
                "send_message",
                args=[inp.agent_id, text],
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=_WRITE_RETRY,
            )
        except Exception as exc:  # noqa: BLE001 — the write stands; the message is extra
            workflow.logger.warning(
                "books_write_report_failed op=%s err=%s", inp.op, str(exc)[:200]
            )
            return False
        return bool(isinstance(res, dict) and res.get("ok"))
