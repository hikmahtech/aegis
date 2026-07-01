"""Personality module: shared helpers for agent voice in flow-emitted
Slack messages and Todoist comments.

The full personality (SOUL.md, AGENTS.md, USER.md) is loaded into LLM
system prompts by `core.services.chat._build_personality_system_prompt`.
This module is for the small structured strings the flows emit
themselves — agent-flavoured one-liners — so the character voice
carries through even when no LLM is in the loop.
"""

from aegis.personalities.voice import voice_line

__all__ = ["voice_line"]
