import json

from aegis_worker.activework import github


class _FakeConnector:
    """Minimal stand-in for RemoteScriptConnector used by the github signals."""

    def __init__(self, capture_result, *, claude_orgs=("acme",)):
        self._host = "node-a"
        self._claude_orgs = {o.lower() for o in claude_orgs}
        self._capture_result = capture_result
        self.last_cmd = None

    def _engine_for(self, github_repo):
        org = github_repo.split("/", 1)[0].lower() if github_repo else ""
        return "claude" if org in self._claude_orgs else "kimi"

    async def _resolve_kimi_host(self):
        return ("node-b", True)

    def _ssh_args_host(self, host, cmd):
        self.last_cmd = cmd
        return ["ssh", host, cmd]

    async def _run_capture(self, ssh_args, timeout):
        return self._capture_result


async def test_open_prs_parses_gh_json():
    payload = json.dumps([
        {"number": 7, "author": {"login": "alice"}, "headRefName": "fix/x", "updatedAt": "2026-06-20T10:00:00Z"},
    ])
    conn = _FakeConnector({"status": "succeeded", "exit_code": 0, "stdout": payload, "stderr": ""})
    prs = await github.open_prs(conn, "example/aegis")
    assert prs == [{"number": 7, "user": "alice", "updated_at": "2026-06-20T10:00:00Z", "head_ref": "fix/x"}]


async def test_open_prs_nonzero_exit_degrades_to_empty():
    conn = _FakeConnector({"status": "failed", "exit_code": 1, "stdout": "", "stderr": "boom"})
    assert await github.open_prs(conn, "example/aegis") == []


async def test_recent_pushes_filters_pushevents_since():
    events = json.dumps([
        {"type": "PushEvent", "created_at": "2026-06-20T12:00:00Z",
         "actor": {"login": "bob"}, "payload": {"ref": "refs/heads/feature/p"}},
        {"type": "PushEvent", "created_at": "2026-06-01T00:00:00Z",
         "actor": {"login": "old"}, "payload": {"ref": "refs/heads/stale"}},
        {"type": "WatchEvent", "created_at": "2026-06-20T13:00:00Z", "actor": {"login": "x"}, "payload": {}},
    ])
    conn = _FakeConnector({"status": "succeeded", "exit_code": 0, "stdout": events, "stderr": ""})
    pushes = await github.recent_pushes(conn, "example/aegis", "2026-06-19T00:00:00+00:00")
    assert pushes == [{"ref": "refs/heads/feature/p", "actor": "bob", "created_at": "2026-06-20T12:00:00Z"}]


async def test_claude_org_repo_runs_on_base_host():
    conn = _FakeConnector({"status": "succeeded", "exit_code": 0, "stdout": "[]", "stderr": ""})
    assert await github._host_for(conn, "acme/news-service") == "node-a"


async def test_other_org_repo_runs_on_kimi_host():
    conn = _FakeConnector({"status": "succeeded", "exit_code": 0, "stdout": "[]", "stderr": ""})
    assert await github._host_for(conn, "example/aegis") == "node-b"


async def test_no_connector_degrades_to_empty():
    assert await github.open_prs(None, "example/aegis") == []
    assert await github.recent_pushes(None, "example/aegis", "2026-06-19T00:00:00+00:00") == []
