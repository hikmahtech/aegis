"""BYO Google OAuth app config (OSS) — DB-first, file fallback, encrypted secret."""

from __future__ import annotations

import json
import types

import pytest_asyncio
from aegis.services.google_oauth import (
    get_google_client_config,
    google_client_status,
    save_google_client,
)


def _settings(secret_key: str = "", cred_file: str = ""):
    return types.SimpleNamespace(secret_key=secret_key, gmail_credentials_file=cred_file)


@pytest_asyncio.fixture(loop_scope="function")
async def clean_google(db_pool):
    await db_pool.execute("DELETE FROM settings WHERE key = 'google_oauth'")
    yield db_pool
    await db_pool.execute("DELETE FROM settings WHERE key = 'google_oauth'")


async def test_none_when_unconfigured(clean_google):
    assert await get_google_client_config(clean_google, _settings()) is None
    st = await google_client_status(clean_google, _settings())
    assert st["configured"] is False


async def test_save_and_get_db_decrypts(clean_google):
    s = _settings(secret_key="k")
    await save_google_client(clean_google, s, client_id="cid.apps", client_secret="shh")
    cfg = await get_google_client_config(clean_google, s)
    assert cfg["web"]["client_id"] == "cid.apps"
    assert cfg["web"]["client_secret"] == "shh"  # decrypted
    st = await google_client_status(clean_google, s)
    assert st == {"configured": True, "client_id": "cid.apps", "source": "db"}


async def test_save_preserves_secret_when_omitted(clean_google):
    s = _settings(secret_key="k")
    await save_google_client(clean_google, s, client_id="cid", client_secret="shh")
    await save_google_client(clean_google, s, client_id="cid2", client_secret=None)
    cfg = await get_google_client_config(clean_google, s)
    assert cfg["web"]["client_id"] == "cid2" and cfg["web"]["client_secret"] == "shh"


async def test_file_fallback_when_no_db(clean_google, tmp_path):
    f = tmp_path / "g.json"
    f.write_text(json.dumps({"web": {"client_id": "file-cid", "client_secret": "fs"}}))
    s = _settings(cred_file=str(f))
    cfg = await get_google_client_config(clean_google, s)
    assert cfg["web"]["client_id"] == "file-cid"
    st = await google_client_status(clean_google, s)
    assert st["source"] == "file" and st["client_id"] == "file-cid"
