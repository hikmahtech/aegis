"""infra_alert_routing — which alerts are infrastructure, and which repo investigates them.

Both used to be Python constants in the worker that named one operator's setup
(a Dagster alert, a ClickHouse alert, the `hikmahtech/homelab-gitops` repo), so
a fork inherited them and no deployment could change them without a code edit
(issue #498). They now live in one `settings` row merged over a generic default.

Read is lenient, write is strict, as in project_repo_map and content_routes: a
malformed row must never stop an alert being investigated, but a typo must not
save and then silently do nothing forever.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from aegis.api.app import create_app
from aegis.api.auth import verify_auth
from aegis.api.deps import get_settings
from aegis.config import Settings
from aegis.services import infra_alert_routing as iar
from httpx import ASGITransport, AsyncClient

# Names only one homelab's alert rules produce. They must not ship as defaults.
_SETUP_SPECIFIC = {
    "dagster pipeline failure",
    "clickhousedown",
    "criticalendpointdown",
    "gpucriticaltemperature",
    "tempordown",
}


@pytest.fixture(autouse=True)
def _fresh_cache():
    iar._cache.update(value=None, ts=0.0)
    yield
    iar._cache.update(value=None, ts=0.0)


# ── defaults ──────────────────────────────────────────────────────────────


def test_defaults_are_generic_and_name_nobody_s_setup():
    # The alerts AEGIS's own heartbeat raises are infra in every deployment.
    for name in ("nodedown", "dockerservicedown", "servicedownprolonged", "heartbeatcollectfailed"):
        assert name in iar.DEFAULT_INFRA_ALERTNAMES
    assert not (iar.DEFAULT_INFRA_ALERTNAMES & _SETUP_SPECIFIC)


def test_unset_row_means_defaults_and_no_infra_repo():
    routing = iar.merge(None)
    assert routing["alertnames"] == sorted(iar.DEFAULT_INFRA_ALERTNAMES)
    assert routing["extra_alertnames"] == []
    assert routing["repo"] == ""


# ── merge (read path: lenient) ────────────────────────────────────────────


def test_extra_names_are_added_to_the_defaults_normalised():
    routing = iar.merge({"extra_alertnames": ["  Dagster Pipeline Failure ", "ClickHouseDown"]})
    assert "dagster pipeline failure" in routing["alertnames"]
    assert "clickhousedown" in routing["alertnames"]
    assert set(routing["alertnames"]) >= iar.DEFAULT_INFRA_ALERTNAMES
    assert routing["extra_alertnames"] == ["clickhousedown", "dagster pipeline failure"]


def test_merge_never_raises_and_keeps_what_it_can_read():
    for junk in ("not-an-object", 42, [], {"extra_alertnames": "one-string"}):
        assert iar.merge(junk)["alertnames"] == sorted(iar.DEFAULT_INFRA_ALERTNAMES)
    # A bad repo does not cost the operator their alert names, and vice versa.
    routing = iar.merge({"extra_alertnames": ["Good", 7, "  "], "repo": "not a repo"})
    assert routing["extra_alertnames"] == ["good"]
    assert routing["repo"] == ""


# ── validate (write path: strict) ─────────────────────────────────────────


def test_validate_normalises_and_dedupes():
    stored = iar.validate({"extra_alertnames": ["A", " a ", "B"], "repo": " acme/infra "})
    assert stored == {"extra_alertnames": ["a", "b"], "repo": "acme/infra", "platform_hint": ""}


@pytest.mark.parametrize(
    "bad, message",
    [
        ("nope", "must be an object"),
        ({"extra_alertname": ["typo"]}, "unknown key"),
        ({"extra_alertnames": "one-string"}, "must be a list"),
        ({"extra_alertnames": ["ok", ""]}, "non-empty"),
        ({"extra_alertnames": [3]}, "non-empty"),
        ({"repo": "justname"}, "owner/name"),
    ],
)
def test_validate_rejects_what_would_silently_do_nothing(bad, message):
    with pytest.raises(ValueError, match=message):
        iar.validate(bad)


def test_validate_accepts_its_own_read_back_so_the_ui_can_round_trip():
    """The GET body carries computed keys; PUTting it back must not 400."""
    routing = iar.merge({"extra_alertnames": ["x"], "repo": "acme/infra"})
    body = {**routing, "default_alertnames": sorted(iar.DEFAULT_INFRA_ALERTNAMES)}
    assert iar.validate(body) == {
        "extra_alertnames": ["x"],
        "repo": "acme/infra",
        "platform_hint": "",
    }


def test_the_platform_hint_is_kept_and_bounded():
    """What the cluster is, in the operator's words, because the generic
    instructions name no orchestrator (#505)."""
    hint = "This cluster is Docker Swarm. Read it with `docker --context swarm node ls`."
    assert iar.validate({"platform_hint": f"  {hint}  "})["platform_hint"] == hint
    assert iar.merge({"platform_hint": hint})["platform_hint"] == hint
    # A hint this long is a prompt, and it goes in front of everything else.
    with pytest.raises(ValueError, match="at most"):
        iar.validate({"platform_hint": "x" * 1001})
    with pytest.raises(ValueError, match="must be a string"):
        iar.validate({"platform_hint": ["swarm"]})
    # Unreadable is not fatal on the read path: a bad hint loses the hint only.
    assert iar.merge({"platform_hint": 7, "repo": "acme/infra"}) == {
        "alertnames": sorted(iar.DEFAULT_INFRA_ALERTNAMES),
        "extra_alertnames": [],
        "repo": "acme/infra",
        "platform_hint": "",
    }


# ── persistence ───────────────────────────────────────────────────────────


async def _clear(db_pool) -> None:
    await db_pool.execute("DELETE FROM settings WHERE key = $1", iar.SETTINGS_KEY)
    iar._cache.update(value=None, ts=0.0)


async def test_round_trip(db_pool):
    await _clear(db_pool)
    try:
        saved = await iar.save_infra_alert_routing(
            db_pool, {"extra_alertnames": ["Dagster Pipeline Failure"], "repo": "acme/infra"}
        )
        assert "dagster pipeline failure" in saved["alertnames"]
        read = await iar.get_infra_alert_routing(db_pool)
        assert read == saved
        assert read["repo"] == "acme/infra"
    finally:
        await _clear(db_pool)


async def test_unset_reads_as_defaults(db_pool):
    await _clear(db_pool)
    assert await iar.get_infra_alert_routing(db_pool) == iar.merge(None)


async def test_no_pool_reads_as_defaults():
    assert await iar.get_infra_alert_routing(None) == iar.merge(None)


async def test_a_malformed_stored_row_reads_leniently(db_pool):
    await _clear(db_pool)
    try:
        await db_pool.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES ($1, $2, NOW())",
            iar.SETTINGS_KEY,
            {"extra_alertnames": ["LokiLate", None], "repo": "no-slash"},
        )
        routing = await iar.get_infra_alert_routing(db_pool)
        assert "lokilate" in routing["alertnames"]
        assert routing["repo"] == ""
    finally:
        await _clear(db_pool)


async def test_saving_invalidates_the_cache(db_pool):
    """A save in this process is visible at once, not 30s later."""
    await _clear(db_pool)
    try:
        assert await iar.get_infra_alert_routing(db_pool) == iar.merge(None)  # now cached
        await iar.save_infra_alert_routing(db_pool, {"extra_alertnames": ["NewAlert"]})
        assert "newalert" in (await iar.get_infra_alert_routing(db_pool))["alertnames"]
    finally:
        await _clear(db_pool)


async def test_saving_a_malformed_value_raises_so_the_put_can_400(db_pool):
    with pytest.raises(ValueError):
        await iar.save_infra_alert_routing(db_pool, {"repo": "not-a-repo"})


# ── admin route ───────────────────────────────────────────────────────────

_SETTINGS = {
    "database_url": "postgresql://test:test@localhost:5432/test",
    "litellm_url": "https://litellm.example.com/v1",
    "temporal_ui_url": "https://temporal.example.com",
    "n8n_ui_url": "https://n8n.example.com",
    "admin_username": "admin",
    "admin_password": "admin",
    "n8n_webhook_secret": "test-secret",
}


@pytest_asyncio.fixture(loop_scope="function")
async def client(db_pool):
    app = create_app(run_lifespan=False)
    app.state.db_pool = db_pool
    app.dependency_overrides[get_settings] = lambda: Settings(**_SETTINGS)
    app.dependency_overrides[verify_auth] = lambda: True
    await _clear(db_pool)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    await _clear(db_pool)


async def test_route_get_shows_defaults_and_effective_list(client):
    resp = await client.get("/api/admin/infra-alert-routing")
    assert resp.status_code == 200
    body = resp.json()
    assert body["default_alertnames"] == sorted(iar.DEFAULT_INFRA_ALERTNAMES)
    assert body["alertnames"] == sorted(iar.DEFAULT_INFRA_ALERTNAMES)
    assert body["repo"] == ""


async def test_route_put_saves_and_round_trips(client):
    resp = await client.put(
        "/api/admin/infra-alert-routing",
        json={"extra_alertnames": ["Dagster Pipeline Failure"], "repo": "acme/infra"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "dagster pipeline failure" in body["alertnames"]
    # What GET returns can be PUT straight back.
    again = await client.put("/api/admin/infra-alert-routing", json=body)
    assert again.status_code == 200
    assert again.json() == body


async def test_route_put_400s_on_a_typo(client):
    resp = await client.put("/api/admin/infra-alert-routing", json={"repo": "not-a-repo"})
    assert resp.status_code == 400
    assert "owner/name" in resp.json()["detail"]
