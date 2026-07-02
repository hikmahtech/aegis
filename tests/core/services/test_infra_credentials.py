"""Encrypted infra credentials: storage, sanitization, materialization.

Real-Postgres tests (db_pool fixture from tests/core/conftest.py) — the
credentials jsonb round-trip and the never-leak-key-material guarantee are
exactly what mocks would hide.
"""

from __future__ import annotations

import json
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


# ── k8s: kubeconfig materialization, ops, provisioning ──────────────────────


def test_kubeconfig_file_materializes_and_cleans_up():
    from aegis.crypto import encrypt_secret

    infra = {"credentials": {"kubeconfig_enc": encrypt_secret(FAKE_KUBECONFIG, SECRET_KEY)}}
    with infra_service.kubeconfig_file(infra, SECRET_KEY) as path:
        assert pathlib.Path(path).read_text() == FAKE_KUBECONFIG
        assert oct(os.stat(path).st_mode & 0o777) == "0o600"
    assert not os.path.exists(path)

    with infra_service.kubeconfig_file({}, SECRET_KEY) as path:
        assert path is None


async def _create_k8s(db_pool, **overrides):
    data = {
        "name": "test-cred-cluster",
        "slug": "test-cred-cluster",
        "kind": "k8s",
        "host": None,
        "ssh_user": None,
        "ssh_private_key": None,
        "kubeconfig": FAKE_KUBECONFIG,
        **overrides,
    }
    return await _create(db_pool, **data)


_POD_JSON = {
    "items": [
        {
            "metadata": {"name": "web-1", "namespace": "default"},
            "spec": {"nodeName": "node-a"},
            "status": {
                "phase": "Running",
                "containerStatuses": [{"ready": True, "restartCount": 3}],
            },
        }
    ]
}

_DEPLOY_JSON = {
    "items": [
        {
            "metadata": {"name": "web", "namespace": "default"},
            "spec": {
                "replicas": 2,
                "template": {"spec": {"containers": [{"image": "nginx:1"}]}},
            },
            "status": {"readyReplicas": 2},
        }
    ]
}


async def test_k8s_list_pods_parses_and_uses_kubeconfig(db_pool):
    await _prepare(db_pool)
    row = await _create_k8s(db_pool)
    seen: list[list[str]] = []

    async def fake_run(args, timeout=30, stdin=None):
        seen.append(args)
        assert args[0] == "kubectl" and args[1] == "--kubeconfig"
        assert pathlib.Path(args[2]).read_text() == FAKE_KUBECONFIG
        return {"ok": True, "exit_code": 0, "stdout": json.dumps(_POD_JSON), "stderr": ""}

    with patch.object(infra_service, "_run_ssh", new=AsyncMock(side_effect=fake_run)):
        result = await infra_service.k8s_list_pods(db_pool, row["id"], SECRET_KEY, "default")

    assert result["ok"] is True
    assert result["pods"] == [
        {
            "name": "web-1",
            "namespace": "default",
            "phase": "Running",
            "ready": "1/1",
            "restarts": 3,
            "node": "node-a",
        }
    ]
    assert seen[0][3:] == ["get", "pods", "-n", "default", "-o", "json"]


async def test_k8s_list_deployments_parses(db_pool):
    await _prepare(db_pool)
    row = await _create_k8s(db_pool)
    with patch.object(
        infra_service,
        "_run_ssh",
        new=AsyncMock(
            return_value={
                "ok": True,
                "exit_code": 0,
                "stdout": json.dumps(_DEPLOY_JSON),
                "stderr": "",
            }
        ),
    ):
        result = await infra_service.k8s_list_deployments(db_pool, row["id"], SECRET_KEY)
    assert result["ok"] is True
    assert result["deployments"] == [
        {"name": "web", "namespace": "default", "ready": "2/2", "images": ["nginx:1"]}
    ]


async def test_k8s_restart_deployment_argv_and_validation(db_pool):
    await _prepare(db_pool)
    row = await _create_k8s(db_pool)
    seen: list[list[str]] = []

    async def fake_run(args, timeout=30, stdin=None):
        seen.append(args)
        return {"ok": True, "exit_code": 0, "stdout": "deployment.apps/web restarted", "stderr": ""}

    with patch.object(infra_service, "_run_ssh", new=AsyncMock(side_effect=fake_run)):
        result = await infra_service.k8s_restart_deployment(
            db_pool, row["id"], SECRET_KEY, "default", "web"
        )
        assert result["ok"] is True
        assert seen[0][3:] == ["rollout", "restart", "deployment/web", "-n", "default"]

        # Flag-injection attempt is rejected before any exec.
        bad = await infra_service.k8s_restart_deployment(
            db_pool, row["id"], SECRET_KEY, "default", "--all"
        )
    assert bad["ok"] is False and bad["status_code"] == 400
    assert len(seen) == 1


async def test_k8s_ops_reject_non_k8s_entry(db_pool):
    await _prepare(db_pool)
    row = await _create(db_pool)  # kind=swarm
    result = await infra_service.k8s_list_pods(db_pool, row["id"], SECRET_KEY)
    assert result["ok"] is False and result["status_code"] == 400


async def test_k8s_provision_checks_cluster_connectivity(db_pool):
    await _prepare(db_pool)
    row = await _create_k8s(db_pool)
    with patch.object(
        infra_service,
        "_run_ssh",
        new=AsyncMock(
            return_value={"ok": True, "exit_code": 0, "stdout": "node/a\nnode/b\n", "stderr": ""}
        ),
    ):
        result = await infra_service.provision_infra(db_pool, row["id"], SECRET_KEY)
    assert result["status"] == "ready"
    steps = {s["step"]: s for s in result["log"]}
    assert steps["kubectl_get_nodes"]["stdout"] == "2 node(s) reachable"


async def test_k8s_provision_fails_without_kubeconfig(db_pool):
    await _prepare(db_pool)
    row = await _create_k8s(db_pool, kubeconfig=None)
    result = await infra_service.provision_infra(db_pool, row["id"], SECRET_KEY)
    assert result["status"] == "error"
    assert "kubeconfig" in result["last_error"]


# ── read_only gate + chat-tool dispatch to registry clusters ────────────────


async def test_read_only_blocks_restart_but_not_reads(db_pool):
    await _prepare(db_pool)
    row = await _create_k8s(db_pool, read_only=True)

    blocked = await infra_service.k8s_restart_deployment(
        db_pool, row["id"], SECRET_KEY, "default", "web"
    )
    assert blocked["ok"] is False and blocked["status_code"] == 403
    assert "read-only" in blocked["error"]

    with patch.object(
        infra_service,
        "_run_ssh",
        new=AsyncMock(
            return_value={
                "ok": True,
                "exit_code": 0,
                "stdout": json.dumps(_POD_JSON),
                "stderr": "",
            }
        ),
    ):
        reads = await infra_service.k8s_list_pods(db_pool, row["id"], SECRET_KEY, "default")
    assert reads["ok"] is True


async def test_read_only_blocks_ssh_provision(db_pool):
    await _prepare(db_pool)
    row = await _create(db_pool, read_only=True)
    with patch.object(infra_service, "_run_ssh", new=AsyncMock()) as run:
        result = await infra_service.provision_infra(db_pool, row["id"], SECRET_KEY)
    assert result["status"] == "error"
    assert "read-only" in result["last_error"]
    run.assert_not_awaited()


async def test_k8s_provision_allowed_when_read_only(db_pool):
    # k8s provisioning is only a connectivity check — read_only must not block it.
    await _prepare(db_pool)
    row = await _create_k8s(db_pool, read_only=True)
    with patch.object(
        infra_service,
        "_run_ssh",
        new=AsyncMock(
            return_value={"ok": True, "exit_code": 0, "stdout": "node/a\n", "stderr": ""}
        ),
    ):
        result = await infra_service.provision_infra(db_pool, row["id"], SECRET_KEY)
    assert result["status"] == "ready"


def _chat_ctx():
    from types import SimpleNamespace

    from aegis.services.chat import ToolContext

    return ToolContext(settings=SimpleNamespace(secret_key=SECRET_KEY))


async def test_chat_list_pods_dispatches_to_registry_cluster(db_pool):
    from aegis.services.chat import _exec_list_pods

    await _prepare(db_pool)
    row = await _create_k8s(db_pool)
    seen: list[list[str]] = []

    async def fake_run(args, timeout=30, stdin=None):
        seen.append(args)
        return {"ok": True, "exit_code": 0, "stdout": json.dumps(_POD_JSON), "stderr": ""}

    with patch.object(infra_service, "_run_ssh", new=AsyncMock(side_effect=fake_run)):
        raw = await _exec_list_pods(db_pool, {"context": row["slug"]}, _chat_ctx())

    payload = json.loads(raw)
    assert payload["pods"][0]["name"] == "web-1"
    assert seen[0][0] == "kubectl"
    # omitted namespace => all namespaces
    assert "--all-namespaces" in seen[0]


async def test_chat_list_pods_status_filter(db_pool):
    from aegis.services.chat import _exec_list_pods

    await _prepare(db_pool)
    row = await _create_k8s(db_pool)
    with patch.object(
        infra_service,
        "_run_ssh",
        new=AsyncMock(
            return_value={
                "ok": True,
                "exit_code": 0,
                "stdout": json.dumps(_POD_JSON),
                "stderr": "",
            }
        ),
    ):
        raw = await _exec_list_pods(
            db_pool, {"context": row["slug"], "status": "Pending"}, _chat_ctx()
        )
    assert json.loads(raw)["pods"] == []


async def test_chat_unknown_context_still_errors(db_pool):
    from aegis.services.chat import _exec_list_pods

    await _prepare(db_pool)
    raw = await _exec_list_pods(db_pool, {"context": "nope-cluster"}, _chat_ctx())
    assert "Unsupported context" in json.loads(raw)["error"]


async def test_chat_argocd_rejected_for_registry_cluster(db_pool):
    from aegis.services.chat import _exec_list_argocd_apps

    await _prepare(db_pool)
    row = await _create_k8s(db_pool)
    raw = await _exec_list_argocd_apps(db_pool, {"context": row["slug"]}, _chat_ctx())
    assert "not available for registry k8s clusters" in json.loads(raw)["error"]


async def test_chat_restart_deployment_and_read_only(db_pool):
    from aegis.services.chat import _exec_restart_deployment

    await _prepare(db_pool)
    row = await _create_k8s(db_pool)
    with patch.object(
        infra_service,
        "_run_ssh",
        new=AsyncMock(
            return_value={
                "ok": True,
                "exit_code": 0,
                "stdout": "deployment.apps/web restarted",
                "stderr": "",
            }
        ),
    ):
        raw = await _exec_restart_deployment(
            db_pool,
            {"context": row["slug"], "namespace": "default", "deployment_name": "web"},
            _chat_ctx(),
        )
    assert "restarted" in json.loads(raw)["output"]

    # read-only entry refuses via chat as well
    ro = await _create_k8s(
        db_pool, name="test-cred-cluster-ro", slug="test-cred-ro", read_only=True
    )
    raw = await _exec_restart_deployment(
        db_pool,
        {"context": ro["slug"], "namespace": "default", "deployment_name": "web"},
        _chat_ctx(),
    )
    assert "read-only" in json.loads(raw)["error"]

    # unknown slug
    raw = await _exec_restart_deployment(
        db_pool, {"context": "ghost", "namespace": "default", "deployment_name": "web"}, _chat_ctx()
    )
    assert "Unknown k8s cluster" in json.loads(raw)["error"]
