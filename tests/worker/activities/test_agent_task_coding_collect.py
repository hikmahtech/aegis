"""collect_coding_run — poll a coding run to completion and extract its transcript."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest
from aegis_worker.activities.agent_task import AgentTaskActivities


def _assistant(text: str) -> str:
    return json.dumps({"role": "assistant", "content": [{"type": "text", "text": text}]})


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """The poll loop sleeps 30s between attempts; every comparable test in this
    repo mocks it (test_alert_investigation.py:305, test_clarify_activities.py:2978).
    `collect_coding_run` does a local `import asyncio`, so patch the global
    attribute rather than a module-qualified path."""
    monkeypatch.setattr("asyncio.sleep", AsyncMock())


class _Remote:
    def __init__(self, responses: list[str | None]):
        self._responses = responses
        self.calls = 0

    async def fetch_kimi_run_output(self, output_file: str, host: str = "") -> str | None:
        value = self._responses[min(self.calls, len(self._responses) - 1)]
        self.calls += 1
        return value


async def test_returns_transcript_once_status_footer_appears():
    partial = _assistant("still reading files")
    final = partial + "\n" + _assistant("Plan: fix the parser\nSTATUS: scoped")
    act = AgentTaskActivities(db_pool=None, remote_script=_Remote([partial, final]))

    result = await act.collect_coding_run("/tmp/run.jsonl", "node-a", max_polls=5)
    assert result["status"] == "succeeded"
    assert "Plan: fix the parser" in result["transcript"]
    # Tool-result noise must never reach the transcript (issue fixed in #150).
    assert '"role"' not in result["transcript"]


async def test_times_out_without_status_footer():
    act = AgentTaskActivities(
        db_pool=None, remote_script=_Remote([_assistant("thinking")])
    )
    result = await act.collect_coding_run("/tmp/run.jsonl", "node-a", max_polls=2)
    assert result["status"] == "timed_out"


async def test_empty_output_is_failed_not_a_silent_success():
    act = AgentTaskActivities(db_pool=None, remote_script=_Remote([None]))
    result = await act.collect_coding_run("/tmp/run.jsonl", "node-a", max_polls=2)
    assert result["status"] in {"timed_out", "failed"}
    assert result["transcript"] == ""


async def test_no_connector_is_failed():
    act = AgentTaskActivities(db_pool=None, remote_script=None)
    assert (await act.collect_coding_run("/tmp/x", ""))["status"] == "failed"
