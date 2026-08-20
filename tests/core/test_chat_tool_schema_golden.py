"""Golden-schema regression gate for the chat tool surface (issue #316).

`services/chat.py` exposes 52 hand-written OpenAI-style tool schemas
(`CHAT_TOOLS`) and their dispatch table (`TOOL_EXECUTORS`). Issue #316 is
migrating those executors, domain by domain, onto the `@aegis_tool` decorator
(`services/tools/registry.py`), which GENERATES the schema from a typed
signature + docstring instead of a hand-written dict. That migration is only
safe if the generated schema is byte-for-byte identical to what it replaces —
a description that loses a sentence, an enum that drops a value, or a
`default` that goes missing would silently change what the LLM is told a tool
does, with no test failing.

`fixtures/chat_tools_golden.json` is a full dump of `CHAT_TOOLS` as of the
last point the whole file was still hand-written. This test locks the live
schema to that snapshot, per tool, so a diff during the migration fails
loudly and names the offending tool instead of surfacing as a production
behavior change.

ONE rule for updating the fixture: a *deliberate* change to a tool's schema
(new parameter, reworded description, adjusted enum, etc.) regenerates the
fixture in the same PR that makes the change. A refactor — moving an executor
into `services/tools/`, switching it to `@aegis_tool`, reordering
`CHAT_TOOLS` — must reproduce the fixture's schemas exactly and never touches
this file.
"""

from __future__ import annotations

import json
from pathlib import Path

from aegis.services.chat import CHAT_TOOLS, TOOL_EXECUTORS

GOLDEN_PATH = Path(__file__).parent / "fixtures" / "chat_tools_golden.json"


def _load_golden() -> dict[str, dict]:
    schemas = json.loads(GOLDEN_PATH.read_text())
    return {schema["function"]["name"]: schema for schema in schemas}


def _live_schemas() -> dict[str, dict]:
    return {schema["function"]["name"]: schema for schema in CHAT_TOOLS}


def test_tool_names_match_golden_snapshot():
    """A tool silently added or dropped from CHAT_TOOLS fails here by name."""
    golden_names = set(_load_golden())
    live_names = set(_live_schemas())

    added = live_names - golden_names
    removed = golden_names - live_names
    assert not added and not removed, (
        f"CHAT_TOOLS drifted from the golden snapshot ({GOLDEN_PATH.name}): "
        f"added={sorted(added)} removed={sorted(removed)}. "
        "If this is a deliberate tool-surface change, regenerate the fixture "
        "in this PR; if it's a refactor, the tool list must not move."
    )


def test_every_tool_schema_matches_golden_snapshot():
    """Per-tool comparison so a failure names the offending tool, not a blob."""
    golden = _load_golden()
    live = _live_schemas()

    # Name drift is covered (with a clearer message) by the test above; here
    # we only compare tools present in both, one assertion per tool.
    shared_names = sorted(set(golden) & set(live))
    for name in shared_names:
        assert live[name] == golden[name], (
            f"tool '{name}' schema drifted from the golden snapshot "
            f"({GOLDEN_PATH.name}).\n"
            f"golden: {json.dumps(golden[name], indent=2, sort_keys=True)}\n"
            f"live:   {json.dumps(live[name], indent=2, sort_keys=True)}"
        )


def test_every_chat_tool_has_an_executor_and_vice_versa():
    """A schema with no executor can never be dispatched; an executor with no
    schema can never be advertised to the LLM — both are dead code paths."""
    schema_names = {t["function"]["name"] for t in CHAT_TOOLS}
    executor_names = set(TOOL_EXECUTORS)

    schema_only = schema_names - executor_names
    executor_only = executor_names - schema_names
    assert not schema_only and not executor_only, (
        f"CHAT_TOOLS/TOOL_EXECUTORS mismatch: "
        f"schema_without_executor={sorted(schema_only)} "
        f"executor_without_schema={sorted(executor_only)}"
    )
