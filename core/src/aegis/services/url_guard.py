"""Only the public internet: the check every fetch of an untrusted URL makes.

A URL is untrusted when a model chose it, a page named it, or a third party
publishes it: `read_url`, `paper_read`, the pages a research run reads, a feed
entry's link, `subscribe_feed`, `pdf_to_text`. The research lane reads pages
that tell it where to go next, so a fetch that could reach the stack's own
services (Temporal's UI, calibre-web, anything on the overlay network) is a
way in.

:func:`public_url_problem` answers "may this URL be fetched?" for one URL, and
:func:`guard_request` asks it again for EVERY request an httpx client sends,
so a public page that redirects inward is refused at the hop that turns
inward. Checking only the first URL, as the research lane first did, let any
public page bounce a fetch onto the overlay network.

What this does not stop: a host whose DNS answer changes between the check and
the connect (DNS rebinding). Closing that needs the connection pinned to the
address that was checked, which httpx does not offer without a custom
transport.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlparse

import httpx


class UnsafeURLError(ValueError):
    """A fetch was refused: the URL, or a redirect it led to, left the public internet."""


async def resolve_host(host: str, port: int) -> list[str]:
    """Every address ``host`` resolves to. The one place DNS is asked (a test seam)."""
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]).split("%", 1)[0] for info in infos]


def _address_problem(host: str, value: str) -> str | None:
    try:
        addr = ipaddress.ip_address(value)
    except ValueError:
        return f"{host} resolves to an address that cannot be checked"
    if not addr.is_global:
        return f"{host} resolves to a non-public address"
    return None


async def public_url_problem(url: str) -> str | None:
    """None when ``url`` is http(s) on the public internet, else why it is not.

    Never raises: a malformed URL (``http://[::1``) is an answer too."""
    try:
        parsed = urlparse(url or "")
        hostname = parsed.hostname
    except ValueError:
        return "the URL is malformed"
    if parsed.scheme not in ("http", "https"):
        return "only http and https URLs can be read"
    host = (hostname or "").lower().rstrip(".")
    if not host:
        return "the URL has no host"
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        return f"{host} is not a public host"
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return "the URL has an invalid port"
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return _address_problem(host, host)
    try:
        addresses = await resolve_host(host, port)
    except OSError as exc:
        return f"{host} does not resolve ({exc})"
    if not addresses:
        return f"{host} does not resolve"
    for value in addresses:
        problem = _address_problem(host, value)
        if problem:
            return problem
    return None


async def guard_request(request: httpx.Request) -> None:
    """An httpx ``request`` event hook. httpx runs it for every request it
    sends, the first one and each redirect, so a hop that turns inward is
    refused before it is made."""
    problem = await public_url_problem(str(request.url))
    if problem:
        raise UnsafeURLError(f"refused {request.url.host or request.url}: {problem}")


def guarded_hooks() -> dict[str, list]:
    """``event_hooks`` for an httpx client that may only reach the public internet."""
    return {"request": [guard_request]}
