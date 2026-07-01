#!/usr/bin/env python3
"""parse_claude_stream.py — Parse Claude Code JSONL stream output and POST results to Core API.

Usage:
    parse_claude_stream.py <stream_file> <run_id> <core_url> <api_key> <status>

The <status> argument is the final run status determined by run_claude.sh (e.g. "succeeded"
or "failed"). The parser never overrides it — it only enriches with metrics.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from typing import Any

# ---------------------------------------------------------------------------
# Core parsing
# ---------------------------------------------------------------------------


def parse_stream_file(path: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Read a JSONL stream file and extract metrics + conversation events.

    Returns:
        (metrics_dict, events_list)
        metrics_dict — fields from the ``result`` message (empty if absent)
        events_list  — list of conversation turn dicts ordered by sequence
    """
    metrics: dict[str, Any] = {}
    events: list[dict[str, Any]] = []

    seq = 0

    def _extract_message(msg_type: str, message: dict) -> None:
        """Extract a complete message (assistant/user) into events."""
        nonlocal seq

        role = message.get("role", msg_type)
        content_blocks = message.get("content", [])
        usage = message.get("usage", {})
        tokens = usage.get("output_tokens")

        text_parts: list[str] = []
        tool_calls: list[dict] = []

        for block in content_blocks:
            btype = block.get("type", "")
            if btype == "text":
                text_parts.append(block.get("text", ""))
            elif btype == "tool_use":
                tool_input = json.dumps(block.get("input", {}))[:2000]
                tool_calls.append({"tool": block.get("name", "unknown"), "input": tool_input})
            elif btype == "tool_result":
                # Tool results appear in user turns — capture content summary
                result_content = block.get("content", "")
                if isinstance(result_content, list):
                    result_content = " ".join(
                        p.get("text", "") for p in result_content if isinstance(p, dict)
                    )
                text_parts.append(str(result_content)[:2000])

        content = "\n".join(text_parts)
        events.append(
            {
                "seq": seq,
                "role": role,
                "content": content,
                "tool_calls": tool_calls if tool_calls else None,
                "tokens": tokens,
            }
        )
        seq += 1

    # SSE delta assembly state (for non-verbose streaming format)
    sse_role: str | None = None
    sse_text: str = ""
    sse_tool_calls: list[dict] = []
    sse_tokens: int | None = None
    sse_tool_name: str | None = None
    sse_tool_input: str = ""

    def _flush_sse() -> None:
        """Push accumulated SSE delta message onto events."""
        nonlocal seq, sse_role, sse_text, sse_tool_calls, sse_tokens
        nonlocal sse_tool_name, sse_tool_input
        if sse_role is None:
            return
        events.append(
            {
                "seq": seq,
                "role": sse_role,
                "content": sse_text,
                "tool_calls": sse_tool_calls if sse_tool_calls else None,
                "tokens": sse_tokens,
            }
        )
        seq += 1
        sse_role = None
        sse_text = ""
        sse_tool_calls = []
        sse_tokens = None
        sse_tool_name = None
        sse_tool_input = ""

    try:
        with open(path, encoding="utf-8") as fh:
            for raw_line in fh:
                raw_line = raw_line.strip()
                if not raw_line:
                    continue

                try:
                    msg = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue

                msg_type = msg.get("type", "")

                # -- Complete message format (--verbose output) --
                if msg_type in ("assistant", "user"):
                    message = msg.get("message", {})
                    if message:
                        _extract_message(msg_type, message)

                # -- SSE delta format (non-verbose streaming) --
                elif msg_type == "message_start":
                    if sse_role is not None:
                        _flush_sse()
                    message = msg.get("message", {})
                    sse_role = message.get("role", "assistant")

                elif msg_type == "content_block_start":
                    block = msg.get("content_block", {})
                    if block.get("type") == "tool_use":
                        sse_tool_name = block.get("name", "unknown")
                        sse_tool_input = ""

                elif msg_type == "content_block_delta":
                    delta = msg.get("delta", {})
                    delta_type = delta.get("type", "")
                    if delta_type == "text_delta":
                        sse_text += delta.get("text", "")
                    elif delta_type == "input_json_delta":
                        sse_tool_input += delta.get("partial_json", "")

                elif msg_type == "content_block_stop":
                    if sse_tool_name is not None:
                        sse_tool_calls.append(
                            {"tool": sse_tool_name, "input": sse_tool_input[:2000]}
                        )
                        sse_tool_name = None
                        sse_tool_input = ""

                elif msg_type == "message_delta":
                    usage = msg.get("usage", {})
                    output_tokens = usage.get("output_tokens")
                    if output_tokens is not None:
                        sse_tokens = output_tokens

                elif msg_type == "message_stop":
                    _flush_sse()

                elif msg_type == "result":
                    # Flush any pending SSE message
                    if sse_role is not None:
                        _flush_sse()

                    metrics["session_id"] = msg.get("session_id")
                    metrics["cost_usd"] = msg.get("total_cost_usd")
                    metrics["duration_ms"] = msg.get("duration_ms")
                    metrics["num_turns"] = msg.get("num_turns")

                    usage = msg.get("usage", {})
                    metrics["input_tokens"] = usage.get("input_tokens")
                    metrics["output_tokens"] = usage.get("output_tokens")
                    metrics["cache_read_tokens"] = usage.get("cache_read_input_tokens")
                    metrics["cache_write_tokens"] = usage.get("cache_creation_input_tokens")

                    metrics = {k: v for k, v in metrics.items() if v is not None}

    except OSError as exc:
        print(f"[parse_claude_stream] Error reading {path}: {exc}", file=sys.stderr)

    return metrics, events


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _post_json(url: str, api_key: str, payload: dict) -> None:
    """POST JSON payload to url with X-API-Key auth header. Best-effort — never raises."""
    try:
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-API-Key": api_key,
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            _ = resp.read()
    except Exception as exc:  # noqa: BLE001
        print(f"[parse_claude_stream] POST to {url} failed: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    if len(sys.argv) != 6:
        print(
            "Usage: parse_claude_stream.py <stream_file> <run_id> <core_url> <api_key> <status>",
            file=sys.stderr,
        )
        sys.exit(1)

    stream_file, run_id, core_url, api_key, status = sys.argv[1:]

    metrics, events = parse_stream_file(stream_file)

    # POST metrics update — merge status from CLI arg so parser never overrides it
    update_payload: dict[str, Any] = {"run_id": run_id, "status": status}
    update_payload.update(metrics)

    _post_json(f"{core_url}/api/admin/claude-runs/update", api_key, update_payload)

    # POST conversation events if any were extracted
    if events:
        events_payload = {"run_id": run_id, "events": events}
        _post_json(f"{core_url}/api/admin/claude-runs/events", api_key, events_payload)


if __name__ == "__main__":
    main()
