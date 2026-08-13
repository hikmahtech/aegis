"""ChatActivities — worker-side wrappers around core's chat surface.

Currently only synthesize_reply (used by AgentChatReplyFlow). Lives in
worker so workflows can invoke it via execute_activity_method.
"""

from __future__ import annotations

from dataclasses import dataclass

from temporalio import activity

from aegis_worker.activities.core_client import CoreClient


@dataclass
class ChatActivities:
    """Activity holder. ``client`` is wired in worker boot."""

    client: CoreClient

    @activity.defn
    async def synthesize_reply(
        self,
        agent_id: str,
        message: str,
        thread_id: str,
        task_id: str | None,
    ) -> dict:
        """Call core /api/chat/agent-reply and return its body verbatim.

        Raises httpx.HTTPStatusError on 5xx. AgentChatReplyFlow runs this on
        NO_RETRY since PR #283 — the call is not idempotent (the tool loop
        behind it can complete a task, open a PR, capture to the inbox), so a
        retry duplicates real-world actions rather than just LLM spend, and the
        flow degrades visibly instead. 200 OK with an ``error`` body field
        signals a PERMANENT failure (agent-not-found or LLM refusal) —
        caller composes apology.

        ``task_id`` may be None on the DM (taskless) path — surface tag in
        user_metadata switches from `todoist_comment` to `chat_dm` on
        the core side.
        """
        resp = await self.client.post(
            "/api/chat/agent-reply",
            json={
                "agent_id": agent_id,
                "message": message,
                "thread_id": thread_id,
                "task_id": task_id,
            },
        )
        resp.raise_for_status()
        return resp.json()
