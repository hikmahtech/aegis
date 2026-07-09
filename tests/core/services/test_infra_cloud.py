"""Cloud provider accounts (kind=cloud) + k8s cloud-account references.

Real-Postgres tests (db_pool fixture). Subprocess and CLI-availability are
mocked at the same seams the kubectl tests use (`_run_ssh`, `_cloud_cli_path`)
— the validation, credential-resolution, and never-leak-a-token guarantees are
what matter here.
"""

from __future__ import annotations

import json
import os
import pathlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from aegis.db import run_migrations
from aegis.services import infra as infra_service

SECRET_KEY = "test-secret-key"
FAKE_AWS_INI = (
    "[default]\naws_access_key_id = AKIADEFAULT\naws_secret_access_key = shhh-default\n"
    "[prod]\naws_access_key_id = AKIAPROD\naws_secret_access_key = shhh-prod\n"
)
FAKE_GCP_SA = json.dumps(
    {
        "type": "service_account",
        "project_id": "acme-main",
        "client_email": "aegis@acme-main.iam.gserviceaccount.com",
        "private_key": "gcp-shhh-secret",
    }
)
FAKE_KUBECONFIG = "apiVersion: v1\nkind: Config"

STS_JSON = json.dumps(
    {
        "UserId": "AIDATEST",
        "Account": "123456789012",
        "Arn": "arn:aws:iam::123456789012:user/aegis",
    }
)

OK = {"ok": True, "exit_code": 0, "stdout": "", "stderr": ""}


async def _prepare(db_pool):
    await run_migrations(db_pool)
    await db_pool.execute("DELETE FROM infra WHERE slug LIKE 'test-cloud-%'")


async def _create_aws(db_pool, **overrides):
    data = {
        "name": "test-cloud-aws",
        "slug": "test-cloud-aws",
        "kind": "cloud",
        "cloud": {"provider": "aws", "default_profile": "prod", "region": "eu-west-2"},
        "aws_credentials_file": FAKE_AWS_INI,
        **overrides,
    }
    return await infra_service.create_infra(db_pool, data, SECRET_KEY)


async def _create_gcp(db_pool, **overrides):
    data = {
        "name": "test-cloud-gcp",
        "slug": "test-cloud-gcp",
        "kind": "cloud",
        "cloud": {"provider": "gcp", "project": "acme-main"},
        "gcp_service_account_json": FAKE_GCP_SA,
        **overrides,
    }
    return await infra_service.create_infra(db_pool, data, SECRET_KEY)


def _cli(path="/usr/local/bin/x"):
    return patch.object(infra_service, "_cloud_cli_path", return_value=path)


def _no_cli():
    return patch.object(infra_service, "_cloud_cli_path", return_value=None)


# ── CRUD + validation ────────────────────────────────────────────────────────


async def test_create_aws_account_normalizes_and_sanitizes(db_pool):
    await _prepare(db_pool)
    row = await _create_aws(db_pool, cloud={"provider": " AWS ", "default_profile": "prod",
                                            "region": "eu-west-2", "identity": {"spoofed": 1}})
    assert row["cloud"] == {"provider": "aws", "default_profile": "prod", "region": "eu-west-2"}
    assert row["has_aws_credentials"] is True
    assert "credentials" not in row
    assert "shhh-prod" not in str(row)


async def test_create_gcp_account(db_pool):
    await _prepare(db_pool)
    row = await _create_gcp(db_pool)
    assert row["cloud"] == {"provider": "gcp", "project": "acme-main"}
    assert row["has_gcp_service_account"] is True
    assert "gcp-shhh-secret" not in str(row)


async def test_create_unknown_kind_rejected(db_pool):
    await _prepare(db_pool)
    with pytest.raises(ValueError, match="unknown kind"):
        await infra_service.create_infra(
            db_pool, {"name": "test-cloud-bad", "kind": "florb"}, SECRET_KEY
        )


async def test_cloud_kind_requires_valid_provider(db_pool):
    await _prepare(db_pool)
    for cloud in (None, {}, {"provider": "azure"}):
        with pytest.raises(ValueError, match="cloud.provider"):
            await infra_service.create_infra(
                db_pool, {"name": "test-cloud-noprov", "kind": "cloud", "cloud": cloud}, SECRET_KEY
            )


async def test_cloud_block_rejected_on_ssh_kinds(db_pool):
    await _prepare(db_pool)
    with pytest.raises(ValueError, match="not valid for a kind=swarm"):
        await infra_service.create_infra(
            db_pool,
            {"name": "test-cloud-swarm", "kind": "swarm", "cloud": {"provider": "aws"}},
            SECRET_KEY,
        )


async def test_bad_profile_rejected(db_pool):
    await _prepare(db_pool)
    with pytest.raises(ValueError, match="default_profile"):
        await _create_aws(db_pool, cloud={"provider": "aws", "default_profile": "ha ha; rm"})


async def test_k8s_cloud_slug_must_reference_cloud_entry(db_pool):
    await _prepare(db_pool)
    account = await _create_aws(db_pool)
    swarm = await infra_service.create_infra(
        db_pool, {"name": "test-cloud-swarm-ref", "kind": "swarm"}, SECRET_KEY
    )

    def k8s_data(cloud):
        return {"name": "test-cloud-k8s", "slug": "test-cloud-k8s", "kind": "k8s",
                "kubeconfig": FAKE_KUBECONFIG, "cloud": cloud}

    with pytest.raises(ValueError, match="does not reference a kind=cloud"):
        await infra_service.create_infra(db_pool, k8s_data({"cloud_slug": "test-cloud-ghost"}),
                                         SECRET_KEY)
    with pytest.raises(ValueError, match="does not reference a kind=cloud"):
        await infra_service.create_infra(db_pool, k8s_data({"cloud_slug": swarm["slug"]}),
                                         SECRET_KEY)
    with pytest.raises(ValueError, match="requires cloud.cloud_slug"):
        await infra_service.create_infra(db_pool, k8s_data({"profile": "prod"}), SECRET_KEY)

    row = await infra_service.create_infra(
        db_pool, k8s_data({"cloud_slug": account["slug"], "profile": "prod"}), SECRET_KEY
    )
    assert row["cloud"] == {"cloud_slug": account["slug"], "profile": "prod"}


async def test_update_preserves_identity_within_provider(db_pool):
    await _prepare(db_pool)
    row = await _create_aws(db_pool)
    # Simulate a prior provision writing the identity.
    await db_pool.execute(
        "UPDATE infra SET cloud = jsonb_set(cloud, '{identity}', $2::jsonb) WHERE id = $1",
        row["id"],
        {"account_id": "123456789012", "arn": "arn:aws:iam::123456789012:user/aegis"},
    )

    updated = await infra_service.update_infra(
        db_pool, row["id"],
        {"cloud": {"provider": "aws", "default_profile": "prod", "region": "us-east-1"}},
        SECRET_KEY,
    )
    assert updated["cloud"]["region"] == "us-east-1"
    assert updated["cloud"]["identity"]["account_id"] == "123456789012"

    # Switching provider drops the stale identity.
    switched = await infra_service.update_infra(
        db_pool, row["id"], {"cloud": {"provider": "gcp", "project": "p"}}, SECRET_KEY
    )
    assert "identity" not in switched["cloud"]


async def test_update_kind_change_revalidates_cloud_block(db_pool):
    await _prepare(db_pool)
    row = await _create_aws(db_pool)
    with pytest.raises(ValueError, match="not valid for a kind=ssh_host"):
        await infra_service.update_infra(db_pool, row["id"], {"kind": "ssh_host"}, SECRET_KEY)


# ── provisioning (identity check) ────────────────────────────────────────────


async def test_provision_aws_runs_sts_and_stores_identity(db_pool):
    await _prepare(db_pool)
    row = await _create_aws(db_pool)
    seen: dict = {}

    async def fake_run(args, timeout=30, stdin=None, env=None, stdout_cap=0):
        seen["args"] = args
        seen["profile"] = env.get("AWS_PROFILE")
        seen["region"] = env.get("AWS_DEFAULT_REGION")
        seen["creds"] = pathlib.Path(env["AWS_SHARED_CREDENTIALS_FILE"]).read_text()
        return {**OK, "stdout": STS_JSON}

    with _cli(), patch.object(infra_service, "_run_ssh", new=AsyncMock(side_effect=fake_run)):
        result = await infra_service.provision_infra(db_pool, row["id"], SECRET_KEY)

    assert result["status"] == "ready"
    assert seen["args"] == ["aws", "sts", "get-caller-identity", "--output", "json"]
    assert seen["profile"] == "prod"
    assert seen["region"] == "eu-west-2"
    assert seen["creds"] == FAKE_AWS_INI

    steps = {s["step"]: s for s in result["log"]}
    assert steps["aws_identity_check"]["ok"] is True
    assert "123456789012" in steps["aws_identity_check"]["stdout"]

    stored = await db_pool.fetchval("SELECT cloud FROM infra WHERE id = $1", row["id"])
    assert stored["identity"]["account_id"] == "123456789012"
    assert stored["identity"]["arn"] == "arn:aws:iam::123456789012:user/aegis"
    assert result["cloud"]["identity"]["account_id"] == "123456789012"


async def test_provision_aws_cli_absent_is_a_clear_error(db_pool):
    await _prepare(db_pool)
    row = await _create_aws(db_pool)
    with _no_cli(), patch.object(infra_service, "_run_ssh", new=AsyncMock()) as run:
        result = await infra_service.provision_infra(db_pool, row["id"], SECRET_KEY)
    assert result["status"] == "error"
    assert "EXTRA_CLOUD_CLIS=aws" in result["last_error"]
    run.assert_not_awaited()


async def test_provision_aws_without_credentials(db_pool):
    await _prepare(db_pool)
    row = await _create_aws(db_pool, aws_credentials_file=None)
    with _cli():
        result = await infra_service.provision_infra(db_pool, row["id"], SECRET_KEY)
    assert result["status"] == "error"
    assert "no AWS credentials stored" in result["last_error"]


async def test_provision_gcp_checks_adc_token_and_never_leaks_it(db_pool):
    await _prepare(db_pool)
    row = await _create_gcp(db_pool)
    token = "ya29.SECRET-ACCESS-TOKEN"
    seen: dict = {}

    async def fake_run(args, timeout=30, stdin=None, env=None, stdout_cap=0):
        seen["args"] = args
        seen["prompts"] = env.get("CLOUDSDK_CORE_DISABLE_PROMPTS")
        seen["sa"] = pathlib.Path(env["GOOGLE_APPLICATION_CREDENTIALS"]).read_text()
        return {**OK, "stdout": token}

    with _cli(), patch.object(infra_service, "_run_ssh", new=AsyncMock(side_effect=fake_run)):
        result = await infra_service.provision_infra(db_pool, row["id"], SECRET_KEY)

    assert result["status"] == "ready"
    assert seen["args"] == ["gcloud", "auth", "application-default", "print-access-token"]
    assert seen["prompts"] == "1"
    assert seen["sa"] == FAKE_GCP_SA
    assert token not in json.dumps(result, default=str)  # the token never leaves the check

    stored = await db_pool.fetchval("SELECT cloud FROM infra WHERE id = $1", row["id"])
    assert stored["identity"] == {
        "project": "acme-main",
        "service_account": "aegis@acme-main.iam.gserviceaccount.com",
    }


async def test_provision_gcp_cli_absent(db_pool):
    await _prepare(db_pool)
    row = await _create_gcp(db_pool)
    with _no_cli():
        result = await infra_service.provision_infra(db_pool, row["id"], SECRET_KEY)
    assert result["status"] == "error"
    assert "EXTRA_CLOUD_CLIS=gcloud" in result["last_error"]


async def test_provision_gcp_without_service_account(db_pool):
    await _prepare(db_pool)
    row = await _create_gcp(db_pool, gcp_service_account_json=None)
    with _cli():
        result = await infra_service.provision_infra(db_pool, row["id"], SECRET_KEY)
    assert result["status"] == "error"
    assert "no GCP service account" in result["last_error"]


async def test_provision_aws_failure_surfaces_stderr(db_pool):
    await _prepare(db_pool)
    row = await _create_aws(db_pool)
    with (
        _cli(),
        patch.object(
            infra_service,
            "_run_ssh",
            new=AsyncMock(
                return_value={"ok": False, "exit_code": 254, "stdout": "",
                              "stderr": "An error occurred (ExpiredToken)"}
            ),
        ),
    ):
        result = await infra_service.provision_infra(db_pool, row["id"], SECRET_KEY)
    assert result["status"] == "error"
    assert "ExpiredToken" in result["last_error"]


# ── k8s entries referencing a cloud account ─────────────────────────────────

_POD_JSON = json.dumps({"items": []})


async def _create_k8s_ref(db_pool, account_slug, profile="", **overrides):
    cloud = {"cloud_slug": account_slug}
    if profile:
        cloud["profile"] = profile
    data = {
        "name": "test-cloud-k8s-ref",
        "slug": "test-cloud-k8s-ref",
        "kind": "k8s",
        "kubeconfig": FAKE_KUBECONFIG,
        "cloud": cloud,
        **overrides,
    }
    return await infra_service.create_infra(db_pool, data, SECRET_KEY)


async def test_k8s_pulls_credentials_and_default_profile_from_account(db_pool):
    await _prepare(db_pool)
    account = await _create_aws(db_pool)
    row = await _create_k8s_ref(db_pool, account["slug"])
    seen: dict = {}

    async def fake_run(args, timeout=30, stdin=None, env=None, stdout_cap=0):
        seen["creds"] = pathlib.Path(env["AWS_SHARED_CREDENTIALS_FILE"]).read_text()
        seen["profile"] = env.get("AWS_PROFILE")
        return {**OK, "stdout": _POD_JSON}

    with patch.object(infra_service, "_run_ssh", new=AsyncMock(side_effect=fake_run)):
        result = await infra_service.k8s_list_pods(db_pool, row["id"], SECRET_KEY, "default")

    assert result["ok"] is True
    assert seen["creds"] == FAKE_AWS_INI  # the ACCOUNT's ini, not inline creds
    assert seen["profile"] == "prod"  # account default_profile


async def test_k8s_entry_profile_overrides_account_default(db_pool):
    await _prepare(db_pool)
    account = await _create_aws(db_pool)
    row = await _create_k8s_ref(db_pool, account["slug"], profile="staging")
    seen: dict = {}

    async def fake_run(args, timeout=30, stdin=None, env=None, stdout_cap=0):
        seen["profile"] = env.get("AWS_PROFILE")
        return {**OK, "stdout": _POD_JSON}

    with patch.object(infra_service, "_run_ssh", new=AsyncMock(side_effect=fake_run)):
        await infra_service.k8s_list_pods(db_pool, row["id"], SECRET_KEY, "default")
    assert seen["profile"] == "staging"


async def test_k8s_falls_back_to_inline_creds_when_account_has_none(db_pool):
    await _prepare(db_pool)
    # An account whose credentials are auth_env only — no ini to materialize.
    account = await _create_aws(
        db_pool, aws_credentials_file=None, auth_env={"AWS_ACCESS_KEY_ID": "AKIAENV"}
    )
    inline = "[inline]\naws_access_key_id = AKIAINLINE\naws_secret_access_key = s\n"
    row = await _create_k8s_ref(db_pool, account["slug"], aws_credentials_file=inline)
    seen: dict = {}

    async def fake_run(args, timeout=30, stdin=None, env=None, stdout_cap=0):
        seen["creds"] = pathlib.Path(env["AWS_SHARED_CREDENTIALS_FILE"]).read_text()
        return {**OK, "stdout": _POD_JSON}

    with patch.object(infra_service, "_run_ssh", new=AsyncMock(side_effect=fake_run)):
        result = await infra_service.k8s_list_pods(db_pool, row["id"], SECRET_KEY, "default")
    assert result["ok"] is True
    assert seen["creds"] == inline


async def test_k8s_provision_uses_account_credentials(db_pool):
    await _prepare(db_pool)
    account = await _create_aws(db_pool)
    row = await _create_k8s_ref(db_pool, account["slug"])
    seen: dict = {}

    async def fake_run(args, timeout=30, stdin=None, env=None, stdout_cap=0):
        seen["profile"] = env.get("AWS_PROFILE")
        seen["creds"] = pathlib.Path(env["AWS_SHARED_CREDENTIALS_FILE"]).read_text()
        return {**OK, "stdout": "node/a\n"}

    with patch.object(infra_service, "_run_ssh", new=AsyncMock(side_effect=fake_run)):
        result = await infra_service.provision_infra(db_pool, row["id"], SECRET_KEY)
    assert result["status"] == "ready"
    assert seen["creds"] == FAKE_AWS_INI
    assert seen["profile"] == "prod"


async def test_k8s_dangling_cloud_ref_is_400(db_pool):
    await _prepare(db_pool)
    account = await _create_aws(db_pool)
    row = await _create_k8s_ref(db_pool, account["slug"])
    await infra_service.delete_infra(db_pool, account["id"])

    result = await infra_service.k8s_list_pods(db_pool, row["id"], SECRET_KEY, "default")
    assert result["ok"] is False and result["status_code"] == 400
    assert "does not reference a kind=cloud" in result["error"]

    provisioned = await infra_service.provision_infra(db_pool, row["id"], SECRET_KEY)
    assert provisioned["status"] == "error"
    assert "does not reference a kind=cloud" in provisioned["last_error"]


async def test_gcp_account_ref_sets_no_aws_profile(db_pool):
    await _prepare(db_pool)
    account = await _create_gcp(db_pool)
    row = await _create_k8s_ref(db_pool, account["slug"])
    seen: dict = {}

    async def fake_run(args, timeout=30, stdin=None, env=None, stdout_cap=0):
        seen["profile"] = env.get("AWS_PROFILE") if env else None
        seen["sa"] = pathlib.Path(env["GOOGLE_APPLICATION_CREDENTIALS"]).read_text()
        return {**OK, "stdout": _POD_JSON}

    with patch.object(infra_service, "_run_ssh", new=AsyncMock(side_effect=fake_run)):
        await infra_service.k8s_list_pods(db_pool, row["id"], SECRET_KEY, "default")
    assert seen["profile"] is None
    assert seen["sa"] == FAKE_GCP_SA


def test_cloud_auth_env_profile_wins_over_auth_env():
    from aegis.crypto import encrypt_secret

    infra = {
        "credentials": {
            "auth_env_enc": encrypt_secret(json.dumps({"AWS_PROFILE": "from-auth-env"}), SECRET_KEY)
        }
    }
    with infra_service.cloud_auth_env(infra, SECRET_KEY, aws_profile="explicit") as env:
        assert env["AWS_PROFILE"] == "explicit"


# ── chat tools ───────────────────────────────────────────────────────────────


def _chat_ctx():
    from aegis.services.chat import ToolContext

    return ToolContext(settings=SimpleNamespace(secret_key=SECRET_KEY))


async def test_chat_list_cloud_accounts(db_pool):
    from aegis.services.chat import _exec_list_cloud_accounts

    await _prepare(db_pool)
    await _create_aws(db_pool)
    await _create_gcp(db_pool)

    payload = json.loads(await _exec_list_cloud_accounts(db_pool, {}, _chat_ctx()))
    accounts = {a["slug"]: a for a in payload["accounts"]
                if a["slug"].startswith("test-cloud-")}
    assert accounts["test-cloud-aws"]["provider"] == "aws"
    assert accounts["test-cloud-aws"]["default_profile"] == "prod"
    assert accounts["test-cloud-aws"]["has_credentials"] is True
    assert accounts["test-cloud-gcp"]["provider"] == "gcp"
    assert accounts["test-cloud-gcp"]["project"] == "acme-main"
    assert "shhh" not in json.dumps(payload)


async def test_chat_list_cloud_accounts_empty(db_pool):
    from aegis.services.chat import _exec_list_cloud_accounts

    await _prepare(db_pool)
    await db_pool.execute("DELETE FROM infra WHERE kind = 'cloud'")
    payload = json.loads(await _exec_list_cloud_accounts(db_pool, {}, _chat_ctx()))
    assert payload["accounts"] == []
    assert "kind=cloud" in payload["note"]


async def test_chat_cloud_identity_live_check_with_profile_override(db_pool):
    from aegis.services.chat import _exec_cloud_identity

    await _prepare(db_pool)
    row = await _create_aws(db_pool)
    seen: dict = {}

    async def fake_run(args, timeout=30, stdin=None, env=None, stdout_cap=0):
        seen["profile"] = env.get("AWS_PROFILE")
        return {**OK, "stdout": STS_JSON}

    with _cli(), patch.object(infra_service, "_run_ssh", new=AsyncMock(side_effect=fake_run)):
        raw = await _exec_cloud_identity(
            db_pool, {"slug": row["slug"], "profile": "staging"}, _chat_ctx()
        )

    payload = json.loads(raw)
    assert payload["provider"] == "aws"
    assert payload["identity"]["account_id"] == "123456789012"
    assert seen["profile"] == "staging"

    # Identity is a live check — it must NOT flip the row's provision status.
    fresh = await infra_service.get_infra(db_pool, row["id"])
    assert fresh["status"] == "unprovisioned"


async def test_chat_cloud_identity_cli_absent_envelope(db_pool):
    from aegis.services.chat import _exec_cloud_identity

    await _prepare(db_pool)
    row = await _create_aws(db_pool)
    with _no_cli():
        raw = await _exec_cloud_identity(db_pool, {"slug": row["slug"]}, _chat_ctx())
    assert "EXTRA_CLOUD_CLIS=aws" in json.loads(raw)["error"]


async def test_chat_cloud_identity_unknown_or_non_cloud_slug(db_pool):
    from aegis.services.chat import _exec_cloud_identity

    await _prepare(db_pool)
    raw = await _exec_cloud_identity(db_pool, {"slug": "test-cloud-ghost"}, _chat_ctx())
    assert "Unknown cloud account" in json.loads(raw)["error"]

    swarm = await infra_service.create_infra(
        db_pool, {"name": "test-cloud-swarm2", "kind": "swarm"}, SECRET_KEY
    )
    raw = await _exec_cloud_identity(db_pool, {"slug": swarm["slug"]}, _chat_ctx())
    assert "Unknown cloud account" in json.loads(raw)["error"]

    raw = await _exec_cloud_identity(db_pool, {"slug": "bad slug!"}, _chat_ctx())
    assert "invalid characters" in json.loads(raw)["error"]


async def test_cloud_identity_check_env_only_credentials(db_pool):
    """AWS creds may live entirely in auth_env (no ini file) — the check must
    still run and must not set AWS_SHARED_CREDENTIALS_FILE."""
    await _prepare(db_pool)
    row = await _create_aws(
        db_pool,
        aws_credentials_file=None,
        auth_env={"AWS_ACCESS_KEY_ID": "AKIAENV", "AWS_SECRET_ACCESS_KEY": "s"},
        cloud={"provider": "aws"},
    )
    full = await infra_service.get_infra(db_pool, row["id"], include_credentials=True)
    seen: dict = {}

    async def fake_run(args, timeout=30, stdin=None, env=None, stdout_cap=0):
        seen["key"] = env.get("AWS_ACCESS_KEY_ID")
        seen["file"] = env.get("AWS_SHARED_CREDENTIALS_FILE")
        seen["profile"] = env.get("AWS_PROFILE")
        return {**OK, "stdout": STS_JSON}

    with _cli(), patch.object(infra_service, "_run_ssh", new=AsyncMock(side_effect=fake_run)):
        result = await infra_service.cloud_identity_check(full, SECRET_KEY)

    assert result["ok"] is True
    assert seen["key"] == "AKIAENV"
    assert seen["file"] is None
    assert seen["profile"] is None  # no default_profile configured

    # os.environ was never polluted by the check.
    assert "AWS_ACCESS_KEY_ID" not in os.environ or os.environ.get(
        "AWS_ACCESS_KEY_ID"
    ) != "AKIAENV"
