"""ResearchFlow — Raphael works one question (#509).

Started two ways: by the `research_topic` chat tool, under an id derived from
the question, and as a child of `AgentTaskFlow` for a `#research` task. Four
steps — gather (knowledge store, web, papers), read the best pages, synthesise
a cited answer, save it — each on the shared implementation in
`aegis.services.research`.

What the flow owes its callers:

* **No step sinks the run.** A failed read or synthesis still ends in an answer
  that says what happened, and the run summary names the step.
* **Only a real answer is saved.** A "synthesis failed" sentence is returned,
  never stored as research (the #508 rule, now kept in one place).
* **A late answer still reaches the user.** Past `reply_after_seconds` the chat
  tool has already said "still researching", so the flow sends the report to
  the agent's channel itself — through `send_message`, as `BooksWriteFlow`
  does. A task-started run passes 0 and the task comment is its delivery.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from html import escape

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from aegis.services.research import DEPTHS, render_report

    from aegis_worker.shared.retry import RETRY_ONCE, TIMEOUT_FAST

_GATHER_TIMEOUT = timedelta(seconds=150)
# Reads run together; each fetch has its own 30s ceiling inside.
_READ_TIMEOUT = timedelta(seconds=240)
_SYNTH_TIMEOUT = timedelta(seconds=360)
_SAVE_TIMEOUT = timedelta(seconds=120)
# Slack under the wait, deciding whether the tool is still listening. It errs
# towards telling the user twice rather than never.
_REPORT_MARGIN_S = 2.0


@dataclass
class ResearchInput:
    agent_id: str = "raphael"
    question: str = ""
    depth: str = "quick"
    domains: list[str] = field(default_factory=list)
    # What the asker wrote beyond the question (a task's description).
    context: str = ""
    # Pages to read before any search result (a task's own links).
    seed_urls: list[str] = field(default_factory=list)
    # How long the chat tool waited before saying "still researching". 0: nobody
    # is waiting on a channel, so the answer is never sent there.
    reply_after_seconds: int = 0


@workflow.defn(name="ResearchFlow")
class ResearchFlow:
    @workflow.run
    async def run(self, inp: ResearchInput) -> dict:
        question = (inp.question or "").strip()
        if not question:
            return {
                "status": "refused",
                "reason": "no question",
                "answer": "",
                "sources": [],
                "report": "",
                "saved": False,
            }
        depth = inp.depth if inp.depth in DEPTHS else "quick"
        notes: dict = {}

        try:
            gathered = await workflow.execute_activity(
                "research_gather",
                args=[
                    {
                        "question": question,
                        "depth": depth,
                        "domains": list(inp.domains or []),
                        "seed_urls": list(inp.seed_urls or []),
                    }
                ],
                start_to_close_timeout=_GATHER_TIMEOUT,
                retry_policy=RETRY_ONCE,
            )
        except Exception as exc:
            workflow.logger.warning("research_gather_degraded err=%s", str(exc)[:200])
            gathered = {"kg": [], "web": [], "papers": [], "to_read": [], "errors": []}
            notes["gather_degraded"] = True
        errors = list(gathered.get("errors") or [])

        pages: list[dict] = []
        if gathered.get("to_read"):
            try:
                read = await workflow.execute_activity(
                    "research_read",
                    args=[list(gathered["to_read"])],
                    start_to_close_timeout=_READ_TIMEOUT,
                    retry_policy=RETRY_ONCE,
                )
                pages = list(read.get("pages") or [])
                errors += list(read.get("errors") or [])
            except Exception as exc:
                workflow.logger.warning("research_read_degraded err=%s", str(exc)[:200])
                notes["read_degraded"] = True

        try:
            synth = await workflow.execute_activity(
                "research_synthesize",
                args=[question, inp.context or "", gathered, pages],
                start_to_close_timeout=_SYNTH_TIMEOUT,
                retry_policy=RETRY_ONCE,
            )
        except Exception as exc:
            workflow.logger.warning("research_synthesis_degraded err=%s", str(exc)[:200])
            synth = {
                "answer": "I gathered sources but the synthesis step failed.",
                "synthesized": False,
                "sources": [],
            }
            notes["synthesis_degraded"] = True
        answer = str(synth.get("answer") or "")
        sources = list(synth.get("sources") or [])

        saved = False
        if synth.get("synthesized"):
            try:
                res = await workflow.execute_activity(
                    "research_save",
                    args=[question, answer, sources],
                    start_to_close_timeout=_SAVE_TIMEOUT,
                    retry_policy=RETRY_ONCE,
                )
                saved = bool(res.get("saved"))
            except Exception as exc:
                workflow.logger.warning("research_save_failed err=%s", str(exc)[:200])
                notes["save_failed"] = True

        report = render_report(answer, sources)
        notified = await self._report_if_late(inp, question, report)
        return {
            "status": "ok" if synth.get("synthesized") else "no_answer",
            "question": question,
            "depth": depth,
            "answer": answer,
            "sources": sources,
            "report": report,
            "pages_read": len(pages),
            "saved": saved,
            "notified": notified,
            "errors": errors[:20],
            "elapsed_s": int(self._elapsed()),
            **notes,
        }

    def _elapsed(self) -> float:
        """Seconds since the workflow was STARTED — queue wait included, which
        is exactly the delay that makes a fast run land after the tool gave up."""
        return (workflow.now() - workflow.info().start_time).total_seconds()

    async def _report_if_late(self, inp: ResearchInput, question: str, report: str) -> bool:
        """Send the report to the agent's channel when the tool stopped waiting.

        Never raises: the answer is already made, and a dead comms server must
        not fail the run that made it."""
        if inp.reply_after_seconds <= 0:
            return False
        if self._elapsed() + _REPORT_MARGIN_S < inp.reply_after_seconds:
            return False
        text = f"<b>Research</b>: {escape(question)}\n\n{escape(report)}"
        try:
            res = await workflow.execute_activity(
                "send_message",
                args=[inp.agent_id, text],
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=RETRY_ONCE,
            )
        except Exception as exc:  # noqa: BLE001 — the answer stands; the message is extra
            workflow.logger.warning("research_report_failed err=%s", str(exc)[:200])
            return False
        return bool(isinstance(res, dict) and res.get("ok"))
