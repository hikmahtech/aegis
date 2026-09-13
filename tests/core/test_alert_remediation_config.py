"""`alert_remediation` — the auto-restart's repeat window (#501, #558).

The row gates `docker service update --force`, so the two halves differ on
purpose: `merge` (the worker's read) turns anything malformed into the default
and never raises, and `validate` (the admin PUT) refuses anything that is not a
whole number of minutes in range, so a typo can never be saved.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from aegis.api.app import create_app
from aegis.api.deps import get_settings
from aegis.config import Settings
from aegis.db import run_migrations
from aegis.services.alert_remediation import (
    DEFAULT_REPEAT_WINDOW_MINUTES,
    MAX_MINUTES,
    merge,
    validate,
)
from httpx import ASGITransport, AsyncClient

_SETTINGS = {
    "database_url": "postgresql://test:test@localhost:5432/test",
    "litellm_url": "https://litellm.example.com/v1",
    "temporal_ui_url": "https://temporal.example.com",
    "admin_username": "admin",
    "admin_password": "admin",
}
AUTH = ("admin", "admin")
URL = "/api/admin/alert-remediation"


# ── merge: the lenient read ──


@pytest.mark.parametrize(
    "value",
    [
        None,
        "60",
        [],
        {},
        {"repeat_window_minutes": True},
        {"repeat_window_minutes": -5},
        {"repeat_window_minutes": "30"},
        {"repeat_window_minutes": 1.5},
        {"repeat_window_minutes": None},
    ],
)
def test_merge_reads_anything_malformed_as_the_default(value):
    assert merge(value) == {"repeat_window_minutes": DEFAULT_REPEAT_WINDOW_MINUTES}


def test_merge_keeps_zero_and_does_not_clamp():
    """0 is a real value (restart every time), and a stored value over the
    write cap still applies — the reader has always honoured it."""
    assert merge({"repeat_window_minutes": 0}) == {"repeat_window_minutes": 0}
    assert merge({"repeat_window_minutes": 5000}) == {"repeat_window_minutes": 5000}


# ── validate: the strict write ──


@pytest.mark.parametrize(
    ("raw", "needle"),
    [
        ("60", "object"),
        ([], "object"),
        (None, "object"),
        ({}, "required"),
        ({"repeat_window_minutes": True}, "whole"),
        ({"repeat_window_minutes": -1}, "whole"),
        ({"repeat_window_minutes": 1.5}, "whole"),
        ({"repeat_window_minutes": "30"}, "whole"),
        ({"repeat_window_minutes": None}, "whole"),
        ({"repeat_window_minutes": MAX_MINUTES + 1}, "cap"),
        ({"repeat_window_minutes": 30, "window": 5}, "unknown"),
    ],
)
def test_validate_refuses(raw, needle):
    with pytest.raises(ValueError, match=needle):
        validate(raw)


def test_validate_accepts_zero_and_the_cap():
    assert validate({"repeat_window_minutes": 0}) == {"repeat_window_minutes": 0}
    assert validate({"repeat_window_minutes": MAX_MINUTES}) == {
        "repeat_window_minutes": MAX_MINUTES
    }


# ── the admin route ──


@pytest_asyncio.fixture(loop_scope="function")
async def pool(db_pool):
    await run_migrations(db_pool)
    await db_pool.execute("DELETE FROM settings WHERE key = 'alert_remediation'")
    await db_pool.execute("DELETE FROM audit_log WHERE action = 'alert_remediation_saved'")
    yield db_pool
    await db_pool.execute("DELETE FROM settings WHERE key = 'alert_remediation'")
    await db_pool.execute("DELETE FROM audit_log WHERE action = 'alert_remediation_saved'")


@pytest_asyncio.fixture(loop_scope="function")
async def client(pool):
    app = create_app(run_lifespan=False)
    app.state.db_pool = pool
    app.state.llm = AsyncMock()
    app.dependency_overrides[get_settings] = lambda: Settings(**_SETTINGS)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def test_requires_auth(client):
    assert (await client.get(URL)).status_code == 401
    assert (await client.put(URL, json={"repeat_window_minutes": 5})).status_code == 401


async def test_get_shows_the_default_with_no_row(client):
    r = await client.get(URL, auth=AUTH)
    assert r.status_code == 200
    assert r.json() == {
        "repeat_window_minutes": 60,
        "defaults": {"repeat_window_minutes": 60},
        "max_minutes": MAX_MINUTES,
        "stored": False,
    }


async def test_put_persists_audits_and_reads_back(client, pool):
    r = await client.put(URL, auth=AUTH, json={"repeat_window_minutes": 0})
    assert r.status_code == 200, r.text
    assert r.json()["repeat_window_minutes"] == 0 and r.json()["stored"] is True
    assert await pool.fetchval(
        "SELECT value FROM settings WHERE key = 'alert_remediation'"
    ) == {"repeat_window_minutes": 0}
    assert (await client.get(URL, auth=AUTH)).json()["repeat_window_minutes"] == 0
    audit = await pool.fetchrow(
        "SELECT actor, details FROM audit_log WHERE action = 'alert_remediation_saved'"
    )
    assert audit["actor"] == "admin"
    assert audit["details"] == {"repeat_window_minutes": 0}


@pytest.mark.parametrize(
    "body",
    [
        {"repeat_window_minutes": "sixty"},
        {"repeat_window_minutes": None},
        {"repeat_window_minutes": MAX_MINUTES + 1},
        {"minutes": 30},
    ],
)
async def test_put_400s_and_writes_nothing(client, pool, body):
    r = await client.put(URL, auth=AUTH, json=body)
    assert r.status_code == 400
    assert r.json()["detail"]
    assert await pool.fetchval("SELECT count(*) FROM settings WHERE key = 'alert_remediation'") == 0
    assert await pool.fetchval(
        "SELECT count(*) FROM audit_log WHERE action = 'alert_remediation_saved'"
    ) == 0
