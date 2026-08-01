"""Shared types for the chat tool executors.

`ToolContext` lives here rather than in `services/chat.py` so a domain module
can annotate against it without importing `chat`, which imports the domain
modules back (that would be a cycle). `chat.py` re-exports it, so
`from aegis.services.chat import ToolContext` is unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class ToolContext:
    """Context passed to tool executor functions."""

    agent_id: str | None = None
    task_id: str | None = None
    knowledge_connector: Any | None = None
    finance_connector: Any | None = None
    chat_context: dict | None = None
    settings: Any = None
    temporal_client: Any = None
    search_connector: Any | None = None
    llm_client: Any | None = None
    remote_script_connector: Any | None = None
    vercel_connector: Any | None = None
    mcp_manager: Any | None = None
    model_light: str = "gemma4:e2b"
