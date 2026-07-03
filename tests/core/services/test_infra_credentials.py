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

    async def fake_run_ssh(ssh_args, timeout=30, stdin=None, env=None, stdout_cap=0):
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

    async def fake_run(args, timeout=30, stdin=None, env=None, stdout_cap=0):
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

    async def fake_run(args, timeout=30, stdin=None, env=None, stdout_cap=0):
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

    async def fake_run(args, timeout=30, stdin=None, env=None, stdout_cap=0):
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


async def test_chat_restart_service_blocked_by_read_only_registry_entry(db_pool):
    from unittest.mock import MagicMock

    from aegis.services.chat import ToolContext, _exec_restart_service

    await _prepare(db_pool)
    # A registered swarm entry mapping to the 'swarm' context via docker_context.
    await _create(db_pool, docker_context="swarm", read_only=True)

    connector = MagicMock()
    connector.run_script = AsyncMock()
    ctx = ToolContext(remote_script_connector=connector)

    raw = await _exec_restart_service(
        db_pool, {"context": "swarm", "service_name": "aegis_core"}, ctx
    )
    assert "read-only" in json.loads(raw)["error"]
    connector.run_script.assert_not_awaited()


async def test_chat_restart_service_allowed_when_not_read_only(db_pool):
    from unittest.mock import MagicMock

    from aegis.services.chat import ToolContext, _exec_restart_service

    await _prepare(db_pool)
    await _create(db_pool, docker_context="swarm", read_only=False)

    connector = MagicMock()
    connector.run_script = AsyncMock(
        return_value={"status": "succeeded", "exit_code": 0, "stdout": "ok", "stderr": ""}
    )
    ctx = ToolContext(remote_script_connector=connector)

    raw = await _exec_restart_service(
        db_pool, {"context": "swarm", "service_name": "aegis_core"}, ctx
    )
    assert raw == "ok"
    connector.run_script.assert_awaited_once()


async def test_chat_list_services_unaffected_by_read_only(db_pool):
    from unittest.mock import MagicMock

    from aegis.services.chat import ToolContext, _exec_list_services

    await _prepare(db_pool)
    await _create(db_pool, docker_context="swarm", read_only=True)

    connector = MagicMock()
    connector.run_script = AsyncMock(
        return_value={"status": "succeeded", "exit_code": 0, "stdout": "[]", "stderr": ""}
    )
    ctx = ToolContext(remote_script_connector=connector)

    await _exec_list_services(db_pool, {"context": "swarm"}, ctx)
    connector.run_script.assert_awaited_once()


# ── auth env + AWS credentials file (exec-plugin kubeconfigs, e.g. EKS) ─────

FAKE_AUTH_ENV = {"AWS_ACCESS_KEY_ID": "AKIATEST", "AWS_SECRET_ACCESS_KEY": "shhh-secret"}
FAKE_AWS_CREDS = "[myprofile]\naws_access_key_id = AKIATEST\naws_secret_access_key = shhh-secret"
FAKE_GCP_SA = json.dumps(
    {"type": "service_account", "project_id": "test-proj", "private_key": "gcp-shhh-secret"}
)


async def test_auth_env_stored_encrypted_and_flagged(db_pool):
    await _prepare(db_pool)
    row = await _create_k8s(db_pool, auth_env=FAKE_AUTH_ENV, aws_credentials_file=FAKE_AWS_CREDS)

    assert row["has_auth_env"] is True
    assert row["has_aws_credentials"] is True
    assert "shhh-secret" not in str(row)

    stored = await db_pool.fetchval("SELECT credentials FROM infra WHERE id = $1", row["id"])
    assert stored["auth_env_enc"]["encrypted"] is True
    assert stored["aws_credentials_file_enc"]["encrypted"] is True
    assert "shhh-secret" not in str(stored)

    # Blank update keeps both.
    updated = await infra_service.update_infra(db_pool, row["id"], {"name": "renamed"}, SECRET_KEY)
    assert updated["has_auth_env"] is True and updated["has_aws_credentials"] is True


async def test_k8s_auth_env_injected_into_kubectl(db_pool):
    await _prepare(db_pool)
    row = await _create_k8s(db_pool, auth_env=FAKE_AUTH_ENV, aws_credentials_file=FAKE_AWS_CREDS)
    seen: dict = {}

    async def fake_run(args, timeout=30, stdin=None, env=None, stdout_cap=0):
        seen["env"] = env
        seen["creds_path"] = env.get("AWS_SHARED_CREDENTIALS_FILE") if env else None
        if seen["creds_path"]:
            seen["creds_content"] = pathlib.Path(seen["creds_path"]).read_text()
            seen["creds_mode"] = oct(os.stat(seen["creds_path"]).st_mode & 0o777)
        return {"ok": True, "exit_code": 0, "stdout": json.dumps(_POD_JSON), "stderr": ""}

    with patch.object(infra_service, "_run_ssh", new=AsyncMock(side_effect=fake_run)):
        result = await infra_service.k8s_list_pods(db_pool, row["id"], SECRET_KEY, "default")

    assert result["ok"] is True
    assert seen["env"]["AWS_ACCESS_KEY_ID"] == "AKIATEST"
    assert seen["env"]["AWS_SECRET_ACCESS_KEY"] == "shhh-secret"
    assert seen["env"].get("PATH")  # os.environ inherited, exec plugin can find binaries
    assert seen["creds_content"] == FAKE_AWS_CREDS
    assert seen["creds_mode"] == "0o600"
    assert not os.path.exists(seen["creds_path"])  # cleaned up after the call


async def test_gcp_service_account_stored_encrypted_and_flagged(db_pool):
    await _prepare(db_pool)
    row = await _create_k8s(db_pool, gcp_service_account_json=FAKE_GCP_SA)

    assert row["has_gcp_service_account"] is True
    assert "gcp-shhh-secret" not in str(row)

    stored = await db_pool.fetchval("SELECT credentials FROM infra WHERE id = $1", row["id"])
    assert stored["gcp_service_account_json_enc"]["encrypted"] is True
    assert "gcp-shhh-secret" not in str(stored)

    # Blank update keeps it.
    updated = await infra_service.update_infra(db_pool, row["id"], {"name": "renamed"}, SECRET_KEY)
    assert updated["has_gcp_service_account"] is True


async def test_gcp_credentials_injected_into_kubectl(db_pool):
    await _prepare(db_pool)
    row = await _create_k8s(db_pool, gcp_service_account_json=FAKE_GCP_SA)
    seen: dict = {}

    async def fake_run(args, timeout=30, stdin=None, env=None, stdout_cap=0):
        seen["env"] = env
        seen["key_path"] = env.get("GOOGLE_APPLICATION_CREDENTIALS") if env else None
        if seen["key_path"]:
            seen["key_content"] = pathlib.Path(seen["key_path"]).read_text()
            seen["key_mode"] = oct(os.stat(seen["key_path"]).st_mode & 0o777)
        return {"ok": True, "exit_code": 0, "stdout": json.dumps(_POD_JSON), "stderr": ""}

    with patch.object(infra_service, "_run_ssh", new=AsyncMock(side_effect=fake_run)):
        result = await infra_service.k8s_list_pods(db_pool, row["id"], SECRET_KEY, "default")

    assert result["ok"] is True
    assert seen["env"]["CLOUDSDK_CORE_DISABLE_PROMPTS"] == "1"
    assert seen["env"].get("PATH")  # os.environ inherited, exec plugin can find binaries
    assert seen["key_content"] == FAKE_GCP_SA
    assert seen["key_mode"] == "0o600"
    assert not os.path.exists(seen["key_path"])  # cleaned up after the call


async def test_aws_and_gcp_credentials_coexist(db_pool):
    await _prepare(db_pool)
    row = await _create_k8s(
        db_pool, aws_credentials_file=FAKE_AWS_CREDS, gcp_service_account_json=FAKE_GCP_SA
    )
    seen: dict = {}

    async def fake_run(args, timeout=30, stdin=None, env=None, stdout_cap=0):
        seen["aws"] = pathlib.Path(env["AWS_SHARED_CREDENTIALS_FILE"]).read_text()
        seen["gcp"] = pathlib.Path(env["GOOGLE_APPLICATION_CREDENTIALS"]).read_text()
        seen["paths"] = (env["AWS_SHARED_CREDENTIALS_FILE"], env["GOOGLE_APPLICATION_CREDENTIALS"])
        return {"ok": True, "exit_code": 0, "stdout": json.dumps(_POD_JSON), "stderr": ""}

    with patch.object(infra_service, "_run_ssh", new=AsyncMock(side_effect=fake_run)):
        result = await infra_service.k8s_list_pods(db_pool, row["id"], SECRET_KEY, "default")

    assert result["ok"] is True
    assert seen["aws"] == FAKE_AWS_CREDS
    assert seen["gcp"] == FAKE_GCP_SA
    assert all(not os.path.exists(p) for p in seen["paths"])  # both cleaned up


async def test_k8s_auth_env_absent_inherits_process_env(db_pool):
    await _prepare(db_pool)
    row = await _create_k8s(db_pool)  # kubeconfig only, no auth material
    seen: dict = {"env": "unset"}

    async def fake_run(args, timeout=30, stdin=None, env=None, stdout_cap=0):
        seen["env"] = env
        return {"ok": True, "exit_code": 0, "stdout": json.dumps(_POD_JSON), "stderr": ""}

    with patch.object(infra_service, "_run_ssh", new=AsyncMock(side_effect=fake_run)):
        await infra_service.k8s_list_pods(db_pool, row["id"], SECRET_KEY, "default")
    assert seen["env"] is None  # inherit — no override needed


async def test_k8s_provision_uses_auth_env(db_pool):
    await _prepare(db_pool)
    row = await _create_k8s(db_pool, auth_env=FAKE_AUTH_ENV)
    seen: dict = {}

    async def fake_run(args, timeout=30, stdin=None, env=None, stdout_cap=0):
        seen["env"] = env
        return {"ok": True, "exit_code": 0, "stdout": "node/a\n", "stderr": ""}

    with patch.object(infra_service, "_run_ssh", new=AsyncMock(side_effect=fake_run)):
        result = await infra_service.provision_infra(db_pool, row["id"], SECRET_KEY)
    assert result["status"] == "ready"
    assert seen["env"]["AWS_ACCESS_KEY_ID"] == "AKIATEST"


# ── regressions: update-only-secret-fields no-op + kubectl stdout cap ───────


async def test_update_with_only_aws_credentials_replaces_stored(db_pool):
    """PUT with ONLY auth_env/aws_credentials_file must persist (regression:
    the credentials merge used to trigger only on ssh_private_key/kubeconfig,
    silently no-opping such updates)."""
    await _prepare(db_pool)
    row = await _create_k8s(db_pool, aws_credentials_file="[old]\nx = 1")

    updated = await infra_service.update_infra(
        db_pool, row["id"], {"aws_credentials_file": "[new]\ny = 2"}, SECRET_KEY
    )
    assert updated["has_aws_credentials"] is True
    full = await infra_service.get_infra(db_pool, row["id"], include_credentials=True)
    seen: dict = {}

    async def fake_run(args, timeout=30, stdin=None, env=None, stdout_cap=0):
        seen["creds"] = pathlib.Path(env["AWS_SHARED_CREDENTIALS_FILE"]).read_text()
        return {"ok": True, "exit_code": 0, "stdout": json.dumps(_POD_JSON), "stderr": ""}

    with patch.object(infra_service, "_run_ssh", new=AsyncMock(side_effect=fake_run)):
        await infra_service.k8s_list_pods(db_pool, full["id"], SECRET_KEY, "default")
    assert seen["creds"] == "[new]\ny = 2"

    # Same for auth_env alone.
    updated = await infra_service.update_infra(
        db_pool, row["id"], {"auth_env": {"K": "V"}}, SECRET_KEY
    )
    assert updated["has_auth_env"] is True


async def test_update_with_only_gcp_service_account_replaces_stored(db_pool):
    """PUT with ONLY gcp_service_account_json must persist (same regression
    path as auth_env/aws_credentials_file — the new field must be in
    _SECRET_INPUT_FIELDS so the credentials merge triggers)."""
    await _prepare(db_pool)
    row = await _create_k8s(db_pool, gcp_service_account_json='{"old": 1}')

    updated = await infra_service.update_infra(
        db_pool, row["id"], {"gcp_service_account_json": '{"new": 2}'}, SECRET_KEY
    )
    assert updated["has_gcp_service_account"] is True
    seen: dict = {}

    async def fake_run(args, timeout=30, stdin=None, env=None, stdout_cap=0):
        seen["key"] = pathlib.Path(env["GOOGLE_APPLICATION_CREDENTIALS"]).read_text()
        return {"ok": True, "exit_code": 0, "stdout": json.dumps(_POD_JSON), "stderr": ""}

    with patch.object(infra_service, "_run_ssh", new=AsyncMock(side_effect=fake_run)):
        await infra_service.k8s_list_pods(db_pool, row["id"], SECRET_KEY, "default")
    assert seen["key"] == '{"new": 2}'


async def test_kubectl_output_not_truncated_at_provision_cap(db_pool):
    """kubectl -o json on a real cluster is megabytes; the 16 KiB provisioning
    stdout cap must not apply (regression: truncated JSON -> 'unparseable')."""
    await _prepare(db_pool)
    row = await _create_k8s(db_pool)

    big = {
        "items": [
            {
                "metadata": {"name": f"pod-{i}", "namespace": "default"},
                "spec": {"nodeName": "n"},
                "status": {"phase": "Running", "containerStatuses": []},
            }
            for i in range(2000)
        ]
    }
    payload = json.dumps(big)
    assert len(payload) > 16 * 1024  # would have been truncated by the old cap

    async def fake_exec(*args, stdin=None, stdout=None, stderr=None, env=None):
        class P:
            returncode = 0

            async def communicate(self, input=None):
                return payload.encode(), b""

        return P()

    with (
        patch.object(infra_service.asyncio, "create_subprocess_exec", new=fake_exec),
        patch.object(infra_service, "kill_and_wait", new=AsyncMock()),
    ):
        result = await infra_service.k8s_list_pods(db_pool, row["id"], SECRET_KEY, "default")

    assert result["ok"] is True
    assert len(result["pods"]) == 2000
