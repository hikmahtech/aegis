"""Encrypted infra credentials: storage, sanitization, materialization.

Real-Postgres tests (db_pool fixture from tests/core/conftest.py) — the
credentials jsonb round-trip and the never-leak-key-material guarantee are
exactly what mocks would hide.
"""

from __future__ import annotations

import os
import pathlib
from unittest.mock import AsyncMock, patch

from aegis.db import run_migrations
from aegis.services import infra as infra_service

SECRET_KEY = "test-secret-key"
FAKE_SSH_KEY = "-----BEGIN OPENSSH PRIVATE KEY-----\nabc123\n-----END OPENSSH PRIVATE KEY-----"
FAKE_KUBECONFIG = "apiVersion: v1\nkind: Config"


async def _prepare(db_pool):
    """Migrations + wipe leftovers — called at the top of each DB test
    (fixture-based setup would cross event loops with the function-scoped
    db_pool; test_seed.py uses the same inline pattern)."""
    await run_migrations(db_pool)
    await db_pool.execute("DELETE FROM infra WHERE slug LIKE 'test-cred-%'")


async def _create(db_pool, **overrides):
    data = {
        "name": "test-cred-host",
        "slug": "test-cred-host",
        "kind": "swarm",
        "host": "10.20.0.1",
        "ssh_user": "ubuntu",
        "ssh_private_key": FAKE_SSH_KEY,
        **overrides,
    }
    return await infra_service.create_infra(db_pool, data, SECRET_KEY)


async def test_create_stores_encrypted_and_sanitizes(db_pool):
    await _prepare(db_pool)
    row = await _create(db_pool, kubeconfig=FAKE_KUBECONFIG)

    # Response is sanitized: booleans present, no key material anywhere.
    assert row["has_ssh_key"] is True
    assert row["has_kubeconfig"] is True
    assert "credentials" not in row
    assert FAKE_SSH_KEY not in str(row)

    # DB row is encrypted — ciphertext, not the plaintext key.
    stored = await db_pool.fetchval("SELECT credentials FROM infra WHERE id = $1", row["id"])
    assert stored["ssh_private_key_enc"]["encrypted"] is True
    assert FAKE_SSH_KEY not in str(stored)


async def test_list_and_get_never_return_credentials(db_pool):
    await _prepare(db_pool)
    row = await _create(db_pool)
    listed = [r for r in await infra_service.list_infra(db_pool) if r["id"] == row["id"]][0]
    assert listed["has_ssh_key"] is True
    assert "credentials" not in listed

    got = await infra_service.get_infra(db_pool, row["id"])
    assert got["has_ssh_key"] is True
    assert "credentials" not in got

    full = await infra_service.get_infra(db_pool, row["id"], include_credentials=True)
    assert full["credentials"]["ssh_private_key_enc"]["value"]


async def test_update_blank_keeps_secret_and_new_value_replaces(db_pool):
    await _prepare(db_pool)
    row = await _create(db_pool)

    # Update without secret fields — key survives.
    updated = await infra_service.update_infra(db_pool, row["id"], {"name": "renamed"}, SECRET_KEY)
    assert updated["name"] == "renamed"
    assert updated["has_ssh_key"] is True

    # Paste a replacement key.
    updated = await infra_service.update_infra(
        db_pool, row["id"], {"ssh_private_key": "new-key-material"}, SECRET_KEY
    )
    assert updated["has_ssh_key"] is True
    full = await infra_service.get_infra(db_pool, row["id"], include_credentials=True)
    with infra_service.ssh_key_file(full, SECRET_KEY) as path:
        assert pathlib.Path(path).read_text() == "new-key-material\n"


def test_ssh_key_file_materializes_0600_and_cleans_up():
    from aegis.crypto import encrypt_secret

    infra = {"credentials": {"ssh_private_key_enc": encrypt_secret(FAKE_SSH_KEY, SECRET_KEY)}}
    with infra_service.ssh_key_file(infra, SECRET_KEY) as path:
        assert pathlib.Path(path).read_text() == FAKE_SSH_KEY + "\n"
        assert oct(os.stat(path).st_mode & 0o777) == "0o600"
    assert not os.path.exists(path)


def test_ssh_key_file_falls_back_to_key_ref():
    with infra_service.ssh_key_file({"ssh_key_ref": "/keys/id_ed25519"}, SECRET_KEY) as path:
        assert path == "/keys/id_ed25519"
    with infra_service.ssh_key_file({}, SECRET_KEY) as path:
        assert path is None


async def test_provision_uses_stored_key(db_pool):
    await _prepare(db_pool)
    row = await _create(db_pool, setup_command="echo ok")
    seen: list[list[str]] = []

    async def fake_run_ssh(ssh_args, timeout=30, stdin=None):
        seen.append(ssh_args)
        key_path = ssh_args[ssh_args.index("-i") + 1]
        assert pathlib.Path(key_path).read_text() == FAKE_SSH_KEY + "\n"
        return {"ok": True, "exit_code": 0, "stdout": "", "stderr": ""}

    with patch.object(infra_service, "_run_ssh", new=AsyncMock(side_effect=fake_run_ssh)):
        result = await infra_service.provision_infra(db_pool, row["id"], SECRET_KEY)

    assert result["status"] == "ready"
    assert "credentials" not in result
    assert len(seen) == 1


async def test_provision_preflight_accepts_stored_key_without_key_ref(db_pool):
    await _prepare(db_pool)
    # No ssh_key_ref, only the stored key — must pass preflight.
    row = await _create(db_pool)
    assert row["ssh_key_ref"] is None
    with patch.object(
        infra_service,
        "_run_ssh",
        new=AsyncMock(return_value={"ok": True, "exit_code": 0, "stdout": "", "stderr": ""}),
    ):
        result = await infra_service.provision_infra(db_pool, row["id"], SECRET_KEY)
    assert result["status"] == "ready"


async def test_provision_preflight_fails_without_any_key(db_pool):
    await _prepare(db_pool)
    row = await _create(db_pool, ssh_private_key=None)
    result = await infra_service.provision_infra(db_pool, row["id"], SECRET_KEY)
    assert result["status"] == "error"
    assert "SSH key" in result["last_error"]
