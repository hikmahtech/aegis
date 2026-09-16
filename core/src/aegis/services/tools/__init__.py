"""Domain-scoped chat tool executors.

`services/chat.py` keeps the chat loop, the `CHAT_TOOLS` list and the single
`TOOL_EXECUTORS` registry; the executor implementations live here, one module
per domain, each decorated with `@aegis_tool` so its advertised schema is
GENERATED from its typed signature and docstring (`registry.py`) rather than
hand-written beside it. `chat.py` imports each executor back under its original
name, so `from aegis.services.chat import _exec_*` keeps working.
"""
