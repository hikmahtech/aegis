"""Shared types and result-shaping helpers for the chat tool executors.

`ToolContext` lives here rather than in `services/chat.py` so a domain module
can annotate against it without importing `chat`, which imports the domain
modules back (that would be a cycle). `chat.py` re-exports it, so
`from aegis.services.chat import ToolContext` is unchanged.

The truncation helpers (`_truncate_result` and friends) also live here: domain
executors (social, mcp), the chat loop, and `routes/mcp_server.py` all need
them, and this module is the lowest rung of the dependency ladder. `chat.py`
re-exports them under their original names for backwards compatibility.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID


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


def _json_default(value: Any) -> Any:
    """Type-specific JSON encoder for tool result payloads.

    Coerces the common DB/HTTP types the LLM sees into shapes it understands:
    Decimal -> float, datetime/date -> ISO string, UUID -> str. Everything
    else falls back to ``str()`` so we never raise during serialization.
    """
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    return str(value)


# Passes used to progressively shrink a tool result that exceeds the byte
# budget. Each tuple is (n_items_to_keep, max_string_length). We try the
# loosest pass first (keep a lot, leave strings long) and tighten on each
# attempt, so results stay as useful as possible. Only when even the
# tightest pass overflows do we fall through to the minimal summary.
_SHRINK_PASSES: tuple[tuple[int, int], ...] = (
    (5, 1000),
    (5, 500),
    (3, 500),
    (3, 250),
    (1, 500),
    (1, 150),
)

# How many dropped key names to spell out before folding the tail into a single
# `… +K more` entry.
_MAX_LISTED_DROPPED_KEYS = 12

# The one marker for prose the model reads as prose. `_shrink_strings` appends it
# inside an over-long JSON string field; `_truncate_text` appends it to a whole
# non-JSON result. Shared deliberately (#239): "was this cut?" must have exactly
# one thing to look for, not a third spelling per code path.
_TRUNCATION_MARKER = "… [truncated]"


def _shrink_strings(obj, max_str_len: int, n_items: int):
    """Recursively shrink over-long strings AND cap nested lists at ``n_items``
    entries, replacing the dropped tail with one in-band ``… +K of N more``
    element. Leaves other types untouched.

    Capping nested lists is what makes an over-budget dict shrinkable at all.
    Before #148 the only lever was dropping whole keys, so a result whose
    payload sat under a non-leading key lost that payload outright, and a
    result whose payload WAS leading couldn't be shrunk enough to fit and fell
    through to the "too large to display" stub. Keeping the dropped count
    inside the list it describes means the same mechanism that removed the
    items can never separate them from their own count.
    """
    if isinstance(obj, str):
        if len(obj) > max_str_len:
            # Leave room for the marker so the caller can tell a cut happened.
            head = max(0, max_str_len - len(_TRUNCATION_MARKER))
            return obj[:head] + _TRUNCATION_MARKER
        return obj
    if isinstance(obj, list):
        kept = [_shrink_strings(x, max_str_len, n_items) for x in obj[:n_items]]
        if len(obj) > n_items:
            kept.append(f"… +{len(obj) - n_items} of {len(obj)} more")
        return kept
    if isinstance(obj, dict):
        return {k: _shrink_strings(v, max_str_len, n_items) for k, v in obj.items()}
    return obj


def _payload_rank(value) -> int:
    """Rank a dict value by how likely it is to BE the answer: a list of
    records outranks a nested object, which outranks a scalar.

    ``sorted`` is stable, so position only breaks ties within a rank — an
    executor that deliberately leads with its payload (``social_timeline``,
    ``list_social_channels``) still wins, while one that leads with a scalar
    ``count``/``ok``/``window_hours`` no longer loses its data to it (#148).
    """
    if isinstance(value, list):
        return 0
    if isinstance(value, dict):
        return 1
    return 2


def _smart_subset(data, n_items: int, max_str_len: int):
    """Keep the first ``n_items`` of a list, or the ``n_items`` highest-ranked
    keys of a dict, shrinking long strings and capping nested lists inside the
    kept portion. Returns the trimmed object plus truncation metadata so the
    LLM knows content was dropped — and, for a dict, WHICH keys were dropped,
    so it can ask a narrower question instead of blind-retrying the same call.
    """
    if isinstance(data, list):
        return {
            "results": [_shrink_strings(item, max_str_len, n_items) for item in data[:n_items]],
            "truncated": True,
            "total": len(data),
        }
    keep = set(sorted(data, key=lambda k: _payload_rank(data[k]))[:n_items])
    subset = {k: _shrink_strings(v, max_str_len, n_items) for k, v in data.items() if k in keep}
    dropped = [k for k in data if k not in keep]
    # Assigned AFTER the selection above, so the signal is never itself a
    # candidate for eviction by the pass that produced it.
    subset["_truncated"] = True
    subset["_total_keys"] = len(data)
    if dropped:
        # Bounded like the `+K more` aggregates in `list_social_channels` and
        # the `social_timeline` roll-up, so naming the casualties can never be
        # what pushes a pass over budget and forces the minimal stub.
        listed = [str(k)[:60] for k in dropped[:_MAX_LISTED_DROPPED_KEYS]]
        if len(dropped) > _MAX_LISTED_DROPPED_KEYS:
            listed.append(f"… +{len(dropped) - _MAX_LISTED_DROPPED_KEYS} more")
        subset["_dropped_keys"] = listed
    return subset


def _truncate_text(text: str, max_bytes: int) -> str:
    """Cut a non-JSON tool result down to ``max_bytes`` and say that it was cut.

    Two things the old ``text[:max_bytes]`` got wrong (#239). It counted
    CHARACTERS against a byte budget, so a non-ASCII result came back over
    budget — up to 4x for multi-byte UTF-8. And it appended nothing, so the
    model received a string ending mid-sentence with no signal it was partial,
    which is the same silent-partial failure the dict and list paths were fixed
    for in #148. This is a live path, not a theoretical one: several executors
    return plain text rather than JSON (``query_observations``,
    ``last_contact_with_person``, the refusal strings).

    Slicing encoded bytes can land inside a multi-byte sequence, so the decode
    drops that trailing partial character instead of raising.
    """
    budget = max_bytes - len(_TRUNCATION_MARKER.encode())
    if budget <= 0:
        # Budget too tight to hold the marker at all — a byte-safe cut is all
        # that fits. Still never over budget, which the character slice could be.
        return text.encode()[:max_bytes].decode(errors="ignore")
    return text.encode()[:budget].decode(errors="ignore") + _TRUNCATION_MARKER


def _truncate_result(result_json: str, max_bytes: int = 4096) -> str:
    """Trim a JSON-serialised tool result so it fits within ``max_bytes``.

    Strategy, loosest → tightest:
    1. Return unchanged if already under budget.
    2. Parse. Non-JSON or scalar → byte-safe cut plus a ``… [truncated]`` marker
       (``_truncate_text``).
    3. For a list/dict, iterate ``_SHRINK_PASSES``: keep the first N items of
       a list or the N highest-ranked keys of a dict (see ``_payload_rank``),
       recursively shrinking long strings and capping nested lists inside the
       kept portion. Return the first pass whose serialisation fits.
    4. Fallback: minimal summary noting the total count.

    This is better than a byte-level slice because the LLM still sees
    structured data with sample content; it's better than the prior
    slice-then-give-up approach because deeply nested records (common for
    knowledge/search tool output) get their big string fields shrunk down
    rather than being replaced entirely by a "too large" note.

    EVERY path out of here carries a truncation signal — ``… [truncated]`` on a
    non-JSON cut, ``truncated``/``total`` for a list, ``_truncated``/
    ``_total_keys`` (plus ``_dropped_keys``) for a dict, ``… +K of N more``
    inside any capped nested list — so a partial result is never mistakable for
    a complete one. Every return is also measured in BYTES, matching the budget's
    own unit.
    """
    if len(result_json.encode()) <= max_bytes:
        return result_json

    try:
        data = json.loads(result_json)
    except (json.JSONDecodeError, TypeError):
        return _truncate_text(result_json, max_bytes)

    if not isinstance(data, (list, dict)):
        return _truncate_text(result_json, max_bytes)

    for n_items, max_str_len in _SHRINK_PASSES:
        candidate = _smart_subset(data, n_items, max_str_len)
        encoded = json.dumps(candidate, default=str)
        if len(encoded.encode()) <= max_bytes:
            return encoded

    # Even a single item with very short strings overflows — emit a minimal
    # summary. Keeps the tool result valid JSON and preserves the count.
    if isinstance(data, list):
        return json.dumps(
            {"truncated": True, "total": len(data), "note": "Results too large to display"}
        )
    return json.dumps(
        {"_truncated": True, "_total_keys": len(data), "note": "Results too large to display"}
    )
