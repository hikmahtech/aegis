"""Calibre connector — a read-only OPDS client for calibre-web (#510).

calibre-web serves the library as OPDS, the Atom catalogue ebook apps read:
`/opds/new` lists every book (paged by `?offset=`), `/opds/search?query=`
searches title, author, tags and the rest, and each entry carries an
acquisition link per format (`/opds/download/<id>/<format>/`). Everything sits
behind HTTP Basic auth for a calibre-web user.

Two rules this module enforces rather than trusts:

* **Never the public host.** `calibre.hikmahtech.in` is behind Cloudflare
  Access, which answers every path — `/opds` included — with a 302 to its
  login page. That is how the Miniflux integration broke silently (#70). The
  constructor refuses that host, and any redirect is an error rather than
  something to follow, so a misconfigured URL fails loudly instead of parsing
  a login page as "no books".
* **Read-only.** Nothing here writes to calibre-web; a download is a GET.
"""

from __future__ import annotations

import html
import re
import time
from typing import Any
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree as ET

import httpx
import structlog

from aegis.connectors._base import HTTPConnector

logger = structlog.get_logger()

# The internal swarm address: calibre-web and aegis-core share the
# `traefik_public` overlay, so this bypasses Cloudflare Access entirely.
DEFAULT_URL = "http://calibre-web_calibre-web:8083"
# Hosts that must never be called (see the module docstring).
PUBLIC_HOSTS = frozenset({"calibre.hikmahtech.in"})
# A book file larger than this is not read. The library's PDFs are textbooks;
# 80 MB covers them without letting one scanned tome exhaust the process.
MAX_DOWNLOAD_BYTES = 80 * 1024 * 1024
# How long a fetched catalogue is reused. A tool call that needs one book's
# metadata should not page through the whole library every time.
CATALOG_TTL_S = 300.0
# Runaway guard on `?offset=` paging: at calibre-web's default of 60 books a
# page this is 3,000 books.
_MAX_PAGES = 50

_ATOM = "{http://www.w3.org/2005/Atom}"
_DCTERMS = "{http://purl.org/dc/terms/}"
_XHTML = "{http://www.w3.org/1999/xhtml}"
_ACQUISITION = "http://opds-spec.org/acquisition"
_COVER = "http://opds-spec.org/image"
_BOOK_ID_RE = re.compile(r"/opds/(?:download|cover)/(\d+)")
_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"[ \t\r\f\v]+")
_REDIRECTS = (301, 302, 303, 307, 308)


class CalibreError(Exception):
    """calibre-web could not be read (unreachable, refused, redirected, not OPDS)."""


def refuse_public_host(url: str) -> None:
    """Raise ValueError when `url` points at a host AEGIS must never call."""
    host = (urlparse(url).hostname or "").lower()
    if host in PUBLIC_HOSTS:
        raise ValueError(
            f"{host} is behind Cloudflare Access and answers every path with a login "
            "redirect; use the internal address "
            f"{DEFAULT_URL} instead"
        )


def _text(el: ET.Element | None) -> str:
    return (el.text or "").strip() if el is not None else ""


def html_to_text(value: str) -> str:
    """Plain text from an HTML fragment (Calibre stores descriptions as HTML)."""
    if not value:
        return ""
    text = re.sub(r"(?i)<\s*(br|/p|/div|/li|/h\d)\s*/?\s*>", "\n", value)
    text = html.unescape(_TAG_RE.sub("", text))
    lines = [_SPACE_RE.sub(" ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def _description(entry: ET.Element) -> str:
    """The book's description: the `<p>` calibre-web renders the comments into.

    The template escapes the comments' HTML into the paragraph's text, so it
    is unescaped and stripped here. The RATING/TAGS/SERIES lines before it are
    not part of the description.
    """
    content = entry.find(f"{_ATOM}content")
    if content is None:
        return ""
    paragraphs = [
        "".join(p.itertext()) for p in content.iter(f"{_XHTML}p") if "".join(p.itertext()).strip()
    ]
    return html_to_text("\n".join(paragraphs))


def parse_entry(entry: ET.Element, base_url: str = "") -> dict | None:
    """One OPDS `<entry>` as a book dict, or None for a navigation entry."""
    formats: list[dict] = []
    cover = ""
    book_id: int | None = None
    for link in entry.findall(f"{_ATOM}link"):
        rel = link.get("rel") or ""
        href = link.get("href") or ""
        if rel == _ACQUISITION:
            size = link.get("length") or ""
            formats.append(
                {
                    "format": (link.get("title") or "").upper(),
                    "href": urljoin(base_url + "/", href) if base_url else href,
                    "size": int(size) if size.isdigit() else None,
                    "type": link.get("type") or "",
                }
            )
        elif rel == _COVER:
            cover = urljoin(base_url + "/", href) if base_url else href
        match = _BOOK_ID_RE.search(href)
        if match and book_id is None:
            book_id = int(match.group(1))
    if book_id is None:
        return None  # a navigation entry (shelf, letter, category), not a book
    uuid = _text(entry.find(f"{_ATOM}id"))
    return {
        "id": book_id,
        "uuid": uuid.removeprefix("urn:uuid:"),
        "title": _text(entry.find(f"{_ATOM}title")),
        "authors": [
            _text(a.find(f"{_ATOM}name"))
            for a in entry.findall(f"{_ATOM}author")
            if _text(a.find(f"{_ATOM}name"))
        ],
        "publisher": _text(entry.find(f"{_ATOM}publisher/{_ATOM}name")),
        "published": _text(entry.find(f"{_ATOM}published"))[:10],
        "updated": _text(entry.find(f"{_ATOM}updated")),
        "languages": [
            _text(lang) for lang in entry.findall(f"{_DCTERMS}language") if _text(lang)
        ],
        "tags": [c.get("term") or "" for c in entry.findall(f"{_ATOM}category") if c.get("term")],
        "description": _description(entry),
        "formats": formats,
        "cover": cover,
    }


def parse_feed(xml_text: str, base_url: str = "") -> tuple[list[dict], str | None]:
    """(books, href of the next page or None) from one OPDS feed document.

    Raises CalibreError when the body is not an Atom feed — which is what a
    login page or a proxy error looks like from here.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise CalibreError(f"calibre-web returned something that is not an OPDS feed: {exc}") from exc
    if root.tag != f"{_ATOM}feed":
        raise CalibreError(f"calibre-web returned {root.tag!r}, not an Atom feed")
    books = [b for b in (parse_entry(e, base_url) for e in root.findall(f"{_ATOM}entry")) if b]
    next_href = None
    for link in root.findall(f"{_ATOM}link"):
        if link.get("rel") == "next" and link.get("href"):
            next_href = link.get("href")
            break
    return books, next_href


class CalibreConnector(HTTPConnector):
    """calibre-web over OPDS: the catalogue, search, and book downloads."""

    connector_name = "calibre"

    def __init__(
        self,
        base_url: str = DEFAULT_URL,
        user: str = "",
        password: str = "",
        *,
        timeout: float = 30.0,
        db_pool: Any = None,
    ) -> None:
        super().__init__(timeout=timeout, db_pool=db_pool)
        self._base_url = (base_url or DEFAULT_URL).rstrip("/")
        refuse_public_host(self._base_url)
        self._user = user or ""
        self._password = password or ""
        self._catalog: tuple[float, list[dict]] | None = None

    @property
    def configured(self) -> bool:
        return bool(self._user and self._password)

    @property
    def base_url(self) -> str:
        return self._base_url

    def _build_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._base_url,
            auth=httpx.BasicAuth(self._user, self._password),
            timeout=httpx.Timeout(self._timeout, connect=5.0),
            # A redirect here means a login page (Cloudflare Access, or
            # calibre-web's own when auth failed): report it, never follow it.
            follow_redirects=False,
        )

    async def _get(self, path: str, params: dict | None = None) -> httpx.Response:
        if not self.configured:
            raise CalibreError("calibre-web is not configured (no user or password)")
        client = await self._ensure_client()
        started = time.monotonic()
        try:
            resp = await client.get(path, params=params)
        except httpx.HTTPError as exc:
            await self._record("get", "error", int((time.monotonic() - started) * 1000), str(exc))
            raise CalibreError(f"calibre-web is unreachable at {self._base_url}: {exc}") from exc
        latency = int((time.monotonic() - started) * 1000)
        if resp.status_code in _REDIRECTS:
            await self._record("get", "error", latency, f"redirect {resp.status_code}")
            raise CalibreError(
                f"calibre-web redirected {path} (HTTP {resp.status_code}) — a login page, not "
                "the library. Check the URL is the internal address and the user's password."
            )
        if resp.status_code == 401:
            await self._record("get", "error", latency, "401")
            raise CalibreError("calibre-web refused the user and password (HTTP 401)")
        if resp.status_code >= 400:
            await self._record("get", "error", latency, str(resp.status_code))
            raise CalibreError(f"calibre-web answered {path} with HTTP {resp.status_code}")
        await self._record("get", "ok", latency)
        return resp

    async def _feed(self, path: str, params: dict | None = None) -> tuple[list[dict], str | None]:
        resp = await self._get(path, params)
        return parse_feed(resp.text, self._base_url)

    async def catalog(self, *, use_cache: bool = True) -> list[dict]:
        """Every book in the library, newest first, deduplicated by id."""
        if use_cache and self._catalog and time.monotonic() - self._catalog[0] < CATALOG_TTL_S:
            return list(self._catalog[1])
        books: dict[int, dict] = {}
        path: str | None = "/opds/new"
        for _ in range(_MAX_PAGES):
            if not path:
                break
            page, next_href = await self._feed(path)
            new = [b for b in page if b["id"] not in books]
            for b in new:
                books[b["id"]] = b
            # A next link that yields nothing new would loop forever.
            path = next_href if new else None
        else:
            logger.warning("calibre_catalog_page_cap", pages=_MAX_PAGES)
        result = list(books.values())
        self._catalog = (time.monotonic(), result)
        return list(result)

    async def get_book(self, book_id: int) -> dict | None:
        """One book by its Calibre id, from the (cached) catalogue."""
        for book in await self.catalog():
            if book["id"] == int(book_id):
                return book
        return None

    async def search(self, query: str) -> list[dict]:
        """calibre-web's own search: title, author, tags, series, description."""
        books, _ = await self._feed("/opds/search", {"query": query})
        return books

    async def download(self, book: dict, fmt: str) -> bytes:
        """The book file in `fmt` (e.g. "EPUB"). Raises CalibreError when the book
        has no such format or the file is over MAX_DOWNLOAD_BYTES."""
        link = next((f for f in book.get("formats") or [] if f["format"] == fmt.upper()), None)
        if link is None:
            raise CalibreError(f"{book.get('title')!r} has no {fmt.upper()} file")
        if link.get("size") and link["size"] > MAX_DOWNLOAD_BYTES:
            raise CalibreError(
                f"{book.get('title')!r} ({fmt.upper()}) is {link['size'] // (1024 * 1024)} MB, "
                f"over the {MAX_DOWNLOAD_BYTES // (1024 * 1024)} MB read limit"
            )
        href = link["href"]
        refuse_public_host(href)
        path = urlparse(href).path or href
        if not self.configured:
            raise CalibreError("calibre-web is not configured (no user or password)")
        client = await self._ensure_client()
        chunks: list[bytes] = []
        total = 0
        try:
            async with client.stream("GET", path, timeout=httpx.Timeout(120.0, connect=5.0)) as resp:
                if resp.status_code in _REDIRECTS or resp.status_code >= 400:
                    raise CalibreError(
                        f"calibre-web answered the {fmt.upper()} download with HTTP {resp.status_code}"
                    )
                async for chunk in resp.aiter_bytes():
                    total += len(chunk)
                    if total > MAX_DOWNLOAD_BYTES:
                        raise CalibreError(
                            f"{book.get('title')!r} is over the "
                            f"{MAX_DOWNLOAD_BYTES // (1024 * 1024)} MB read limit"
                        )
                    chunks.append(chunk)
        except httpx.HTTPError as exc:
            raise CalibreError(f"the download failed: {exc}") from exc
        return b"".join(chunks)
