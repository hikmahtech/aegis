"""Configurable LLM backend + secret crypto (Phase A — BYO key + backend)."""

from __future__ import annotations

import types

import pytest_asyncio
from aegis.crypto import decrypt_secret, encrypt_secret
from aegis.services.llm_backend import get_llm_backend, invalidate, save_llm_backend


def _settings(secret_key: str = ""):
    return types.SimpleNamespace(
        secret_key=secret_key,
        litellm_url="http://env-proxy:4000",
        litellm_api_key="env-key",
        model_fast="env-fast",
        model_balanced="env-balanced",
        model_smart="env-smart",
        models_yaml_path="/nonexistent/models.yaml",  # force the model_* fallback
    )


def test_crypto_roundtrip_with_key():
    enc = encrypt_secret("sk-secret", "app-key")
    assert enc["encrypted"] is True and enc["value"] != "sk-secret"
    assert decrypt_secret(enc, "app-key") == "sk-secret"


def test_crypto_plaintext_without_key():
    enc = encrypt_secret("sk-secret", "")
    assert enc["encrypted"] is False and enc["value"] == "sk-secret"
    assert decrypt_secret(enc, "") == "sk-secret"


def test_crypto_wrong_key_returns_empty():
    enc = encrypt_secret("sk-secret", "app-key")
    assert decrypt_secret(enc, "other-key") == ""


@pytest_asyncio.fixture(loop_scope="function")
async def clean_backend(db_pool):
    invalidate()
    await db_pool.execute("DELETE FROM settings WHERE key = 'llm_backend'")
    yield db_pool
    await db_pool.execute("DELETE FROM settings WHERE key = 'llm_backend'")
    invalidate()


async def test_env_fallback_when_no_db_row(clean_backend):
    b = await get_llm_backend(clean_backend, _settings(), use_cache=False)
    assert b["source"] == "env"
    assert b["base_url"] == "http://env-proxy:4000"
    assert b["api_key"] == "env-key"
    assert b["tiers"] == {"fast": "env-fast", "balanced": "env-balanced", "smart": "env-smart"}


async def test_db_override_and_key_decrypt(clean_backend):
    s = _settings(secret_key="app-key")
    await save_llm_backend(
        clean_backend,
        s,
        provider="openrouter",
        base_url="https://openrouter.ai/api/v1",
        tiers={"fast": "m-fast", "balanced": "m-bal", "smart": "m-smart"},
        api_key="sk-byo",
    )
    b = await get_llm_backend(clean_backend, s, use_cache=False)
    assert b["source"] == "db" and b["provider"] == "openrouter"
    assert b["base_url"] == "https://openrouter.ai/api/v1"
    assert b["api_key"] == "sk-byo"  # decrypted
    assert b["tiers"]["balanced"] == "m-bal"


async def test_save_preserves_key_when_omitted(clean_backend):
    s = _settings(secret_key="app-key")
    await save_llm_backend(
        clean_backend, s, provider="x", base_url="u", tiers={"fast": "f"}, api_key="sk-keep"
    )
    # Re-save without api_key (write-only field left blank) → key preserved.
    await save_llm_backend(
        clean_backend, s, provider="x2", base_url="u2", tiers={"fast": "f2"}, api_key=None
    )
    b = await get_llm_backend(clean_backend, s, use_cache=False)
    assert b["provider"] == "x2" and b["base_url"] == "u2"
    assert b["api_key"] == "sk-keep"


async def test_db_base_url_falls_back_to_env_when_blank(clean_backend):
    s = _settings()
    await save_llm_backend(
        clean_backend, s, provider="custom", base_url="", tiers={"fast": "f"}, api_key=None
    )
    b = await get_llm_backend(clean_backend, s, use_cache=False)
    assert b["base_url"] == "http://env-proxy:4000"  # blank base_url → env fallback
