"""AgentChatReplyFlow — per-agent chat reply (two trigger paths).

Triggered by either:

  - ClarifyFlow when the per-agent short-circuit fires
    (@sebas/@raphael/@maou + fresh user comment on a Todoist task)
  - Telegram bot DM @mention path (no task) via the
    `/api/chat/agent-reply/trigger` route

Orchestrates:

  1. ChatActivities.synthesize_reply  (HTTP wrapper → core)
  2. DeliveryActivities.send_telegram (existing PR #248 path)
  3. ClarifyActivities.post_agent_reply_comment (Todoist mirror — TASK PATH ONLY)

On failure (synthesize or post errors): if a task is present, runs
ClarifyActivities.post_agent_reply_error_comment so the user always
sees something on the originating Todoist task. Telegram-only failures
degrade to telegram_message_id=None and still post the Todoist comment
(or degrade silently on the DM path).

TASKLESS MODE: when `task_id is None`, the Todoist mirror + error-comment
steps are skipped entirely. The bot path uses this for DMs where there
is no Todoist anchor. The active comms adapter (Slack) routes the reply by
the agent's channel.
"""

from __future__ import annotations

from dataclasses import dataclass

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from aegis_worker.activities.chat import ChatActivities
    from aegis_worker.activities.clarify import ClarifyActivities
    from aegis_worker.activities.delivery import DeliveryActivities
    from aegis_worker.shared.retry import (
        NO_RETRY,
        RETRY_ONCE,
        STANDARD,
        TIMEOUT_CHAT_REPLY,
        TIMEOUT_FAST,
    )


def _err_str(exc: BaseException) -> str:
    """Unwrap ActivityError cause chain to get the original message."""
    cause = exc.__cause__ or exc
    return str(cause)[:200]


@dataclass
class AgentChatReplyInput:
    """Construction guard for the spawn payload.

    `task_id` is None on the bot DM path — Todoist mirror and error-comment
    steps are skipped. The active comms adapter (Slack) routes the reply by the
    agent's channel, so there is no chat override.
    """

    target_agent: str
    synthetic_user_message: str
    thread_id: str
    task_id: str | None = None


@workflow.defn(name="AgentChatReplyFlow")
class AgentChatReplyFlow:
    @workflow.run
    async def run(self, inp: AgentChatReplyInput) -> dict:
        workflow.logger.info(
            "agent_chat_reply_flow_starting target_agent=%s task_id=%s dm=%s",
            inp.target_agent,
            inp.task_id,
            inp.task_id is None,
        )
        is_taskless = inp.task_id is None

        # Step 1 — synthesize the reply. Smart-tier agents (pandoras-actor on
        # claude-sonnet) routinely take 3-6 min when invoking heavy tools
        # (remote_script kimi SSH, deep KS search). Use TIMEOUT_CHAT_REPLY
        # (600s) to match the Telegram chat path (PR #248). RETRY_ONCE
        # instead of STANDARD: a single retry covers transient 5xx without
        # compounding the LLM cost on legitimate slow runs.
        try:
            synth = await workflow.execute_activity_method(
                ChatActivities.synthesize_reply,
                args=[
                    inp.target_agent,
                    inp.synthetic_user_message,
                    inp.thread_id,
                    inp.task_id,
                ],
                start_to_close_timeout=TIMEOUT_CHAT_REPLY,
                retry_policy=RETRY_ONCE,
            )
        except Exception as exc:  # noqa: BLE001
            err_msg = _err_str(exc)
            workflow.logger.warning(
                "agent_chat_reply_synthesize_failed task_id=%s err=%s",
                inp.task_id,
                err_msg,
            )
            if not is_taskless:
                await workflow.execute_activity_method(
                    ClarifyActivities.post_agent_reply_error_comment,
                    args=[inp.task_id, inp.target_agent, f"synthesize failed: {err_msg[:120]}"],
                    start_to_close_timeout=TIMEOUT_FAST,
                    retry_policy=NO_RETRY,
                )
            return {
                "status": "error",
                "reason": err_msg,
                "telegram_message_id": None,
                "agent_id": inp.target_agent,
            }

        # Permanent error in synth (agent-not-found / refusal) → compose
        # apology and deliver via Telegram + Todoist (if task is present).
        if synth.get("error"):
            apology_prefix = (
                "I couldn't reply on the Todoist task"
                if not is_taskless
                else "I couldn't reply"
            )
            reply_text = f"{apology_prefix}: {synth['error']}"
            tool_trace_summary = ""
            permanent_error = True
        else:
            reply_text = synth.get("reply_text") or ""
            tool_trace_summary = synth.get("tool_trace_summary") or ""
            permanent_error = False

        # Step 2 — deliver the reply. Failure here degrades to
        # telegram_message_id=None but DOES NOT short-circuit Step 3. The
        # active comms adapter (Slack) routes by the agent's channel.
        telegram_message_id: int | None = None
        telegram_failed = False
        try:
            tg = await workflow.execute_activity_method(
                DeliveryActivities.send_telegram,
                args=[inp.target_agent, reply_text],
                start_to_close_timeout=TIMEOUT_FAST,
                retry_policy=STANDARD,
            )
            telegram_message_id = (tg or {}).get("message_id")
        except Exception as exc:  # noqa: BLE001
            workflow.logger.warning(
                "agent_chat_reply_telegram_failed task_id=%s err=%s",
                inp.task_id,
                _err_str(exc),
            )
            telegram_failed = True

        # Step 3 — Todoist comment mirror. SKIPPED on the DM (taskless) path.
        # Failure raises (post itself uses outbox for retryable; permanent
        # rejections raise).
        if not is_taskless:
            try:
                await workflow.execute_activity_method(
                    ClarifyActivities.post_agent_reply_comment,
                    args=[
                        inp.task_id,
                        inp.target_agent,
                        reply_text,
                        tool_trace_summary,
                        telegram_message_id,
                    ],
                    start_to_close_timeout=TIMEOUT_FAST,
                    retry_policy=NO_RETRY,
                )
            except Exception as exc:  # noqa: BLE001
                workflow.logger.warning(
                    "agent_chat_reply_todoist_post_failed task_id=%s err=%s",
                    inp.task_id,
                    _err_str(exc),
                )
                # Re-raise to record workflow failure. post_agent_reply_comment
                # only raises on actual exceptions (rejections return posted=False
                # which is not exceptional).
                raise

        status = "error" if permanent_error else ("degraded" if telegram_failed else "ok")
        # Permanent-error replies carry the upstream synth error as the
        # `reason`; happy + degraded paths leave it None so the shape is
        # uniform across all return points.
        reason: str | None = None
        if permanent_error:
            reason = synth.get("error")
        return {
            "status": status,
            "reason": reason,
            "telegram_message_id": telegram_message_id,
            "agent_id": inp.target_agent,
        }
