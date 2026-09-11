"""The Calibre library (#510) — one implementation for Raphael's library tools,
`ResearchFlow` and `CalibreSyncFlow`.

Calibre is the record; the knowledge store is only an index of it. Each book
gets ONE `knowledge_content` row (`source_type='book'`): title, authors, tags
and description, which is what `library_suggest` and research rank. The full
text is never bulk-indexed — arXiv PDFs were 93% of the corpus's chunks and 78
of 10,284 were ever used — so a book's text is read on demand here, bounded,
cited, and not stored.

Configuration is the Integrations page (`calibre_url`, `calibre_user`,
`calibre_password`); a connector is built from those values on first use and
rebuilt when they change, so core needs no restart after a save.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import posixpath
import re
import zipfile
from typing import Any
from urllib.parse import unquote
from xml.etree import ElementTree as ET

import structlog

from aegis.connectors.calibre import DEFAULT_URL, CalibreConnector, CalibreError, html_to_text

logger = structlog.get_logger()

BOOK_SOURCE_TYPE = "book"
# What a read returns by default, and the most it will ever return.
READ_CHARS = 12_000
MAX_READ_CHARS = 40_000
# Passage search: this many best windows of about this many characters.
PASSAGES = 4
PASSAGE_CHARS = 1_200
# A PDF read with no pages, section or query returns the opening pages.
PDF_DEFAULT_PAGES = 5
# The most pages one read returns, and the most a query scans. pdfminer is
# slow on a long textbook, and the chat tool's budget is LIBRARY_READ_TIMEOUT_S.
PDF_MAX_SPAN = 30
PDF_QUERY_SCAN_PAGES = 150
# The formats that can be read. MOBI/AZW3 would need Calibre's own converter.
READABLE_FORMATS = ("EPUB", "PDF")
# Chat-tool budget for a read: a download plus extraction.
LIBRARY_READ_TIMEOUT_S = 150
# Research (ResearchFlow's gather step): how many books to consider, and how
# close the best one must be before a passage is read from it.
RESEARCH_BOOK_HITS = 3
RESEARCH_PASSAGE_MIN_SIMILARITY = 0.5
RESEARCH_PASSAGE_CHARS = 3_000
# Research reads under ResearchFlow's gather budget, so it scans fewer PDF
# pages than a chat read and gives up on the passage after this long.
RESEARCH_PDF_SCAN_PAGES = 60
RESEARCH_LIBRARY_READ_S = 60

NOT_CONFIGURED = (
    "The Calibre library is not configured: set the calibre-web user and password "
    "under Integrations → Calibre (library)."
)

# One live connector, keyed on the config it was built from.
_connectors: dict[tuple[str, str, str], CalibreConnector] = {}


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


def calibre_settings(settings: Any) -> tuple[str, str, str]:
    """(url, user, password) from the Settings overlay, the URL defaulting to
    the internal swarm address."""
    url = (getattr(settings, "calibre_url", "") or "").strip() or DEFAULT_URL
    return url, getattr(settings, "calibre_user", "") or "", getattr(settings, "calibre_password", "") or ""


def connector_or_reason(settings: Any) -> tuple[CalibreConnector | None, str]:
    """The connector, or (None, why): `not_configured`, or `refused: …` for a
    URL on the public host."""
    url, user, password = calibre_settings(settings)
    if not (user and password):
        return None, "not_configured"
    key = (url, user, hashlib.sha256(password.encode()).hexdigest())
    conn = _connectors.get(key)
    if conn is None:
        try:
            conn = CalibreConnector(url, user, password)
        except ValueError as exc:
            return None, f"refused: {exc}"
        # One config at a time: a changed password drops the old client.
        _connectors.clear()
        _connectors[key] = conn
    return conn, ""


# --------------------------------------------------------------------------
# Books as the index sees them
# --------------------------------------------------------------------------


def book_url(book_id: int) -> str:
    """The stable key a book's index row is stored under."""
    return f"calibre://book/{int(book_id)}"


def book_fingerprint(book: dict) -> str:
    """What the index row was built from, so an unchanged book is not re-embedded."""
    keyed = {
        k: book.get(k)
        for k in ("title", "authors", "tags", "description", "published", "updated", "publisher")
    }
    keyed["formats"] = sorted(f.get("format") or "" for f in book.get("formats") or [])
    return hashlib.sha256(json.dumps(keyed, sort_keys=True, default=str).encode()).hexdigest()


def book_brief(book: dict, *, description_chars: int = 600) -> dict:
    """What a tool shows for one book."""
    return {
        "id": book.get("id"),
        "title": book.get("title") or "",
        "authors": list(book.get("authors") or []),
        "tags": list(book.get("tags") or []),
        "published": book.get("published") or "",
        "formats": [f.get("format") for f in book.get("formats") or [] if f.get("format")],
        "description": (book.get("description") or "")[:description_chars],
    }


def book_document(book: dict) -> dict:
    """The one knowledge row for a book: metadata only, never its text."""
    title = book.get("title") or f"Book {book.get('id')}"
    authors = list(book.get("authors") or [])
    tags = list(book.get("tags") or [])
    description = book.get("description") or ""
    lines = [title]
    if authors:
        lines.append("By " + ", ".join(authors))
    if tags:
        lines.append("Tags: " + ", ".join(tags))
    if book.get("publisher") or book.get("published"):
        lines.append(
            "Published " + " ".join(x for x in (book.get("publisher"), book.get("published")) if x)
        )
    body = "\n".join(lines) + (f"\n\n{description}" if description else "")
    return {
        "url": book_url(book["id"]),
        "title": title,
        "summary": description[:500] or title,
        "raw_text": body,
        "tags": [BOOK_SOURCE_TYPE, *tags[:30]],
        "metadata": {
            "calibre_id": book["id"],
            "uuid": book.get("uuid") or "",
            "authors": authors,
            "tags": tags,
            "formats": [f.get("format") for f in book.get("formats") or [] if f.get("format")],
            "published": book.get("published") or "",
            "updated": book.get("updated") or "",
            "languages": list(book.get("languages") or []),
            "fingerprint": book_fingerprint(book),
        },
    }


def book_hit(hit: dict) -> dict:
    """A knowledge-store search result over book rows, as a tool shows it."""
    md = hit.get("metadata") or {}
    return {
        "id": md.get("calibre_id"),
        "title": hit.get("title") or "",
        "authors": list(md.get("authors") or []),
        "tags": list(md.get("tags") or []),
        "formats": list(md.get("formats") or []),
        "similarity": round(float(hit.get("similarity") or 0.0), 3),
        "summary": (hit.get("summary") or "")[:400],
    }


# --------------------------------------------------------------------------
# EPUB
# --------------------------------------------------------------------------

_HEAD_RE = re.compile(r"(?is)<(head|script|style)\b.*?</\1\s*>")
_HEADING_RE = re.compile(r"(?is)<h[1-3][^>]*>(.*?)</h[1-3]\s*>")


def html_section_text(raw: str) -> str:
    """Plain text of one XHTML chapter file. Deterministic tag stripping — a
    chapter is clean markup, and an article extractor drops short sections."""
    return html_to_text(_HEAD_RE.sub("", raw or ""))


def _heading(raw: str) -> str:
    match = _HEADING_RE.search(raw or "")
    return html_to_text(match.group(1)) if match else ""


def _join(base_dir: str, href: str) -> str:
    """An href inside an EPUB, relative to the file it appears in, as a zip path."""
    path = unquote((href or "").split("#", 1)[0])
    return posixpath.normpath(posixpath.join(base_dir, path)) if path else ""


def _toc_titles(z: zipfile.ZipFile, opf: ET.Element, manifest: dict, opf_dir: str) -> dict[str, str]:
    """Chapter file → its table-of-contents title (EPUB 3 nav, else EPUB 2 NCX)."""
    titles: dict[str, str] = {}
    nav = next(
        (i for i in manifest.values() if "nav" in (i.get("properties") or "").split()), None
    )
    try:
        if nav is not None:
            nav_path = _join(opf_dir, nav.get("href"))
            root = ET.fromstring(z.read(nav_path))
            # iterfind, not iter: only the XPath methods understand `{*}`.
            for a in root.iterfind(".//{*}a"):
                target = _join(posixpath.dirname(nav_path), a.get("href") or "")
                label = " ".join("".join(a.itertext()).split())
                if target and label:
                    titles.setdefault(target, label)
        if not titles:
            spine = opf.find(".//{*}spine")
            ncx_item = manifest.get(spine.get("toc")) if spine is not None else None
            if ncx_item is not None:
                ncx_path = _join(opf_dir, ncx_item.get("href"))
                root = ET.fromstring(z.read(ncx_path))
                for point in root.iterfind(".//{*}navPoint"):
                    label_el = point.find("{*}navLabel/{*}text")
                    content = point.find("{*}content")
                    if label_el is None or content is None:
                        continue
                    target = _join(posixpath.dirname(ncx_path), content.get("src") or "")
                    label = " ".join((label_el.text or "").split())
                    if target and label:
                        titles.setdefault(target, label)
    except (KeyError, ET.ParseError) as exc:
        logger.info("epub_toc_unreadable", error=str(exc)[:200])
    return titles


def epub_sections(data: bytes) -> list[dict]:
    """The book's chapters in reading order: [{n, title, text}]. Empty files
    (a cover page, a blank separator) are skipped."""
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise CalibreError("the EPUB file is not a readable zip archive") from exc
    with z:
        try:
            container = ET.fromstring(z.read("META-INF/container.xml"))
            rootfile_el = container.find(".//{*}rootfile")
            rootfile = rootfile_el.get("full-path") if rootfile_el is not None else None
            if not rootfile:
                raise CalibreError("the EPUB names no package file")
            opf = ET.fromstring(z.read(rootfile))
        except (KeyError, ET.ParseError) as exc:
            raise CalibreError(f"the EPUB's package could not be read: {exc}") from exc
        opf_dir = posixpath.dirname(rootfile)
        manifest = {i.get("id"): i for i in opf.iterfind(".//{*}manifest/{*}item")}
        titles = _toc_titles(z, opf, manifest, opf_dir)
        sections: list[dict] = []
        for ref in opf.iterfind(".//{*}spine/{*}itemref"):
            item = manifest.get(ref.get("idref"))
            if item is None or "html" not in (item.get("media-type") or ""):
                continue
            path = _join(opf_dir, item.get("href"))
            try:
                raw = z.read(path).decode("utf-8", errors="replace")
            except KeyError:
                continue
            text = html_section_text(raw)
            if not text:
                continue
            n = len(sections) + 1
            sections.append({"n": n, "title": titles.get(path) or _heading(raw) or f"Section {n}", "text": text})
    return sections


# --------------------------------------------------------------------------
# PDF
# --------------------------------------------------------------------------


def pdf_page_count(data: bytes) -> int:
    from pdfminer.pdfpage import PDFPage

    return sum(1 for _ in PDFPage.get_pages(io.BytesIO(data)))


def pdf_pages(data: bytes, first: int, last: int) -> list[str]:
    """Text of pages first..last (1-based, inclusive), one string per page.
    pdfminer ends every page with a form feed, which is the split."""
    from pdfminer.high_level import extract_text

    text = extract_text(io.BytesIO(data), page_numbers=list(range(first - 1, last)))
    return [p.strip() for p in text.split("\f")][: last - first + 1]


_PAGES_RE = re.compile(r"^\s*(\d+)\s*(?:[-–—]\s*(\d+)\s*)?$")


def parse_pages(pages: str, count: int) -> tuple[int, int]:
    """"12-18" or "12" → (first, last), clamped to the book and PDF_MAX_SPAN.
    Raises ValueError for anything else."""
    match = _PAGES_RE.match(pages or "")
    if not match:
        raise ValueError(f"pages must be a page or a range like 12-18, not {pages!r}")
    first = int(match.group(1))
    last = int(match.group(2) or first)
    if last < first:
        first, last = last, first
    first = max(1, min(first, count))
    last = max(first, min(last, count, first + PDF_MAX_SPAN - 1))
    return first, last


# --------------------------------------------------------------------------
# Passages
# --------------------------------------------------------------------------

_WORD_RE = re.compile(r"[a-z0-9]{3,}")
_STOP = frozenset(
    {
        "the", "and", "for", "with", "that", "this", "from", "are", "was", "were", "what",
        "which", "when", "how", "why", "who", "whom", "into", "about", "have", "has", "had",
        "not", "but", "you", "your", "our", "their", "its", "can", "will", "would", "could",
        "should", "than", "then", "them", "they", "there", "these", "those", "also", "more",
        "most", "such", "does", "did", "doing", "been", "being", "over", "under", "between",
        "book", "books", "chapter",
    }
)


def query_terms(query: str) -> set[str]:
    return {w for w in _WORD_RE.findall((query or "").lower()) if w not in _STOP}


def _windows(text: str, size: int) -> list[str]:
    """Paragraph-aligned windows of about `size` characters."""
    out: list[str] = []
    current = ""
    for para in re.split(r"\n\s*\n|\n", text or ""):
        para = para.strip()
        if not para:
            continue
        while len(para) > size:
            if current:
                out.append(current)
                current = ""
            out.append(para[:size])
            para = para[size:]
        if len(current) + len(para) + 1 > size and current:
            out.append(current)
            current = para
        else:
            current = f"{current}\n{para}" if current else para
    if current:
        out.append(current)
    return out


def best_passages(
    parts: list[tuple[str, str]], query: str, *, k: int = PASSAGES, size: int = PASSAGE_CHARS
) -> list[dict]:
    """The `k` windows that best match `query`, each with where it came from.

    Scored on distinct query terms first (a window that mentions three of the
    question's words beats one that repeats a single word), then on total hits.
    """
    terms = query_terms(query)
    if not terms:
        return []
    scored: list[tuple[int, int, str, str]] = []
    for where, text in parts:
        for window in _windows(text, size):
            words = _WORD_RE.findall(window.lower())
            distinct = {w for w in words if w in terms}
            if not distinct:
                continue
            score = len(distinct) * 10 + sum(1 for w in words if w in terms)
            scored.append((score, len(scored), where, window))
    scored.sort(key=lambda s: (-s[0], s[1]))
    return [{"where": where, "text": text, "score": score} for score, _, where, text in scored[:k]]


# --------------------------------------------------------------------------
# The operations the tools and flows share
# --------------------------------------------------------------------------


def _clip(text: str, limit: int) -> tuple[str, bool]:
    return (text, False) if len(text) <= limit else (text[: limit - 2] + " …", True)


def pick_format(book: dict) -> str | None:
    have = {f.get("format") for f in book.get("formats") or []}
    return next((fmt for fmt in READABLE_FORMATS if fmt in have), None)


def _pick_section(sections: list[dict], section: str) -> dict | None:
    wanted = (section or "").strip()
    if wanted.isdigit():
        n = int(wanted)
        return next((s for s in sections if s["n"] == n), None)
    lowered = wanted.lower()
    return next((s for s in sections if lowered in s["title"].lower()), None)


async def search_books(
    conn: CalibreConnector, query: str = "", *, author: str = "", tag: str = "", limit: int = 10
) -> list[dict]:
    """calibre-web's search (the newest books when `query` is blank), then the
    author and tag filters."""
    books = await conn.search(query) if (query or "").strip() else await conn.catalog()
    if author:
        books = [b for b in books if any(author.lower() in a.lower() for a in b.get("authors") or [])]
    if tag:
        books = [b for b in books if any(tag.lower() in t.lower() for t in b.get("tags") or [])]
    return [book_brief(b) for b in books[: max(1, limit)]]


async def suggest_books(
    knowledge_connector: Any, conn: CalibreConnector | None, topic: str, *, limit: int = 5
) -> dict:
    """Books for a topic: the index first (ranked by meaning), calibre-web's
    own word search when the index has none yet."""
    books: list[dict] = []
    if knowledge_connector is not None:
        hits = await knowledge_connector.search(topic, limit=limit, source_type=BOOK_SOURCE_TYPE)
        books = [b for b in (book_hit(h) for h in hits or []) if b["id"] is not None]
    if books:
        return {"topic": topic, "books": books, "via": "index"}
    if conn is None:
        return {"topic": topic, "books": [], "via": "none"}
    found = await search_books(conn, topic, limit=limit)
    return {
        "topic": topic,
        "books": found,
        "via": "calibre_search",
        "note": "the library index has no books yet (CalibreSyncFlow has not run); "
        "these are calibre-web's word matches",
    }


async def read_book(
    conn: CalibreConnector,
    book_id: int,
    *,
    section: str = "",
    pages: str = "",
    query: str = "",
    max_chars: int = READ_CHARS,
    pdf_scan_pages: int = PDF_QUERY_SCAN_PAGES,
) -> dict:
    """Read from one book. `query` → the best-matching passages; `section`
    (EPUB) → one chapter; `pages` (PDF) → a page range; none → the opening.
    Every result carries a `cite`. Raises CalibreError when the library cannot
    be read; returns {"error": …} for a request that cannot be met."""
    max_chars = max(500, min(int(max_chars or READ_CHARS), MAX_READ_CHARS))
    book = await conn.get_book(book_id)
    if book is None:
        return {"error": f"there is no book {book_id} in the library"}
    brief = book_brief(book, description_chars=300)
    title = brief["title"]
    fmt = pick_format(book)
    if fmt is None:
        return {
            "error": f"{title!r} is only in {', '.join(brief['formats']) or 'no format'}; "
            "only EPUB and PDF can be read",
            "book": brief,
        }
    data = await conn.download(book, fmt)

    if fmt == "EPUB":
        sections = await asyncio.to_thread(epub_sections, data)
        if not sections:
            return {"error": f"{title!r} has no readable text", "book": brief}
        toc = [{"n": s["n"], "title": s["title"]} for s in sections][:80]
        if query:
            found = best_passages(
                [(f"chapter {s['n']} ({s['title']})", s["text"]) for s in sections], query
            )
            return {
                "book": brief,
                "format": fmt,
                "query": query,
                "passages": [{"cite": f"{title}, {p['where']}", "text": p["text"]} for p in found],
                "toc": toc,
            }
        if section:
            chosen = _pick_section(sections, section)
            if chosen is None:
                return {"error": f"{title!r} has no chapter matching {section!r}", "book": brief, "toc": toc}
        else:
            chosen = next((s for s in sections if len(s["text"]) >= 500), sections[0])
        text, truncated = _clip(chosen["text"], max_chars)
        return {
            "book": brief,
            "format": fmt,
            "section": {"n": chosen["n"], "title": chosen["title"]},
            "cite": f"{title}, chapter {chosen['n']} ({chosen['title']})",
            "text": text,
            "truncated": truncated,
            "toc": toc,
        }

    count = await asyncio.to_thread(pdf_page_count, data)
    if count <= 0:
        return {"error": f"{title!r} has no readable pages", "book": brief}
    if query:
        last = min(count, max(1, int(pdf_scan_pages)))
        texts = await asyncio.to_thread(pdf_pages, data, 1, last)
        found = best_passages([(f"p. {i + 1}", t) for i, t in enumerate(texts)], query)
        return {
            "book": brief,
            "format": fmt,
            "query": query,
            "passages": [{"cite": f"{title}, {p['where']}", "text": p["text"]} for p in found],
            "page_count": count,
            "pages_scanned": last,
        }
    try:
        first, last = parse_pages(pages, count) if pages else (1, min(count, PDF_DEFAULT_PAGES))
    except ValueError as exc:
        return {"error": str(exc), "book": brief, "page_count": count}
    texts = await asyncio.to_thread(pdf_pages, data, first, last)
    body = "\n\n".join(f"[p. {first + i}]\n{t}" for i, t in enumerate(texts) if t)
    text, truncated = _clip(body, max_chars)
    return {
        "book": brief,
        "format": fmt,
        "pages": {"first": first, "last": last},
        "page_count": count,
        "cite": f"{title}, pp. {first}-{last}" if last > first else f"{title}, p. {first}",
        "text": text,
        "truncated": truncated,
    }


async def book_details(conn: CalibreConnector, book_id: int) -> dict:
    """One book's metadata, full description, and an EPUB's table of contents."""
    book = await conn.get_book(book_id)
    if book is None:
        return {"error": f"there is no book {book_id} in the library"}
    out = {**book_brief(book, description_chars=4000), "publisher": book.get("publisher") or ""}
    out["languages"] = list(book.get("languages") or [])
    if pick_format(book) == "EPUB":
        try:
            sections = await asyncio.to_thread(epub_sections, await conn.download(book, "EPUB"))
            out["toc"] = [{"n": s["n"], "title": s["title"]} for s in sections][:80]
        except CalibreError as exc:
            out["toc_error"] = str(exc)
    out["readable"] = pick_format(book) is not None
    return out
