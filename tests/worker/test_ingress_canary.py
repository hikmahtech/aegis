"""HomelabActivities.probe_ingress — the canary on the way IN (#492).

The rule under test is which answers count as healthy. Core's own healthcheck
runs inside its container and so stayed green through a 3.5-hour outage in
which the proxy in front of it answered every request with a 504. What the
canary asks is whether bytes reach core at all, so any reply under 500 is a
pass — including the 401/404/405 that an identity proxy or a webhook-only path
will give a bare GET.
"""

from __future__ import annotations

import httpx
import pytest
import respx
from aegis_worker.activities.homelab import HomelabActivities
from httpx import Response
from temporalio.testing import ActivityEnvironment

URL = "https://aegis.example.com/api/webhooks/github"


async def _probe(url: str = URL, expect_status: int = 0) -> dict:
    # The probe touches nothing but httpx: no swarm, no DB, no delivery.
    acts = HomelabActivities(homelab=None, delivery=None, db_pool=None)
    return await ActivityEnvironment().run(acts.probe_ingress, url, expect_status)


@respx.mock
@pytest.mark.parametrize("status", [200, 204, 302, 401, 404, 405])
async def test_any_answer_under_500_means_the_path_works(status):
    respx.get(URL).mock(return_value=Response(status))
    probe = await _probe()
    assert probe["ok"] is True
    assert probe["status"] == status
    assert probe["error"] == ""


@respx.mock
@pytest.mark.parametrize("status", [500, 502, 503, 504])
async def test_a_5xx_is_a_fault_because_that_is_what_a_backendless_proxy_says(status):
    respx.get(URL).mock(return_value=Response(status))
    probe = await _probe()
    assert probe["ok"] is False
    assert probe["status"] == status
    assert probe["error"] == f"HTTP {status}"


@respx.mock
async def test_a_transport_failure_is_a_fault_and_says_what_it_was():
    respx.get(URL).mock(side_effect=httpx.ConnectTimeout("timed out"))
    probe = await _probe()
    assert probe["ok"] is False
    assert probe["status"] == 0
    assert probe["error"].startswith("ConnectTimeout")
    assert probe["configured"] is True


@respx.mock
async def test_a_redirect_off_the_host_is_a_fault_however_cheerful_it_is():
    """An identity proxy sends the probe to its own login page, which answers
    200 forever whether or not the origin is alive. A canary that green-lights
    a page core never saw is worse than no canary.

    Falsifiable: drop the host comparison and this passes as healthy.
    """
    respx.get(URL).mock(
        return_value=Response(302, headers={"Location": "https://login.example.net/sso"})
    )
    respx.get("https://login.example.net/sso").mock(return_value=Response(200))

    probe = await _probe()

    assert probe["ok"] is False
    assert "login.example.net" in probe["error"]


@respx.mock
async def test_a_redirect_within_the_host_is_fine():
    """A bare path redirecting to a real one is still core answering."""
    respx.get(URL).mock(return_value=Response(301, headers={"Location": f"{URL}/"}))
    respx.get(f"{URL}/").mock(return_value=Response(405))
    assert (await _probe())["ok"] is True


@respx.mock
async def test_an_expected_status_catches_the_proxys_own_answer():
    """A proxy that lost its route to core serves its own 404. Pin the status
    only core gives — 405 for a webhook path on a bare GET — and that 404
    stops being "healthy"."""
    respx.get(URL).mock(return_value=Response(404))
    strict = await _probe(expect_status=405)
    assert strict["ok"] is False
    assert strict["error"] == "HTTP 404, expected 405"
    # Unpinned, the same answer passes: reaching core is the default question.
    assert (await _probe())["ok"] is True

    respx.get(URL).mock(return_value=Response(405))
    assert (await _probe(expect_status=405))["ok"] is True


async def test_no_url_is_not_a_fault():
    """An unconfigured canary must never raise a problem: a fork ships nobody's
    hostname, and "I was not told where to look" is not an outage."""
    probe = await _probe("  ")
    assert probe == {"url": "", "ok": True, "status": 0, "ms": 0, "error": "", "configured": False}
