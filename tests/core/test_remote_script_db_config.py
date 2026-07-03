"""RemoteScriptConnector DB-first config (infra registry `coding` block).

Real-Postgres tests: an enabled infra row must override the env constructor
args (host/user/port/key + coding block), the DB-stored SSH key must be
materialized to a mode-0600 temp file per SSH call and unlinked after, and
the env config must apply when no enabled row exists.
"""

from __future__ import annotations

import os
import pathlib
from unittest.mock import AsyncMock, patch

from aegis.connectors.remote_script import RemoteScriptConnector
from aegis.db import run_migrations
from aegis.services import infra as infra_service

SECRET_KEY = "test-secret-key"
FAKE_SSH_KEY = "-----BEGIN OPENSSH PRIVATE KEY-----\ndbkey\n-----END OPENSSH PRIVATE KEY-----"

DB_CODING = {
    "enabled": True,
    "repo_base": "/srv/workspace",
    "engines": {
        "claude": {
            "binary_path": "/db/bin/claude",
            "config_dirs": {
                "work": "/home/coder/.claude-work",
                "personal": "/home/coder/.claude-personal",
            },
            "default_account": "personal",
        },
        "kimi": {"binary_path": "/db/bin/kimi"},
    },
    "routing": {
        "orgs": {"acme": {"engine": "claude", "account": "work"}},
        "default_engine": "kimi",
    },
    "tmux": {"session": "coding", "window_cap": 5},
    "self_repo_path": "personal/aegis",
    "runbooks_dir": "/srv/runbooks",
}


async def _prepare(db_pool):
    await run_migrations(db_pool)
    await db_pool.execute("DELETE FROM infra WHERE slug LIKE 'test-rsdb-%'")
    # Neutralize any coding-enabled leftovers from other test modules — the
    # connector resolves the single enabled row DB-globally.
    await db_pool.execute(
        "UPDATE infra SET coding = '{}'::jsonb WHERE coding->>'enabled' = 'true'"
    )


async def _create_coding_row(db_pool, coding=None, **overrides):
    data = {
        "name": "test-rsdb-host",
        "slug": "test-rsdb-host",
        "kind": "ssh_host",
        "host": "10.9.9.9",
        "ssh_user": "coder",
        "ssh_port": 2222,
        "ssh_private_key": FAKE_SSH_KEY,
        "coding": coding if coding is not None else DB_CODING,
        **overrides,
    }
    return await infra_service.create_infra(db_pool, data, SECRET_KEY)


def _connector(db_pool):
    """Connector with obvious env-fallback values so DB wins are visible."""
    return RemoteScriptConnector(
        host="env-host",
        user="envuser",
        key_file="/env/key",
        repo_base="/env/workspace",
        claude_orgs="envorg",
        claude_binary="/env/bin/claude",
        kimi_binary="/env/bin/kimi",
        self_repo_path="env/aegis",
        runbooks_dir="/env/runbooks",
        db_pool=db_pool,
        secret_key=SECRET_KEY,
    )


def _recording_exec(calls, results=None):
    """Async create_subprocess_exec stand-in that snapshots the `-i` key file
    (content + mode) at call time — materialization must be alive during the
    call and gone after."""
    results = results or []

    async def fake_exec(*args, stdin=None, stdout=None, stderr=None, env=None):
        rec = {"args": list(args)}
        arglist = list(args)
        if "-i" in arglist:
            key_path = arglist[arglist.index("-i") + 1]
            rec["key_path"] = key_path
            p = pathlib.Path(key_path)
            if p.exists():
                rec["key_content"] = p.read_text()
                rec["key_mode"] = oct(os.stat(key_path).st_mode & 0o777)
        idx = len(calls)
        calls.append(rec)
        rc, out, err = results[idx] if idx < len(results) else (0, b"", b"")

        class _Proc:
            returncode = rc

            async def communicate(self, input=None):
                return out, err

            def kill(self):
                pass

            async def wait(self):
                return rc

        return _Proc()

    return fake_exec


async def test_db_config_overrides_env_and_materializes_key(db_pool):
    await _prepare(db_pool)
    await _create_coding_row(db_pool)
    conn = _connector(db_pool)
    calls: list[dict] = []

    with patch("asyncio.create_subprocess_exec", new=_recording_exec(calls)):
        result = await conn.run_script("infra/list")

    assert result["status"] == "succeeded"
    argv = " ".join(calls[0]["args"])
    # SSH identity comes from the infra row, not the env fallback.
    assert "coder@10.9.9.9" in argv
    assert "-p 2222" in argv
    assert "envuser@env-host" not in argv
    assert "/env/key" not in argv
    # DB key: 0600 temp file alive during the call, unlinked afterwards.
    assert calls[0]["key_content"] == FAKE_SSH_KEY + "\n"
    assert calls[0]["key_mode"] == "0o600"
    assert not os.path.exists(calls[0]["key_path"])
    # Coding block applied too.
    assert conn._repo_base == "/srv/workspace"
    assert conn._tmux_session == "coding"
    assert conn._tmux_window_cap == 5


async def test_env_fallback_when_no_enabled_row(db_pool):
    await _prepare(db_pool)
    # A DISABLED coding block must not hijack the connector.
    await _create_coding_row(db_pool, coding=dict(DB_CODING, enabled=False))
    conn = _connector(db_pool)
    calls: list[dict] = []

    with patch("asyncio.create_subprocess_exec", new=_recording_exec(calls)):
        await conn.run_script("infra/list")

    argv = " ".join(calls[0]["args"])
    assert "envuser@env-host" in argv
    assert "-i /env/key" in argv
    assert "-p " not in argv  # env config has no custom port
    assert conn._repo_base == "/env/workspace"


async def test_row_disable_reverts_to_env_after_ttl(db_pool):
    await _prepare(db_pool)
    row = await _create_coding_row(db_pool)
    conn = _connector(db_pool)
    calls: list[dict] = []

    with patch("asyncio.create_subprocess_exec", new=_recording_exec(calls)):
        await conn.run_script("infra/list")
        assert "coder@10.9.9.9" in " ".join(calls[0]["args"])

        await infra_service.update_infra(
            db_pool, row["id"], {"coding": dict(DB_CODING, enabled=False)}, SECRET_KEY
        )
        conn._config_expiry = 0.0  # fast-forward past the TTL
        await conn.run_script("infra/list")

    assert "envuser@env-host" in " ".join(calls[1]["args"])


async def test_coding_settings_accessor_reports_db_values(db_pool):
    await _prepare(db_pool)
    await _create_coding_row(db_pool)
    conn = _connector(db_pool)

    got = await conn.coding_settings()
    assert got["host"] == "10.9.9.9"
    assert got["repo_base"] == "/srv/workspace"
    assert got["kimi_binary"] == "/db/bin/kimi"
    assert got["claude_binary"] == "/db/bin/claude"
    assert got["self_repo_path"] == "personal/aegis"
    assert got["runbooks_dir"] == "/srv/runbooks"
    assert got["source"] == "db:test-rsdb-host"


# ── engine routing from the DB block ─────────────────────────────────────────


def _proc_results(n, window_list_at=None):
    """(rc, stdout, stderr) tuples for a start_kimi_run call sequence."""
    results = [(0, b"", b"")] * n
    if window_list_at is not None:
        results[window_list_at] = (0, b"@0:bash:0\n", b"")
    return results


async def test_routing_org_to_claude_with_account_config_dir(db_pool):
    await _prepare(db_pool)
    await _create_coding_row(db_pool)
    conn = _connector(db_pool)
    calls: list[dict] = []

    # claude run: test-d, pull, worktree, prompt, tmux ensure+list, new-window
    with patch(
        "asyncio.create_subprocess_exec",
        new=_recording_exec(calls, _proc_results(6, window_list_at=4)),
    ):
        result = await conn.start_kimi_run(
            repo="bcp", prompt="investigate", kimi_binary="/env/bin/kimi",
            github_repo="Acme/bcp",
        )

    assert result["status"] == "running"
    assert result["engine"] == "claude"
    assert result["host"] == "10.9.9.9"  # claude pins the base (coding) host
    launch = " ".join(calls[-1]["args"])
    assert "/db/bin/claude" in launch
    assert "CLAUDE_CONFIG_DIR=/home/coder/.claude-work" in launch  # routed account
    assert "tmux new-window" in launch
    assert "-t coding" in launch  # DB tmux session name


async def test_routing_unknown_org_uses_default_engine_and_db_kimi_binary(db_pool):
    await _prepare(db_pool)
    await _create_coding_row(db_pool)
    conn = _connector(db_pool)
    calls: list[dict] = []

    # no kimi_host_slug → kimi runs on the base host via nohup (5 calls)
    with patch("asyncio.create_subprocess_exec", new=_recording_exec(calls, _proc_results(5))):
        result = await conn.start_kimi_run(
            repo="tool", prompt="x", kimi_binary="/env/bin/kimi",
            github_repo="stranger/tool",
        )

    assert result["engine"] == "kimi"
    launch = " ".join(calls[-1]["args"])
    assert "/db/bin/kimi" in launch  # DB binary beats the caller's env path
    assert "/env/bin/kimi" not in launch
    assert "nohup" in launch
    assert "/srv/workspace/tool" in launch  # DB repo_base


async def test_engine_override_wins_and_uses_default_account(db_pool):
    await _prepare(db_pool)
    await _create_coding_row(db_pool)
    conn = _connector(db_pool)
    calls: list[dict] = []

    with patch(
        "asyncio.create_subprocess_exec",
        new=_recording_exec(calls, _proc_results(6, window_list_at=4)),
    ):
        result = await conn.start_kimi_run(
            repo="tool", prompt="x", kimi_binary="/env/bin/kimi",
            github_repo="stranger/tool",  # unrouted org — would be kimi
            engine_override="claude",
        )

    assert result["engine"] == "claude"
    launch = " ".join(calls[-1]["args"])
    # default_account=personal → its config dir backs the fallback login
    assert "CLAUDE_CONFIG_DIR=/home/coder/.claude-personal" in launch

    # An explicit claude_config_dir still wins over the default account.
    calls.clear()
    with patch(
        "asyncio.create_subprocess_exec",
        new=_recording_exec(calls, _proc_results(6, window_list_at=4)),
    ):
        await conn.start_kimi_run(
            repo="tool", prompt="x", kimi_binary="/env/bin/kimi",
            github_repo="stranger/tool",
            engine_override="claude",
            claude_config_dir="/explicit/dir",
        )
    assert "CLAUDE_CONFIG_DIR=/explicit/dir" in " ".join(calls[-1]["args"])


async def test_kimi_host_slug_resolves_other_row_and_fails_closed(db_pool):
    await _prepare(db_pool)
    await infra_service.create_infra(
        db_pool,
        {
            "name": "test-rsdb-kimi",
            "slug": "test-rsdb-kimi",
            "kind": "ssh_host",
            "host": "10.7.7.7",
            "ssh_user": "coder",
        },
        SECRET_KEY,
    )
    await _create_coding_row(db_pool, coding=dict(DB_CODING, kimi_host_slug="test-rsdb-kimi"))

    conn = _connector(db_pool)
    await conn.ensure_config()
    assert conn._kimi_host == "10.7.7.7"
    assert conn.workspace_scan_host() == "10.7.7.7"

    # Reachable → tmux mode on the kimi host; unreachable → base host (fail-closed).
    with patch.object(conn, "_probe_host", AsyncMock(return_value=True)):
        assert await conn._resolve_kimi_host() == ("10.7.7.7", True)
    with patch.object(conn, "_probe_host", AsyncMock(return_value=False)):
        assert await conn._resolve_kimi_host() == ("10.9.9.9", False)


async def test_unresolvable_kimi_host_slug_degrades_to_base(db_pool):
    await _prepare(db_pool)
    await _create_coding_row(db_pool, coding=dict(DB_CODING, kimi_host_slug="test-rsdb-ghost"))
    conn = _connector(db_pool)
    await conn.ensure_config()
    assert conn._kimi_host == ""  # unset ⇒ kimi runs on the base host, no probe
    assert await conn._resolve_kimi_host() == ("10.9.9.9", False)


async def test_no_host_anywhere_fails_with_clear_error(db_pool):
    await _prepare(db_pool)  # no enabled row
    conn = RemoteScriptConnector(db_pool=db_pool, secret_key=SECRET_KEY)  # no env host either
    result = await conn.run_script("infra/list")
    assert result["status"] == "failed"
    assert "not configured" in result["stderr"]
