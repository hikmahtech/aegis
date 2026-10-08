"""Reading a coding-CLI run's output (kimi or claude, `--output-format stream-json`).

Shared by the agent-run lane (`activities/agent_run.py`). These helpers lived in
the alert investigation activities until the infra lane moved to the DevOps
vertical (a2-devops); the coding lane still reads its runs with them.
"""

from __future__ import annotations

import json

# Cap on a run's transcript kept in an activity's return value.
_INVESTIGATION_OUTPUT_CAP = 8 * 1024


def _iter_kimi_assistant_text(raw: str):
    """Yield decoded text content from each assistant message in a coding-CLI
    stream-json output (kimi or claude).

    Both engines run in `--output-format stream-json`, so each non-empty line
    is a JSON event. Pre-0.31 kimi assistant turns were flat, content a list
    of typed blocks:

        {"role":"assistant","content":[
          {"type":"think","text":"..."},
          {"type":"text","text":"...STATUS: scoped"}
        ]}

    kimi CLI 0.31.x (issue #271) flattens `content` further, to a plain
    string with no block wrapper:

        {"role":"assistant","content":"...STATUS: scoped","tool_calls":[...]}

    Claude wraps the pre-0.31-kimi shape one level down under "message":

        {"type":"assistant","message":{"role":"assistant","content":[
          {"type":"text","text":"...STATUS: scoped"}
        ]}}

    The STATUS/BRANCH lines the agent promises to emit live INSIDE one of
    those `text` fields (or directly in the 0.31.x string). Searching the raw
    file with a multiline regex misses them because the `\\n` between log
    content and `STATUS:` is a JSON escape, not a real newline. Decoding via
    json.loads restores the real newlines, so regexes that key on `^STATUS:`
    (multiline) match again.

    Non-JSON lines (e.g. kimi's trailing "To resume this session: kimi -r
    <id>") and non-assistant events (role=tool, role=user, plain session
    init events, claude system/result events) are skipped — we only care
    about the agent's own assertions.
    """
    if not raw:
        return
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            evt = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if evt.get("role") != "assistant":
            # claude shape: assistant payload nested under "message"
            msg = evt.get("message")
            if not (isinstance(msg, dict) and msg.get("role") == "assistant"):
                continue
            evt = msg
        content = evt.get("content")
        if isinstance(content, str):
            # kimi 0.31.x: content is the flat text itself, not a block list.
            if content:
                yield content
            continue
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            text = block.get("text")
            if isinstance(text, str) and text:
                yield text


def _extract_kimi_transcript(raw: str) -> str:
    """Return a human-readable transcript of a kimi stream-json run.

    Concatenates every assistant-text block in order with blank-line
    separators. Falls back to the raw input when the file isn't
    stream-json (e.g. plain-text test fixtures). Skips JSON wrapping
    so the resulting text is greppable / pasteable.

    The fallback keeps only lines that are NOT JSON events, so stream-json
    with no recoverable assistant text yields "" instead of raw bytes. That
    bare `return raw` used to leak truncated JSON into the assessor prompt —
    `_exec` caps stdout to the LAST 32KB, so a tool-heavy run's tail holds
    tool results and no complete assistant event. That noise is why every
    kimi verdict in prod came back confidence=0.0 with `{"role":"tool"…`
    sitting in its root_cause.
    """
    if not raw:
        return ""
    chunks = list(_iter_kimi_assistant_text(raw))
    if chunks:
        return "\n\n".join(c.strip() for c in chunks if c.strip())
    # Plain-text (or hybrid) output: kimi emits a `{"session_id": …}` header
    # then prose. Keep the prose, drop every JSON event line.
    prose: list[str] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            prose.append(stripped)
    return "\n".join(prose)
