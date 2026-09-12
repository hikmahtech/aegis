"""Only the public internet: every hop of an untrusted fetch is checked, a
malformed URL is an answer, and a response body is read only to its cap.

Before, the research lane checked the URL a model named and then let
`fetch_and_extract` follow redirects unchecked, so any public page could
bounce a fetch onto the stack's own network.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx
from aegis.services import content_extract, feeds, url_guard
from aegis.services import research as rs
from aegis.services.tools.base import ToolContext
from aegis.services.url_guard import UnsafeURLError, public_url_problem

pytestmark = pytest.mark.asyncio

PUBLIC = "93.184.216.34"


@pytest.fixture
def dns(monkeypatch):
    """Every name resolves to a public address, unless the test lists it."""
    private: dict[str, str] = {}

    async def fake_resolve(host: str, port: int) -> list[str]:
        return [private.get(host, PUBLIC)]

    monkeypatch.setattr(url_guard, "resolve_host", fake_resolve)
    return private


async def test_a_malformed_url_is_an_answer_not_a_raise():
    assert await public_url_problem("http://[::1") == "the URL is malformed"
    out = await rs.read_url("http://[::1")
    assert out["error"] == "the URL is malformed"


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/x",
        "http://[::1]/x",
        "http://10.0.0.5/",
        "http://169.254.169.254/latest/meta-data",
    ],
)
async def test_a_literal_private_address_is_refused_without_asking_dns(url, monkeypatch):
    async def no_dns(host: str, port: int) -> list[str]:
        raise AssertionError("an IP literal needs no DNS lookup")

    monkeypatch.setattr(url_guard, "resolve_host", no_dns)
    assert await public_url_problem(url)


async def test_a_name_that_resolves_inward_is_refused(dns):
    dns["sneaky.test"] = "10.1.2.3"
    assert "non-public" in await public_url_problem("https://sneaky.test/page")
    assert await public_url_problem("https://fine.test/page") is None


@respx.mock
async def test_a_redirect_to_a_private_address_is_refused_at_that_hop(dns):
    respx.get("https://public.test/story").mock(
        return_value=httpx.Response(302, headers={"location": "http://127.0.0.1:8080/admin"})
    )
    inward = respx.get("http://127.0.0.1:8080/admin").mock(
        return_value=httpx.Response(200, text="the stack's own admin page")
    )
    with pytest.raises(UnsafeURLError):
        await content_extract.fetch_and_extract("https://public.test/story")
    assert not inward.called, "the inward hop was fetched"


@respx.mock
async def test_read_url_says_why_a_redirect_was_refused(dns):
    dns["internal.test"] = "172.18.0.4"
    respx.get("https://public.test/a").mock(
        return_value=httpx.Response(301, headers={"location": "http://internal.test/b"})
    )
    inward = respx.get("http://internal.test/b").mock(return_value=httpx.Response(200, text="x"))
    out = await rs.read_url("https://public.test/a")
    assert "non-public" in out["error"]
    assert not inward.called


@respx.mock
async def test_subscribe_refuses_a_feed_that_redirects_inward(dns):
    dns["internal.test"] = "10.0.0.9"
    respx.get("https://public.test/feed.xml").mock(
        return_value=httpx.Response(302, headers={"location": "http://internal.test/feed.xml"})
    )
    inward = respx.get("http://internal.test/feed.xml").mock(
        return_value=httpx.Response(200, text="<rss></rss>")
    )
    out = await feeds.inspect_feed("https://public.test/feed.xml")
    assert out["ok"] is False and "non-public" in out["error"]
    assert not inward.called


@respx.mock
async def test_the_operator_route_may_still_fetch_an_internal_page(dns):
    dns["wiki.lan.test"] = "10.20.0.5"
    respx.get("http://wiki.lan.test/doc").mock(
        return_value=httpx.Response(200, text="internal doc", headers={"content-type": "text/plain"})
    )
    text, _ = await content_extract.fetch_and_extract("http://wiki.lan.test/doc", allow_private=True)
    assert text == "internal doc"


@respx.mock
async def test_a_huge_body_is_read_only_to_the_cap(dns, monkeypatch):
    monkeypatch.setattr(content_extract, "_MAX_BYTES", 1000)
    respx.get("https://public.test/big").mock(
        return_value=httpx.Response(
            200, content=b"a" * 50_000, headers={"content-type": "text/plain"}
        )
    )
    data, content_type = await content_extract._fetch("https://public.test/big", allow_private=False)
    assert len(data) == 1000
    assert content_type == "text/plain"


async def test_pdf_to_text_reports_a_refused_url(monkeypatch):
    from aegis.services.chat import _exec_pdf_to_text

    async def refuse(url, content_type=None, max_chars=0, **_kw):
        raise UnsafeURLError("refused internal.test: internal.test resolves to a non-public address")

    monkeypatch.setattr(content_extract, "fetch_and_extract", refuse)
    out = json.loads(await _exec_pdf_to_text(None, {"url": "https://x.test/a.pdf"}, ToolContext()))
    assert "cannot be fetched" in out["error"]
