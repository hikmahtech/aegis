"""The vault chat tools and note ranking (#514)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aegis.services import chat
from aegis.services import notes_write as nw
from aegis.services.source_types import get_rank_boost
from aegis.services.tools.base import ToolContext
from aegis.services.tools.notes import (
    _exec_note_link,
    _exec_note_read,
    _exec_note_search,
    _exec_note_write,
)


class _Handle:
    def __init__(self, result):
        self._result = result

    async def result(self):
        return self._result


class _Client:
    def __init__(self, result):
        self.started: list = []
        self._result = result

    async def start_workflow(self, name, arg, *, id, task_queue):
        self.started.append({"name": name, "arg": arg, "id": id, "task_queue": task_queue})
        return _Handle(self._result)

    def get_workflow_handle(self, workflow_id):  # pragma: no cover — not reached here
        return _Handle(self._result)


def _configured(tmp_path):
    (tmp_path / "notes_deploy_key").write_text("k\n")
    return SimpleNamespace(
        gmail_token_dir=str(tmp_path), notes_repo_url="git@x:y.git", notes_path=str(tmp_path / "c")
    )


async def test_note_search_searches_notes_only_and_returns_paths():
    kc = AsyncMock()
    kc.search = AsyncMock(
        return_value=[
            {
                "url": "vault://journal/12 Sep 26.md",
                "title": "12 Sep 26",
                "similarity": 0.81234,
                "content": "Standup with Zara.",
            }
        ]
    )
    out = json.loads(
        await _exec_note_search(None, {"query": "zara"}, ToolContext(knowledge_connector=kc))
    )
    assert kc.search.await_args.kwargs["source_type"] == "note"
    assert out["notes"] == [
        {
            "path": "journal/12 Sep 26.md",
            "title": "12 Sep 26",
            "similarity": 0.812,
            "snippet": "Standup with Zara.",
        }
    ]


async def test_note_read_says_when_the_vault_is_off():
    out = json.loads(
        await _exec_note_read(
            None, {"path": "journal/12 Sep 26.md"}, ToolContext(settings=SimpleNamespace())
        )
    )
    assert "not configured" in out["error"]


async def test_note_write_when_the_vault_is_off_dispatches_nothing():
    client = _Client({"message": "x"})
    ctx = ToolContext(settings=SimpleNamespace(), temporal_client=client)
    out = await _exec_note_write(None, {"path": "topics/rag", "text": "t"}, ctx)
    assert out.startswith("error: the vault is not configured")
    assert client.started == []


async def test_note_write_hands_the_normalised_write_to_the_flow(tmp_path):
    client = _Client({"message": "wrote to raphael/topics/rag.md"})
    ctx = ToolContext(settings=_configured(tmp_path), temporal_client=client, agent_id="raphael")
    out = await _exec_note_write(
        None, {"path": "topics/rag", "text": "RAG notes", "heading": "Sources"}, ctx
    )
    assert out == "wrote to raphael/topics/rag.md"
    started = client.started[0]
    payload = {
        "path": "raphael/topics/rag.md",
        "text": "RAG notes",
        "heading": "Sources",
        "title": "",
    }
    assert started["name"] == "NotesWriteFlow" and started["task_queue"] == "aegis-main"
    assert started["arg"]["payload"] == payload and started["arg"]["op"] == "write"
    assert started["id"] == nw.write_workflow_id("write", payload)


async def test_note_write_refuses_a_path_outside_raphael(tmp_path):
    client = _Client({"message": "x"})
    ctx = ToolContext(settings=_configured(tmp_path), temporal_client=client)
    out = await _exec_note_write(None, {"path": "../journal/12 Sep 26", "text": "t"}, ctx)
    assert out.startswith("error:") and "Nothing was written" in out
    assert client.started == []


async def test_note_link_dispatches_a_link(tmp_path):
    client = _Client({"message": "wrote to raphael/topics/rag.md"})
    ctx = ToolContext(settings=_configured(tmp_path), temporal_client=client)
    await _exec_note_link(
        None, {"path": "raphael/topics/rag", "target": "Designing Data-Intensive Apps"}, ctx
    )
    assert client.started[0]["arg"]["op"] == "link"
    assert client.started[0]["arg"]["payload"]["target"] == "Designing Data-Intensive Apps"


def test_the_four_tools_are_registered_and_the_writers_are_not_read_only():
    from aegis.api.routes import mcp_server as mcp

    names = {t["function"]["name"] for t in chat.CHAT_TOOLS}
    for tool in ("note_search", "note_read", "note_write", "note_link"):
        assert tool in names and tool in chat.TOOL_EXECUTORS
    assert {"note_search", "note_read"} <= mcp._READ_ONLY_TOOLS
    assert not {"note_write", "note_link"} & mcp._READ_ONLY_TOOLS


def test_notes_rank_above_raw_documents():
    assert get_rank_boost("note") == 1.25
    assert get_rank_boost("article") == 1.0
    assert get_rank_boost("no-such-type") == 1.0
    items = chat._apply_knowledge_decay(
        [
            {"source_type": "article", "similarity": 0.8},
            {"source_type": "note", "similarity": 0.7},
        ]
    )
    assert items[1]["effective_score"] > items[0]["effective_score"]
    assert items[0]["effective_score"] == 0.8
