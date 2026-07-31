"""Tests for the SPA catch-all route (core/src/aegis/api/app.py::serve_spa).

D4: an unmatched /api/* path must 404, not return 200 with a null body.

`admin-panel/frontend/dist/` is gitignored and not built in CI, so these
tests point AEGIS_ADMIN_DIST_DIR at a minimal synthetic dist directory to
force the SPA catch-all route to register deterministically, regardless of
whether a real frontend build is present on disk.
"""

import pytest
from aegis.api.app import create_app
from aegis.api.deps import get_settings
from httpx import ASGITransport, AsyncClient


@pytest.fixture
def app(test_settings, tmp_path, monkeypatch):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html><body>spa</body></html>")
    monkeypatch.setenv("AEGIS_ADMIN_DIST_DIR", str(dist))

    application = create_app(run_lifespan=False)
    application.dependency_overrides[get_settings] = lambda: test_settings
    return application


async def test_unmatched_api_path_returns_404(app):
    """A typo'd/retired /api/* route must 404, not look like a successful
    empty (200 null) response."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/definitely-not-a-real-route")
        assert resp.status_code == 404


async def test_health_route_unaffected(app):
    """/health must still behave exactly as before: 200 with status/version,
    served by its own router — never by the SPA catch-all."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["version"] == "0.1.0"


async def test_non_api_path_falls_through_to_spa(app):
    """A non-api, non-existent-file path still hits the SPA fallback and
    gets served index.html (current behaviour when a built frontend is
    present)."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/some/spa/route")
        assert resp.status_code == 200
        assert "spa" in resp.text
