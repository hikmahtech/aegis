"""Guards the PWA install contract in admin-panel/frontend/public/.

These assert on the *source* files, not a build: `dist/` is gitignored and CI
never runs `vite build`, so a test that needed the bundle would silently pass
by skipping.

The load-bearing one is `test_service_worker_has_no_fetch_handler`. AEGIS is
served behind an authenticating proxy (Cloudflare Access). A service worker
that answers navigation requests from cache turns an expired proxy session
into a bricked app — the shell boots from cache, its API calls hit the proxy's
cross-origin redirect to the login page, that redirect fails silently inside
fetch(), and the user cannot re-authenticate without uninstalling the app.
Chrome does not need a fetch handler to offer "Install app", so we ship none.
"""

import json
from pathlib import Path

import pytest

_PUBLIC = Path(__file__).resolve().parents[2] / "admin-panel" / "frontend" / "public"
_INDEX_HTML = _PUBLIC.parent / "index.html"


@pytest.fixture(scope="module")
def manifest() -> dict:
    return json.loads((_PUBLIC / "manifest.json").read_text())


def test_service_worker_has_no_fetch_handler():
    """The whole reason this app survives an expired Access session.

    Read the module docstring before relaxing this. If offline caching is ever
    genuinely needed, the handler MUST pass navigations straight through
    (`if (event.request.mode === 'navigate') return`) — and this test should be
    tightened to assert that, not deleted.
    """
    source = (_PUBLIC / "sw.js").read_text()
    # Strip comments: the file documents the banned pattern at length, and the
    # prose must not trip the assertion it is explaining.
    code = "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith("//")
    )
    assert "fetch" not in code, (
        "sw.js must not handle fetch events — a caching service worker bricks "
        "the app when the auth proxy's session expires. See the module docstring."
    )


def test_manifest_meets_chrome_install_criteria(manifest):
    """Chrome refuses to offer install if any of these is missing or wrong."""
    assert manifest["name"]
    assert manifest["short_name"]
    assert manifest["start_url"] == "/"
    assert manifest["display"] in {"standalone", "fullscreen", "minimal-ui"}
    assert not manifest.get("prefer_related_applications", False)

    sizes = {icon["sizes"] for icon in manifest["icons"]}
    assert {"192x192", "512x512"} <= sizes, f"need 192 and 512 PNG icons, got {sizes}"


def test_manifest_icons_exist_and_are_png(manifest):
    """A manifest pointing at a missing icon fails install with no useful error."""
    for icon in manifest["icons"]:
        path = _PUBLIC / icon["src"].lstrip("/")
        assert path.is_file(), f"manifest references missing icon: {icon['src']}"
        # PNG magic number — guards against an SVG renamed to .png, which
        # Chrome will not accept for the sized-icon requirement.
        assert path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n", f"{icon['src']} is not a PNG"


def test_index_html_links_the_manifest():
    """Without this tag the manifest is never read and install is never offered."""
    html = _INDEX_HTML.read_text()
    assert 'rel="manifest"' in html
    assert 'name="theme-color"' in html
