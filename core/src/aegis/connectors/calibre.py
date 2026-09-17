"""Calibre connector — a read-only OPDS client for calibre-web (#510).

calibre-web serves the library as OPDS, the Atom catalogue ebook apps read:
`/opds/new` lists every book (paged by `?offset=`), `/opds/search?query=`
searches title, author, tags and the rest, and each entry carries an
acquisition link per format (`/opds/download/<id>/<format>/`). Everything sits
behind HTTP Basic auth for a calibre-web user.

Three rules this module enforces rather than trusts:

* **Never a host behind an SSO login page.** A calibre-web fronted by an
  identity proxy (Cloudflare Access, Authelia, oauth2-proxy) answers every
  path — `/opds` included — with a 302 to its login page. That is how the
  Miniflux integration broke silently (#70). So no redirect is ever followed:
  any 3xx is an error, and a misconfigured URL fails loudly instead of parsing
  a login page as "no books". Point `calibre_url` at the internal address the
  stack reaches directly.
* **Same host only.** The client carries the library's credentials, so a
  `next` page link or a download link that names another host is refused
  rather than followed with the password.
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
from aegis.errors import error_text

logger = structlog.get_logger()

# No default address: a fork's calibre-web lives wherever it lives, and a
# blank URL means "not configured" (`library.connector_or_reason`).
DEFAULT_URL = ""
# A book file larger than this is not read: the default behind the
# `calibre_max_book_mb` Integrations key (80 MB covers a textbook PDF without
# letting one scanned tome exhaust the process).
MAX_DOWNLOAD_BYTES = 80 * 1024 * 1024
# How long a fetched catalogue is reused. A tool call that needs one book's
# metadata should not page through the whole library every time.
CATALOG_TTL_S = 300.0
# Runaway guard on `?offset=` paging: the default behind `calibre_max_books`
# (3,000 books at calibre-web's default of 60 a page).
MAX_BOOKS = 3000
_BOOKS_PER_PAGE = 60

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
        base_url: str,
        user: str = "",
        password: str = "",
        *,
        timeout: float = 30.0,
        db_pool: Any = None,
        max_download_bytes: int = MAX_DOWNLOAD_BYTES,
        max_books: int = MAX_BOOKS,
    ) -> None:
        super().__init__(timeout=timeout, db_pool=db_pool)
        self._base_url = (base_url or "").strip().rstrip("/")
        parsed = urlparse(self._base_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("the calibre-web URL must be an http(s) address")
        self._user = user or ""
        self._password = password or ""
        self._catalog: tuple[float, list[dict]] | None = None
        self.max_download_bytes = max(1, int(max_download_bytes or MAX_DOWNLOAD_BYTES))
        self.max_pages = max(1, -(-max(1, int(max_books or MAX_BOOKS)) // _BOOKS_PER_PAGE))

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
            # A redirect here means a login page (an SSO proxy in front of
            # calibre-web, or its own when auth failed): report it, never
            # follow it.
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
            await self._record("get", "error", int((time.monotonic() - started) * 1000), error_text(exc, 500))
            raise CalibreError(f"calibre-web is unreachable at {self._base_url}: {exc}") from exc
        latency = int((time.monotonic() - started) * 1000)
        if resp.status_code in _REDIRECTS:
            await self._record("get", "error", latency, f"redirect {resp.status_code}")
            raise CalibreError(
                f"calibre-web redirected {path} (HTTP {resp.status_code}) — a login page, not "
                "the library. The URL must reach calibre-web directly, never a host behind an "
                "SSO login page; check it and the user's password."
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

    def _next_path(self, href: str) -> str:
        """The next page's path, only while it stays on calibre-web itself.

        The client carries the library's basic-auth credentials, and httpx
        sends an absolute URL to that URL's own host, not to `base_url` — so a
        feed whose `next` link named another host would hand it the password."""
        parsed = urlparse(href)
        if not (parsed.scheme or parsed.netloc):
            return href
        base = urlparse(self._base_url)
        same = (
            parsed.scheme == base.scheme
            and (parsed.hostname or "").lower() == (base.hostname or "").lower()
            and parsed.port == base.port
        )
        if not same:
            raise CalibreError(
                f"calibre-web pointed its next page at another host ({parsed.hostname}); "
                "not following it with the library's credentials"
            )
        return parsed.path + (f"?{parsed.query}" if parsed.query else "")

    async def catalog(self, *, use_cache: bool = True) -> list[dict]:
        """Every book in the library, newest first, deduplicated by id."""
        if use_cache and self._catalog and time.monotonic() - self._catalog[0] < CATALOG_TTL_S:
            return list(self._catalog[1])
        books: dict[int, dict] = {}
        path: str | None = "/opds/new"
        for _ in range(self.max_pages):
            if not path:
                break
            page, next_href = await self._feed(path)
            new = [b for b in page if b["id"] not in books]
            for b in new:
                books[b["id"]] = b
            # A next link that yields nothing new would loop forever.
            path = self._next_path(next_href) if new and next_href else None
        else:
            logger.warning("calibre_catalog_page_cap", pages=self.max_pages)
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
        has no such format or the file is over `max_download_bytes`."""
        limit = self.max_download_bytes
        link = next((f for f in book.get("formats") or [] if f["format"] == fmt.upper()), None)
        if link is None:
            raise CalibreError(f"{book.get('title')!r} has no {fmt.upper()} file")
        if link.get("size") and link["size"] > limit:
            raise CalibreError(
                f"{book.get('title')!r} ({fmt.upper()}) is {link['size'] // (1024 * 1024)} MB, "
                f"over the {limit // (1024 * 1024)} MB read limit"
            )
        # The same-host rule `_next_path` keeps: a download link naming another
        # host is never fetched with the library's credentials.
        path = self._next_path(link["href"])
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
                    if total > limit:
                        raise CalibreError(
                            f"{book.get('title')!r} is over the "
                            f"{limit // (1024 * 1024)} MB read limit"
                        )
                    chunks.append(chunk)
        except httpx.HTTPError as exc:
            raise CalibreError(f"the download failed: {exc}") from exc
        return b"".join(chunks)
