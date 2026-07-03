import json

from aegis_worker.activities.active_work import ActiveWorkActivities
from temporalio.testing import ActivityEnvironment


class _FakeConnector:
    def __init__(self, prs_json="[]", events_json="[]"):
        self._host = "node-a"
        self._claude_orgs = set()
        self._prs_json = prs_json
        self._events_json = events_json

    def _engine_for(self, repo):
        return "kimi"

    async def _resolve_kimi_host(self):
        return ("node-b", True)

    async def ensure_config(self):
        pass

    async def run_on_host(self, host, cmd, timeout=30, stdin=None):
        stdout = self._prs_json if "gh pr list" in cmd else self._events_json
        return {"status": "succeeded", "exit_code": 0, "stdout": stdout, "stderr": ""}


class _RaisingConnector(_FakeConnector):
    """A connector whose run_on_host always raises, simulating a GitHub collector failure."""

    async def run_on_host(self, host, cmd, timeout=30, stdin=None):
        raise RuntimeError("simulated gh failure")


# db_pool=None turns the Todoist signal off so these stay deterministic (and DB-free).
async def test_check_active_work_open_pr():
    conn = _FakeConnector(prs_json=json.dumps([{"number": 9, "author": {"login": "alice"}}]))
    acts = ActiveWorkActivities(db_pool=None, remote_script=conn, lookback_hours=48)
    out = await ActivityEnvironment().run(acts.check_active_work, {"service": "aegis"}, "example/aegis")
    assert out["active"] is True
    assert any("open PR #9" in r for r in out["reasons"])


async def test_check_active_work_no_signals_inactive():
    acts = ActiveWorkActivities(db_pool=None, remote_script=None, lookback_hours=48)
    out = await ActivityEnvironment().run(acts.check_active_work, {"service": "nope"}, "example/aegis")
    assert out == {"active": False, "reasons": []}


async def test_check_active_work_github_failure_degrades():
    """A raising GitHub collector degrades cleanly — no crash, no PR reason."""
    conn = _RaisingConnector()
    acts = ActiveWorkActivities(db_pool=None, remote_script=conn, lookback_hours=48)
    out = await ActivityEnvironment().run(acts.check_active_work, {"service": "aegis"}, "example/aegis")
    assert out["active"] is False
    assert not any("open PR" in r for r in out["reasons"])
