"""Domain-scoped chat tool executors.

`services/chat.py` keeps the chat loop, the `CHAT_TOOLS` schemas and the single
`TOOL_EXECUTORS` registry; the executor implementations live here, one module
per domain. Splitting is a pure move — tool names, executor identities and
behaviour are unchanged, and `chat.py` imports each executor back under its
original name so `from aegis.services.chat import _exec_*` keeps working.
"""
