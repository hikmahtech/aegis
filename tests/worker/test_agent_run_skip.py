"""launch_agent_run defers to a busy human session.

The `started` flag on the fake is what makes these falsifiable: asserting only
on the returned status would pass even if the run had been launched anyway.
"""

from __future__ import annotations

import inspect

from aegis_worker.activities.agent_run import AgentRunActivities
from temporalio.testing import ActivityEnvironment

BUSY = [{"owner": "human", "status": "busy", "repo": "acme/api", "name": "api-2d"}]


class _Connector:
    def __init__(self, sessions):
        self._sessions = sessions
        self.started = False

    async def list_coding_sessions(self):
        return {
            "status": "ok",
            "sessions": self._sessions,
            "errors": [],
            "skip_when_busy": True,
        }

    async def coding_settings(self):
        return {"kimi_binary": "/bin/kimi", "repo_base": "/w"}

    async def start_kimi_run(self, **kwargs):
        self.started = True
        return {"status": "running", "run_id": "r1", "output_file": "/tmp/o", "host": "h"}


async def _launch(conn):
    acts = AgentRunActivities(remote_script=conn)
    return await ActivityEnvironment().run(acts.launch_agent_run, "do it", "acme/api")


async def test_skips_when_a_human_is_busy_in_the_repo():
    conn = _Connector(BUSY)
    result = await _launch(conn)
    assert result["status"] == "skipped"
    assert result["reason"] == "repo_busy"
    assert conn.started is False


async def test_launches_when_the_session_is_idle():
    conn = _Connector([dict(BUSY[0], status="idle")])
    result = await _launch(conn)
    assert result["status"] == "running"
    assert conn.started is True


async def test_launches_when_the_busy_session_is_aegis_own_run():
    """AEGIS's own runs register in the same registry; they must not self-block."""
    conn = _Connector([dict(BUSY[0], owner="aegis")])
    result = await _launch(conn)
    assert result["status"] == "running"
    assert conn.started is True


async def test_launches_when_the_busy_session_is_a_different_repo():
    conn = _Connector([dict(BUSY[0], repo="acme/web")])
    result = await _launch(conn)
    assert result["status"] == "running"
    assert conn.started is True


def test_fake_connector_matches_the_real_signatures():
    """Guards against the fake drifting from the real connector."""
    from aegis.connectors.remote_script import RemoteScriptConnector

    for name in ("list_coding_sessions", "coding_settings"):
        real = inspect.signature(getattr(RemoteScriptConnector, name))
        fake = inspect.signature(getattr(_Connector, name))
        assert list(real.parameters) == list(fake.parameters), name
