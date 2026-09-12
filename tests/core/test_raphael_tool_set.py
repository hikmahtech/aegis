"""Raphael's seed tool set (#514, from the audit).

Two places seed an agent's tools: `config/seed/agents.yaml` and
`chat.AGENT_TOOL_SETS`. The notes tools were added to the first and not the
second, so a Raphael seeded from the code default had no vault tools. (A
running deployment's DB `metadata.tool_set` wins over both.)"""

from __future__ import annotations

from pathlib import Path

import yaml
from aegis.services.chat import AGENT_TOOL_SETS

SEED = Path(__file__).resolve().parents[2] / "config" / "seed" / "agents.yaml"


def _seeded_tools(agent_id: str) -> set[str]:
    data = yaml.safe_load(SEED.read_text("utf-8"))
    agents = data.get("agents", data) if isinstance(data, dict) else data
    agent = next(a for a in agents if a.get("id") == agent_id)
    return set((agent.get("metadata") or {}).get("tool_set") or agent.get("tool_set") or [])


def test_raphaels_code_default_is_his_seed_tool_set():
    assert AGENT_TOOL_SETS["raphael"] == _seeded_tools("raphael")


def test_raphael_has_the_vault_tools():
    assert {"note_search", "note_read", "note_write", "note_link"} <= AGENT_TOOL_SETS["raphael"]
