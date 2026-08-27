"""Pure parsing/normalisation for the coding-session inventory, plus the guard.

The private-field tests exist because the CLI's own session objects carry a
`messagingSocketPath` and a sibling `.key` file. Those are an undocumented
interface and auth material; nothing here may ever surface them.
"""

from __future__ import annotations

import inspect

import pytest
from aegis.connectors.coding_sessions import (
    busy_human_sessions,
    match_busy,
    normalise_repo,
    parse_agents_json,
    to_records,
)

BASE = "/home/user/Workspace"

# Real shape of `claude agents --json`, trimmed. Note `state` (background
# sessions) vs `status` (interactive) and `id` vs `sessionId`.
SAMPLE = """[
  {"id": "451cebcb", "cwd": "/home/user/Workspace/acme/api", "kind": "background",
   "sessionId": "451cebcb-ab17-4454-99ca-ed14e84a1cf2", "name": "build thing",
   "state": "blocked"},
  {"pid": 368220, "cwd": "/home/user/Workspace/acme/api", "kind": "interactive",
   "sessionId": "493b827c-a432-4c37-a5ed-dde55c25bf1a", "name": "api-2d",
   "status": "busy", "messagingSocketPath": "/run/user/1000/cc-socks/368220.sock"}
]"""


def test_parse_skips_leading_noise():
    """One profile prints a config-restore warning before its JSON array."""
    noisy = (
        "Claude configuration file not found at: /home/user/.claude/.claude.json\n"
        "A backup file exists at: /home/user/.claude/backups/x\n\n" + SAMPLE
    )
    assert len(parse_agents_json(noisy)) == 2


def test_parse_ignores_non_dict_entries():
    assert parse_agents_json('["junk", {"cwd": "/x"}]') == [{"cwd": "/x"}]


def test_parse_empty_array():
    assert parse_agents_json("[]") == []


@pytest.mark.parametrize("raw", ["", "not json at all", "[{oops}]", "{}"])
def test_parse_rejects_unusable_output(raw):
    with pytest.raises(ValueError):
        parse_agents_json(raw)


@pytest.mark.parametrize(
    "cwd,expected",
    [
        (f"{BASE}/acme/api", ("acme/api", "human")),
        # AEGIS's own per-run worktree convention.
        (f"{BASE}/acme/api-aegis-wt/954df86d", ("acme/api", "aegis")),
        # Claude Code's own worktree convention.
        (f"{BASE}/acme/api/.claude/worktrees/issue-12", ("acme/api", "human")),
        # Outside repo_base can never match anything.
        ("/tmp/scratch", ("", "human")),
        (BASE, ("", "human")),
        ("", ("", "human")),
    ],
)
def test_normalise_repo(cwd, expected):
    assert normalise_repo(cwd, BASE) == expected


def test_records_never_leak_private_fields():
    """messagingSocketPath and pid must never reach a record."""
    records = to_records(parse_agents_json(SAMPLE), "personal", BASE)
    for record in records:
        assert "messagingSocketPath" not in record
        assert "pid" not in record
    blob = repr(records)
    assert "cc-socks" not in blob
    assert ".key" not in blob


def test_records_read_status_and_state():
    records = to_records(parse_agents_json(SAMPLE), "personal", BASE)
    by_name = {r["name"]: r for r in records}
    assert by_name["api-2d"]["status"] == "busy"
    assert by_name["build thing"]["status"] == "blocked"
    assert by_name["api-2d"]["account"] == "personal"
    assert by_name["api-2d"]["repo"] == "acme/api"


def test_match_busy_only_matches_busy_human_same_repo():
    sessions = [
        {"owner": "human", "status": "busy", "repo": "acme/api", "name": "hit"},
        {"owner": "human", "status": "idle", "repo": "acme/api", "name": "idle"},
        {"owner": "aegis", "status": "busy", "repo": "acme/api", "name": "own-run"},
        {"owner": "human", "status": "busy", "repo": "acme/web", "name": "other-repo"},
    ]
    assert [s["name"] for s in match_busy(sessions, "acme/api")] == ["hit"]


def test_match_busy_empty_repo_never_matches():
    sessions = [{"owner": "human", "status": "busy", "repo": "", "name": "x"}]
    assert match_busy(sessions, "") == []
    assert match_busy(sessions, "acme/api") == []


# ── the guard ────────────────────────────────────────────────────────────────


class _FakeConnector:
    def __init__(self, result=None, boom=False):
        self._result = result or {}
        self._boom = boom

    async def list_coding_sessions(self):
        if self._boom:
            raise RuntimeError("ssh exploded")
        return self._result


_BUSY_RESULT = {
    "status": "ok",
    "skip_when_busy": True,
    "sessions": [{"owner": "human", "status": "busy", "repo": "acme/api", "name": "api-2d"}],
    "errors": [],
}


async def test_helper_returns_the_collision():
    found = await busy_human_sessions(_FakeConnector(_BUSY_RESULT), "acme/api")
    assert [s["name"] for s in found] == ["api-2d"]


async def test_helper_observe_only_mode_never_blocks():
    """skip_when_busy=False reports nothing to block on, by design."""
    result = dict(_BUSY_RESULT, skip_when_busy=False)
    assert await busy_human_sessions(_FakeConnector(result), "acme/api") == []


@pytest.mark.parametrize(
    "result",
    [
        {"status": "disabled", "sessions": [], "errors": [], "skip_when_busy": True},
        {"status": "unavailable", "sessions": [], "errors": [], "skip_when_busy": True},
    ],
)
async def test_helper_fails_open_on_non_ok(result):
    assert await busy_human_sessions(_FakeConnector(result), "acme/api") == []


async def test_helper_fails_open_when_the_connector_raises():
    assert await busy_human_sessions(_FakeConnector(boom=True), "acme/api") == []


async def test_helper_fails_open_with_no_connector():
    assert await busy_human_sessions(None, "acme/api") == []


def test_fake_connector_matches_the_real_signature():
    """Guards against the fake drifting from RemoteScriptConnector."""
    from aegis.connectors.remote_script import RemoteScriptConnector

    real = inspect.signature(RemoteScriptConnector.list_coding_sessions)
    fake = inspect.signature(_FakeConnector.list_coding_sessions)
    assert list(real.parameters) == list(fake.parameters)
