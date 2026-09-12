"""NotesWriteFlow — one durable vault write, started by a chat tool (#514).

`note_write` / `note_link` validate the call, start this workflow under an id
derived from the write's own content, and wait a few seconds for it — the
`BooksWriteFlow` seam (#388), for the same reason: a vault write is flock →
clone-or-pull → append → commit → push, possibly twice, and the chat loop
cannot cancel a git subprocess.

* **One write per content hash.** The workflow id IS that hash, so a retried
  chat turn attaches to this run; the note's own marker covers a call
  genuinely repeated later.
* **The outcome is reported even when nobody is waiting any more.** Past
  `reply_after_seconds` the tool has already said "still running", so the
  flow delivers the result to the agent's channel itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from html import escape

from temporalio import workflow
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    from aegis.services.notes_write import NOTES_WRITE_TIMEOUT_S

    from aegis_worker.shared.retry import RETRY_ONCE, TIMEOUT_FAST

_WRITE_TIMEOUT = timedelta(seconds=NOTES_WRITE_TIMEOUT_S)
# The write is idempotent (its marker), so one retry is free insurance against
# a worker restart; more would only multiply a stuck write's budget.
_WRITE_RETRY = RETRY_ONCE
_REPORT_MARGIN_S = 2.0


@dataclass
class NotesWriteInput:
    agent_id: str
    op: str = ""
    payload: dict = field(default_factory=dict)
    reply_after_seconds: int = 20


@workflow.defn(name="NotesWriteFlow")
class NotesWriteFlow:
    @workflow.run
    async def run(self, inp: NotesWriteInput) -> dict:
        try:
            result = await workflow.execute_activity(
                "notes_write",
                args=[inp.op, inp.payload],
                start_to_close_timeout=_WRITE_TIMEOUT,
                retry_policy=_WRITE_RETRY,
            )
        except Exception as exc:
            message = f"error: the vault {inp.op} write failed: {str(exc)[:200]}"
            await self._report_if_late(inp, message)
            raise ApplicationError(
                f"notes_write_failed at step=write op={inp.op}: {exc!r}", non_retryable=True
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
        """Since the workflow was STARTED, queue wait included — that delay is
        what makes a fast write land after the tool stopped waiting."""
        return (workflow.now() - workflow.info().start_time).total_seconds()

    async def _report_if_late(self, inp: NotesWriteInput, message: str) -> bool:
        """Deliver the outcome when the tool has stopped waiting. Never raises:
        the write is already done either way."""
        elapsed = self._elapsed()
        if elapsed + _REPORT_MARGIN_S < inp.reply_after_seconds:
            return False
        text = f"<b>Vault write</b> ({escape(inp.op)}, took {int(elapsed)}s): {escape(message)}"
        try:
            res = await workflow.execute_activity(
                "send_message",
                args=[inp.agent_id, text],
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=_WRITE_RETRY,
            )
        except Exception as exc:  # noqa: BLE001 — the write stands; the message is extra
            workflow.logger.warning(
                "notes_write_report_failed op=%s err=%s", inp.op, str(exc)[:200]
            )
            return False
        return bool(isinstance(res, dict) and res.get("ok"))
